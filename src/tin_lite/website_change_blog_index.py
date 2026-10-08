"""website.change, phase 3: the blog index plan, applied as it is.

`content.blog_index` (#239) plans and opens no pull request. Its run writes
`reports/blog-index/{run_id}/PLAN.md` with one fenced JSON block between
`<!-- blog-index-patch.json:start -->` and `<!-- blog-index-patch.json:end -->`:

    {"schema": "blog-index-patch/1", "repository": "owner/repo", "base_ref": "<branch>",
     "base_sha": "<sha the plan read>", "route": "/blog", "summary": "…",
     "files": [{"path": "…", "action": "create|update", "content": "<full file text>"}],
     "caps": {"max_files": 5}}

A website.change run with `source: blog_index` reads the newest succeeded content.blog_index
run's plan server-side and records one `website_changes` row: change ID `bi_` plus the first
20 hex digits of the SHA-256 of the canonical JSON of `files`, kind `index`, the route as its
path, and the full SHA-256 as the content an approval covers. Nothing here needs judgment, so
no Codex session runs: Tin opens the pull request with exactly those files, and under the same
mode rules as the other sources merges it once the required checks pass when the row was
approved and touches no protected page. Otherwise the founder merges it.

A plan read from an older commit is applied only when none of its files changed upstream
since; otherwise the row stays and the start says which file moved. Never more than five
files, and never dependencies, lockfiles, CI, deploy settings or secrets. The rules are shared
with the other planned patches (website_change_patch).
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from tin_lite import content_repository_delivery as delivery
from tin_lite import technical_batch as batch_rules
from tin_lite import website_change
from tin_lite import website_change_patch as patch

SOURCE = delivery.BLOG_INDEX_SOURCE
PLAN_WORKFLOW = "content.blog_index"
SCHEMA = "blog-index-patch/1"
MAX_FILES = 5
MAX_BYTES = 400_000
BLOCK = re.compile(
    r"<!-- blog-index-patch\.json:start -->\s*```json\s*(\{.*\})\s*```\s*"
    r"<!-- blog-index-patch\.json:end -->",
    re.S,
)
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}")
FILE_PATH = re.compile(r"[A-Za-z0-9_.\-()\[\]+@ ][A-Za-z0-9_.\-()\[\]+@ /]{0,250}")
NONE_YET = (
    "No blog index plan yet: content.blog_index has not finished a run in this project, so "
    "there is nothing to apply."
)


def plan_path(run_id) -> str:
    return f"reports/blog-index/{UUID(str(run_id))}/PLAN.md"


def blocked(path: str) -> str | None:
    """Why a blog index may not write this file, or None."""
    if path.rsplit("/", 1)[-1] in delivery.DEPENDENCY_FILES:
        return "dependencies"
    return batch_rules.blocked(path)


def parse_plan(text: str) -> dict[str, Any]:
    """The plan's patch, validated against the contract; raises ValueError naming the fault."""
    found = BLOCK.search(text)
    if not found:
        raise ValueError("The blog index plan has no blog-index-patch.json block.")
    try:
        plan = json.loads(found.group(1))
    except ValueError as exc:
        raise ValueError("The blog index plan's JSON block does not parse.") from exc
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise ValueError(f"The blog index plan is not {SCHEMA}.")
    if not isinstance(plan.get("repository"), str) or not REPOSITORY.fullmatch(plan["repository"]):
        raise ValueError("The blog index plan names no owner/repository.")
    if not isinstance(plan.get("base_ref"), str) or not plan["base_ref"].strip():
        raise ValueError("The blog index plan names no base branch.")
    if not isinstance(plan.get("base_sha"), str) or not SHA.fullmatch(plan["base_sha"]):
        raise ValueError("The blog index plan names no commit it read.")
    route = website_change.site_path(plan.get("route"))
    if route is None or "{" in route:
        raise ValueError("The blog index plan's route is not a site path such as /blog.")
    caps = plan.get("caps") if isinstance(plan.get("caps"), dict) else {}
    limit = min(MAX_FILES, int(caps.get("max_files") or MAX_FILES))
    files = plan.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= limit:
        raise ValueError(f"A blog index plan changes one to {limit} files.")
    seen, total = set(), 0
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "action", "content"}
            or item["action"] not in {"create", "update"}
            or not isinstance(item["path"], str)
            or not isinstance(item["content"], str)
        ):
            raise ValueError("Each blog index file is a path, a create or update action, and text.")
        path = item["path"]
        if not FILE_PATH.fullmatch(path) or ".." in path.split("/") or path in seen:
            raise ValueError(f"{path!r} is not a repository file path.")
        reason = blocked(path)
        if reason:
            raise ValueError(f"A blog index may not change {path} ({reason}).")
        if "\x00" in item["content"]:
            raise ValueError("A blog index changes text files only.")
        seen.add(path)
        total += len(item["content"].encode())
    if total > MAX_BYTES:
        raise ValueError(f"A blog index plan stays under {MAX_BYTES // 1000} KB.")
    return {
        "schema": SCHEMA,
        "repository": plan["repository"],
        "base_ref": plan["base_ref"],
        "base_sha": plan["base_sha"],
        "route": route,
        "summary": " ".join(str(plan.get("summary") or "").split())[:500],
        "files": [{key: item[key] for key in ("path", "action", "content")} for item in files],
    }


