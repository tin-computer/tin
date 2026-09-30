"""Versioned technical repair policies; never a merge or a deploy.

`missing-html-title-v1` through `html-metadata-v3` repair a missing title or description in
matched static or packaged HTML. `site-fix-v4` adds robots.txt, sitemap and page-tag repairs
for one finding: static files are checked from the diff in the worker, and framework source
is bounded by the files Tin names and checked on the live site after the founder deploys it.
`site-fix-v5` repairs every fixable finding of one audit in one pull request, in any
framework, with judgment calls answered through MCP (technical_repair_plan, technical_batch).
"""

import asyncio
import hashlib
import io
import ipaddress
import socket
import tarfile
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from tin_lite import technical_batch as batch_rules
from tin_lite import technical_repair_plan as repair_plan
from tin_lite import technical_site_rules as site_rules
from tin_lite.organic_audit import in_scope_url, public_site
from tin_lite.technical_metadata_rules import (
    DESCRIPTION_CHECK,
    SUPPORTED_CHECKS,
    TITLE_CHECK,
    has_metadata,
    verify_metadata_change,
)
from tin_lite.technical_title_rules import (
    MAX_HTML_BYTES,
    TitleParser,
    has_title,
)

KEY = "organic.technical_fix"
LEGACY_POLICY = "missing-html-title-v1"
WHOLE_FINDING_POLICY = "html-metadata-v2"
POLICY = "html-metadata-v3"
SITE_POLICY = "site-fix-v4"
BATCH_POLICY = repair_plan.POLICY
# Policies that may repair only part of a finding and list the pages they left alone.
PARTIAL_POLICIES = frozenset({POLICY, SITE_POLICY, BATCH_POLICY})
LEGACY_CHECK_COMMAND = "python3 /opt/tin-lite/verify-technical-title.py"
CHECK_COMMAND = (
    "/opt/tin-lite/metadata-venv/bin/python -I /opt/tin-lite/verify-technical-metadata.py"
)
# The sandbox command each policy declares. site-fix-v4 declares none: its static repairs
# are checked from the diff in the worker, and the sandbox image stays as it is.
POLICY_COMMANDS = {
    LEGACY_POLICY: LEGACY_CHECK_COMMAND,
    WHOLE_FINDING_POLICY: CHECK_COMMAND,
    POLICY: CHECK_COMMAND,
    SITE_POLICY: None,
    BATCH_POLICY: None,
}
# The most files a policy's pull request may change.
POLICY_MAX_FILES = {BATCH_POLICY: repair_plan.MAX_FILES}


def policy_commands(policy):
    """The exact verification command list a policy's procedure definition declares."""
    if policy not in POLICY_COMMANDS:
        raise ValueError("Unsupported technical repair policy.")
    command = POLICY_COMMANDS[policy]
    return [command] if command else []


def supported_checks(policy):
    if policy == LEGACY_POLICY:
        return frozenset({TITLE_CHECK})
    if policy in {WHOLE_FINDING_POLICY, POLICY}:
        return SUPPORTED_CHECKS
    if policy == SITE_POLICY:
        return SUPPORTED_CHECKS | frozenset(site_rules.SITE_FIXES)
    if policy == BATCH_POLICY:
        return repair_plan.supported_checks()
    raise ValueError("Unsupported technical repair policy.")


def batches(policy):
    """Whether a policy repairs every fixable finding of an audit in one run."""
    return policy == BATCH_POLICY


def current_policy():
    """The repair policy new runs pin: the one the shipped catalog declares."""
    from tin_lite.catalog import BUILTIN_WORKFLOWS

    template = next(row for row in BUILTIN_WORKFLOWS if row.key == KEY)
    return definition_policy(template.definition)


def definition_policy(definition):
    policy = definition.get("procedure", {}).get("output", {}).get("repair_policy", LEGACY_POLICY)
    supported_checks(policy)
    return policy


def selected_check(prepared):
    if prepared.get("policy", LEGACY_POLICY) == LEGACY_POLICY:
        return TITLE_CHECK
    check = prepared["selection"]["finding"]["check_id"]
    if check not in supported_checks(prepared["policy"]):
        raise ValueError("Unsupported selected check.")
    return check


