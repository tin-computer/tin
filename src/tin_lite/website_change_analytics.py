"""website.change, source analytics: Tin Analytics on the founder's site.

Agent Traffic counts a site's visits through a script line in the head of every page and, on
hosts that can run it, a request reporter that also counts crawlers and assistants that never
run scripts (agent_traffic_connection; contract: https://with.md/s/qkptksy_). With GitHub
connected, Tin puts them on the site as one change row the founder approves once:

- `install`: the script line, the CSP entries (only where the site already sets a policy) and
  the reporter file for the site's host, copied unchanged and wired in;
- `replace`: the line for a new site, after the founder stopped measuring and set up again
  while the old line was still on the site;
- `remove`: after Stop measuring, the line, the CSP entries and the reporter's wiring taken
  out again. Procedures can't delete files, so the copied reporter file stays and the pull
  request says so.

An approval covers those pieces: the line, the entries and the reporter by its SHA-256
(`content_sha256`), never the files the run later writes. Approving starts website.change: a
Codex session (the site-analytics skill) finds the layout file and the wiring and writes the
patch, and `check_patch` holds it to exactly the pieces. Tin writes the reporter's bytes itself,
so the copy on the site always matches the published SHA-256. A patch with any other edit
opens a pull request for the founder to review; otherwise Tin merges it once the site's build
and the repository's required checks pass, under website.change's rules.

A linked site (one Agent Traffic measured before Tin, such as tin.computer's) already has its
line, so it gets no row. Rows are proposed by Set up and Stop measuring, never by a run.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import posixpath
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tin_lite import content_repository_delivery as delivery
from tin_lite import website_change
from tin_lite.integrations import PULL_REQUEST_MAX_FILES
from tin_lite.organic_audit import canonical_json
from tin_lite.website_change import (
    ChangeRow,
    WebsiteChangeConflict,
    approval_for,
    file_protected,
    project_protected_paths,
    protected_paths,
)

logger = logging.getLogger(__name__)

SOURCE = delivery.ANALYTICS_SOURCE
KINDS = ("install", "replace", "remove")
PREFIX = "an"
ACTIVE = ("pending", "running", "needs_input")
# A runaway guard: the line goes in a shared layout, or in each HTML page of a static site, and
# the reporter adds one file and its wiring. A pull request can't hold more than this anyway.
MAX_FILES = PULL_REQUEST_MAX_FILES
# A file Tin can't read whole can't be checked line by line.
MAX_FILE_BYTES = 400_000
REPORTER_MAX_BYTES = 200_000
FETCH_TIMEOUT_SECONDS = 10.0
UNAPPROVED = "No one approved this change in Tin yet, so the pull request waits for your review."

# The reporter files Agent Traffic publishes, by variant: who they are for, the name the copy
# has in the repository, and where the founder pastes TIN_REPORTER_TOKEN.
VARIANTS: dict[str, dict[str, str]] = {
    "nextjs": {
        "label": "Next.js",
        "where": "copied unchanged and added to the site's middleware",
    },
    "vercel": {
        "label": "Vercel",
        "where": "copied unchanged as middleware.js at the project's root",
    },
    "netlify": {
        "label": "Netlify",
        "where": "copied unchanged as netlify/edge-functions/tin-reporter.js",
    },
    "node": {
        "label": "Node",
        "where": "copied unchanged as tin-reporter.cjs and added before the routes",
    },
    "cloudflare-worker": {
        "label": "Cloudflare Worker",
        "where": "copied unchanged and wrapped around the Worker's handler",
    },
}
# Who each variant is for, as the contract's table says, for a coding agent choosing one.
VARIANT_FOR = {
    "nextjs": "Next.js sites, through middleware.ts, or proxy.ts from Next.js 16",
    "vercel": "other frameworks on Vercel, through Vercel's Routing Middleware",
    "netlify": "sites on Netlify, as an edge function",
    "node": "Node servers with Express-style middleware, such as Express and Connect",
    "cloudflare-worker": "sites served by a Cloudflare Worker",
}
# Where the founder pastes the reporter's value, by the host the repository deploys to.
HOSTS: dict[str, dict[str, str]] = {
    "vercel": {
        "label": "Vercel",
        "where": "Vercel: your project → Settings → Environment Variables. Add it for "
        "Production, then redeploy so the reporter reads it.",
    },
    "netlify": {
        "label": "Netlify",
        "where": "Netlify: Site configuration → Environment variables. Add it with the "
        "Functions scope, because variables in netlify.toml don't reach edge functions, then "
        "deploy again.",
    },
    "cloudflare": {
        "label": "Cloudflare",
        "where": "Cloudflare: add it as a secret of the Worker (Settings → Variables and "
        "Secrets, or wrangler secret put TIN_REPORTER_TOKEN), then deploy again.",
    },
}
OTHER_HOST_WHERE = (
    "Your host's environment variables for the site's server. Add it, then deploy or restart "
    "so the reporter reads it."
)


# --- What the founder's repository runs on --------------------------------------------------


def detect_host(names: set[str], hosting: str | None) -> str | None:
    """The host the site deploys to: from the site folder's files, then the setup answer. When
    the files name more than one host (a vercel.json kept after a move to Cloudflare), the setup
    answer picks between them, and without one Tin doesn't guess."""
    named = [
        host
        for host, files in (
            ("vercel", {"vercel.json", ".vercel"}),
            ("netlify", {"netlify.toml"}),
            ("cloudflare", {"wrangler.toml", "wrangler.json", "wrangler.jsonc"}),
        )
        if names & files
    ]
    words = (hosting or "").lower()
    answered = [host for host in ("vercel", "netlify", "cloudflare") if host in words]
    if len(named) == 1:
        return named[0]
    if named:
        return next((host for host in answered if host in named), None)
    return answered[0] if answered else None


