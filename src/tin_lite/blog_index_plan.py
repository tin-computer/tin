"""content.blog_index's plan: PLAN.md and the patch website.change applies after approval.

content.blog_index reads the connected repository read-only and opens no pull request. It
writes `reports/blog-index/{run_id}/PLAN.md`: the plan in prose, a fenced JSON patch between
the markers below, and a closing `blog-index` summary block. website.change (`source:
blog_index`) applies the patch once the founder approves it, against `base_sha`.

The patch, `blog-index-patch/1`, is a contract shared with website.change:

    {"schema": "blog-index-patch/1", "repository": "owner/repo", "base_ref": "<branch>",
     "base_sha": "<sha the plan read>", "route": "/blog", "summary": "...",
     "files": [{"path": "...", "action": "create|update", "content": "<full file text>"}],
     "caps": {"max_files": 5}}

At most five files, each with its full text; never package.json, a lockfile, or CI and
deploy settings. A no-change plan (check mode, nothing to fix, no route for articles, a
source Tin cannot trace or a hosted blog) has no files, and without a route it carries the
founder question. This module only checks the shape; website.change checks the files against
the repository at `base_sha` before it writes anything.
"""

from __future__ import annotations

import json
import re
from posixpath import basename

VALIDATOR = "blog-index-plan.v1"
PATH_TEMPLATE = "reports/blog-index/{run_id}/PLAN.md"
SCHEMA = "blog-index-patch/1"
START = "<!-- blog-index-patch.json:start -->"
END = "<!-- blog-index-patch.json:end -->"
MAX_FILES = 5
MAX_CONTENT_BYTES = 120_000
MAX_PLAN_BYTES = 200_000
KEYS = {"schema", "repository", "base_ref", "base_sha", "route", "summary", "files", "caps"}
REASONS = {"check_only", "nothing_to_fix", "no_article_route", "source_not_found", "hosted_blog"}
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
ROUTE = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*|/")
# Framework route files use brackets, $, + and @ (Next.js [slug], Remix $slug, SvelteKit +page).
FILE_PATH = re.compile(r"[A-Za-z0-9._@()\[\]+$~=-]+(?:/[A-Za-z0-9._@()\[\]+$~=-]+)*")
# Dependencies and their lockfiles, and the settings that build, test or deploy the site.
LOCKFILES = {
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "deno.lock",
    "Gemfile",
    "Gemfile.lock",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
}
SETTINGS = {
    "vercel.json",
    "netlify.toml",
    "fly.toml",
    "render.yaml",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Procfile",
    ".gitlab-ci.yml",
}
SETTINGS_DIRS = (".github/", ".circleci/", ".git/", ".husky/")


