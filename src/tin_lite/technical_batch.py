"""site-fix-v5 delivery checks, live predicates and the run report for batch repairs.

A batch pull request can touch any file that serves an audit finding, in any framework, so
most of it can't be checked from the diff the way site-fix-v4's single static edits were.
What the worker still proves before the PR opens:

- the patch stays inside the run's bounds (files, changed lines, file sizes, text only);
- it never touches dependencies, lockfiles, CI, deploy settings, secrets or submodules
  (a host's redirect list is the one deploy setting it may edit, and only that list);
- files the site serves byte for byte (robots.txt, a sitemap, a static page) change only as
  their findings call for, checked from the diff as in site-fix-v4;
- anything else carries Tin's sentence that it couldn't build the site.

After the PR merges, Tin re-reads the affected pages and files and records per finding
whether the problem is gone (technical_fix_live), so a framework change is checked on the
live site rather than trusted.
"""

from __future__ import annotations

import difflib
import html as htmllib
import json
import posixpath
import re
import tomllib
from datetime import UTC, datetime
from urllib.parse import urlsplit

from tin_lite import technical_repair_plan as plan
from tin_lite import technical_site_rules as site_rules
from tin_lite.organic_audit_site import (
    AI_SEARCH_CRAWLERS,
    crawler_stances,
    html_facts,
    is_noindex,
    language_prefix,
    named_groups_missing_wildcard_rules,
    parse_robots,
    path_allowed,
    robots_group,
    url_key,
)

FRAMEWORK_NOTE = site_rules.FRAMEWORK_NOTE

# Never edited by a repair, whatever the finding.
BLOCKED_NAMES = frozenset(
    {
        *site_rules.BLOCKED_NAMES,
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "Gemfile",
        "Gemfile.lock",
        "composer.json",
        "composer.lock",
        "pom.xml",
        "build.gradle",
        "deno.json",
        "deno.jsonc",
        "deno.lock",
        ".gitmodules",
        ".gitlab-ci.yml",
        ".travis.yml",
        "azure-pipelines.yml",
        "bitbucket-pipelines.yml",
        "Jenkinsfile",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "fly.toml",
        "render.yaml",
        "app.yaml",
        "Procfile",
        "wrangler.toml",
        "firebase.json",
    }
)
BLOCKED_PREFIXES = (".github/", ".circleci/", ".husky/", ".git/")
# Host settings whose redirect list, and nothing else, a merge or redirect repair may edit.
REDIRECT_KEYS = {"vercel.json": "redirects", "netlify.toml": "redirects"}


def blocked(path: str) -> str | None:
    """Why a repair may not touch this path, or None."""
    name = posixpath.basename(path)
    if path.startswith(BLOCKED_PREFIXES) or "/.github/" in path:
        return "CI settings"
    if name.startswith(".env"):
        return "secrets"
    if name in BLOCKED_NAMES and name not in REDIRECT_KEYS:
        return "dependencies, CI or deploy settings"
    return None


def changed_lines(before: str, after: str) -> int:
    return sum(
        1
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    )


def _redirects_only(path: str, before: str, after: str) -> None:
    name = posixpath.basename(path)
    key = REDIRECT_KEYS[name]
    try:
        old = json.loads(before) if name.endswith(".json") else tomllib.loads(before)
        new = json.loads(after) if name.endswith(".json") else tomllib.loads(after)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path} must stay valid.") from exc
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ValueError(f"{path} must stay valid.")
    if {k: v for k, v in old.items() if k != key} != {k: v for k, v in new.items() if k != key}:
        raise ValueError(f"Only the redirect list in {path} may change.")


