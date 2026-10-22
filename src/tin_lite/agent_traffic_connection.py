"""Analytics through Agent Traffic's edge service: the project's site, what Tin last read about
it, and the removals Tin still owes the service.

Agent Traffic records the visits to a founder's site through a script line and, on hosts that
can run it, a request reporter (the API contract: https://with.md/s/qkptksy_). Tin holds one
service credential per deployment (`TIN_LITE_AGENT_TRAFFIC_SERVICE_TOKEN`), which creates,
changes and removes a project's site and reads nothing, and one read token per site, sealed with
the connection, which reads only that site. Neither reaches a log, the browser, a workflow input
or an MCP response. See docs/agent-traffic-connection.md.

The connection's configuration holds what the service said about the site at the last PUT, and
changes only when Tin sends PUT again: a run fails when its connection's configuration changes
under it. The site's state (waiting, live, quiet) moves on its own, so Tin keeps it apart, in
`agent_traffic_sites`, with its stored reports in `agent_traffic_reports`. Removing a site is
queued in `agent_traffic_removals` and sent until the service accepts it, after the project's own
data is gone, because the founder's data there has to be deleted (`removal_loop`).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx

from tin_lite.connection_records import (
    ServiceArgumentError,
    integer,
    json_size,
    open_credential,
    read_bounded_json,
    record_call,
    seal_credential,
    text,
)
from tin_lite.integrations import (
    AGENT_TRAFFIC_CAPABILITIES,
    AGENT_TRAFFIC_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationError,
    IntegrationInputError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ServiceCallRefused,
    ServiceResponseTooLarge,
)

logger = logging.getLogger(__name__)

PROVIDER = AGENT_TRAFFIC_PROVIDER
# Cloudflare's zone protection in front of the service lets this user agent through.
USER_AGENT = "Tin-Project-API/1"
TIMEOUT_SECONDS = 10.0
# A week's report with its Markdown is the largest answer.
MAX_RESPONSE_BYTES = 4_000_000
MAX_ERROR_BYTES = 16_000
SITE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,59}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
STATES = frozenset({"waiting", "live", "quiet"})
# While no visit has arrived, opening the card or the page asks again at most this often.
WAITING_SECONDS = 30
# Otherwise Tin reads its stored copy, and asks again once it is older than this.
STALE_SECONDS = 600
# The reporter counts as connected while a request record arrived within this many days.
REPORTER_DAYS = 7
# The site's retention; older dates are refused.
RETENTION_DAYS = 180
# A removal waits longer after each failed attempt, up to this.
REMOVAL_MAX_WAIT_SECONDS = 6 * 3600
# Each pass sends at most this many removals; the service allows 60 admin calls a minute.
REMOVALS_PER_PASS = 20
REMOVAL_PASS_SECONDS = 60

NOT_SET_UP = "Analytics isn't set up on this deployment."
UNAVAILABLE = "Analytics is unavailable right now; try again in a few minutes."
ADDRESS = (
    "Enter your website's public address, such as https://www.example.com. Tin can't measure "
    "an IP address, localhost or an internal name."
)
NO_ADDRESS = "Enter your website's address to set up analytics."
STILL_REMOVING = (
    "Tin is still removing this project's earlier analytics site. Try again in a few minutes."
)
READ_LOST = (
    "Tin Analytics can't read this site anymore. Press Set up again on its card in Integrations."
)
# Hosts a founder's site lives on as one exact host among many. A host on one of them in
# `uncounted_hosts` is usually a preview deployment, not a move.
SHARED_HOSTING = (
    "vercel.app",
    "netlify.app",
    "pages.dev",
    "workers.dev",
    "github.io",
    "webflow.io",
    "framer.website",
    "framer.app",
    "web.app",
    "firebaseapp.com",
    "onrender.com",
    "fly.dev",
    "herokuapp.com",
    "railway.app",
    "amplifyapp.com",
    "azurestaticapps.net",
    "wixsite.com",
    "squarespace.com",
    "myshopify.com",
    "replit.app",
    "lovable.app",
)
INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")

LATEST_ADDRESS_SQL = """
    SELECT id, input->>'product_url' AS url FROM workflow_runs
    WHERE project_id = $1 AND executor = 'growth.onboarding' AND status <> 'superseded'
      AND coalesce(input->>'product_url', '') <> ''
    ORDER BY created_at DESC, id DESC LIMIT 1
