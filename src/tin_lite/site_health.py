from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from tin_lite.integrations import GitHubFileChange, GitHubRepositorySnapshot
from tin_lite.model_providers import (
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelRoute,
    ModelRouter,
    ProviderName,
    ReasoningEffort,
)

SITE_HEALTH_MODEL_ROUTE = ModelRoute(
    key="site-health-fix-v1",
    provider=ProviderName.OPENAI,
    model="gpt-6-luna",
    capabilities=frozenset(
        {
            ModelCapability.TEXT,
            ModelCapability.JSON_SCHEMA,
            ModelCapability.REASONING_EFFORT,
        }
    ),
)
MAX_SITE_RESPONSE_BYTES = 500_000
# The provider's wait for the proposal: 16,000 output tokens at high effort take minutes, not
# the client's 90-second default. The drafting activity heartbeats and allows 15 minutes.
MODEL_TIMEOUT_SECONDS = 300
MAX_SITE_CHANGE_BYTES = 512_000

_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 600},
        "diagnosis": {"type": "string", "minLength": 1, "maxLength": 4000},
        "pull_request_title": {"type": "string", "minLength": 1, "maxLength": 120},
        "pull_request_body": {"type": "string", "minLength": 1, "maxLength": 8000},
        "files": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 300},
                    "content": {"type": "string", "minLength": 1, "maxLength": 200000},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "required": ["path", "content", "reason"],
            },
        },
        "verification": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    },
    "required": [
        "summary",
        "diagnosis",
        "pull_request_title",
        "pull_request_body",
        "files",
        "verification",
    ],
}


class SiteHealthProtocolError(RuntimeError):
    """A model or public page returned data outside the bounded site-health contract."""


def site_health_model_route_definition() -> dict[str, Any]:
    return {
        "key": SITE_HEALTH_MODEL_ROUTE.key,
        "provider": SITE_HEALTH_MODEL_ROUTE.provider.value,
        "model": SITE_HEALTH_MODEL_ROUTE.model,
        "capabilities": sorted(item.value for item in SITE_HEALTH_MODEL_ROUTE.capabilities),
    }


def validate_site_health_model_route(definition: dict[str, Any]) -> str:
    if definition.get("model_route") != site_health_model_route_definition():
        raise SiteHealthProtocolError("pinned site-health model route is unavailable")
    return SITE_HEALTH_MODEL_ROUTE.key