def check_bounds(files: list[dict], originals: dict[str, str | None]) -> int:
    """The patch's shape: count, paths, sizes and changed lines. Returns the line count."""
    if not 1 <= len(files) <= plan.MAX_FILES:
        raise ValueError(f"A technical fix changes one to {plan.MAX_FILES} files.")
    total = 0
    for item in files:
        path, content = item["path"], item["content"]
        reason = blocked(path)
        if reason:
            raise ValueError(f"A technical fix may not change {path} ({reason}).")
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("A technical fix changes text files only.")
        before = originals.get(path)
        if before is None:
            if len(content.encode()) > plan.MAX_NEW_FILE_BYTES:
                raise ValueError(f"{path} is too large for a new file in a technical fix.")
            total += len(content.splitlines())
        else:
            if len(content.encode()) > plan.MAX_FILE_BYTES:
                raise ValueError(f"{path} is too large.")
            if content == before:
                raise ValueError(f"{path} did not change.")
            if posixpath.basename(path) in REDIRECT_KEYS:
                _redirects_only(path, before, content)
            total += changed_lines(before, content)
    if total > plan.MAX_CHANGED_LINES:
        raise ValueError(
            f"The patch changes {total} lines; a technical fix changes at most "
            f"{plan.MAX_CHANGED_LINES}."
        )
    return total


# --- Files the site serves byte for byte ---------------------------------------------------

STRICT_ROBOTS = {"robots_sitemap_line", "robots_allow_ai_search"}
STRICT_SITEMAP = {"sitemap_remove_urls", "sitemap_add_urls"}
STRICT_HTML = {
    "html_title",
    "html_description",
    "html_noindex",
    "html_self_canonical",
    "html_one_canonical",
    "html_lang",
    "html_h1",
}
MAX_STRICT_HTML_LINES = 12


def verify_robots(before: str | None, after: str, expected: dict) -> None:
    """robots.txt may gain the expected Sitemap lines and let the listed AI search crawlers
    in, and nothing else about what any crawler may fetch may change."""
    if len(after.encode()) > site_rules.MAX_TEXT_BYTES:
        raise ValueError("robots.txt is too large.")
    new = parse_robots(after)
    old = parse_robots(before) if before is not None else {"groups": [], "sitemaps": []}
    sitemaps = expected.get("sitemaps")
    if sitemaps:
        if old["sitemaps"] or new["sitemaps"] != sitemaps:
            raise ValueError("robots.txt must gain exactly the site's Sitemap line.")
    elif new["sitemaps"] != old["sitemaps"]:
        raise ValueError("The Sitemap lines must not change.")
    agents = {agent.lower() for agent in expected.get("agents", [])}
    for agent in expected.get("agents", []):
        _, rules = robots_group(new, agent)
        if not path_allowed(rules, "/"):
            raise ValueError(f"{agent} is still blocked at the site root.")
    if before is None:
        if any(rule == "disallow" for group in new["groups"] for rule, _ in group["rules"]):
            raise ValueError("A new robots.txt may not disallow anything.")
        return
    paths = site_rules._probe_paths(old, new)
    for agent in site_rules._agents(old, new) - agents:
        _, before_rules = robots_group(old, agent)
        _, after_rules = robots_group(new, agent)
        for path in paths:
            if path_allowed(before_rules, path) != path_allowed(after_rules, path):
                raise ValueError(f"The change also alters what {agent} may crawl.")


def verify_sitemap(before: str, after: str, expected: dict) -> None:
    """Only whole <url> entries for the findings' pages may leave or join the sitemap."""
    head, old, tail = site_rules._blocks(before)
    new_head, new, new_tail = site_rules._blocks(after)
    if (head, tail) != (new_head, new_tail):
        raise ValueError("Only <url> entries may change in the sitemap.")
    remove = {url_key(url) for url in expected.get("remove", [])}
    add = {url_key(url): url for url in expected.get("add", [])}
    kept = [row for row in old if url_key(row[0]) not in remove]
    existing = [row for row in new if row in old]
    added = [row for row in new if row not in old]
    if existing != kept:
        raise ValueError("The sitemap may lose only the listed pages' entries.")
    if {url_key(loc) for loc, _ in added} - set(add):
        raise ValueError("The sitemap may gain only the listed pages.")
    for loc, block in added:
        if not site_rules._NEW_BLOCK.fullmatch(block.strip()) or loc != add[url_key(loc)]:
            raise ValueError("A new sitemap entry holds one <loc> and an optional <lastmod>.")