def _block(text: str, start: str, end: str) -> str | None:
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        return None
    inner = text[text.index(start) + len(start) : text.index(end)].strip()
    lines = inner.split("\n")
    if lines and lines[0].strip() in ("```json", "```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _fenced(text: str, info: str) -> list[str]:
    return re.findall(rf"^```json {re.escape(info)}\n(.*?)\n```$", text, re.M | re.S)


def check_path(path: object) -> str:
    """A repository-relative file path the patch may write, or ValueError."""
    if not isinstance(path, str) or not 1 <= len(path) <= 300 or not FILE_PATH.fullmatch(path):
        raise ValueError("A planned file path must be a relative repository path.")
    parts = path.split("/")
    if any(part in (".", "..") for part in parts):
        raise ValueError("A planned file path must not leave the repository.")
    if basename(path) in LOCKFILES:
        raise ValueError(f"The plan must not change {basename(path)}.")
    if basename(path) in SETTINGS or (path + "/").startswith(SETTINGS_DIRS):
        raise ValueError("The plan must not change build, CI or deploy settings.")
    return path


def check_patch(patch: object) -> dict:
    """The parsed blog-index-patch/1 object, or ValueError naming the broken rule."""
    if not isinstance(patch, dict) or set(patch) != KEYS:
        raise ValueError("The patch must have exactly the blog-index-patch/1 keys.")
    if patch["schema"] != SCHEMA:
        raise ValueError(f"The patch schema must be {SCHEMA}.")
    if not isinstance(patch["repository"], str) or not REPOSITORY.fullmatch(patch["repository"]):
        raise ValueError("The patch repository must be owner/repo.")
    ref = patch["base_ref"]
    if (
        not isinstance(ref, str)
        or not 1 <= len(ref) <= 255
        or ref.startswith("-")
        or any(c.isspace() or ord(c) < 32 for c in ref)
    ):
        raise ValueError("The patch base_ref must be a branch name.")
    if not isinstance(patch["base_sha"], str) or not SHA.fullmatch(patch["base_sha"]):
        raise ValueError("The patch base_sha must be the commit the plan read.")
    route = patch["route"]
    if not isinstance(route, str) or (route and not ROUTE.fullmatch(route)):
        raise ValueError("The patch route must be a site path such as /blog.")
    if not isinstance(patch["summary"], str) or len(patch["summary"]) > 1000:
        raise ValueError("The patch summary must be text of at most 1000 characters.")
    if patch["caps"] != {"max_files": MAX_FILES}:
        raise ValueError(f"The patch caps must be {{'max_files': {MAX_FILES}}}.")
    files = patch["files"]
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError(f"The patch holds at most {MAX_FILES} files.")
    seen, size = set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "action", "content"}:
            raise ValueError("Each planned file has exactly path, action and content.")
        path = check_path(item["path"])
        if path in seen:
            raise ValueError("A planned file appears twice.")
        seen.add(path)
        if item["action"] not in ("create", "update"):
            raise ValueError("A planned file's action is create or update.")
        if not isinstance(item["content"], str) or not item["content"]:
            raise ValueError("A planned file carries its full text.")
        size += len(item["content"].encode())
    if size > MAX_CONTENT_BYTES:
        raise ValueError(f"The planned files exceed {MAX_CONTENT_BYTES} bytes.")
    if files and not route:
        raise ValueError("A patch with files names the index route.")
    return patch


def parse(content: bytes | str) -> dict:
    """{"patch": ..., "summary": ...} from a PLAN.md, or ValueError naming the broken rule."""
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    text = text.replace("\r\n", "\n")
    if len(text.encode()) > MAX_PLAN_BYTES:
        raise ValueError(f"The plan exceeds {MAX_PLAN_BYTES} bytes.")
    if not re.search(r"^## Outcome$", text, re.M):
        raise ValueError("The plan starts its report with an Outcome section.")
    raw = _block(text, START, END)
    if raw is None:
        raise ValueError("The plan holds exactly one blog-index-patch.json block.")
    try:
        patch = check_patch(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("The blog-index-patch.json block is not JSON.") from exc
    blocks = _fenced(text, "blog-index")
    if not blocks:
        raise ValueError("The plan ends with a blog-index summary block.")
    try:
        summary = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        raise ValueError("The blog-index summary block is not JSON.") from exc
    if not isinstance(summary, dict) or summary.get("outcome") not in ("plan", "no_change"):
        raise ValueError("The summary's outcome is plan or no_change.")
    if summary["outcome"] == "plan" and (not patch["files"] or summary.get("reason")):
        raise ValueError("A plan outcome carries files and no reason.")
    if summary["outcome"] == "no_change":
        if patch["files"]:
            raise ValueError("A no_change outcome carries no files.")
        if summary.get("reason") not in REASONS:
            raise ValueError("A no_change outcome names its reason.")
    if summary.get("reason") == "no_article_route":
        asks = _fenced(text, "ask-the-founder")
        try:
            ask = json.loads(asks[-1]) if asks else None
        except json.JSONDecodeError:
            ask = None
        if not isinstance(ask, dict) or (ask.get("then") or {}).get("name") != "save_page_route":
            raise ValueError("Without a route for articles, the plan asks the founder.")
    return {"patch": patch, "summary": summary}


def validate(content: bytes | str) -> None:
    parse(content)