@dataclass(frozen=True)
class LivePageEvidence:
    requested_url: str
    final_url: str
    status_code: int
    title: str
    description: str
    canonical: str
    html_language: str
    viewport: str
    h1s: tuple[str, ...]
    image_count: int
    images_missing_alt: int

    def definition(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SiteHealthProposal:
    summary: str
    diagnosis: str
    pull_request_title: str
    pull_request_body: str
    files: tuple[GitHubFileChange, ...]
    reasons: tuple[str, ...]
    verification: tuple[str, ...]
    provider: str
    model: str
    request_id: str | None
    usage: dict[str, int | None]

    def receipt(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "diagnosis": self.diagnosis,
            "pull_request_title": self.pull_request_title,
            "pull_request_body": self.pull_request_body,
            "files": [
                {"path": item.path, "content": item.content, "reason": reason}
                for item, reason in zip(self.files, self.reasons, strict=True)
            ],
            "verification": list(self.verification),
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "usage": self.usage,
        }

    @classmethod
    def from_receipt(cls, value: dict[str, Any]) -> SiteHealthProposal:
        files = value.get("files")
        verification = value.get("verification")
        usage = value.get("usage")
        if (
            not isinstance(files, list)
            or not files
            or any(not isinstance(item, dict) for item in files)
            or not isinstance(verification, list)
            or not verification
            or any(not isinstance(item, str) or not item.strip() for item in verification)
            or not isinstance(usage, dict)
        ):
            raise SiteHealthProtocolError("stored site-health proposal is invalid")
        return cls(
            summary=_required_string(value, "summary"),
            diagnosis=_required_string(value, "diagnosis"),
            pull_request_title=_required_string(value, "pull_request_title"),
            pull_request_body=_required_string(value, "pull_request_body"),
            files=tuple(
                GitHubFileChange(
                    path=_required_string(item, "path"),
                    content=_required_string(item, "content"),
                )
                for item in files
            ),
            reasons=tuple(_required_string(item, "reason") for item in files),
            verification=tuple(item.strip() for item in verification),
            provider=_required_string(value, "provider"),
            model=_required_string(value, "model"),
            request_id=value.get("request_id")
            if isinstance(value.get("request_id"), str)
            else None,
            usage={
                str(key): item if isinstance(item, int) else None for key, item in usage.items()
            },
        )


class SiteHealthImprover:
    def __init__(self, *, router: ModelRouter, skill_suite: str) -> None:
        self._router = router
        self._skill_suite = skill_suite

    async def draft(
        self,
        *,
        route_key: str,
        site_url: str,
        focus: str,
        context: str,
        change_budget: int,
        live_page: LivePageEvidence,
        snapshot: GitHubRepositorySnapshot,
    ) -> SiteHealthProposal:
        reference = {
            "site_url": site_url,
            "focus": focus,
            "founder_context": context,
            "change_budget": change_budget,
            "live_page": live_page.definition(),
            "repository": {
                "name": snapshot.repository,
                "default_branch": snapshot.default_branch,
                "head_sha": snapshot.head_sha,
                "files": [{"path": item.path, "content": item.content} for item in snapshot.files],
            },
        }
        result = await self._router.generate(
            route_key,
            ModelRequest(
                system=self._skill_suite,
                messages=(
                    ModelMessage(
                        role=MessageRole.USER,
                        content=json.dumps(reference, ensure_ascii=False, separators=(",", ":")),
                    ),
                ),
                max_output_tokens=16_000,
                reasoning_effort=ReasoningEffort.HIGH,
                output_schema=_PROPOSAL_SCHEMA,
                output_schema_name="site_health_fix",
            ),
            timeout_seconds=MODEL_TIMEOUT_SECONDS,
        )
        if not isinstance(result.parsed, dict):
            raise SiteHealthProtocolError("site-health model returned no structured proposal")
        proposal = _proposal_from_model(
            result.parsed,
            snapshot=snapshot,
            change_budget=change_budget,
            provider=result.provider.value,
            model=result.model,
            request_id=result.request_id,
            usage=asdict(result.usage),
        )
        return proposal


async def fetch_live_page_evidence(site_url: str) -> LivePageEvidence:
    current = _validated_public_url(site_url)
    requested = current
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for _hop in range(4):
            await _require_public_hostname(current)
            async with client.stream(
                "GET",
                current,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "Tin-Lite-Site-Health/1.0",
                },
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SiteHealthProtocolError("site redirect has no destination")
                    current = _validated_public_url(urljoin(current, location))
                    continue
                if response.status_code >= 400:
                    raise SiteHealthProtocolError(f"site returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "")
                if "html" not in content_type.casefold():
                    raise SiteHealthProtocolError("site URL did not return HTML")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_SITE_RESPONSE_BYTES:
                        raise SiteHealthProtocolError("site HTML exceeds the audit limit")
                    chunks.append(chunk)
                body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                parser = _SitePageParser()
                parser.feed(body)
                return LivePageEvidence(
                    requested_url=requested,
                    final_url=current,
                    status_code=response.status_code,
                    title=parser.title.strip()[:500],
                    description=parser.description.strip()[:1000],
                    canonical=parser.canonical.strip()[:1000],
                    html_language=parser.html_language.strip()[:100],
                    viewport=parser.viewport.strip()[:500],
                    h1s=tuple(value.strip()[:500] for value in parser.h1s if value.strip())[:10],
                    image_count=parser.image_count,
                    images_missing_alt=parser.images_missing_alt,
                )
    raise SiteHealthProtocolError("site redirected too many times")


def build_site_health_report(
    *,
    site_url: str,
    snapshot: GitHubRepositorySnapshot,
    proposal: SiteHealthProposal,
    pull_request_url: str,
    pull_request_number: int,
    pull_request_branch: str,
) -> bytes:
    changes = "\n".join(
        f"- `{item.path}` — {reason}"
        for item, reason in zip(proposal.files, proposal.reasons, strict=True)
    )
    checks = "\n".join(f"- {item}" for item in proposal.verification)
    sources = "\n".join(
        [
            f"- Live page: {site_url}",
            f"- Repository snapshot: `github://{snapshot.repository}@{snapshot.head_sha}`",
            f"- Pull request: {pull_request_url}",
        ]
    )
    report = f"""# Site-health improvement

{proposal.summary}

## Diagnosis

{proposal.diagnosis}

## Proposed changes

{changes}

## Verification for review

{checks}

## Pull request

[Review PR #{pull_request_number}]({pull_request_url}) from `{pull_request_branch}`.
Tin did not merge or deploy it.

## Sources

{sources}
"""
    encoded = report.encode()
    if not encoded or len(encoded) > 120_000:
        raise SiteHealthProtocolError("site-health report has an invalid size")
    return encoded


def _proposal_from_model(
    value: dict[str, Any],
    *,
    snapshot: GitHubRepositorySnapshot,
    change_budget: int,
    provider: str,
    model: str,
    request_id: str | None,
    usage: dict[str, int | None],
) -> SiteHealthProposal:
    raw_files = value.get("files")
    raw_verification = value.get("verification")
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= change_budget:
        raise SiteHealthProtocolError("site-health proposal exceeded its change budget")
    if (
        not isinstance(raw_verification, list)
        or not raw_verification
        or any(not isinstance(item, str) or not item.strip() for item in raw_verification)
    ):
        raise SiteHealthProtocolError("site-health proposal has no verification steps")
    source_by_path = {item.path: item.content for item in snapshot.files}
    files: list[GitHubFileChange] = []
    reasons: list[str] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in raw_files:
        if not isinstance(item, dict):
            raise SiteHealthProtocolError("site-health proposal has an invalid file change")
        path = _required_string(item, "path")
        content = _required_string(item, "content")
        reason = _required_string(item, "reason")
        if path not in source_by_path or path in seen:
            raise SiteHealthProtocolError(
                "site-health proposal may only modify supplied existing files"
            )
        if content == source_by_path[path]:
            raise SiteHealthProtocolError("site-health proposal did not change a selected file")
        encoded = content.encode()
        total_bytes += len(encoded)
        if len(encoded) > 200_000 or total_bytes > MAX_SITE_CHANGE_BYTES:
            raise SiteHealthProtocolError("site-health proposal exceeds the file-size limit")
        seen.add(path)
        files.append(GitHubFileChange(path=path, content=content))
        reasons.append(reason)
    return SiteHealthProposal(
        summary=_required_string(value, "summary"),
        diagnosis=_required_string(value, "diagnosis"),
        pull_request_title=_required_string(value, "pull_request_title"),
        pull_request_body=_required_string(value, "pull_request_body"),
        files=tuple(files),
        reasons=tuple(reasons),
        verification=tuple(item.strip() for item in raw_verification),
        provider=provider,
        model=model,
        request_id=request_id,
        usage=usage,
    )


def _validated_public_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 500:
        raise SiteHealthProtocolError("site URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SiteHealthProtocolError("site URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise SiteHealthProtocolError("site URL must be a public HTTPS URL")
    return parsed.geturl()


async def _require_public_hostname(url: str) -> None:
    hostname = urlsplit(url).hostname
    if hostname is None:
        raise SiteHealthProtocolError("site URL has no hostname")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise SiteHealthProtocolError("site hostname could not be resolved") from exc
    resolved = {item[4][0] for item in addresses}
    if not resolved:
        raise SiteHealthProtocolError("site hostname could not be resolved")
    for address in resolved:
        parsed = ipaddress.ip_address(address)
        if not parsed.is_global:
            raise SiteHealthProtocolError("site URL resolved to a non-public address")


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise SiteHealthProtocolError(f"site-health proposal has no {key}")
    return item.strip()


class _SitePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.canonical = ""
        self.html_language = ""
        self.viewport = ""
        self.h1s: list[str] = []
        self.image_count = 0
        self.images_missing_alt = 0
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "html":
            self.html_language = values.get("lang", self.html_language)
        elif lowered == "title":
            self._in_title = True
        elif lowered == "h1":
            self._in_h1 = True
            self.h1s.append("")
        elif lowered == "meta":
            name = values.get("name", "").casefold()
            if name == "description" and not self.description:
                self.description = values.get("content", "")
            elif name == "viewport" and not self.viewport:
                self.viewport = values.get("content", "")
        elif lowered == "link" and values.get("rel", "").casefold() == "canonical":
            self.canonical = values.get("href", self.canonical)
        elif lowered == "img":
            self.image_count += 1
            if "alt" not in values:
                self.images_missing_alt += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title":
            self._in_title = False
        elif lowered == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._in_h1 and self.h1s:
            self.h1s[-1] += data