def _visible_text(html: str) -> list[str]:
    """The words a reader sees in <body>, in order."""
    body = re.split(r"<body\b[^>]*>", html, maxsplit=1, flags=re.I)[-1]
    body = re.sub(r"<(script|style|template)\b.*?</\1\s*>", " ", body, flags=re.I | re.S)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", body)).split()


def verify_html(before: str, after: str, kinds: list[str], page_url: str) -> None:
    """One change: site-fix-v4's exact tag check. Several: each finding gone, the visible text
    unchanged (an added H1 aside), and the diff small."""
    if len(kinds) == 1:
        return site_rules.verify_html_change(before, after, kinds[0], page_url)
    site_rules._same_page(before, after)
    for kind in kinds:
        if site_rules.page_needs(kind, after, page_url):
            raise ValueError(f"The page still needs its {kind.replace('_', ' ')} change.")
    old, new = _visible_text(before), _visible_text(after)
    if new != old:
        added = [word for word in new]
        for word in old:
            if word in added:
                added.remove(word)
        if "html_h1" not in kinds or len(new) - len(old) != len(added) or len(added) > 16:
            raise ValueError("A served page's visible text may not change.")
    if changed_lines(before, after) > MAX_STRICT_HTML_LINES:
        raise ValueError("A served page changes only in the tags its findings call for.")


def verify_strict(entry: dict, before: str, after: str) -> None:
    target = entry["target"]
    if target == "robots":
        return verify_robots(
            before if entry.get("exists", True) else None, after, entry["expected"]
        )
    if target == "sitemap":
        return verify_sitemap(before, after, entry["expected"])
    return verify_html(before, after, entry["kinds"], entry["page_url"])


def strict_kinds(target: str, kinds: set[str]) -> bool:
    """Whether a served file's findings are all ones the diff can prove."""
    allowed = {"robots": STRICT_ROBOTS, "sitemap": STRICT_SITEMAP, "html": STRICT_HTML}[target]
    return bool(kinds) and kinds <= allowed


def validate(manifest: dict, prepared: dict, originals: dict[str, str | None] | None) -> None:
    """A batch patch, before its PR opens. `originals` is the pinned repository's content of
    each changed path (None for a new file); without it (a PR already delivered) only the
    shape is checked again."""
    batch = prepared["batch"]
    files = manifest["files"]
    if manifest.get("outcome") != "patch":
        raise ValueError("The technical result has no valid outcome.")
    overlap = set(batch.get("overlap_paths", []))
    for item in files:
        if item["path"] in overlap:
            raise ValueError(f"An open pull request already changes {item['path']}.")
    for item in files:
        reason = blocked(item["path"])
        if reason:
            raise ValueError(f"A technical fix may not change {item['path']} ({reason}).")
    if originals is None:
        if not 1 <= len(files) <= plan.MAX_FILES:
            raise ValueError(f"A technical fix changes one to {plan.MAX_FILES} files.")
        return
    check_bounds(files, originals)
    strict = batch.get("strict_files", {})
    unproven = False
    for item in files:
        entry = strict.get(item["path"])
        if entry is None:
            unproven = True
            continue
        verify_strict(entry, originals.get(item["path"]) or "", item["content"])
    if unproven and FRAMEWORK_NOTE not in manifest.get("body", ""):
        raise ValueError("A PR with changes Tin couldn't build must say so.")


# --- Live predicates ----------------------------------------------------------------------