BUILD_MANIFESTS = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "deno.json",
        "deno.jsonc",
    }
)
INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "audit_run_id": {"type": "string", "format": "uuid", "title": "Audit run"},
        "audit_revision": {
            "type": "string",
            "pattern": "^[0-9a-f]{40}$",
            "title": "Audit revision",
        },
        "finding_id": {"type": "string", "pattern": "^oa_[0-9a-f]{20}$", "title": "Finding"},
        "expected_repository": {
            "type": "string",
            "minLength": 3,
            "maxLength": 140,
            "title": "GitHub owner/repository",
        },
        "repository_serves_site": {
            "type": "boolean",
            "default": False,
            "title": "This repository serves the audited website",
        },
        "context": {
            "type": "string",
            "maxLength": 2000,
            "default": "",
            "title": "Additional context",
        },
    },
    "required": [
        "project_id",
        "audit_run_id",
        "audit_revision",
        "finding_id",
        "expected_repository",
        "repository_serves_site",
    ],
}


# site-fix-v5 takes an audit, not one finding: every fixable finding, or the ones listed,
# with the coding agent's answers to the judgment calls as `finding_id=choice` strings.
BATCH_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "audit_run_id": {"type": "string", "format": "uuid", "title": "Audit run"},
        "audit_revision": {
            "type": "string",
            "pattern": "^[0-9a-f]{40}$",
            "title": "Audit revision",
        },
        "finding_ids": {
            "type": "array",
            "title": "Only these findings",
            "description": "Leave empty to fix every finding the audit found that Tin can fix.",
            "items": {"type": "string", "pattern": "^oa_[0-9a-f]{20}$"},
            "maxItems": repair_plan.MAX_FINDINGS,
            "uniqueItems": True,
            "default": [],
        },
        "decisions": {
            "type": "array",
            "title": "Decisions",
            "description": "Answers to preflight_technical_fix's decisions_needed, each "
            "written finding_id=choice.",
            "items": {"type": "string", "pattern": "^oa_[0-9a-f]{20}=.{1,500}$"},
            "maxItems": repair_plan.MAX_DECISIONS,
            "default": [],
        },
        "expected_repository": {
            "type": "string",
            "minLength": 3,
            "maxLength": 140,
            "title": "GitHub owner/repository",
        },
        "repository_serves_site": {
            "type": "boolean",
            "default": False,
            "title": "This repository serves the audited website",
        },
        "context": {
            "type": "string",
            "maxLength": 2000,
            "default": "",
            "title": "Additional context",
        },
    },
    "required": [
        "project_id",
        "audit_run_id",
        "audit_revision",
        "expected_repository",
        "repository_serves_site",
    ],
}


def verified_page_host(url, target):
    """The trusted audit binding may include an observed www peer, never arbitrary hosts."""
    host = urlsplit(url).hostname
    if host not in target.get("site_hosts", [target["host"]]):
        raise ValueError("Fresh verification left the audited host binding.")
    return host


