"""website.change's technical repair contract; never a merge or a deploy.

`site-fix-v5` repairs every fixable finding of one audit in one pull request, in any
framework, with judgment calls answered through MCP (technical_repair_plan, technical_batch).
"""

import asyncio
import hashlib
import ipaddress
import socket
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from tin_lite import technical_batch as batch_rules
from tin_lite import technical_repair_plan as repair_plan
from tin_lite import technical_site_rules as site_rules
from tin_lite.organic_audit import in_scope_url, public_site
from tin_lite.repository_limits import describe_omissions
from tin_lite.technical_metadata_rules import (
    DESCRIPTION_CHECK,
    has_metadata,
)
from tin_lite.technical_title_rules import (
    MAX_HTML_BYTES,
    has_title,
)

# The receipt operation for a repair's reads; existing receipts carry this name.
OPERATION = "organic.technical_fix"
BATCH_POLICY = repair_plan.POLICY
# The sandbox command each policy declares. site-fix-v5 declares none: served files are
# checked from the diff in the worker, and the sandbox image stays as it is.
POLICY_COMMANDS = {BATCH_POLICY: None}
# The most files a policy's pull request may change.
POLICY_MAX_FILES = {BATCH_POLICY: repair_plan.MAX_FILES}


def supported_checks(policy):
    if policy == BATCH_POLICY:
        return repair_plan.supported_checks()
    raise ValueError("Unsupported technical repair policy.")


def batches(policy):
    """Whether a policy repairs every fixable finding of an audit in one run."""
    return policy == BATCH_POLICY


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
    batch_rules.validate(manifest, prepared, None)


# Preparation outcomes where Tin couldn't do the job, so the run ends failed rather than
# succeeded with "no change".
FAILED_REASONS = frozenset({"repository_incomplete", "unsupported_source", "plan_too_large"})
# A failed run's reason names at most this many of the files Tin couldn't read.
MAX_NAMED_MISSING = 20


def preparation_failed(prepared):
    """Whether a run that stopped in preparation failed, rather than found nothing to change."""
    return prepared.get("reason") in FAILED_REASONS


def preparation_summary(prepared):
    """The run's one-line outcome; for a failed run, its error message."""
    reason = prepared.get("reason")
    if not preparation_failed(prepared):
        return "Technical finding checked. No change proposed."
    if reason == "repository_incomplete":
        named = describe_omissions(
            prepared.get("repository_missing") or [],
            limit=3,
            total=prepared.get("repository_missing_count"),
        )
        return (
            "Tin couldn't read every file in the repository"
            + (f": {named}" if named else "")
            + ". No change proposed."
        )[:900]
    return batch_rules.REASONS[reason]


def missing_record(bundle):
    """What a snapshot left out that a fix could need, for the preparation receipt: the
    first MAX_NAMED_MISSING files and how many there are. Empty when it is complete."""
    if getattr(bundle, "complete", True):
        return {}
    missing = [dict(item) for item in getattr(bundle, "missing", ())]
    return {
        "repository_missing": missing[:MAX_NAMED_MISSING],
        "repository_missing_count": len(missing),
    }


def report(prepared, *, reason=None, pull_request=None):
    return batch_rules.report(prepared, reason=reason, pull_request=pull_request)
