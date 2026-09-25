"""Versioned, metadata-only repair policies; no guessed routes or arbitrary builds."""

import asyncio
import hashlib
import io
import ipaddress
import socket
import tarfile
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

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
LEGACY_CHECK_COMMAND = "python3 /opt/tin-lite/verify-technical-title.py"
CHECK_COMMAND = (
    "/opt/tin-lite/metadata-venv/bin/python -I /opt/tin-lite/verify-technical-metadata.py"
)
POLICY_COMMANDS = {
    LEGACY_POLICY: LEGACY_CHECK_COMMAND,
    WHOLE_FINDING_POLICY: CHECK_COMMAND,
    POLICY: CHECK_COMMAND,
}


def supported_checks(policy):
    if policy == LEGACY_POLICY:
        return frozenset({TITLE_CHECK})
    if policy in {WHOLE_FINDING_POLICY, POLICY}:
        return SUPPORTED_CHECKS
    raise ValueError("Unsupported technical repair policy.")


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
    if manifest.get("outcome") != "patch" or {f["path"] for f in files} != set(
        prepared["originals"]
    ):
        raise ValueError("The patch must address exactly the selected source files.")
    for item in files:
        verify_metadata_change(
            prepared["originals"][item["path"]], item["content"], selected_check(prepared)
        )


def report(prepared, *, reason=None, pull_request=None):
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