def _plan_lines(source: dict, plan: dict) -> list[str]:
    return [
        f"From content.blog_index run `{source['plan_run_id']}`, read at `{plan['base_sha']}` "
        f"of {plan['base_ref']}, for {plan['route']}.",
        "",
        *[f"- {item['action']} `{item['path']}`" for item in plan["files"]],
        "",
        "Tin applied the plan's files as they are and wrote no copy of its own.",
    ]


def _body_lines(plan: dict) -> list[str]:
    return [
        plan["summary"] or f"A blog index for {plan['route']}.",
        "",
        "Files, as content.blog_index planned them:",
        "",
        *[f"- {item['action']} `{item['path']}`" for item in plan["files"]],
    ]


SPEC = patch.PatchSource(
    source=SOURCE,
    kind="index",
    prefix="bi",
    plan_workflow=PLAN_WORKFLOW,
    noun="blog index plan",
    label="blog index",
    title_prefix="Blog index",
    rerun="Run content.blog_index again.",
    none_yet=NONE_YET,
    plan_path=plan_path,
    parse_plan=parse_plan,
    max_plan_bytes=MAX_BYTES,
    detail=lambda plan: {},
    plan_lines=_plan_lines,
    body_lines=_body_lines,
)


def change_id(files: list[dict]) -> str:
    """`bi_` and the first 20 hex digits of the SHA-256 of the canonical JSON of `files`."""
    return f"bi_{patch.files_sha256(files)[:20]}"


async def plan_changes(
    *, database, storage, integrations, project_id: UUID, inputs: dict, bind: bool = True
) -> dict[str, Any]:
    """Record the newest blog index plan as one row and say whether the next run applies it."""
    return await patch.plan_changes(
        SPEC,
        database=database,
        storage=storage,
        integrations=integrations,
        project_id=project_id,
        inputs=inputs,
        bind=bind,
    )


async def select_source(*, database, storage, integrations, project_id, inputs) -> dict:
    return await patch.select_source(
        SPEC,
        database=database,
        storage=storage,
        integrations=integrations,
        project_id=project_id,
        inputs=inputs,
    )


async def guard_source(conn, *, project_id, inputs, source) -> None:
    return await patch.guard_source(SPEC, conn, project_id=project_id, inputs=inputs, source=source)


async def apply(*, database, storage, integrations, run, sleep=None, clock=None) -> bool:
    return await patch.apply(
        SPEC,
        database=database,
        storage=storage,
        integrations=integrations,
        run=run,
        sleep=sleep,
        clock=clock,
    )