"""


def ready(settings: Any) -> bool:
    return getattr(settings, "agent_traffic_service_token", None) is not None


def enabled(settings: Any, project_id: UUID | None) -> bool:
    if project_id is None:
        return False
    values = getattr(settings, "agent_traffic_projects", ())
    return str(project_id) in {str(value) for value in values}


def project_ref(settings: Any, project_id: UUID) -> str:
    """`tin:<project>` in production; staging and development use the staging credential."""

    production = getattr(settings, "deployment_environment", "production") == "production"
    return f"{'tin' if production else 'tin-staging'}:{project_id}"


def _base_url(settings: Any) -> str:
    return str(getattr(settings, "agent_traffic_url", "https://edge.tin.computer")).rstrip("/")


# ---------------------------------------------------------------- addresses


def site_address(value: Any) -> str:
    """The founder's website address as the service takes it, or IntegrationInputError.

    The service uses only the host; Tin refuses here what the contract refuses as
    `invalid_url`, so that answer always means a bug in Tin's call.
    """

    raw = value.strip() if isinstance(value, str) else ""
    if raw and "://" not in raw:
        raw = "https://" + raw
    if not raw or len(raw) > 2048 or any(ord(c) < 33 for c in raw):
        raise IntegrationInputError(ADDRESS)
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise IntegrationInputError(ADDRESS) from None
    try:
        # An internationalized name goes as its ASCII form, as browsers send it.
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        raise IntegrationInputError(ADDRESS) from None
    if parts.scheme not in {"http", "https"} or parts.username or parts.password or not host:
        raise IntegrationInputError(ADDRESS)
    if not _public_name(host):
        raise IntegrationInputError(ADDRESS)
    path = parts.path if parts.path not in {"", "/"} else ""
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}{path}"


def _public_name(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    return (
        "." in host
        and host != "localhost"
        and not host.endswith(INTERNAL_SUFFIXES)
        and re.fullmatch(r"[a-z0-9.-]{1,253}", host) is not None
    )


def host_of(url: Any) -> str:
    try:
        return (urlsplit(url).hostname or "").rstrip(".") if isinstance(url, str) else ""
    except ValueError:
        return ""


def domain(url: Any) -> str:
    """The site's domain as the service counts it: the host without a leading `www.`."""

    return host_of(url).removeprefix("www.")


def on_shared_hosting(host: str) -> bool:
    return any(host == item or host.endswith("." + item) for item in SHARED_HOSTING)


