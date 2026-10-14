from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

import uvicorn
from temporalio.client import Client

from tin_lite.catalog import sync_builtin_workflows
from tin_lite.code_storage import CodeStorage
from tin_lite.community import PrivateCopyError, private_key, validate_all
from tin_lite.db import Database, apply_migrations
from tin_lite.rollouts import parse_rollout_filename, render_rollout_trace
from tin_lite.schedules import TemporalScheduleService
from tin_lite.settings import get_settings
from tin_lite.system_wiki import sync_system_wiki

ROOT = Path(__file__).parents[2]


async def _migrate() -> None:
    settings = get_settings()
    applied = await apply_migrations(settings.migration_dsn, ROOT / "migrations")
    print("applied migrations:", ", ".join(applied) if applied else "none")


async def _repair_schedule_timeouts(*, apply: bool) -> None:
    settings = get_settings()
    temporal = await Client.connect(
        settings.temporal_endpoint,
        namespace=settings.temporal_namespace,
        api_key=settings.temporal_api_key.get_secret_value(),
        tls=True,
    )
    service = TemporalScheduleService(client=temporal, settings=settings)
    # Read Temporal itself: stored schedules can outlive their product configuration.
    async for schedule in await temporal.list_schedules():
        prefix = "tin-lite-project-workflow:"
        if not schedule.id.startswith(prefix):
            continue
        configured_id = str(UUID(schedule.id.removeprefix(prefix)))
        changed = await service.remove_legacy_review_timeout(configured_id, apply=apply)
        action = ("updated" if apply else "would update") if changed else "unchanged"
        print(f"{action}: {schedule.id}")


async def _create_project(name: str, repo_id: str) -> None:
    settings = get_settings()
    storage = CodeStorage(
        organization=settings.code_storage_org,
        private_key=settings.code_storage_api_key.get_secret_value(),
        repo_prefix=settings.code_storage_repo_prefix,
    )
    await storage.ensure_repo(repo_id)
    database = Database(settings.runtime_dsn)
    await database.connect()
    try:
        project = await database.create_project(name=name, state_repo_id=repo_id)
    finally:
        await database.close()
    print(project.id)


async def _sync_builtins() -> None:
    settings = get_settings()
    storage = CodeStorage(
        organization=settings.code_storage_org,
        private_key=settings.code_storage_api_key.get_secret_value(),
        repo_prefix=settings.code_storage_repo_prefix,
    )
    database = Database(settings.runtime_dsn)
    await database.connect()
    try:
        system_wiki = await sync_system_wiki(
            storage=storage,
            source_root=ROOT / "system_wiki",
        )
        await sync_builtin_workflows(
            database=database,
            storage=storage,
            system_wiki=system_wiki,
        )
    finally:
        await database.close()
    print("built-in workflow catalog synchronized")


async def _grant_project_access(project_id: UUID, clerk_user_id: str) -> None:
    settings = get_settings()
    database = Database(settings.runtime_dsn)
    await database.connect()
    try:
        project = await database.get_project(project_id)
        if project is None:
            raise SystemExit(f"project {project_id} does not exist")
        await database.grant_project_membership(
            project_id=project_id,
            clerk_user_id=clerk_user_id,
        )
    finally:
        await database.close()
    print(f"granted access to project {project_id}")


async def _grant_workspace_access(workspace_id: UUID, clerk_user_id: str) -> None:
    settings = get_settings()
    database = Database(settings.runtime_dsn)
    await database.connect()
    try:
        workspace = await database.get_workspace(workspace_id)
        if workspace is None:
            raise SystemExit(f"workspace {workspace_id} does not exist")
        await database.grant_workspace_membership(
            workspace_id=workspace_id,
            clerk_user_id=clerk_user_id,
        )
    finally:
        await database.close()
    print(f"granted administration of workspace {workspace_id}")


async def _rollouts(run_id: UUID, *, out: Path | None, trace: bool, max_output_chars: int) -> None:
    """Operator-only read path for captured Codex rollouts; never a product surface."""
    settings = get_settings()
    database = Database(settings.runtime_dsn)
    await database.connect()
    try:
        rollouts = await database.list_run_rollouts(run_id)
        if not rollouts:
            raise SystemExit(f"no rollouts captured for run {run_id}")
        print(
            "id generation stage attempt sandbox_id filename size_bytes stored_bytes "
            "truncated redactions created_at"
        )
        for rollout in rollouts:
            print(
                f"{rollout.id} {rollout.generation} {rollout.stage} {rollout.activity_attempt} "
                f"{rollout.sandbox_id} {rollout.filename} {rollout.size_bytes} "
                f"{rollout.stored_bytes} {rollout.truncated} {rollout.redactions} "
                f"{rollout.created_at.isoformat()}"
            )
        if out is None and not trace:
            return
        for rollout in rollouts:
            if parse_rollout_filename(rollout.filename) is None:
                raise SystemExit(f"refusing to handle rollout {rollout.id}: unexpected filename")
            loaded = await database.read_run_rollout(rollout.id)
            if loaded is None:
                continue
            _, content = loaded
            if out is not None:
                directory = out / (
                    f"{rollout.generation}-{rollout.stage}-attempt{rollout.activity_attempt}-"
                    f"{rollout.sandbox_id}"
                )
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / rollout.filename
                target.write_bytes(content)
                print(f"wrote {target}")
            if trace:
                print(f"=== {rollout.filename} (thread {rollout.thread_id}) ===")
                for line in render_rollout_trace(content, max_output_chars=max_output_chars):
                    print(line)
    finally:
        await database.close()