def page_fixed(predicate: str, html: str, url: str, entry: dict) -> bool:
    """Whether a freshly read page no longer shows its finding."""
    facts = html_facts(html.encode(), url=url, charset="utf-8", truncated=False)
    if predicate == "noindex":
        return is_noindex(facts)
    if predicate == "indexable":
        return not is_noindex(facts)
    if predicate == "self_canonical":
        return not facts.get("canonical") or url_key(facts["canonical"]) == url_key(url)
    if predicate == "one_canonical":
        return facts.get("canonical_count", 0) <= 1
    if predicate == "title":
        return bool(facts.get("title"))
    if predicate == "description":
        return facts.get("description_length") is not None
    if predicate == "h1":
        return facts.get("h1_count", 0) >= 1
    if predicate == "one_h1":
        return facts.get("h1_count", 0) == 1
    if predicate == "lang":
        return bool(facts.get("lang"))
    if predicate == "lang_match":
        prefix = language_prefix(url)
        lang = (facts.get("lang") or "").lower()
        return not prefix or lang.split("-")[0] == prefix.lower().split("-")[0]
    if predicate == "hreflang":
        return bool(facts.get("hreflang"))
    if predicate == "viewport":
        return bool(facts.get("viewport"))
    if predicate == "image_alt":
        return not facts.get("images_without_alt")
    if predicate == "accessible_name":
        return not facts.get("unnamed_controls")
    if predicate == "form_label":
        return not facts.get("unlabeled_fields")
    if predicate == "schema":
        return not facts.get("schema_problems") and not facts.get("schema_invalid_blocks")
    if predicate == "open_graph":
        return "title" in set(facts.get("open_graph") or [])
    if predicate == "reachable":
        return True  # The read itself succeeded, so the loop is gone.
    raise ValueError(f"Unknown page check {predicate}.")


def robots_fixed(predicate: str, text: str, status_code: int, entry: dict) -> bool:
    state = "observed" if status_code == 200 else "missing"
    parsed = parse_robots(text) if state == "observed" else None
    if predicate == "robots":
        if entry["kind"] == "robots_sitemap_line":
            return bool(parsed and parsed["sitemaps"])
        if entry["kind"] == "robots_allow_ai_search":
            return parsed is None or not any(
                row["agent"] in AI_SEARCH_CRAWLERS and row["stance"] == "blocked"
                for row in crawler_stances({"status": "observed", **parsed})
            )
    if parsed is None:
        return predicate != "robots_named"
    if predicate == "robots_paths":
        _, rules = robots_group(parsed, "googlebot")
        return all(path_allowed(rules, urlsplit(url).path or "/") for url in entry["urls"][:10])
    if predicate == "robots_wildcard":
        _, rules = robots_group(parsed, "*")
        return path_allowed(rules, "/")
    if predicate == "robots_named":
        return not named_groups_missing_wildcard_rules({"status": "observed", **parsed})
    raise ValueError(f"Unknown robots check {predicate}.")


def sitemap_fixed(predicate: str, texts: list[str], entry: dict) -> bool:
    keys = {url_key(loc) for text in texts for loc in site_rules.sitemap_locs(text)}
    if predicate == "sitemap_remove":
        return not any(url_key(url) in keys for url in entry["urls"])
    if predicate == "sitemap_add":
        return all(url_key(url) in keys for url in entry["urls"])
    if predicate == "sitemap_exists":
        return bool(keys)
    raise ValueError(f"Unknown sitemap check {predicate}.")


PAGE_PREDICATES = frozenset(
    {
        "noindex",
        "indexable",
        "self_canonical",
        "one_canonical",
        "title",
        "description",
        "h1",
        "one_h1",
        "lang",
        "lang_match",
        "hreflang",
        "viewport",
        "image_alt",
        "accessible_name",
        "form_label",
        "schema",
        "open_graph",
        "reachable",
    }
)
ROBOTS_PREDICATES = frozenset({"robots", "robots_paths", "robots_wildcard", "robots_named"})
SITEMAP_PREDICATES = frozenset({"sitemap_remove", "sitemap_add", "sitemap_exists"})


# --- The report ----------------------------------------------------------------------------