def _dependencies(package: str | None) -> set[str]:
    try:
        value = json.loads(package or "{}")
    except ValueError:
        return set()
    found: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        if isinstance(value, dict) and isinstance(value.get(key), dict):
            found |= {name for name in value[key] if isinstance(name, str)}
    return found


def detect_variant(facts: dict[str, Any], host: str | None) -> str | None:
    """The reporter variant for this site, from its folder's files: the host's own reporter when
    it sees each response's status (a Cloudflare Worker, Netlify), then Next.js's on any other
    host, then Vercel's, then an Express-style Node server's. None when nothing says which one
    runs, so the change carries the line alone."""
    names = set(facts.get("names") or ())
    dependencies = _dependencies(facts.get("package.json"))
    if host == "cloudflare":
        # Workers name their entry point; a Pages site without one has no Worker to wrap, and a
        # generated entry (OpenNext's .open-next/worker.js) is rebuilt on every deploy.
        main = re.search(
            r'(?:^|\n)\s*"?main"?\s*[=:]\s*["\']([^"\']+)', facts.get("wrangler") or ""
        )
        if main and not any(part.startswith(".") for part in main[1].split("/")[:-1]):
            return "cloudflare-worker"
    if host == "netlify":
        return "netlify"
    if "next" in dependencies or any(name.startswith("next.config.") for name in names):
        return "nextjs"
    if host == "vercel":
        return "vercel"
    if dependencies & {"express", "connect"}:
        return "node"
    return None


def reporter_piece(configuration: dict[str, Any], variant: str | None) -> dict[str, str] | None:
    """The reporter variant the service published, verified by its SHA-256, or None."""
    if variant is None:
        return None
    for item in (configuration.get("reporter") or {}).get("variants") or []:
        if item.get("variant") == variant:
            return {key: item[key] for key in ("variant", "version", "url", "sha256")}
    return None


# --- The change row ------------------------------------------------------------------------


def change_id(kind: str, site_id: str) -> str:
    digest = hashlib.sha256(f"analytics|{kind}|{site_id}".encode()).hexdigest()
    return f"{PREFIX}_{digest[:20]}"


def pieces_sha256(pieces: dict[str, Any]) -> str:
    """What an approval covers: the kind, the site, the line, the CSP entries, the reporter by
    its SHA-256, and for a replace or remove the line already on the site."""
    return hashlib.sha256(canonical_json(pieces)).hexdigest()


def change_row(
    *,
    kind: str,
    host: str,
    repository: str,
    site_id: str,
    script: str,
    csp: dict[str, list[str]],
    reporter: dict[str, str] | None,
    reporter_host: str | None,
    folder: str = "",
    previous: dict[str, Any] | None = None,
) -> ChangeRow:
    pieces = {
        "kind": kind,
        "site_id": site_id,
        "script": script,
        "csp": csp,
        "reporter": reporter,
        "folder": folder,
        "previous_script": (previous or {}).get("script"),
    }
    title = {
        "install": f"Add Tin Analytics to {host}",
        "replace": f"Update Tin Analytics on {host}",
        "remove": f"Remove Tin Analytics from {host}",
    }[kind]
    detail: dict[str, Any] = {
        "repository": repository,
        "host": host,
        "site_id": site_id,
        "script": script,
        "csp": csp,
        "reporter": (
            {
                **reporter,
                "label": VARIANTS.get(reporter["variant"], {}).get("label", reporter["variant"]),
                "where": VARIANTS.get(reporter["variant"], {}).get("where", "copied unchanged"),
            }
            if reporter
            else None
        ),
        "reporter_host": reporter_host,
        # The folder the site is built from, "" for the repository's root.
        "folder": folder,
    }
    if previous:
        detail["previous"] = {
            key: previous[key]
            for key in ("script", "site_id", "reporter", "files", "new_files", "run_id")
            if previous.get(key) is not None
        }
    return ChangeRow(
        change_id=change_id(kind, site_id),
        source=SOURCE,
        kind=kind,
        title=title,
        paths=("/",),
        content_sha256=pieces_sha256(pieces),
        detail=detail,
    )


# --- What Tin's earlier changes left on the site -------------------------------------------

RUNS_SQL = """
    SELECT r.id, r.status, r.created_at, r.error_message, s.result AS source,
           p.response_summary AS publication, m.result AS merge, m.updated_at AS merged_at
    FROM workflow_runs r
    LEFT JOIN effect_receipts s ON s.execution_key = 'content-delivery:' || r.id::text
        || ':source' AND s.status = 'completed'
    LEFT JOIN integration_call_receipts p
        ON p.execution_key = r.id::text || ':procedure_pull_request' AND p.status = 'completed'
    LEFT JOIN effect_receipts m ON m.execution_key = 'content-delivery:' || r.id::text
        || ':merge' AND m.status = 'completed'
    WHERE r.project_id = $1 AND r.workflow_id = $2 AND r.input->>'source' = $3
    ORDER BY r.created_at DESC, r.id DESC LIMIT 20
"""