async def _validate_community(
    root: Path | None, private: bool = False, package: str | None = None
) -> None:
    try:
        results = await validate_all(root, private=private, only=package)
    except (ValueError, OSError) as error:
        print(f"FAIL  {error}")
        raise SystemExit(1) from None
    if not results:
        print("No contributed workflow packages found.")
        return
    failed = 0
    for package, error in results:
        if error is None:
            print(f"ok    {package.key}")
            continue
        failed += 1
        print(f"FAIL  {package.key}")
        print(f"      {error}")
        if isinstance(error, PrivateCopyError):
            print(
                "      The public package is valid. For a private test run, change this in"
                f" your {private_key(package.key)} copy only."
            )
        else:
            print(f"      Fix it in {package.path}, then run this command again.")
    print(f"\n{len(results) - failed} of {len(results)} packages are valid.")
    if failed:
        raise SystemExit(1)


def _serve() -> None:
    from tin_lite.main import app  # imported here: building the app reads settings
    from tin_lite.serving import DrainingServer

    settings = get_settings()

    async def drain_worker() -> None:
        runtime = getattr(app.state, "runtime", None)
        if runtime is not None:
            await runtime.worker.shutdown()

    # OAuth providers return short-lived authorization codes in callback query
    # strings. Uvicorn's generic access logger includes the full query string;
    # disable it and retain only deliberate application-level event logging.
    config = uvicorn.Config(
        app,
        host="0.0.0.0",  # noqa: S104
        port=8000,
        access_log=False,
        # Persistent MCP/SSE connections must not hold shutdown until systemd
        # kills the process. This applies after the worker drain below.
        timeout_graceful_shutdown=20,
    )
    DrainingServer(
        config,
        drain=drain_worker,
        # The worker cancels activities at its graceful timeout; the margin covers
        # reporting their results and stopping the pollers.
        drain_timeout=settings.worker_graceful_shutdown_seconds + 60,
    ).run()


def main() -> None:
    parser = argparse.ArgumentParser(prog="tin-lite")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("sync-builtins")
    commands.add_parser("serve")
    schedule_timeouts = commands.add_parser(
        "repair-schedule-timeouts", help="remove legacy 24-hour dispatcher deadlines"
    )
    schedule_timeouts.add_argument(
        "--apply", action="store_true", help="update stored schedules (default: dry run)"
    )
    community = commands.add_parser(
        "validate-community", help="check the contributed workflow packages in this checkout"
    )
    community.add_argument(
        "--root", type=Path, help="checkout to read instead of the one this package lives in"
    )
    community.add_argument(
        "--private",
        action="store_true",
        help="also check that each package activates as a custom.* private workflow",
    )
    community.add_argument("--package", help="check only the package with this key")
    project = commands.add_parser("create-project")
    project.add_argument("--name", required=True)
    project.add_argument("--repo-id", required=True)
    access = commands.add_parser("grant-project-access")
    access.add_argument("--project-id", required=True, type=UUID)
    access.add_argument("--clerk-user-id", required=True)
    workspace_access = commands.add_parser("grant-workspace-access")
    workspace_access.add_argument("--workspace-id", required=True, type=UUID)
    workspace_access.add_argument("--clerk-user-id", required=True)
    rollouts = commands.add_parser(
        "rollouts", help="list, export, or print the Codex rollouts captured for a run"
    )
    rollouts.add_argument("run_id", type=UUID)
    rollouts.add_argument("--out", type=Path, help="directory to write the .jsonl files into")
    rollouts.add_argument("--trace", action="store_true", help="print a readable transcript")
    rollouts.add_argument("--max-output-chars", type=int, default=2000)
    spike = commands.add_parser(
        "linkedin-spike",
        help="probe what a project's saved LinkedIn session can read, or send one request",
    )
    spike.add_argument("project_id", type=UUID)
    spike.add_argument("--where", choices=("host", "e2b"), default="host")
    spike.add_argument("--post", help="activity ID to read reactions and comments from")
    spike.add_argument("--profile", action="append", help="profile URL to read (repeatable)")
    spike.add_argument("--gap", type=float, default=4.0, help="seconds between reads")
    spike.add_argument("--invite", help="profile URL to send one connection request to")
    spike.add_argument("--confirm-invite", help="the same URL again, to confirm the send")
    args = parser.parse_args()
    if args.command == "migrate":
        asyncio.run(_migrate())
    elif args.command == "sync-builtins":
        asyncio.run(_sync_builtins())
    elif args.command == "repair-schedule-timeouts":
        asyncio.run(_repair_schedule_timeouts(apply=args.apply))
    elif args.command == "validate-community":
        asyncio.run(_validate_community(args.root, args.private, args.package))
    elif args.command == "serve":
        _serve()
    elif args.command == "create-project":
        asyncio.run(_create_project(args.name, args.repo_id))
    elif args.command == "grant-project-access":
        asyncio.run(_grant_project_access(args.project_id, args.clerk_user_id))
    elif args.command == "grant-workspace-access":
        asyncio.run(_grant_workspace_access(args.workspace_id, args.clerk_user_id))
    elif args.command == "linkedin-spike":
        from tin_lite.linkedin_spike import command

        command(args)
    elif args.command == "rollouts":
        asyncio.run(
            _rollouts(
                args.run_id,
                out=args.out,
                trace=args.trace,
                max_output_chars=args.max_output_chars,
            )
        )


if __name__ == "__main__":
    main()