REASONS = {
    "nothing_to_fix": (
        "Nothing in this audit is left for a technical fix: every finding is copy, manual, "
        "already resolved, or waiting for a decision. No change proposed."
    ),
    "already_resolved": "The live site no longer shows these problems. No change proposed.",
    "site_unreadable": "Tin couldn't read the live site for these findings. No change proposed.",
    "unsupported_source": "Tin couldn't read the repository. No change proposed.",
    "incomplete_pr_evidence": "Open-PR evidence is incomplete. No change proposed.",
    "no_safe_patch": "Codex could not prepare a safe change. The findings remain unresolved.",
}

LEFT_OUT = (
    ("decision_unanswered", "Waiting for a decision"),
    ("decided_keep", "Kept as they are, as you chose"),
    ("already_resolved", "Already fixed on the live site"),
    ("over_cap", "Left for a later run"),
    ("copy", "Copy, left to the content workflows"),
    ("manual", "Manual steps"),
    ("no_change", "No change needed"),
    ("ineligible", "Not eligible"),
)


def report(prepared: dict, *, reason: str | None = None, pull_request=None) -> bytes:
    batch = prepared["batch"]
    lines = ["# Technical fix", "", "## Result", ""]
    if reason:
        lines += [REASONS[reason], ""]
    else:
        count = len(batch["repairs"])
        lines += [
            f"A pull request that fixes {count} audit finding{'s' if count != 1 else ''} is "
            "ready for review.",
            "",
        ]
    if pull_request is not None:
        lines += [f"Pull request: {pull_request.url}", ""]
        if batch.get("unproven", True):
            lines += [FRAMEWORK_NOTE, ""]
    if batch["repairs"] and not reason:
        lines += ["## In this pull request", ""]
        for group, entries in plan.grouped(batch["repairs"]):
            lines += [f"### {plan.GROUP_TITLES[group]}", ""]
            for entry in entries:
                decided = f" (decision: {entry['decision']})" if entry.get("decision") else ""
                pages = ", ".join(entry["urls"][:3])
                more = (entry.get("affected_count") or len(entry["urls"])) - min(
                    3, len(entry["urls"])
                )
                lines.append(
                    f"- {entry['issue']}: {entry['change']}{decided}."
                    + (
                        f" Pages: {pages}" + (f" and {more} more." if more > 0 else ".")
                        if pages
                        else ""
                    )
                    + f" (`{entry['finding_id']}`)"
                )
            lines.append("")
    for key, title in LEFT_OUT:
        rows = batch["left_out"].get(key, [])
        if not rows:
            continue
        lines += [f"## {title}", ""]
        for row in rows:
            lines.append(f"- {row['issue']}: {row['reason']} (`{row['id']}`)")
        lines.append("")
    source, binding = prepared["source"], prepared["repository_binding"]
    lines += [
        "## How it was checked",
        "",
        f"Audit: `{source['audit_run_id']}` at `{source['audit_revision']}`.",
        "",
        f"Repository: `{binding['repository']}` at `{binding['head_sha']}`.",
        "",
        "Files the site serves as they are (robots.txt, sitemaps, static pages) were checked "
        "from the diff: they change only as their findings call for. Every other change stays "
        f"within {plan.MAX_FILES} files and {plan.MAX_CHANGED_LINES} changed lines, never "
        "touches dependencies, CI, deploy settings or secrets, and is checked on the live site "
        "after it merges.",
        "",
    ]
    if batch.get("observations"):
        lines += ["## Fresh observations", ""]
        for row in batch["observations"]:
            lines.append(
                f"- {row['url']}: HTTP {row.get('status_code')}, observed {row.get('observed_at')}."
            )
        lines.append("")
    lines += [
        "No merge, deployment, content publication or outreach was performed. A PR is not a "
        "deployed repair. After it merges, Tin checks the live site for each finding.",
        "",
        f"Report written {datetime.now(UTC).date().isoformat()}.",
        "",
    ]
    return "\n".join(lines).encode()