def moved_hosts(status: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Uncounted hosts that look like the site's real new address, not previews."""

    found = []
    for item in (status or {}).get("uncounted_hosts") or []:
        host = item.get("host") if isinstance(item, dict) else None
        if isinstance(host, str) and _public_name(host) and not on_shared_hosting(host):
            found.append({"host": host, "pages_7d": item.get("pages_7d")})
    return found


# ---------------------------------------------------------------- the service


class Answer(NamedTuple):
    status: int
    body: Any
    retry_after: int | None

    @property
    def code(self) -> str | None:
        value = self.body.get("error") if isinstance(self.body, dict) else None
        return value if isinstance(value, str) and ERROR_CODE.fullmatch(value) else None


class AnalyticsAccessLost(ServiceCallRefused):
    """A read was refused with 401 or 404: the connection needs the founder's Set up again."""

    def __init__(self, code: str) -> None:
        super().__init__(READ_LOST, code=code)


class AgentTraffic:
    """Fixed calls to Agent Traffic's service; each names its path from Tin's own values."""

    def __init__(self, client: httpx.AsyncClient, settings: Any) -> None:
        self._client, self._settings = client, settings

    def _service_token(self) -> str:
        token = getattr(self._settings, "agent_traffic_service_token", None)
        if token is None:
            raise IntegrationNotConfiguredError(NOT_SET_UP)
        return token.get_secret_value()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Answer:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        try:
            async with self._client.stream(
                method,
                _base_url(self._settings) + path,
                params=params,
                json=body,
                headers=headers,
                follow_redirects=False,
                timeout=TIMEOUT_SECONDS,
            ) as response:
                status = response.status_code
                success = 200 <= status < 300
                try:
                    payload = await read_bounded_json(
                        response,
                        maximum=MAX_RESPONSE_BYTES if success else MAX_ERROR_BYTES,
                        provider="Analytics",
                    )
                except (IntegrationUpstreamError, ServiceCallRefused):
                    if success:
                        raise IntegrationUpstreamError(UNAVAILABLE) from None
                    payload = None
                raw = response.headers.get("retry-after", "")
        except httpx.HTTPError:
            raise IntegrationUpstreamError(UNAVAILABLE) from None
        return Answer(status, payload, min(int(raw), 3600) if raw.isdigit() else None)

    @staticmethod
    def _site_path(ref: str) -> str:
        return f"/v1/admin/projects/{quote(ref, safe=':')}/site"

    def _refused(self, answer: Answer, action: str) -> IntegrationError:
        """What an admin call's error means for Tin; logs what only operators can fix."""

        if answer.status in {401, 403}:
            # Tin's own credential: nothing a founder can fix. 403 usually means the staging
            # credential met a `tin:` reference, or the other way round.
            logger.error(
                "Agent Traffic refused Tin's service credential (%s, HTTP %s %s)",
                action,
                answer.status,
                answer.code,
            )
            return IntegrationNotConfiguredError(NOT_SET_UP)
        if answer.status == 400:
            logger.error("Agent Traffic refused Tin's %s call as %s", action, answer.code)
            if answer.code == "invalid_url":
                return IntegrationInputError(ADDRESS)
            return IntegrationUpstreamError(UNAVAILABLE)
        if answer.status == 409 and answer.code == "staging_limit":
            return ServiceCallRefused(
                "Staging already has 20 analytics sites. Remove one, then set up again.",
                code="staging_limit",
            )
        if answer.status == 429:
            return IntegrationRateLimitedError(
                "Analytics is busy; try again in a minute.", retry_after=answer.retry_after
            )
        return IntegrationUpstreamError(UNAVAILABLE)

    async def put_site(self, ref: str, url: str) -> dict[str, Any]:
        answer = await self._call(
            "PUT", self._site_path(ref), token=self._service_token(), body={"url": url}
        )
        if answer.status not in {200, 201}:
            raise self._refused(answer, "PUT site")
        if not isinstance(answer.body, dict):
            raise IntegrationUpstreamError(UNAVAILABLE)
        return answer.body

    async def delete_site(self, ref: str) -> Answer:
        return await self._call("DELETE", self._site_path(ref), token=self._service_token())

    async def status(self, site_id: str, token: str, tz: str) -> Answer:
        return await self._call(
            "GET", f"/v1/sites/{_site(site_id)}/status", token=token, params={"tz": tz}
        )

    async def report(self, site_id: str, token: str, *, period: str, day: date, tz: str) -> Answer:
        return await self._call(
            "GET",
            f"/v1/sites/{_site(site_id)}/report",
            token=token,
            params={"period": period, "date": day.isoformat(), "tz": tz},
        )


def _site(site_id: Any) -> str:
    if not isinstance(site_id, str) or not SITE_ID.fullmatch(site_id):
        raise IntegrationUpstreamError("Analytics returned an invalid site")
    return site_id


# ---------------------------------------------------------------- what Tin keeps


def _strings(value: Any, *, limit: int, length: int = 255) -> list[str]:
    items = value if isinstance(value, list) else []
    found = (text(item, length) for item in items[:limit])
    return [item for item in found if item]


def _csp(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        directive: _strings(sources, limit=10, length=500)
        for directive, sources in list(value.items())[:10]
        if isinstance(directive, str) and re.fullmatch(r"[a-z-]{1,40}", directive)
    }


def _variants(value: Any) -> list[dict[str, str]]:
    """Reporter files that are ready; a variant Tin can't verify is left out."""

    found = []
    for item in value[:10] if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        url, sha = item.get("url"), item.get("sha256")
        variant, version = text(item.get("variant"), 40), text(item.get("version"), 20)
        if (
            variant
            and version
            and isinstance(url, str)
            and url.startswith("https://")
            and len(url) <= 500
            and isinstance(sha, str)
            and SHA256.fullmatch(sha)
        ):
            found.append({"variant": variant, "version": version, "url": url, "sha256": sha})
    return found


def site_from(payload: dict[str, Any], ref: str) -> tuple[dict[str, Any], str, str | None]:
    """The connection's configuration, the new read token and the state, from a PUT answer."""

    site_id = _site(payload.get("site_id"))
    if payload.get("project_ref") != ref:
        raise IntegrationUpstreamError("Analytics answered for another project")
    token = payload.get("read_token")
    if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{16,512}", token):
        raise IntegrationUpstreamError("Analytics returned no usable read token")
    script = payload.get("script")
    if not isinstance(script, str) or not script.startswith("<script") or len(script) > 2000:
        raise IntegrationUpstreamError("Analytics returned no script line")
    reporter = payload.get("reporter") if isinstance(payload.get("reporter"), dict) else {}
    state = payload.get("state")
    configuration = {
        "site_id": site_id,
        "project_ref": ref,
        "url": text(payload.get("url"), 2048),
        "linked": payload.get("linked") is True,
        "counted_hosts": _strings(payload.get("counted_hosts"), limit=50),
        "script": script,
        "csp": _csp(payload.get("csp")),
        # Only what changes with a PUT: whether a record arrived lives with the status.
        "reporter": {
            "env_name": text(reporter.get("env_name"), 100),
            "variants": _variants(reporter.get("variants")),
        },
        "granted_capabilities": list(AGENT_TRAFFIC_CAPABILITIES),
    }
    return configuration, token, state if state in STATES else None


def _moment(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _hosts(value: Any) -> list[dict[str, Any]]:
    found = []
    for item in value[:50] if isinstance(value, list) else []:
        host = text(item.get("host"), 255) if isinstance(item, dict) else None
        if host:
            found.append({"host": host.lower(), "pages_7d": integer(item.get("pages_7d"))})
    return found


def status_from(payload: Any, site_id: str) -> dict[str, Any]:
    """The status call's answer as Tin keeps it; unknown fields are left out."""

    if not isinstance(payload, dict) or payload.get("site_id") != site_id:
        raise IntegrationUpstreamError("Analytics answered for another site")
    state = payload.get("state")
    return {
        "site_id": site_id,
        "state": state if state in STATES else None,
        "first_page_at": _moment(payload.get("first_page_at")),
        "last_page_at": _moment(payload.get("last_page_at")),
        "pages_today": integer(payload.get("pages_today")),
        "hosts_seen": _hosts(payload.get("hosts_seen")),
        "uncounted_hosts": _hosts(payload.get("uncounted_hosts")),
        "requests_last_at": _moment(payload.get("requests_last_at")),
        "checked_at": _moment(payload.get("checked_at")),
    }


def report_from(payload: Any, site_id: str, period: str) -> dict[str, Any]:
    """The report record, as the service wrote it. Tin passes it through whole: the service
    adds fields within a contract version, and every reader shows what it knows."""

    if (
        not isinstance(payload, dict)
        or payload.get("site_id") != site_id
        or payload.get("period") != period
    ):
        raise IntegrationUpstreamError("Analytics returned another report")
    return payload


# The page's tabs: Today and Yesterday read a day, the last 7 days a week ending today.
PAGE_VIEWS = {"today": ("day", 0), "yesterday": ("day", 1), "week": ("week", 0)}

# The numbers the report's `previous` repeats for the period before, and where each is now.
COMPARED = (
    ("visits", ("totals", "visits")),
    ("page_views", ("totals", "page_views")),
    ("assistant_visits", ("totals", "assistant_visits")),
    ("no_automation_seen", ("kinds", "no_automation_seen", "visits")),
    ("automated_browser", ("kinds", "automated_browser", "visits")),
    ("declared_crawler", ("kinds", "declared_crawler", "visits")),
)
# Below this many before, a percentage overstates a small change ("+300%" from 1 to 4); the
# page says "up from 1" instead.
PERCENT_FROM = 10


def comparison(report: dict[str, Any]) -> dict[str, Any] | None:
    """Each number's change from the period before, from the report's `previous`.

    None when there is no period before to compare with (`previous` is null past retention).
    A number withheld in either period (null) has no change. `percent` is given only when the
    period before had at least PERCENT_FROM; `before` is 0 for days before the first visit.
    While the period is unfinished, `previous` stops at the same point and `until` says where
    ("against Tuesday up to 10:50"); `until` is None when it covers the whole period before.
    """

    previous = report.get("previous")
    if not isinstance(previous, dict):
        return None
    changes = {}
    for name, path in COMPARED:
        now: Any = report
        for key in path:
            now = now.get(key) if isinstance(now, dict) else None
        now, before = integer(now), integer(previous.get(name))
        if now is None or before is None or now < 0 or before < 0:
            continue
        change: dict[str, Any] = {"now": now, "before": before}
        if before >= PERCENT_FROM:
            change["percent"] = round((now - before) * 100 / before)
        changes[name] = change
    return {
        "start": text(previous.get("start"), 10),
        "end": text(previous.get("end"), 10),
        "until": _moment(previous.get("until")),
        "changes": changes,
    }


def reporter_connected(status: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    moment = _moment((status or {}).get("requests_last_at"))
    if moment is None:
        return False
    when = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    return (now or datetime.now(UTC)) - when <= timedelta(days=REPORTER_DAYS)


def _label(configuration: dict[str, Any]) -> str:
    host = host_of(configuration.get("url")) or configuration["site_id"]
    return host[:200]


def _record(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    for key in ("status", "report"):
        if isinstance(value.get(key), str):
            value[key] = json.loads(value[key])
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def removal_wait(attempts: int, retry_after: int | None = None) -> int:
    """Seconds before the next DELETE: one minute, doubling, up to six hours."""

    wait = min(60 * 2 ** min(max(attempts - 1, 0), 12), REMOVAL_MAX_WAIT_SECONDS)
    return max(wait, retry_after or 0)


# ---------------------------------------------------------------- workflow reads


@dataclass(frozen=True)
class Operation:
    capability: str
    arguments: frozenset[str]


# The reviewed operation table; the gateway maps (provider, operation) through it explicitly.
OPERATIONS: dict[str, Operation] = {
    "site.status": Operation("status.read", frozenset()),
    "report.read": Operation("reports.read", frozenset({"period", "date"})),
}
assert {op.capability for op in OPERATIONS.values()} == set(AGENT_TRAFFIC_CAPABILITIES)


def report_date(value: Any, *, today: date | None = None) -> date | None:
    """A YYYY-MM-DD date within the site's retention, or None for today."""

    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ServiceArgumentError("date must be a YYYY-MM-DD date")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise ServiceArgumentError("date must be a YYYY-MM-DD date") from None
    today = today or datetime.now(UTC).date()
    # A day ahead or behind UTC is still today somewhere.
    if not today - timedelta(days=RETENTION_DAYS) <= day <= today + timedelta(days=1):
        raise ServiceArgumentError(f"date must be within the last {RETENTION_DAYS} days")
    return day


def request_for(operation: str, arguments: Any) -> tuple[Operation, dict[str, Any]]:
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise ServiceArgumentError("unknown Analytics operation")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ServiceArgumentError("arguments must be an object")
    unknown = sorted(set(arguments) - spec.arguments)
    if unknown:
        raise ServiceArgumentError(f"unsupported argument {unknown[0]}")
    if operation == "site.status":
        return spec, {}
    period = arguments.get("period", "day")
    if period not in {"day", "week"}:
        raise ServiceArgumentError("period must be day or week")
    return spec, {"period": period, "date": report_date(arguments.get("date"))}


def check_arguments(operation: str, arguments: Any) -> None:
    """Gateway hook: reject a malformed call before any receipt or service request exists."""

    request_for(operation, arguments)


# ---------------------------------------------------------------- the connection


class AgentTrafficConnections:
    def __init__(self, integrations: Any) -> None:
        self.integrations = integrations
        self.db = integrations._database
        self.settings = integrations._settings

    @property
    def service(self) -> AgentTraffic:
        return AgentTraffic(self.integrations._client, self.settings)

    def _require_enabled(self, project_id: UUID) -> None:
        if not enabled(self.settings, project_id):
            raise IntegrationNotConfiguredError("Analytics is not available for this project yet")
        if not self.integrations.is_configured(PROVIDER):
            raise IntegrationNotConfiguredError(NOT_SET_UP)

    async def address(self, project_id: UUID) -> tuple[str, UUID] | None:
        """The website address from the project's latest Start here, and that run."""

        row = await self.db.pool.fetchrow(LATEST_ADDRESS_SQL, project_id)
        if row is None:
            return None
        try:
            return site_address(row["url"]), row["id"]
        except IntegrationInputError:
            return None

    async def site(self, project_id: UUID) -> dict[str, Any] | None:
        return _record(
            await self.db.pool.fetchrow(
                "SELECT * FROM agent_traffic_sites WHERE project_id = $1", project_id
            )
        )

    async def removal(self, project_id: UUID) -> dict[str, Any] | None:
        return _record(
            await self.db.pool.fetchrow(
                "SELECT * FROM agent_traffic_removals WHERE project_ref = $1",
                project_ref(self.settings, project_id),
            )
        )

    # ----- set up and address changes

    async def set_up(self, *, project_id: UUID, clerk_user_id: str, url: str | None = None):
        """Create the project's site, or update it, from Start here's address or `url`."""

        self._require_enabled(project_id)
        latest = await self.address(project_id)
        if url is not None:
            address, source = site_address(url), "entered"
        elif latest is not None:
            address, source = latest[0], "setup"
        else:
            raise IntegrationInputError(NO_ADDRESS)
        return await self._put(
            project_id,
            clerk_user_id,
            address=address,
            source=source,
            run_id=latest[1] if latest else None,
        )

    async def count_host(self, *, project_id: UUID, clerk_user_id: str, host: str):
        """Count the host the script moved to instead, from the card's offer."""

        self._require_enabled(project_id)
        site = await self.site(project_id)
        offered = {item["host"] for item in moved_hosts((site or {}).get("status"))}
        if not isinstance(host, str) or host.lower() not in offered:
            raise IntegrationInputError(
                "Tin can count only an address the script ran on in the last 7 days."
            )
        latest = await self.address(project_id)
        return await self._put(
            project_id,
            clerk_user_id,
            address=site_address(host.lower()),
            source="uncounted_host",
            run_id=latest[1] if latest else None,
            event="integration_configured",
        )

    async def _put(
        self,
        project_id: UUID,
        clerk_user_id: str,
        *,
        address: str,
        source: str,
        run_id: UUID | None,
        event: str = "integration_connected",
    ):
        ref = project_ref(self.settings, project_id)
        async with self.db.integration_refresh_lock(project_id=project_id, provider_key=PROVIDER):
            # DELETE removes the project's site by its reference, so an earlier removal the
            # service hasn't accepted yet would take the new site with it.
            if not await self._settle_removal(ref):
                raise IntegrationUpstreamError(STILL_REMOVING)
            payload = await self.service.put_site(ref, address)
            configuration, token, state = site_from(payload, ref)
            ciphertext, version = seal_credential(
                self.integrations, project_id=project_id, provider_key=PROVIDER, value=token
            )
            # One statement stores the token with the rest of the connection. The row keeps
            # its id, so when the site's facts are unchanged a run holding the connection keeps
            # going with the new token; the old one works for ten minutes meanwhile.
            connection = await self.db.upsert_integration_connection(
                project_id=project_id,
                provider_key=PROVIDER,
                external_account_id=configuration["site_id"],
                external_account_label=_label(configuration),
                configuration=configuration,
                credential_ciphertext=ciphertext,
                credential_key_version=version,
                connected_by_clerk_user_id=clerk_user_id,
            )
            await self._save_site(
                project_id, configuration["site_id"], state, source=source, run_id=run_id
            )
        summary = (
            f"Analytics set up for {_label(configuration)}."
            if event == "integration_connected"
            else f"Analytics now counts {_label(configuration)}."
        )
        await self.integrations._record_activity(connection, event, summary, suffix=str(uuid4()))
        return connection

    async def _save_site(
        self,
        project_id: UUID,
        site_id: str,
        state: str | None,
        *,
        source: str,
        run_id: UUID | None,
    ) -> None:
        initial = json.dumps({"site_id": site_id, "state": state})
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO agent_traffic_sites (project_id, site_id, address_source,
                    address_run_id, status)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (project_id) DO UPDATE SET
                    address_source = EXCLUDED.address_source,
                    address_run_id = EXCLUDED.address_run_id,
                    -- The same site keeps what Tin read; a new one starts from the PUT's state.
                    status = CASE WHEN agent_traffic_sites.site_id = EXCLUDED.site_id
                        THEN coalesce(agent_traffic_sites.status, '{}'::jsonb)
                             || jsonb_strip_nulls(EXCLUDED.status)
                        ELSE EXCLUDED.status END,
                    status_checked_at = CASE WHEN agent_traffic_sites.site_id = EXCLUDED.site_id
                        THEN agent_traffic_sites.status_checked_at END,
                    site_id = EXCLUDED.site_id,
                    status_claimed_at = NULL, status_retry_at = NULL, status_failures = 0,
                    updated_at = now()
                """,
                project_id,
                site_id,
                source,
                run_id,
                initial,
            )
            await conn.execute(
                "DELETE FROM agent_traffic_reports WHERE project_id = $1 AND site_id <> $2",
                project_id,
                site_id,
            )

    async def _sync_address(self, connection: Any, site: dict[str, Any]):
        """Send PUT again when a newer Start here names another host than the site's."""

        latest = await self.address(connection.project_id)
        if latest is None or latest[1] == site.get("address_run_id"):
            return connection
        url, run_id = latest
        if domain(url) == domain(connection.configuration.get("url")):
            await self.db.pool.execute(
                "UPDATE agent_traffic_sites SET address_run_id = $2, updated_at = now() "
                "WHERE project_id = $1",
                connection.project_id,
                run_id,
            )
            return connection
        return await self._put(
            connection.project_id,
            connection.connected_by_clerk_user_id,
            address=url,
            source="setup",
            run_id=run_id,
            event="integration_configured",
        )

    # ----- the card

    async def card(self, project_id: UUID, connection: Any | None) -> dict[str, Any]:
        """What the Tin Analytics card shows. Opening it asks the service again when Tin's stored
        status is due (contract: The card); a failure keeps the stored copy."""

        if not self.integrations.is_configured(PROVIDER):
            return {"state": "unavailable", "message": NOT_SET_UP}
        if connection is None:
            removal = await self.removal(project_id)
            latest = await self.address(project_id)
            view: dict[str, Any] = {
                "state": "not_set_up",
                "address": latest[0] if latest else None,
            }
            if removal is not None:
                view["removal"] = {
                    "state": removal["state"],
                    "requested_at": _iso(removal["requested_at"]),
                    "deleted_by": _iso(removal["deleted_by"]),
                }
            return view
        site = await self.site(project_id)
        if site is None:
            # Stopped between storing the connection and its status row: start the row now.
            await self._save_site(
                project_id, connection.external_account_id, None, source="setup", run_id=None
            )
            site = await self.site(project_id)
        if connection.status == "connected":
            try:
                connection, site = await self.refresh(connection, site)
            except IntegrationError as exc:
                if isinstance(exc, IntegrationNotConfiguredError):
                    return {"state": "unavailable", "message": NOT_SET_UP}
                # A refused read may have marked the connection; the rest keeps the stored copy.
                connection = (
                    await self.db.get_integration_connection(
                        project_id=project_id, provider_key=PROVIDER
                    )
                    or connection
                )
                site = await self.site(project_id)
        status = (site or {}).get("status") or {}
        state = "needs_attention" if connection.status != "connected" else status.get("state")
        configuration = connection.configuration
        return {
            "state": state or "waiting",
            "address": configuration.get("url"),
            "site_id": configuration.get("site_id"),
            "linked": configuration.get("linked") is True,
            "counted_hosts": configuration.get("counted_hosts") or [],
            "address_source": (site or {}).get("address_source"),
            # The line on the founder's site and its CSP entries; neither is a credential.
            "script": configuration.get("script"),
            "csp": configuration.get("csp") or {},
            "pages_today": status.get("pages_today"),
            "first_page_at": status.get("first_page_at"),
            "last_page_at": status.get("last_page_at"),
            "hosts_seen": status.get("hosts_seen") or [],
            "requests_last_at": status.get("requests_last_at"),
            "reporter_connected": reporter_connected(status),
            "moved_hosts": moved_hosts(status),
            "checked_at": _iso((site or {}).get("status_checked_at")),
            # Tin couldn't ask just now (rate limit, outage) and shows its stored copy.
            "stale": bool(
                site and site.get("status_retry_at") and site["status_retry_at"] > datetime.now(UTC)
            ),
        }

    async def refresh(self, connection: Any, site: dict[str, Any]):
        """Ask for the site's status when it is due; returns the connection and site after."""

        claimed = _record(
            await self.db.pool.fetchrow(
                """
                UPDATE agent_traffic_sites SET status_claimed_at = now()
                WHERE project_id = $1
                  AND (status_claimed_at IS NULL
                       OR status_claimed_at <= now() - make_interval(secs => $2))
                  AND (status_retry_at IS NULL OR status_retry_at <= now())
                  AND (status_checked_at IS NULL OR status_checked_at <= now() - make_interval(
                        secs => CASE WHEN coalesce(status->>'state', 'waiting') = 'waiting'
                                     THEN $2 ELSE $3 END))
                RETURNING *
                """,
                connection.project_id,
                WAITING_SECONDS,
                STALE_SECONDS,
            )
        )
        if claimed is None:
            return connection, site
        connection = await self._sync_address(connection, claimed)
        site_id = connection.external_account_id
        token = open_credential(self.integrations, connection)
        tz = await self.timezone(connection.project_id)
        try:
            answer = await self.service.status(site_id, token, tz)
        except IntegrationUpstreamError:
            await self._status_failed(connection.project_id)
            raise
        if answer.status != 200:
            raise await self._read_refused(connection, answer, "status")
        status = status_from(answer.body, site_id)
        await self.db.pool.execute(
            """UPDATE agent_traffic_sites SET status = $3::jsonb, status_checked_at = now(),
               status_failures = 0, status_retry_at = NULL, updated_at = now()
               WHERE project_id = $1 AND site_id = $2""",
            connection.project_id,
            site_id,
            json.dumps(status),
        )
        return connection, await self.site(connection.project_id)

    async def _status_failed(self, project_id: UUID, retry_after: int | None = None) -> None:
        # One minute, doubling with each failure in a row, up to an hour; or the service's own
        # retry-after.
        await self.db.pool.execute(
            """UPDATE agent_traffic_sites SET status_failures = status_failures + 1,
               status_retry_at = now() + make_interval(secs => greatest(
                   least(60 * power(2, least(status_failures, 6)), 3600), $2::int)),
               updated_at = now()
               WHERE project_id = $1""",
            project_id,
            retry_after or 0,
        )

    async def _read_refused(self, connection: Any, answer: Answer, read: str) -> IntegrationError:
        """What a refused read means, after marking the connection or backing off."""

        if answer.status in {401, 404}:
            # The site's read token or the site itself is gone without Tin asking to stop.
            code = "authentication_failed" if answer.status == 401 else "site_not_found"
            await self.db.mark_integration_attention(
                project_id=connection.project_id, provider_key=PROVIDER, error_code=code
            )
            return AnalyticsAccessLost(code)
        if answer.status == 429:
            await self._status_failed(connection.project_id, answer.retry_after)
            return IntegrationRateLimitedError(
                "Analytics is rate limiting reads of this site; try again shortly.",
                retry_after=answer.retry_after,
            )
        if answer.status == 400:
            # Tin checks every argument first, so this is a bug in Tin's call.
            logger.error("Agent Traffic refused Tin's %s read as %s", read, answer.code)
            return ServiceCallRefused(
                f"Analytics refused the {read} read ({answer.code or 'invalid_request'}).",
                code=answer.code or "invalid_request",
            )
        await self._status_failed(connection.project_id)
        return ServiceCallRefused(
            f"Analytics could not complete the {read} read (HTTP {answer.status}); "
            "try again later.",
            code="provider_error",
        )

    async def timezone(self, project_id: UUID) -> str:
        from tin_lite.organic_system_activities import founder_timezone

        return await founder_timezone(self.db, project_id)

    # ----- the page

    async def page(self, project_id: UUID, view: str) -> dict[str, Any]:
        """The Analytics page for one tab: the card's state (which asks for status when due) and,
        once a visit has arrived, Tin's stored report for Today, Yesterday or the last 7 days."""

        if view not in PAGE_VIEWS:
            raise IntegrationInputError("Choose today, yesterday or week")
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=PROVIDER
        )
        card = await self.card(project_id, connection)
        tz = await self.timezone(project_id)
        result: dict[str, Any] = {"card": card, "view": view, "tz": tz, "report": None}
        if connection is None or card["state"] in {"unavailable", "not_set_up", "waiting"}:
            return result
        period, back = PAGE_VIEWS[view]
        day = datetime.now(ZoneInfo(tz)).date() - timedelta(days=back)
        try:
            stored = await self.report(project_id, period=period, day=day)
        except IntegrationError as exc:
            # A first read that failed has no stored copy to fall back on.
            result["error"] = getattr(exc, "code", None) or "unavailable"
            return result
        result.update(stored or {})
        result["date"] = day.isoformat()
        return result

    # ----- reports

    async def report(
        self, project_id: UUID, *, period: str, day: date | None = None
    ) -> dict[str, Any] | None:
        """Tin's stored report for the period, asked for again once it is more than ten minutes
        old and not complete. Returns `{report, fetched_at, stale, comparison}`, or None before
        any read."""

        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=PROVIDER
        )
        if connection is None:
            raise IntegrationAuthorizationError("Set up Analytics first")
        tz = await self.timezone(project_id)
        day = day or datetime.now(ZoneInfo(tz)).date()
        site_id = connection.external_account_id
        key = (project_id, site_id, period, day, tz)
        stored = _record(
            await self.db.pool.fetchrow(
                """SELECT report, fetched_at FROM agent_traffic_reports WHERE project_id = $1
                   AND site_id = $2 AND period = $3 AND report_date = $4 AND tz = $5""",
                *key,
            )
        )
        fresh = stored is not None and (
            stored["report"].get("complete") is True
            or stored["fetched_at"] > datetime.now(UTC) - timedelta(seconds=STALE_SECONDS)
        )
        if fresh or connection.status != "connected":
            return _stored(stored)
        try:
            answer = await self.service.report(
                site_id,
                open_credential(self.integrations, connection),
                period=period,
                day=day,
                tz=tz,
            )
            if answer.status != 200:
                raise await self._read_refused(connection, answer, "report")
            report = report_from(answer.body, site_id, period)
        except IntegrationError:
            if stored is None:
                raise
            return {**_stored(stored), "stale": True}
        await self.db.pool.execute(
            """INSERT INTO agent_traffic_reports (project_id, site_id, period, report_date, tz,
                   report, fetched_at)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, now())
               ON CONFLICT (project_id, site_id, period, report_date, tz)
               DO UPDATE SET report = EXCLUDED.report, fetched_at = now()""",
            *key,
            json.dumps(report),
        )
        return {
            "report": report,
            "fetched_at": datetime.now(UTC).isoformat(),
            "stale": False,
            "comparison": comparison(report),
        }

    # ----- workflow reads

    async def call(
        self,
        operation: str,
        arguments: Any,
        *,
        connection: Any,
        run_id: UUID | None,
        execution_key: str,
        max_response_bytes: int | None,
    ) -> dict[str, Any]:
        spec, params = request_for(operation, arguments)
        granted = connection.configuration.get("granted_capabilities") or []
        if spec.capability not in granted:
            raise IntegrationAuthorizationError("Analytics does not allow this read")
        token = open_credential(self.integrations, connection)
        site_id = connection.external_account_id
        tz = await self.timezone(connection.project_id)
        if operation == "report.read" and params["date"] is None:
            params["date"] = datetime.now(ZoneInfo(tz)).date()
        fingerprint = hashlib.sha256(
            json.dumps(
                {"site": site_id, "operation": operation, "params": params, "tz": tz},
                default=str,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        receipt = {
            "connection": connection,
            "capability": spec.capability,
            "fingerprint": fingerprint,
            "execution_key": execution_key,
            "run_id": run_id,
        }
        try:
            if operation == "site.status":
                answer = await self.service.status(site_id, token, tz)
                if answer.status != 200:
                    raise await self._read_refused(connection, answer, "status")
                result = status_from(answer.body, site_id)
                summary = {"state": result["state"]}
            else:
                answer = await self.service.report(
                    site_id, token, period=params["period"], day=params["date"], tz=tz
                )
                if answer.status != 200:
                    raise await self._read_refused(connection, answer, "report")
                result = report_from(answer.body, site_id, params["period"])
                summary = {"period": params["period"], "complete": result.get("complete")}
        except IntegrationError as exc:
            await record_call(
                self.db,
                **receipt,
                status="failed",
                error_code=getattr(exc, "code", "provider_request_failed"),
            )
            raise
        if max_response_bytes is not None and json_size(result) > max_response_bytes:
            # The service answered; the outcome is known even though it doesn't fit the bound.
            await record_call(
                self.db,
                **receipt,
                status="completed",
                response_summary={**summary, "error": "record_too_large"},
            )
            raise ServiceResponseTooLarge("the report is larger than the binding allows")
        await record_call(self.db, **receipt, status="completed", response_summary=summary)
        return result

    # ----- stopping

    async def stop(self, connection: Any) -> None:
        """Stop measuring: queue the site's removal durably, forget Tin's copies, and send the
        first DELETE now. The caller deletes the connection and its read token."""

        ref = connection.configuration.get("project_ref") or project_ref(
            self.settings, connection.project_id
        )
        await self.db.pool.execute(
            """
            INSERT INTO agent_traffic_removals (project_ref, project_id, site_id, state)
            VALUES ($1, $2, $3, 'pending')
            ON CONFLICT (project_ref) DO UPDATE SET
                site_id = EXCLUDED.site_id, state = 'pending', requested_at = now(),
                attempts = 0, next_attempt_at = now(), last_error = NULL, deleted_by = NULL,
                finished_at = NULL
            """,
            ref,
            connection.project_id,
            connection.external_account_id,
        )
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM agent_traffic_reports WHERE project_id = $1", connection.project_id
            )
            await conn.execute(
                "DELETE FROM agent_traffic_sites WHERE project_id = $1", connection.project_id
            )
        await self._settle_removal(ref)

    async def _settle_removal(self, ref: str) -> bool:
        """Send the reference's pending removal now; True once none is left waiting."""

        row = _record(
            await self.db.pool.fetchrow(
                """UPDATE agent_traffic_removals SET attempts = attempts + 1,
                   next_attempt_at = now() + interval '5 minutes'
                   WHERE project_ref = $1 AND finished_at IS NULL RETURNING *""",
                ref,
            )
        )
        if row is None:
            return True
        return await self._send_removal(row)

    async def process_removals(self, *, limit: int = REMOVALS_PER_PASS) -> int:
        """Send the removals that are due; returns how many the service accepted."""

        if not ready(self.settings):
            return 0
        # Claimed by pushing their next attempt out, so another process skips them meanwhile.
        rows = await self.db.pool.fetch(
            """
            UPDATE agent_traffic_removals SET attempts = attempts + 1,
                next_attempt_at = now() + interval '5 minutes'
            WHERE project_ref IN (
                SELECT project_ref FROM agent_traffic_removals
                WHERE finished_at IS NULL AND next_attempt_at <= now()
                ORDER BY next_attempt_at LIMIT $1 FOR UPDATE SKIP LOCKED)
            RETURNING *
            """,
            limit,
        )
        accepted = 0
        for row in rows:
            accepted += await self._send_removal(_record(row))
        return accepted

    async def _send_removal(self, row: dict[str, Any]) -> bool:
        ref = row["project_ref"]
        try:
            answer = await self.service.delete_site(ref)
        except IntegrationError as exc:
            await self._removal_waits(row, type(exc).__name__)
            return False
        body = answer.body if isinstance(answer.body, dict) else {}
        if answer.status in {200, 202, 404}:
            state = (
                "not_found"
                if answer.status == 404
                else "removing"
                if answer.status == 202
                else body.get("state")
                if body.get("state") in {"removed", "unlinked"}
                else "removed"
            )
            deleted_by = _moment(body.get("deleted_by")) or _moment(body.get("deleted_at"))
            await self.db.pool.execute(
                """UPDATE agent_traffic_removals SET state = $2, finished_at = now(),
                   deleted_by = $3::timestamptz, last_error = NULL WHERE project_ref = $1""",
                ref,
                state,
                datetime.fromisoformat(deleted_by.replace("Z", "+00:00")) if deleted_by else None,
            )
            return True
        if answer.status in {400, 401, 403}:
            logger.error(
                "Agent Traffic refused Tin's DELETE for %s (HTTP %s %s); Tin keeps retrying",
                ref,
                answer.status,
                answer.code,
            )
        else:
            logger.warning("Agent Traffic DELETE for %s answered HTTP %s", ref, answer.status)
        await self._removal_waits(
            row,
            f"http_{answer.status}" + (f"_{answer.code}" if answer.code else ""),
            retry_after=answer.retry_after,
        )
        return False

    async def _removal_waits(
        self, row: dict[str, Any], error: str, *, retry_after: int | None = None
    ) -> None:
        await self.db.pool.execute(
            """UPDATE agent_traffic_removals SET last_error = $2,
               next_attempt_at = now() + make_interval(secs => $3) WHERE project_ref = $1
               AND finished_at IS NULL""",
            row["project_ref"],
            error[:120],
            removal_wait(row["attempts"], retry_after),
        )


def _stored(stored: dict[str, Any] | None) -> dict[str, Any] | None:
    if stored is None:
        return None
    return {
        "report": stored["report"],
        "fetched_at": _iso(stored["fetched_at"]),
        "stale": False,
        "comparison": comparison(stored["report"]),
    }


async def removal_loop(integrations: Any) -> None:
    """Send the removals Tin still owes the service, for as long as the switchboard runs."""

    while True:
        try:
            await integrations.agent_traffic.process_removals()
        except Exception:  # noqa: BLE001 — the next pass retries
            logger.warning("Analytics removals will retry.")
        await asyncio.sleep(REMOVAL_PASS_SECONDS)