async def fetch_page(url, *, host, client=None, resolver=None):
    """Pin each socket to a public IP; redirects never broaden the audited host."""
    owned = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=15, follow_redirects=False)
    resolver = resolver or asyncio.get_running_loop().getaddrinfo
    redirects = []
    try:
        for _ in range(5):
            public_site(f"https://{host}/")
            if not in_scope_url(url, host) or any(ord(c) < 33 for c in url):
                raise ValueError("Fresh verification left the audited HTTPS host.")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != host
                or parsed.port not in {None, 443}
            ):
                raise ValueError("Fresh verification left the audited HTTPS host.")
            addresses = await resolver(host, 443, type=socket.SOCK_STREAM)
            # Keep the resolver's route preference; lexical sorting can select unreachable IPv6.
            ips = list(dict.fromkeys(row[4][0] for row in addresses))
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise ValueError("Fresh verification requires public network addresses.")
            pinned_url = httpx.URL(url).copy_with(host=ips[0])
            async with client.stream(
                "GET",
                pinned_url,
                headers={
                    "Host": host,
                    "Accept": "text/html",
                    "User-Agent": "Tin-Technical-Verification/1.0",
                },
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Fresh verification received an invalid redirect.")
                    redirects.append(url)
                    url = urljoin(url, location)
                    continue
                if (
                    response.status_code != 200
                    or "text/html" not in response.headers.get("content-type", "").lower()
                ):
                    raise ValueError("Fresh verification did not return a successful HTML page.")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_HTML_BYTES:
                        raise ValueError("Fresh HTML exceeds the verification limit.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                html = raw.decode("utf-8")
                return {
                    "url": url,
                    "redirects": redirects,
                    "status_code": 200,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "html": html,
                    "has_title": has_title(html),
                    "has_description": has_metadata(html, DESCRIPTION_CHECK),
                }
        raise ValueError("Fresh verification exceeded its redirect limit.")
    finally:
        if owned:
            await client.aclose()


SITE_FILE_TYPES = {
    "robots": ("text/plain",),
    "sitemap": ("application/xml", "text/xml", "text/plain"),
}


async def fetch_site_file(url, *, host, kind, client=None, resolver=None):
    """robots.txt or a sitemap, read like `fetch_page`: pinned public IPs, the audited host only.

    robots.txt may answer 404, which means the site has none. Anything else must be 200 with
    a text or XML content type. Returns the body as text with its SHA-256.
    """
    if kind not in SITE_FILE_TYPES:
        raise ValueError("Unsupported site file.")
    owned = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=15, follow_redirects=False)
    resolver = resolver or asyncio.get_running_loop().getaddrinfo
    redirects = []
    try:
        for _ in range(5):
            public_site(f"https://{host}/")
            parsed = urlsplit(url)
            if (
                not in_scope_url(url, host)
                or any(ord(c) < 33 for c in url)
                or parsed.scheme != "https"
                or parsed.hostname != host
                or parsed.port not in {None, 443}
            ):
                raise ValueError("Fresh verification left the audited HTTPS host.")
            addresses = await resolver(host, 443, type=socket.SOCK_STREAM)
            ips = list(dict.fromkeys(row[4][0] for row in addresses))
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise ValueError("Fresh verification requires public network addresses.")
            async with client.stream(
                "GET",
                httpx.URL(url).copy_with(host=ips[0]),
                headers={
                    "Host": host,
                    "Accept": "text/plain, application/xml, text/xml",
                    "User-Agent": "Tin-Technical-Verification/1.0",
                },
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Fresh verification received an invalid redirect.")
                    redirects.append(url)
                    url = urljoin(url, location)
                    continue
                observed_at = datetime.now(UTC).isoformat()
                if kind == "robots" and response.status_code == 404:
                    return {
                        "url": url,
                        "redirects": redirects,
                        "status_code": 404,
                        "observed_at": observed_at,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                        "text": "",
                    }
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code != 200 or not any(
                    allowed in content_type for allowed in SITE_FILE_TYPES[kind]
                ):
                    raise ValueError(f"Fresh verification could not read the site's {kind} file.")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > site_rules.MAX_TEXT_BYTES:
                        raise ValueError(f"The site's {kind} file exceeds the verification limit.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return {
                    "url": url,
                    "redirects": redirects,
                    "status_code": 200,
                    "observed_at": observed_at,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "text": raw.decode("utf-8"),
                }
        raise ValueError("Fresh verification exceeded its redirect limit.")
    finally:
        if owned:
            await client.aclose()


def matched_sources(archive, pages):
    """No guessed framework routes: require one exact source per still-broken page."""
    candidates = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        # Only the static-HTML verification profile is proven in v1. A framework
        # build cannot be replaced with our title parser, even if an HTML file matches.
        if any(
            member.name.split("/")[-1] in BUILD_MANIFESTS
            or member.name.endswith((".csproj", ".fsproj"))
            for member in source.getmembers()
        ):
            return None
        for member in source.getmembers():
            if not member.isfile() or not member.name.endswith((".html", ".htm")):
                continue
            if member.size > MAX_HTML_BYTES:
                continue
            raw = source.extractfile(member).read()
            try:
                html = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            candidates[member.name] = html
    result = {}
    for page in pages:
        if page["has_title"]:
            continue
        matches = [
            path
            for path, html in candidates.items()
            if html == page["html"] and TitleParser(html).heads == 1
        ]
        if len(matches) != 1:
            return None
        result[matches[0]] = candidates[matches[0]]
    return (
        result
        if 1 <= len(result) <= 3 and sum(len(x.encode()) for x in result.values()) <= 60_000
        else None
    )


def validate_manifest(manifest, prepared):
    binding = prepared["repository_binding"]
    if any(
        manifest.get(key) != binding[key] for key in ("repository", "default_branch", "head_sha")
    ):
        raise ValueError("The technical result does not match its pinned repository.")
    files = manifest["files"]
    if manifest.get("outcome") == "no_change":
        if files or manifest.get("reason") != "no_safe_patch":
            raise ValueError("No-change results cannot contain changes or claim a repair.")
        return
    if prepared.get("batch"):
        return batch_rules.validate(manifest, prepared, None)
    if prepared.get("site_fix"):
        return validate_site_manifest(manifest, prepared)
    if manifest.get("outcome") != "patch" or {f["path"] for f in files} != set(
        prepared["originals"]
    ):
        raise ValueError("The patch must address exactly the selected source files.")
    for item in files:
        verify_metadata_change(
            prepared["originals"][item["path"]], item["content"], selected_check(prepared)
        )


def validate_site_manifest(manifest, prepared):
    """A site-fix-v4 patch: exact static changes, or bounded framework source changes."""
    fix, originals = prepared["site_fix"], prepared["originals"]
    files = manifest["files"]
    if manifest.get("outcome") != "patch":
        raise ValueError("The technical result has no valid outcome.")
    paths = {item["path"] for item in files}
    if fix["mode"] == "framework":
        site_rules.verify_framework_change(files, originals, fix["new_paths"])
        if site_rules.FRAMEWORK_NOTE not in manifest.get("body", ""):
            raise ValueError("A framework repair PR must say Tin could not build the site.")
        return
    allowed = set(originals) | set(fix["new_paths"])
    if not paths <= allowed or not set(originals) <= paths or not paths:
        raise ValueError("The patch must address exactly the selected source files.")
    for item in files:
        site_rules.verify_static_change(
            fix["kind"],
            originals.get(item["path"]),
            item["content"],
            {**fix["expected"], "page_url": fix["pages"].get(item["path"])},
        )


SITE_REASONS = {
    "already_resolved": "The live site no longer shows this problem. No change proposed.",
    "unsupported_source": (
        "No file in the repository matches what the site serves, and Tin doesn't recognize "
        "the code that builds it. No change proposed."
    ),
    "site_unreadable": (
        "Tin couldn't read the live file or pages this finding is about. No change proposed."
    ),
    "no_sitemap_to_reference": (
        "The site has no readable sitemap for robots.txt to point to. No change proposed."
    ),
    "open_pr_overlap": "An existing PR touches the same files. No duplicate proposed.",
    "incomplete_pr_evidence": "Open-PR evidence is incomplete. No change proposed.",
    "no_safe_patch": "Codex could not prepare a safe change. The finding remains unresolved.",
}


def site_report(prepared, *, reason=None, pull_request=None):
    fix = prepared["site_fix"]
    finding = prepared["selection"]["finding"]
    lines = ["# Technical fix", "", "## Result", ""]
    if reason:
        lines += [SITE_REASONS[reason], ""]
    else:
        lines += [f"A pull request that {fix['change']} is ready for review.", ""]
    if pull_request:
        lines += [f"Pull request: {pull_request.url}", ""]
        if fix["mode"] == "framework":
            lines += [site_rules.FRAMEWORK_NOTE, ""]
        else:
            lines += [
                "Checked before delivery: the changed files differ from what the site serves "
                f"only in the change this finding calls for ({fix['change']}).",
                "",
            ]
    if reason == "open_pr_overlap":
        for existing in prepared.get("overlapping_pull_requests", []):
            lines += [f"Existing overlapping PR #{existing['number']}: {existing['url']}", ""]
    if fix.get("remaining"):
        lines += [
            "## Coverage",
            "",
            f"This change covers part of the finding. {fix['remaining']} more affected "
            f"{'URL is' if fix['remaining'] == 1 else 'URLs are'} left for a later repair.",
            "",
        ]
    source, binding = prepared["source"], prepared["repository_binding"]
    lines += [
        f"Audit: `{source['audit_run_id']}` at `{source['audit_revision']}`.",
        "",
        f"Finding: `{finding['id']}` (`{finding['check_id']}`).",
        "",
        f"Repository: `{binding['repository']}` at `{binding['head_sha']}`.",
        "",
        "How the change was chosen: "
        + (
            "the site serves these files byte for byte from the repository."
            if fix["mode"] == "static"
            else "Tin found the code that builds this part of the site and named the files "
            "the change may touch."
        ),
        "",
        "## Fresh observations",
        "",
    ]
    for row in fix["observations"]:
        state = "still needs the fix" if row["needed"] else "already fine"
        lines.append(
            f"- {row['url']} — HTTP {row['status_code']}, {state}, observed "
            f"{row['observed_at']}; SHA-256 `{row['sha256']}`."
        )
    lines += [
        "",
        "No merge, deployment, content publication or outreach was performed. A PR is not a "
        "deployed repair. After the PR merges, Tin checks the live site for this one finding.",
        "",
    ]
    return "\n".join(lines).encode()


def report(prepared, *, reason=None, pull_request=None):
    if prepared.get("batch"):
        return batch_rules.report(prepared, reason=reason, pull_request=pull_request)
    if prepared.get("site_fix"):
        return site_report(prepared, reason=reason, pull_request=pull_request)
    metadata = "title" if selected_check(prepared) == TITLE_CHECK else "meta description"
    labels = {
        "already_resolved": f"The selected pages now have a {metadata}. No change proposed.",
        "unsupported_source": "No supported source/build profile matched. No change proposed.",
        "open_pr_overlap": "An existing PR touches the matched source. No duplicate proposed.",
        "incomplete_pr_evidence": "Open-PR evidence is incomplete. No change proposed.",
        "no_safe_patch": "Codex could not prepare a safe patch. The finding remains unresolved.",
    }
    lines = [
        "# Technical fix",
        "",
        "## Result",
        "",
        labels[reason] if reason else f"A {metadata}-only pull request is ready for review.",
        "",
    ]
    if pull_request:
        lines.extend(
            [
                f"Pull request: {pull_request.url}",
                "",
                f"Verified before delivery: the pinned HTML was missing its {metadata}; "
                f"the proposed HTML has one nonempty {metadata} and no other content changes.",
                "",
            ]
        )
    if reason == "open_pr_overlap":
        for existing in prepared.get("overlapping_pull_requests", []):
            lines.extend([f"Existing overlapping PR #{existing['number']}: {existing['url']}", ""])
    unsupported = prepared.get("unsupported_pages", [])
    if unsupported:
        lines.extend(
            [
                "## Coverage",
                "",
                "This is a partial source match, not a repair of the whole finding.",
                "",
            ]
        )
        for observation in prepared.get("verification_profile", {}).get("observations", []):
            lines.append(f"- Supported source: {observation['url']} → `{observation['path']}`.")
        lines.extend(["", "Left untouched; these pages still need separate investigation:", ""])
        for page in unsupported:
            lines.append(f"- {page['url']} — {page['reason']}.")
        lines.append("")
    source, binding = prepared["source"], prepared["repository_binding"]
    profile = prepared.get("verification_profile", {}).get("kind")
    profile = profile or ("static-html-v1" if prepared.get("originals") else "unmatched")
    lines.extend(
        [
            f"Audit: `{source['audit_run_id']}` at `{source['audit_revision']}`.",
            "",
            f"Finding: `{prepared['selection']['finding']['id']}`.",
            "",
            f"Repository: `{binding['repository']}` at `{binding['head_sha']}`.",
            "",
            "Repository ownership was confirmed by the requesting member. Matching is "
            "based on the whole served HTML (with bounded public literal substitutions for "
            "supported templates), not guessed framework routes.",
            "",
            f"Verification profile: `{profile}`. "
            + (
                "Python syntax, offline wheel build and packaged HTML before/after checks passed. "
                "This is not a full application or browser test."
                if pull_request
                and prepared.get("verification_profile", {}).get("kind")
                == "hatchling-wheel-html-v1"
                else "No build or repair is claimed for a no-change result."
                if reason
                else "Static HTML before/after checks passed."
            ),
            "",
            "## Fresh observations",
            "",
        ]
    )
    for page in prepared["pages"]:
        field = "has_description" if metadata == "meta description" else "has_title"
        present = "present" if page.get(field, False) else "missing"
        lines.append(
            f"- {page['url']} — HTTP 200, {metadata} "
            f"{present}, "
            f"observed {page['observed_at']}; SHA-256 `{page['sha256']}`."
        )
    lines.extend(
        [
            "",
            "No merge, deployment, content publication, or outreach was performed. "
            "A PR is not a deployed repair. This check does not claim broader SEO improvements.",
            "",
        ]
    )
    return "\n".join(lines).encode()