def _decoded(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


async def earlier_runs(database, project_id: UUID) -> list[dict[str, Any]]:
    """The project's analytics runs, newest first, with their change, PR and merge."""
    rows = await database.pool.fetch(RUNS_SQL, project_id, website_change.WORKFLOW_ID, SOURCE)
    found = []
    for row in rows:
        source = _decoded(row["source"])
        found.append(
            {
                "run_id": str(row["id"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "error": row["error_message"],
                "change": source.get("change") or {},
                "pull_request": _decoded(row["publication"]) or None,
                "merge": _decoded(row["merge"]) or None,
                "merged_at": row["merged_at"],
            }
        )
    return found


def merged(run: dict[str, Any]) -> bool:
    return (run.get("merge") or {}).get("status") == "merged"


async def line_on_site(database, project_id: UUID) -> dict[str, Any] | None:
    """The line Tin's newest merged change left on the site, with its reporter and files, or
    None when Tin never added one or took it out again."""
    for run in await earlier_runs(database, project_id):
        if not merged(run):
            continue
        change = run["change"]
        if change.get("kind") == "remove":
            return None
        detail = change.get("detail") or {}
        result = run["merge"]
        return {
            "script": detail.get("script"),
            "site_id": detail.get("site_id"),
            "folder": detail.get("folder") or "",
            "reporter": result.get("reporter"),
            "files": result.get("files") or [],
            "new_files": result.get("new_files") or [],
            "run_id": run["run_id"],
        }
    return None


# --- Proposing at Set up and Stop measuring -------------------------------------------------


async def website_repository(integrations, project_id: UUID) -> str | None:
    """The website repository chosen on Integrations, when Tin may open pull requests in it."""
    from tin_lite.integrations import GITHUB_PROVIDER

    connection = await integrations._database.get_integration_connection(
        project_id=project_id, provider_key=GITHUB_PROVIDER
    )
    if connection is None or connection.status != "connected":
        return None
    configuration = connection.configuration or {}
    permissions = configuration.get("permissions") or {}
    repository = configuration.get("selected_repository")
    if (
        not isinstance(repository, str)
        or repository.count("/") != 1
        or configuration.get("write_opted_in") is not True
        or permissions.get("contents") != "write"
        or permissions.get("pull_requests") != "write"
    ):
        return None
    return repository


async def setup_hosting(database, project_id: UUID) -> str | None:
    """The setup answer for the site's framework and hosting, as the founder wrote it."""
    value = await database.pool.fetchval(
        "SELECT input->>'system_hosting' FROM workflow_runs WHERE project_id = $1 "
        "AND executor = 'growth.onboarding' AND status <> 'superseded' "
        "AND coalesce(input->>'system_hosting', '') <> '' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        project_id,
    )
    return value if isinstance(value, str) else None


async def propose_install(integrations, *, project_id: UUID, configuration: dict, host: str):
    """After Set up: the change that puts this site's line on the site, in Decisions.

    Nothing for a linked site, without a website repository Tin may write, or when Tin's line
    for this site is already on the site. When an older site's line is still there, the
    change replaces it. Any other pending analytics row (a remove after an earlier stop) gives
    way to this one. Returns the stored row, or None.
    """
    if configuration.get("linked") is True:
        return None
    database = integrations._database
    repository = await website_repository(integrations, project_id)
    if repository is None:
        return None
    on_site = await line_on_site(database, project_id)
    site_id = configuration["site_id"]
    if on_site and on_site.get("script") == configuration["script"]:
        await website_change.retire(database, project_id=project_id, source=SOURCE, keep=[])
        return None
    facts = await integrations.github_site_files(project_id=project_id, repository=repository)
    reporter_host = detect_host(
        set(facts.get("names") or ()), await setup_hosting(database, project_id)
    )
    kind = "replace" if on_site else "install"
    reporter = (
        None
        if kind == "replace"
        else reporter_piece(configuration, detect_variant(facts, reporter_host))
    )
    row = change_row(
        kind=kind,
        host=host,
        repository=repository,
        site_id=site_id,
        script=configuration["script"],
        csp=configuration.get("csp") or {},
        reporter=reporter,
        reporter_host=reporter_host,
        folder=facts.get("folder") or "",
        previous=on_site,
    )
    await website_change.retire(
        database, project_id=project_id, source=SOURCE, keep=[row.change_id]
    )
    [stored] = await website_change.propose(database, project_id=project_id, rows=[row])
    return stored


async def propose_remove(integrations, *, project_id: UUID, configuration: dict, host: str):
    """After Stop measuring: the change that takes Tin's line back off the site, when Tin put
    it there. A pending install for the stopped site is withdrawn either way."""
    database = integrations._database
    await website_change.retire(database, project_id=project_id, source=SOURCE, keep=[])
    if configuration.get("linked") is True:
        return None
    on_site = await line_on_site(database, project_id)
    repository = await website_repository(integrations, project_id)
    if on_site is None or repository is None:
        return None
    row = change_row(
        kind="remove",
        host=host,
        repository=repository,
        site_id=on_site.get("site_id") or configuration["site_id"],
        script=on_site["script"],
        csp=configuration.get("csp") or {},
        reporter=None,
        reporter_host=None,
        folder=on_site.get("folder") or "",
        previous=on_site,
    )
    [stored] = await website_change.propose(database, project_id=project_id, rows=[row])
    return stored


# --- What the card shows ---------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


async def install_view(database, project_id: UUID, site_id: str | None) -> dict[str, Any]:
    """Where Tin's change for this site stands: waiting in Decisions, being added, added, left
    open, or none (the founder pastes the line). For the card and the MCP tool."""
    rows = await database.pool.fetch(
        "SELECT * FROM website_changes WHERE project_id = $1 AND source = $2 "
        "ORDER BY proposed_at DESC LIMIT 10",
        project_id,
        SOURCE,
    )
    changes = [website_change.view(row) for row in rows]
    runs = await earlier_runs(database, project_id)
    view: dict[str, Any] = {"state": None}
    on_site = None
    for run in runs:
        if merged(run):
            on_site = run
            break
    if on_site and on_site["change"].get("kind") != "remove":
        result = on_site["merge"]
        view["added"] = {
            "at": _iso(on_site["merged_at"]),
            "pull_request": result.get("pull_request"),
            "number": result.get("number"),
            "reporter": result.get("reporter"),
            "site_id": (on_site["change"].get("detail") or {}).get("site_id"),
        }
    # A measured site's own install or replace; after Stop measuring, taking the line out.
    current = next(
        (
            change
            for change in changes
            if (change["kind"] == "remove") == (site_id is None)
            and (site_id is None or change["detail"].get("site_id") == site_id)
        ),
        None,
    )
    if current is None:
        if view.get("added") and view["added"]["site_id"] == site_id:
            view["state"] = "added"
        return view
    view["change"] = {
        key: current[key] for key in ("change_id", "kind", "title", "status", "content_sha256")
    }
    view["reporter_host"] = current["detail"].get("reporter_host")
    view["reporter"] = current["detail"].get("reporter")
    view["reporter_added"] = bool((current["detail"].get("previous") or {}).get("reporter"))
    if current["status"] == "pending":
        view["state"] = "decide"
        return view
    if current["status"] == "declined":
        view["state"] = "declined"
        return view
    run = next(
        (run for run in runs if run["change"].get("change_id") == current["change_id"]), None
    )
    pull_request = (run or {}).get("pull_request") or {}
    if pull_request:
        view["pull_request"] = {
            "number": pull_request.get("number"),
            "url": pull_request.get("url"),
        }
    if run is None:
        view["state"] = "approved"
    elif run["status"] in ACTIVE or (
        run["status"] == "succeeded" and pull_request and not run["merge"]
    ):
        view["state"] = "adding"
    elif merged(run):
        view["state"] = "removed" if current["kind"] == "remove" else "added"
    elif run["merge"]:
        view["state"] = "left_open"
        view["reason"] = run["merge"].get("reason")
    elif run["status"] == "succeeded":
        view["state"] = "not_added"
    else:
        view["state"] = "failed"
        view["reason"] = run["error"]
    return view


# --- Starting the change after approval -----------------------------------------------------


async def start_after_approval(*, runtime, settings, project_id: UUID, change: dict, actor: str):
    """Start website.change for an approved analytics change. Returns (run, None), or (None,
    why it did not start); the approval stands either way, and the card offers to start it
    again."""
    from tin_lite.integrations import IntegrationError
    from tin_lite.run_service import start_workflow_run

    workflow = await runtime.database.get_workflow(website_change.WORKFLOW_ID)
    if workflow is None:
        return None, "website.change is not installed on this Tin."
    attempts = sum(
        1
        for run in await earlier_runs(runtime.database, project_id)
        if run["change"].get("change_id") == change["change_id"]
    )
    try:
        run = await start_workflow_run(
            runtime=runtime,
            settings=settings,
            workflow=workflow,
            project_id=project_id,
            started_by_clerk_user_id=actor,
            start_idempotency_key=(
                f"website-change:{change['change_id']}:{change['content_sha256'][:16]}:{attempts}"
            ),
            input_payload={
                "source": SOURCE,
                "change_id": change["change_id"],
                "expected_repository": change["detail"]["repository"],
            },
        )
    except (IntegrationError, LookupError, RuntimeError, ValueError) as exc:
        # BillingError and an uncertain Temporal start are RuntimeErrors.
        return None, str(exc)
    return run, None


async def start_again(*, runtime, settings, project_id: UUID, actor: str) -> str | None:
    """Start the approved change again from the card, when its start was refused or its run
    ended without a pull request. Returns why it can't start, or None."""
    from tin_lite.agent_traffic_connection import PROVIDER

    connection = await runtime.database.get_integration_connection(
        project_id=project_id, provider_key=PROVIDER
    )
    view = await install_view(
        runtime.database, project_id, connection.external_account_id if connection else None
    )
    if view["state"] not in {"approved", "failed", "not_added"}:
        return "There is no approved analytics change waiting to start."
    change = await website_change.get_change(
        runtime.database, project_id=project_id, change_id=view["change"]["change_id"]
    )
    _run, reason = await start_after_approval(
        runtime=runtime, settings=settings, project_id=project_id, change=change, actor=actor
    )
    return reason


# --- Admission ----------------------------------------------------------------------------


async def select_source(*, database, storage, integrations, project_id, inputs) -> dict:
    """Pin the approved change, the repository and the publish mode for one run."""
    from tin_lite.agent_traffic_connection import PROVIDER

    change = await website_change.get_change(
        database, project_id=project_id, change_id=inputs.get("change_id") or ""
    )
    if change is None or change["source"] != SOURCE:
        raise ValueError("Choose the analytics change to make (change_id).")
    if change["status"] == "declined":
        raise ValueError("You declined this change, so Tin won't make it.")
    if change["status"] != "approved":
        raise ValueError("Approve the change in Decisions first.")
    detail = change["detail"]
    if inputs.get("expected_repository") != detail["repository"]:
        raise ValueError(f"This change is for {detail['repository']}.")
    connection = await database.get_integration_connection(
        project_id=project_id, provider_key=PROVIDER
    )
    measuring = connection is not None and connection.external_account_id == detail["site_id"]
    if change["kind"] != "remove" and not measuring:
        raise ValueError(
            "You stopped measuring this site, so its line no longer belongs on the site."
        )
    binding = await integrations.github_repository_binding(
        project_id=project_id, expected_repository=detail["repository"]
    )
    approval = await approval_for(database.pool, project_id=project_id, change=change)
    setting = await project_protected_paths(database.pool, project_id=project_id)
    roots = protected_paths(setting["paths"], inputs.get("protected_paths"))
    return {
        "source": SOURCE,
        "binding": {**asdict(binding), "connection_id": str(binding.connection_id)},
        "change": {
            **{
                key: change[key]
                for key in (
                    "change_id",
                    "source",
                    "kind",
                    "title",
                    "paths",
                    "content_revision",
                    "content_sha256",
                    "detail",
                )
            },
            "approval": approval,
        },
        "protected_paths": roots,
        "protected_paths_revision": setting["revision"],
        "publish": (
            {
                "mode": "direct",
                "reason": "You approved it in Tin, so Tin merges the pull request once your "
                "site's build and your repository's required checks pass.",
            }
            if approval
            else {"mode": "pull_request", "reason": UNAPPROVED}
        ),
    }


async def guard_source(conn, *, project_id, inputs, source) -> None:
    """Under create_run's project lock: the approval still stands, and no other analytics run
    of this project is still working or already made this change."""
    change = source.get("change") or {}
    if (
        inputs.get("source") != SOURCE
        or source.get("source") != SOURCE
        or inputs.get("change_id") != change.get("change_id")
    ):
        raise ValueError("This change is not the one this run was admitted for.")
    try:
        current = await approval_for(conn, project_id=project_id, change=change)
    except WebsiteChangeConflict:
        current = None
    if current is None or current != change.get("approval"):
        raise ValueError("The change's approval changed while starting. Start again.")
    rows = await conn.fetch(RUNS_SQL, project_id, website_change.WORKFLOW_ID, SOURCE)
    for row in rows:
        made = _decoded(row["source"]).get("change") or {}
        if row["status"] in ACTIVE:
            raise ValueError(f"Website changes run {row['id']} is still adding Tin Analytics.")
        if made.get("change_id") == change["change_id"] and (
            _decoded(row["merge"]).get("status") == "merged"
            or (row["publication"] and not row["merge"])
        ):
            raise ValueError("Tin already made this change.")


# --- Preparation: the reporter's bytes ----------------------------------------------------


def reporter_key(run_id) -> str:
    return f"website-change:{UUID(str(run_id))}:analytics-reporter"


def reporter_header(text: str) -> str:
    """The reporter file's opening comment, which says how to wire it in."""
    lines = []
    for line in text.splitlines():
        if not line.startswith("//"):
            break
        lines.append(line)
    return "\n".join(lines)


async def fetch_reporter(client, piece: dict[str, str]) -> str:
    """The published reporter file, checked against the SHA-256 the founder approved."""
    response = await client.get(
        piece["url"],
        headers={"User-Agent": "Tin-Project-API/1", "Accept-Encoding": "identity"},
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=False,
    )
    raw = response.content
    if response.status_code != 200 or len(raw) > REPORTER_MAX_BYTES:
        raise ValueError(f"Tin couldn't download the {piece['variant']} reporter file.")
    if hashlib.sha256(raw).hexdigest() != piece["sha256"]:
        raise ValueError(
            f"The {piece['variant']} reporter file doesn't match the SHA-256 you approved."
        )
    return raw.decode("utf-8")


async def prepare(*, database, integrations, run) -> None:
    """Download the approved reporter file once, before the session, and keep its bytes."""
    source = await delivery.saved_source(database, run.id)
    piece = source["change"]["detail"].get("reporter")
    key = reporter_key(run.id)
    async with database.effect_lock(key, "website_change_analytics_reporter") as (conn, receipt):
        if receipt and receipt.status == "completed":
            return
        await database.start_effect(
            conn, execution_key=key, operation="website_change_analytics_reporter"
        )
        result: dict[str, Any] = {"reporter": None}
        if source["change"]["kind"] == "remove":
            # The reporter Tin added, for its wiring notes: the remove takes the wiring out.
            piece = ((source["change"]["detail"].get("previous") or {}).get("reporter")) or None
        if piece and piece.get("url") and source["change"]["kind"] != "replace":
            content = await fetch_reporter(integrations._client, piece)
            result["reporter"] = {
                **{name: piece[name] for name in ("variant", "version", "url", "sha256")},
                "content": content,
            }
        await database.complete_effect(conn, execution_key=key, result=result)


async def prepared_reporter(database, run_id) -> dict[str, Any] | None:
    receipt = await database.get_effect(reporter_key(run_id))
    if not receipt or receipt.status != "completed":
        raise ValueError("The analytics change's reporter file was not prepared.")
    return (receipt.result or {}).get("reporter")


def workspace(source: dict, reporter: dict | None) -> dict:
    """What the site-analytics skill reads: the change, its pieces and the rules."""
    change = source["change"]
    detail = change["detail"]
    pieces: dict[str, Any] = {
        "kind": change["kind"],
        "script": detail["script"],
        "csp": detail.get("csp") or {},
        "folder": detail.get("folder") or "",
    }
    if change["kind"] in {"replace", "remove"}:
        pieces["previous_script"] = (detail.get("previous") or {}).get("script")
    if change["kind"] == "remove":
        previous = detail.get("previous") or {}
        pieces["changed_files"] = previous.get("files") or []
        # Files Tin's change added. Procedures can't delete files, so these stay; the pull
        # request lists them for the founder.
        pieces["added_files"] = previous.get("new_files") or []
        if reporter:
            pieces["reporter_added"] = {
                "variant": reporter["variant"],
                "path": ((detail.get("previous") or {}).get("reporter") or {}).get("path"),
                "wiring": reporter_header(reporter["content"]),
            }
    if reporter and change["kind"] == "install":
        pieces["reporter"] = {
            "variant": reporter["variant"],
            "version": reporter["version"],
            "sha256": reporter["sha256"],
            "file_names": list(REPORTER_NAMES[reporter["variant"]]),
            "wiring": reporter_header(reporter["content"]),
            "content": reporter["content"],
        }
    return {
        "source": SOURCE,
        "change_ids": [change["change_id"]],
        "analytics": pieces,
        "publish": source["publish"],
        "protected_paths": source["protected_paths"],
    }


# --- The patch check ------------------------------------------------------------------------

# Where each variant's copy lives: a basename anywhere, or an exact path.
REPORTER_NAMES = {
    "nextjs": ("tin-reporter.js",),
    "vercel": ("middleware.js", "tin-reporter.js"),
    "netlify": ("netlify/edge-functions/tin-reporter.js",),
    "node": ("tin-reporter.cjs",),
    "cloudflare-worker": ("tin-reporter.js",),
}
# New files a variant's wiring may add besides the copy: Next.js's middleware or proxy.
WIRING_FILES = {
    "nextjs": re.compile(r"(?:src/)?(?:middleware|proxy)\.(?:ts|js|mjs)"),
}
# Words the reporter's wiring is written in, beside the import and the matcher.
WIRING_WORDS = frozenset(
    {
        "export",
        "default",
        "const",
        "let",
        "async",
        "function",
        "await",
        "return",
        "withTinReporter",
        "reportToTin",
        "tinReporter",
        "middleware",
        "proxy",
        "config",
        "matcher",
        "request",
        "req",
        "event",
        "context",
        "ctx",
        "env",
        "app",
        "server",
        "router",
        "use",
    }
)
REPORTER_IMPORT = re.compile(
    r"import\s*\{\s*(?:withTinReporter|reportToTin|tinReporter)\s*\}\s*from\s*"
    r"([\"'])(?:\.{1,2}/)+(?:[\w.@-]+/)*tin-reporter(?:\.c?js)?\1\s*;?"
    r"|const\s*\{\s*tinReporter\s*\}\s*=\s*require\(\s*([\"'])(?:\.{1,2}/)+"
    r"(?:[\w.@-]+/)*tin-reporter\.cjs\2\s*\)\s*;?"
)
# Comments the session may leave; `//` only where it can't be part of an address.
COMMENTS = re.compile(r"/\*.*?\*/|<!--.*?-->|(?<![:\w\"'])//[^\n]*$", re.S | re.M)
PUNCTUATION = re.compile(r"[\s(){}\[\];,:=.'\"`<>/!?*&|+]*")
WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
# Files a change to the site's analytics never touches, whatever the session thought.
NEVER = (".github/", ".env", ".gitmodules")


class PatchRefused(ValueError):
    """The patch touches what no analytics change may: dependencies, CI or secrets."""


def is_reporter(path: str, variant: str | None, folder: str = "") -> bool:
    if variant is None:
        return False
    for name in REPORTER_NAMES[variant]:
        if "/" in name:
            if path == name or path.endswith("/" + name):
                return True
        elif posixpath.basename(path) == name and (
            # Vercel runs Routing Middleware only from the site folder's root.
            name != "middleware.js" or path == posixpath.join(folder, name)
        ):
            return True
    return False


def _matcher(header: str) -> list[str]:
    """The matcher the reporter's header comment shows, as source text."""
    text = "\n".join(line[2:] for line in header.splitlines())
    found = re.search(r"matcher:\s*\[(.*?)\]", text, re.S)
    return [found.group(1).strip()] if found else []


def allowed_text(pieces: dict[str, Any], header: str = "") -> list[str]:
    """The exact strings an analytics patch adds or takes away, longest first."""
    strings = [pieces["script"], "<head>", "</head>", *_matcher(header)]
    if pieces.get("previous_script"):
        strings.append(pieces["previous_script"])
    for sources in (pieces.get("csp") or {}).values():
        strings += [source for source in sources if isinstance(source, str) and source]
    return sorted({value for value in strings if value}, key=len, reverse=True)


def residue(text: str, strings: list[str]) -> str | None:
    """What is left of a chunk the patch adds or removes once the pieces, the reporter's import
    and wiring words are taken out; None when nothing else is in it."""
    for value in strings:
        text = text.replace(value, " ")
    text = REPORTER_IMPORT.sub(" ", text)
    text = COMMENTS.sub(" ", text)
    words = [word for word in WORD.findall(text) if word not in WIRING_WORDS]
    rest = WORD.sub("", text)
    if not words and PUNCTUATION.fullmatch(rest):
        return None
    return " ".join(words[:3]) or rest.strip()[:40]


def chunks(before: str, after: str) -> tuple[list[str], list[str]]:
    """The text the patch adds to a file and the text it takes out, line by line, then within
    each changed block."""
    added, removed = [], []
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        if tag == "insert":
            added.append("".join(new[j1:j2]))
        elif tag == "delete":
            removed.append("".join(old[i1:i2]))
        elif tag == "replace":
            a, b = "".join(old[i1:i2]), "".join(new[j1:j2])
            for op, k1, k2, l1, l2 in difflib.SequenceMatcher(
                None, a, b, autojunk=False
            ).get_opcodes():
                if op in {"insert", "replace"}:
                    added.append(b[l1:l2])
                if op in {"delete", "replace"}:
                    removed.append(a[k1:k2])
    return added, removed


def check_patch(
    manifest: dict[str, Any],
    originals: dict[str, str | None],
    source: dict[str, Any],
    reporter: dict[str, Any] | None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Hold an analytics patch to the approved pieces.

    Returns the files to open the pull request with, the reporter's copy holding exactly the
    published bytes, and the reasons the pull request waits for the founder (none when only
    the pieces changed). Raises PatchRefused for what no analytics change may touch.
    """
    change = source["change"]
    kind = change["kind"]
    detail = change["detail"]
    pieces = {
        "script": detail["script"],
        "csp": detail.get("csp") or {},
        "previous_script": (detail.get("previous") or {}).get("script")
        if kind in {"replace", "remove"}
        else None,
    }
    variant = reporter["variant"] if reporter else None
    folder = detail.get("folder") or ""
    strings = allowed_text(pieces, reporter_header(reporter["content"]) if reporter else "")
    files: list[dict[str, str]] = []
    problems: list[str] = []
    if len(manifest["files"]) > MAX_FILES:
        raise PatchRefused(
            f"The patch changes {len(manifest['files'])} files; at most {MAX_FILES}."
        )
    copies = 0
    for item in manifest["files"]:
        path, after = item["path"], item["content"]
        name = posixpath.basename(path)
        if path.startswith(NEVER) or name.startswith(".env") or name in delivery.DEPENDENCY_FILES:
            raise PatchRefused(f"The patch changes {path}, which an analytics change never does.")
        before = originals.get(path)
        if reporter and kind == "install" and is_reporter(path, variant, folder):
            copies += 1
            if before is not None:
                problems.append(f"It writes the reporter over the existing {path}.")
            # Tin writes the published bytes itself: the copy is exact whatever the session wrote.
            files.append({"path": path, "content": reporter["content"]})
            continue
        files.append({"path": path, "content": after})
        if before is None:
            wiring = WIRING_FILES.get(variant or "")
            inside = posixpath.relpath(path, folder or ".")
            if kind != "install" or not wiring or not wiring.fullmatch(inside):
                problems.append(f"It adds {path}.")
                continue
            before = ""
        if len(before) > MAX_FILE_BYTES or len(after) > MAX_FILE_BYTES:
            problems.append(f"{path} is too large for Tin to check.")
            continue
        if kind == "replace":
            if after.replace(pieces["script"], pieces["previous_script"] or "") != before:
                problems.append(f"It changes more than the line in {path}.")
            continue
        added, removed = chunks(before, after)
        taken = added if kind == "install" else removed
        other = removed if kind == "install" else added
        if any(text.strip() for text in other):
            problems.append(
                f"It {'removes' if kind == 'install' else 'adds'} other text in {path}."
            )
            continue
        for text in taken:
            left = residue(text, strings)
            if left:
                problems.append(
                    f"It {'adds' if kind == 'install' else 'removes'} {left!r} in {path}."
                )
                break
    total = {
        "install": lambda: sum(
            f["content"].count(pieces["script"])
            - (originals.get(f["path"]) or "").count(pieces["script"])
            for f in files
        ),
        "replace": lambda: sum(f["content"].count(pieces["script"]) for f in files),
        "remove": lambda: sum(
            (originals.get(f["path"]) or "").count(pieces["previous_script"] or "")
            - f["content"].count(pieces["previous_script"] or "")
            for f in files
        ),
    }[kind]()
    if total < 1:
        problems.append(
            "It doesn't take the line out." if kind == "remove" else "It doesn't add the line."
        )
    if copies > 1:
        problems.append("It copies the reporter more than once.")
    return files, problems


def reporter_path(
    files: list[dict[str, str]], reporter: dict[str, Any] | None, folder: str = ""
) -> str | None:
    if not reporter:
        return None
    return next(
        (f["path"] for f in files if is_reporter(f["path"], reporter["variant"], folder)), None
    )


async def snapshot_files(integrations, run, binding, paths) -> dict[str, str | None]:
    """The changed files as the run's snapshot had them; None for a file it adds."""
    from tin_lite.technical_build_profile import archive_files

    bundle = await integrations.github_repository_bundle(
        project_id=run.project_id,
        run_id=run.id,
        execution_key=f"{run.id}:procedure_repository_workspace",
        expected_binding=binding,
    )
    wanted = set(paths)
    files = archive_files(bundle.archive, select=lambda path, _size: path in wanted)
    found: dict[str, str | None] = {}
    for path in paths:
        raw = files.get(path)
        try:
            found[path] = raw.decode("utf-8") if raw is not None else None
        except UnicodeDecodeError as exc:
            raise PatchRefused(f"{path} is not a text file.") from exc
    return found


async def checked_files(integrations, run, source, manifest, reporter):
    """check_patch against the run's snapshot: the files, the reasons to wait, and the files
    the patch adds."""
    if manifest.get("outcome") == "no_change":
        return [], [], []
    binding = delivery.binding_from(source)
    before = await snapshot_files(
        integrations, run, binding, [f["path"] for f in manifest["files"]]
    )
    files, problems = check_patch(manifest, before, source, reporter)
    return files, problems, [path for path, text in before.items() if text is None]


# --- The run's report -----------------------------------------------------------------------


def report(
    source: dict, manifest: dict, *, problems=(), pull_request=None, reporter_at=None
) -> bytes:
    change = source["change"]
    detail = change["detail"]
    lines = [f"# {change['title']}", ""]
    if manifest.get("outcome") == "no_change":
        lines += [
            "Tin made no change: the session found no safe way to make it.",
            "",
            str(manifest.get("body") or "").strip()[:4000],
            "",
            "The line and where it goes are on the Analytics page; you can paste it yourself.",
        ]
        return ("\n".join(lines) + "\n").encode()
    if pull_request is not None:
        lines += [f"Pull request: {pull_request.url}", ""]
    lines += ["## What it changes", "", f"- The line: `{detail['script']}`"]
    if detail.get("csp"):
        lines.append(
            "- CSP: "
            + ", ".join(f"{name} {' '.join(values)}" for name, values in detail["csp"].items())
            + " (only where the site sets a policy)"
        )
    if reporter_at:
        reporter = detail.get("reporter") or {}
        lines.append(
            f"- Reporter: {reporter.get('label')} reporter, copied unchanged to `{reporter_at}` "
            f"(sha256 `{reporter.get('sha256')}`)"
        )
    lines += ["", "## Who merges it", ""]
    if problems:
        lines += ["It waits for your review:", "", *(f"- {reason}" for reason in problems)]
    else:
        lines.append(source["publish"]["reason"])
    approval = change.get("approval")
    if approval:
        lines += ["", f"Approved by {approval['by']} on {approval['at'][:10]}."]
    return ("\n".join(lines) + "\n").encode()


# --- After the pull request opens -----------------------------------------------------------


async def hold_reason(database, run, source, manifest, problems) -> str | None:
    """Why Tin leaves the pull request open, or None when it may merge: the pinned mode, the
    approval as it stands now, the site still measured, the protected pages, and the patch
    held to the pieces."""
    from tin_lite.agent_traffic_connection import PROVIDER

    publish = source.get("publish") or {}
    if publish.get("mode") != "direct":
        return publish.get("reason") or UNAPPROVED
    change = source["change"]
    try:
        current = await approval_for(database.pool, project_id=run.project_id, change=change)
    except WebsiteChangeConflict:
        current = None
    if current != change.get("approval"):
        return "Its approval changed after Tin made the change, so it waits for your review."
    if change["kind"] != "remove":
        connection = await database.get_integration_connection(
            project_id=run.project_id, provider_key=PROVIDER
        )
        if connection is None or connection.external_account_id != change["detail"]["site_id"]:
            return "You stopped measuring this site, so Tin leaves the pull request for you."
    setting = await project_protected_paths(database.pool, project_id=run.project_id)
    roots = protected_paths(source.get("protected_paths"), setting["paths"])
    hit = next(
        (root for item in manifest["files"] if (root := file_protected(item["path"], roots))),
        None,
    )
    if hit:
        return f"It touches {hit}, a protected page, so it waits for your review."
    if problems:
        return problems[0] + " So it waits for your review."
    return None


async def publish(*, database, storage, integrations, run, sleep=None, clock=None):
    """After the run's pull request opens: merge it once the required checks pass when the
    change was approved and holds only its pieces; else leave it open and say why. The
    outcome is recorded once, with the files and the reporter's path, which the card and a
    later remove read."""
    source = await delivery.saved_source(database, run.id)
    publication = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    published = (publication.result or {}) if publication else {}
    if (
        not publication
        or publication.status != "completed"
        or not published.get("external_url")
        or type(published.get("pull_request_number")) is not int
    ):
        return None
    key = delivery.merge_key(run.id)
    sleep = sleep or asyncio.sleep
    clock = clock or (lambda: datetime.now(UTC))
    async with database.effect_lock(key, delivery.MERGE_OPERATION) as (conn, receipt):
        if receipt and receipt.status == "completed":
            return receipt.result
        await database.start_effect(conn, execution_key=key, operation=delivery.MERGE_OPERATION)
        try:
            manifest = await delivery.saved_manifest(database, storage, run)
            reporter = await prepared_reporter(database, run.id)
            files, problems, added = await checked_files(
                integrations, run, source, manifest, reporter
            )
            number = published["pull_request_number"]
            kind = source["change"]["kind"]
            detail = source["change"]["detail"]
            at = (
                reporter_path(files, reporter, detail.get("folder") or "")
                if kind == "install"
                else None
            )
            # A replace keeps the earlier change's reporter and the files it added on the site.
            previous = (detail.get("previous") or {}) if kind == "replace" else {}
            base = {
                "pull_request": published["external_url"],
                "number": number,
                "kind": kind,
                "files": [item["path"] for item in files],
                "new_files": [*added, *(previous.get("new_files") or [])],
                # What a later remove reads: the variant, where the copy went and its file.
                "reporter": (
                    {
                        **{
                            name: reporter[name] for name in ("variant", "version", "url", "sha256")
                        },
                        "path": at,
                    }
                    if at
                    else previous.get("reporter")
                ),
            }
            hold = await hold_reason(database, run, source, manifest, problems)
            if hold:
                result = {**base, "status": "left_open", "reason": hold}
            else:
                result = {
                    **base,
                    "merge_rule": "approved_changes",
                    **await delivery._merge_when_clean(
                        integrations=integrations,
                        run=run,
                        source=source,
                        manifest={**manifest, "files": files},
                        new_paths=(),
                        number=number,
                        branch=published.get("pull_request_branch"),
                        sleep=sleep,
                        clock=clock,
                        ready=delivery.REQUIRED_CHECKS_READY,
                        record_state=True,
                    ),
                }
            if result["status"] == "merged":
                event = "website_change_merged"
                summary = (
                    f"Tin merged PR #{number}: {source['change']['title']}."
                    if result.get("merged_by") == "tin"
                    else f"PR #{number} was merged: {source['change']['title']}."
                )
            else:
                event = "website_change_left_open"
                summary = f"PR #{number} is open. {result['reason']}"
            async with conn.transaction():
                await database.complete_effect(conn, execution_key=key, result=result)
                await database.add_activity(
                    conn=conn,
                    run_id=run.id,
                    event_type=event,
                    audience="product",
                    summary=summary[:240],
                    details={
                        "kind": "runs",
                        "external_url": result.get("url") or published["external_url"],
                        "external_label": f"View PR #{number}",
                        "change_ids": [source["change"]["change_id"]],
                    },
                    dedupe_key=f"{key}:{event}:{run.id}",
                )
        except Exception:
            await database.fail_effect(
                conn,
                execution_key=key,
                error_message="Tin could not confirm the merge. The pull request is unchanged.",
            )
            raise
    return result
