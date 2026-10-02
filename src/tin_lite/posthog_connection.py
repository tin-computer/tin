"""PostHog through OAuth: client metadata, region, project selection, refresh and reads.

Tin identifies itself to PostHog with a Client ID Metadata Document served from a fixed path
on TIN_LITE_PUBLIC_URL, so there is no registration row that a scope change could replace and
no client secret. The founder authorizes one project on PostHog's consent screen; the token
response says which region (US or EU Cloud) holds the data and which projects the grant
reaches. Tokens are stored encrypted in one credential and refreshed under the project's
integration refresh lock. Workflows never see a token: they call the reviewed read operations
below through the service gateway and receive small projected records for the selected project.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

import httpx

from tin_lite.connection_records import (
    ServiceArgumentError,
    boolean,
    fit_records,
    integer,
    json_size,
    open_credential,
    read_bounded_json,
    record_call,
    seal_credential,
    text,
)
from tin_lite.integrations import (
    POSTHOG_CAPABILITIES,
    POSTHOG_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ProviderOption,
    ServiceCallRefused,
    ServiceResponseTooLarge,
)
from tin_lite.provider_errors import ProviderErrorDetail
from tin_lite.provider_errors import detail as provider_error_detail

# The region-agnostic authorization server fronts US and EU Cloud; the token response names
# the region. API reads and refreshes then go to that region's fixed host only.
POSTHOG_OAUTH = "https://oauth.posthog.com"
REGION_HOSTS = {"us": "https://us.posthog.com", "eu": "https://eu.posthog.com"}
CLIENT_METADATA_PATH = "/integrations/posthog/client.json"
CALLBACK_PATH = "/integrations/callback/posthog"
# Each capability needs every listed scope. Project and user reads identify the account,
# its region and the selectable projects.
CAPABILITY_SCOPES = {
    "query.read": ("query:read",),
    "definitions.read": ("event_definition:read", "property_definition:read"),
    "insights.read": ("insight:read",),
}
IDENTITY_SCOPES = ("project:read", "user:read")
REQUESTED_SCOPES = tuple(
    sorted({*IDENTITY_SCOPES, *(s for scopes in CAPABILITY_SCOPES.values() for s in scopes)})
)
# What a stored grant must hold to count as connected. Kept apart from REQUESTED_SCOPES so a
# scope added later widens new consents without forcing existing connections to reconnect.
REQUIRED_SCOPES = frozenset({"project:read"})
REFRESH_MARGIN_SECONDS = 300
MAX_UPSTREAM_BYTES = 8_000_000
MAX_ERROR_BYTES = 64_000
MAX_OFFSET = 10_000
MAX_OPTIONS = 200
HOGQL_MAX_BYTES = 8_000
HOGQL_MAX_LIMIT = 1_000
CELL_MAX_CHARS = 2_000
QUERY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:-]{0,63}\Z")
ORG_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
READ_PATHS = (
    re.compile(r"/api/users/@me/\Z"),
    re.compile(r"/api/projects/\Z"),
    re.compile(r"/api/projects/\d{1,12}/\Z"),
    re.compile(r"/api/organizations/[0-9a-f-]{36}/projects/\Z"),
    re.compile(r"/api/projects/\d{1,12}/(event_definitions|property_definitions|insights)/\Z"),
)
QUERY_PATH = re.compile(r"/api/projects/\d{1,12}/query/\Z")


# ---------------------------------------------------------------- operator configuration


def public_origin(settings: Any) -> str:
    return settings.switchboard_public_url.rstrip("/")


def client_id(settings: Any) -> str:
    """The client_id is the metadata document's own URL, derived from the public URL only."""
    return public_origin(settings) + CLIENT_METADATA_PATH


def redirect_uri(settings: Any) -> str:
    return public_origin(settings) + CALLBACK_PATH


def oauth_ready(settings: Any) -> bool:
    if not getattr(settings, "posthog_oauth_enabled", False):
        return False
    url = urlsplit(getattr(settings, "switchboard_public_url", "") or "")
    return url.scheme == "https" and url.hostname not in {None, "localhost", "127.0.0.1"}


def client_metadata(settings: Any) -> dict[str, Any]:
    """The document PostHog fetches at client_id: a public client (PKCE, no secret) whose
    `com.posthog.scopes` is Tin's scope ceiling, so no token can carry more than these reads."""
    namespace: dict[str, Any] = {"scopes": list(REQUESTED_SCOPES)}
    token = getattr(settings, "posthog_oauth_verification_token", None)
    if token:
        namespace["verification_token"] = token
    return {
        "client_id": client_id(settings),
        "client_name": "Tin",
        "client_uri": public_origin(settings),
        "redirect_uris": [redirect_uri(settings)],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(REQUESTED_SCOPES),
        "com.posthog": namespace,
    }


def authorization_url(settings: Any, *, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "client_id": client_id(settings),
            "redirect_uri": redirect_uri(settings),
            "response_type": "code",
            "scope": " ".join(REQUESTED_SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # PostHog's consent screen then asks for exactly one project, not the account.
            "required_access_level": "project",
        }
    )
    return f"{POSTHOG_OAUTH}/oauth/authorize/?{query}"


def capabilities_for(scopes: set[str]) -> list[str]:
    return [
        capability
        for capability in POSTHOG_CAPABILITIES
        if set(CAPABILITY_SCOPES[capability]) <= scopes
    ]


def region_from_token(payload: dict[str, Any]) -> str | None:
    """PostHog adds posthog_region/posthog_base_url to token responses. Only a known region
    maps to a host; an unknown base URL is never used as an API host."""
    region = payload.get("posthog_region")
    if isinstance(region, str) and region.lower() in REGION_HOSTS:
        return region.lower()
    base = payload.get("posthog_base_url")
    if isinstance(base, str):
        for name, host in REGION_HOSTS.items():
            if base.rstrip("/").lower() == host:
                return name
    return None


# ---------------------------------------------------------------- HogQL guard


def check_hogql(query: Any) -> str:
    """One bounded SELECT with an explicit final LIMIT of at most 1000 and no OFFSET.

    PostHog's /query endpoint is for ad-hoc analytics, not exports, and rejects OFFSET paging
    for programmatic callers. Tin enforces that shape before any request: strings, quoted
    identifiers and comments are masked, then the remaining code must be a single statement
    starting with SELECT or WITH, have no top-level set operation, and end in `LIMIT n`.
    Page with a WHERE on a sort key (for example `timestamp < '<last seen>'`) instead.
    """
    if not isinstance(query, str) or not query.strip():
        raise ServiceArgumentError("query must be a HogQL SELECT")
    if len(query.encode()) > HOGQL_MAX_BYTES:
        raise ServiceArgumentError(f"query must be at most {HOGQL_MAX_BYTES} bytes")
    if any(ord(c) < 32 and c not in "\t\n\r" for c in query):
        raise ServiceArgumentError("query contains control characters")
    code, top = _mask_hogql(query)
    statement = top.rstrip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if ";" in statement or ";" in code.rstrip().rstrip(";"):
        raise ServiceArgumentError("query must be a single statement")
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", statement)
    if not words or words[0].upper() not in {"SELECT", "WITH"}:
        raise ServiceArgumentError("query must start with SELECT or WITH")
    if re.search(r"\bOFFSET\b", code, re.IGNORECASE):
        raise ServiceArgumentError(
            "query must not use OFFSET; page with a WHERE on a sort key such as timestamp"
        )
    upper = [word.upper() for word in words]
    for index, word in enumerate(upper):
        following = upper[index + 1] if index + 1 < len(upper) else ""
        if word in {"UNION", "INTERSECT"} or (
            word == "EXCEPT" and following in {"SELECT", "WITH", "DISTINCT", "ALL"}
        ):
            raise ServiceArgumentError("query must be one SELECT without UNION or INTERSECT")
    match = re.search(r"\bLIMIT\s+(\d{1,7})\s*\Z", statement, re.IGNORECASE)
    if match is None:
        raise ServiceArgumentError(
            f"query must end with an explicit LIMIT of at most {HOGQL_MAX_LIMIT}"
        )
    if not 1 <= int(match.group(1)) <= HOGQL_MAX_LIMIT:
        raise ServiceArgumentError(f"query LIMIT must be from 1 to {HOGQL_MAX_LIMIT}")
    if re.search(r"\bLIMIT\s+\d+\s*,", statement, re.IGNORECASE):
        raise ServiceArgumentError("query must not use LIMIT offset, count")
    return query.strip().rstrip(";").rstrip()


def _mask_hogql(query: str) -> tuple[str, str]:
    """Return (all code, depth-zero code) with literals, identifiers and comments masked."""
    code: list[str] = []
    top: list[str] = []
    depth, index, size = 0, 0, len(query)

    def emit(value: str) -> None:
        code.append(value)
        top.append(value if depth == 0 else " ")

    while index < size:
        char = query[index]
        pair = query[index : index + 2]
        if pair == "--":
            end = query.find("\n", index)
            index = size if end < 0 else end
            emit(" ")
            continue
        if pair == "/*":
            end = query.find("*/", index + 2)
            if end < 0:
                raise ServiceArgumentError("query has an unterminated comment")
            index = end + 2
            emit(" ")
            continue
        if char == "#":
            raise ServiceArgumentError("query must not use # comments")
        if char in "'\"`":
            index = _skip_quoted(query, index, char)
            emit(" ? ")
            continue
        if char == "(":
            emit(char)
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ServiceArgumentError("query has unbalanced parentheses")
            emit(char)
        else:
            emit(char)
        index += 1
    if depth:
        raise ServiceArgumentError("query has unbalanced parentheses")
    return "".join(code), "".join(top)


def _skip_quoted(query: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(query):
        char = query[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            if query[index + 1 : index + 2] == quote:  # doubled quote escapes itself
                index += 2
                continue
            return index + 1
        index += 1
    raise ServiceArgumentError("query has an unterminated quoted value")


# ---------------------------------------------------------------- operations and arguments


@dataclass(frozen=True)
class Operation:
    capability: str
    resource: str
    arguments: frozenset[str]
    project: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _checked(arguments: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise ServiceArgumentError("arguments must be an object")
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ServiceArgumentError(f"unsupported argument {unknown[0]}")
    return arguments


def _search(value: Any, field: str = "search") -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 200
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ServiceArgumentError(f"{field} must be text of 1 to 200 characters")
    return value


def _offset(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"\d{1,6}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_OFFSET:
        raise ServiceArgumentError(f"cursor must be a next_cursor value up to {MAX_OFFSET}")
    return value


def request_for(operation: str, arguments: Any) -> tuple[Operation, dict[str, Any]]:
    """Validate one call's closed arguments; return the resource and its request values.

    List operations return `{"params": ...}` for a GET; `query.hogql` returns `{"body": ...}`.
    The project id and host are never arguments: Tin injects the selected project.
    """
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise ServiceArgumentError("unknown PostHog operation")
    args = _checked(arguments, spec.arguments)
    if operation == "query.hogql":
        if "query" not in args:
            raise ServiceArgumentError("query is required")
        name = args.get("name")
        if name is not None and (not isinstance(name, str) or not QUERY_NAME.fullmatch(name)):
            raise ServiceArgumentError(
                "name must be 1 to 64 letters, digits, spaces or _.:- characters"
            )
        body = {
            "query": {"kind": "HogQLQuery", "query": check_hogql(args["query"])},
            "refresh": "blocking",
            "name": f"tin: {name}" if name else "tin workflow",
        }
        return spec, {"body": body}
    limit = args.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ServiceArgumentError("limit must be an integer from 1 to 100")
    params: dict[str, Any] = {"limit": limit, "offset": 0}
    if args.get("cursor") is not None:
        params["offset"] = _offset(args["cursor"])
    if args.get("search") is not None:
        params["search"] = _search(args["search"])
    if operation == "property_definitions.list":
        params["type"] = "event"
        names = args.get("event_names")
        if names is not None:
            if (
                not isinstance(names, list)
                or not 1 <= len(names) <= 20
                or len(set(map(str, names))) != len(names)
            ):
                raise ServiceArgumentError("event_names must be a list of 1 to 20 unique names")
            params["event_names"] = json.dumps(
                [_search(name, "event_names") for name in names], ensure_ascii=False
            )
            params["filter_by_event_names"] = "true"
    if operation == "insights.list":
        params.update(basic="true", saved="true")
    return spec, {"params": params}


def check_arguments(operation: str, arguments: Any) -> None:
    """Gateway hook: reject a malformed call before any receipt or provider request exists."""
    request_for(operation, arguments)


# ---------------------------------------------------------------- projections


def project_event_definition(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": text(value.get("name"), 400),
        "volume_30_day": integer(value.get("volume_30_day")),
        "query_usage_30_day": integer(value.get("query_usage_30_day")),
        "last_seen_at": text(value.get("last_seen_at"), 40),
    }


def project_property_definition(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": text(value.get("name"), 400),
        "property_type": text(value.get("property_type"), 40),
        "is_numerical": boolean(value.get("is_numerical")),
    }


def project_insight(value: dict[str, Any]) -> dict[str, Any]:
    query = value.get("query") if isinstance(value.get("query"), dict) else {}
    source = query.get("source") if isinstance(query.get("source"), dict) else {}
    return {
        "id": integer(value.get("id")),
        "short_id": text(value.get("short_id"), 20),
        "name": text(value.get("name")) or text(value.get("derived_name")),
        "kind": text(query.get("kind"), 60),
        "query_kind": text(source.get("kind"), 60) or text(query.get("kind"), 60),
        "last_refresh": text(value.get("last_refresh"), 40),
    }


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")[:CELL_MAX_CHARS]
    if isinstance(value, float) and value != value:  # NaN is not JSON
        return None
    if isinstance(value, list):
        return [_cell(item) for item in value[:100]]
    if isinstance(value, dict):
        return {str(k)[:200]: _cell(v) for k, v in list(value.items())[:100]}
    return value if value is None or isinstance(value, bool | int | float) else str(value)[:200]


def project_query(payload: Any, *, max_response_bytes: int | None) -> dict[str, Any]:
    """Project a HogQL response to `{columns, types, rows, has_more, truncated}`.

    Rows keep PostHog's column order. Tin keeps the leading rows that fit the byte bound;
    `truncated` says rows were left out here and `has_more` that more rows exist beyond them.
    """
    if not isinstance(payload, dict):
        raise IntegrationUpstreamError("PostHog returned an invalid query response")
    rows = payload.get("results")
    if rows is None and isinstance(payload.get("query_status"), dict):
        raise ServiceCallRefused(
            "PostHog did not finish the query within one call; read less data or add a "
            "narrower time range, then try again in a new step.",
            code="query_incomplete",
        )
    columns = payload.get("columns")
    if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
        raise IntegrationUpstreamError("PostHog returned an invalid query response")
    raw_types = payload.get("types") if isinstance(payload.get("types"), list) else []
    types = [
        text(item[1], 100) if isinstance(item, list | tuple) and len(item) == 2 else text(item, 100)
        for item in raw_types[:500]
    ]
    projected = [[_cell(cell) for cell in row[:500]] for row in rows]
    result = {
        "columns": [text(c, 200) for c in columns[:500]] if isinstance(columns, list) else [],
        "types": types,
        "rows": projected,
        "has_more": payload.get("hasMore") is True,
        "truncated": False,
    }
    if max_response_bytes is None or json_size(result) <= max_response_bytes:
        return result
    skeleton = {**result, "rows": [], "has_more": True, "truncated": True}
    used = json_size(skeleton)
    kept = 0
    for index, row in enumerate(projected):
        used += json_size(row) + (2 if index else 0)
        if used > max_response_bytes:
            break
        kept += 1
    if projected and not kept:
        raise ServiceResponseTooLarge("A single row exceeds the response bound.")
    return {**skeleton, "rows": projected[:kept]}


def project_list(
    operation: str, payload: Any, *, offset: int, max_response_bytes: int | None
) -> dict[str, Any]:
    """Project one PostHog list page and fit its leading records to the byte bound."""
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("results"), list)
        or any(not isinstance(item, dict) for item in payload["results"])
    ):
        raise IntegrationUpstreamError("PostHog returned an unexpected list")
    project = OPERATIONS[operation].project
    assert project is not None
    records = [project(item) for item in payload["results"]]
    return fit_records(
        records,
        max_response_bytes=max_response_bytes,
        has_more=bool(payload.get("next")),
        offset=offset,
    )


# The reviewed operation table; the gateway maps (provider, operation) through it explicitly.
_PAGE = frozenset({"search", "cursor", "limit"})
OPERATIONS: dict[str, Operation] = {
    "query.hogql": Operation("query.read", "query", frozenset({"query", "name"})),
    "event_definitions.list": Operation(
        "definitions.read", "event_definitions", _PAGE, project_event_definition
    ),
    "property_definitions.list": Operation(
        "definitions.read",
        "property_definitions",
        _PAGE | {"event_names"},
        project_property_definition,
    ),
    "insights.list": Operation("insights.read", "insights", _PAGE, project_insight),
}
assert {op.capability for op in OPERATIONS.values()} == set(POSTHOG_CAPABILITIES)


# ---------------------------------------------------------------- client


class PostHogUnauthorized(ServiceCallRefused):
    def __init__(self, provider_error: ProviderErrorDetail | None = None) -> None:
        super().__init__(
            "PostHog no longer accepts Tin's access. Reconnect PostHog in Integrations.",
            code="reauthorization_required",
            provider_error=provider_error,
        )


class PostHogPermissionDenied(ServiceCallRefused):
    def __init__(self, provider_error: ProviderErrorDetail | None = None) -> None:
        super().__init__(
            "PostHog refused this read for the selected project. Reconnect PostHog and approve "
            "read access to that project, or choose a project the account can read.",
            code="permission_denied",
            provider_error=provider_error,
        )


class PostHogRequestRefused(RuntimeError):
    """A method or path outside the reviewed reads was attempted; it is never sent."""


def _retry_after(value: str | None) -> int | None:
    if value and value.strip().isdigit():
        return min(int(value.strip()), 86_400)
    return None


def rate_limited(
    *, budget: bool, retry_after: int | None, provider_error: ProviderErrorDetail | None = None
) -> IntegrationRateLimitedError:
    """PostHog's 429: the hourly query budget (`api_queries_budget_exceeded`) or a rate limit.

    Retryable under a new step; `retry_after` carries PostHog's Retry-After seconds.
    """
    return IntegrationRateLimitedError(
        (
            "PostHog's hourly query budget for this project is used up"
            if budget
            else "PostHog rate-limited this read"
        )
        + (f"; retry after {retry_after} seconds" if retry_after is not None else "; try later")
        + " in a new step.",
        reason="api_queries_budget_exceeded" if budget else "rate_limited",
        retry_after=retry_after,
        provider_error=provider_error,
    )


def query_rejected(provider_error: ProviderErrorDetail | None) -> ServiceCallRefused:
    """A 400 from /query; PostHog's own diagnostic (a HogQL error) travels as provider_error."""
    return ServiceCallRefused(
        "PostHog rejected the query (HTTP 400).", code="query_error", provider_error=provider_error
    )


class PostHogReader:
    """Reads against one fixed region host with a bearer token; only the query POST writes."""

    def __init__(self, client: httpx.AsyncClient, host: str, token: str) -> None:
        if host not in REGION_HOSTS.values():
            raise PostHogRequestRefused("PostHog host is not a known region")
        self._client, self._host, self._token = client, host, token

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        if method == "POST":
            if not QUERY_PATH.fullmatch(path):
                raise PostHogRequestRefused("only the query endpoint accepts POST")
        elif method != "GET" or not any(pattern.fullmatch(path) for pattern in READ_PATHS):
            raise PostHogRequestRefused(f"{method} {path} is not a reviewed PostHog read")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "Tin-PostHog/1",
        }
        async with self._client.stream(
            method,
            self._host + path,
            params=params or {},
            json=body,
            headers=headers,
            follow_redirects=False,
            timeout=22.0,
        ) as response:
            request_id = response.headers.get("x-request-id")
            status = response.status_code
            if status == 200:
                payload = await read_bounded_json(
                    response, maximum=MAX_UPSTREAM_BYTES, provider="PostHog"
                )
                if not isinstance(payload, dict):
                    raise IntegrationUpstreamError("PostHog returned an invalid response")
                return payload, request_id
            try:
                error = await read_bounded_json(
                    response, maximum=MAX_ERROR_BYTES, provider="PostHog"
                )
            except IntegrationError:
                error = None
        detail = provider_error_detail("PostHog", status, error, secrets=(self._token,))
        if status == 401:
            raise PostHogUnauthorized(detail)
        if status == 403:
            raise PostHogPermissionDenied(detail)
        if status == 429:
            code = error.get("code") if isinstance(error, dict) else None
            raise rate_limited(
                budget=code == "api_queries_budget_exceeded",
                retry_after=_retry_after(response.headers.get("retry-after")),
                provider_error=detail,
            )
        if status == 400 and QUERY_PATH.fullmatch(path):
            raise query_rejected(detail)
        if status == 404:
            raise ServiceCallRefused(
                "PostHog could not find the selected project. Choose the project again in "
                "Integrations.",
                code="project_unavailable",
                provider_error=detail,
            )
        raise ServiceCallRefused(
            f"PostHog could not complete the read (HTTP {status}).",
            code="provider_error",
            provider_error=detail,
        )


# ---------------------------------------------------------------- connection lifecycle


def _credential(integrations: Any, connection: Any) -> dict[str, Any]:
    try:
        value = json.loads(open_credential(integrations, connection))
    except (ValueError, TypeError):
        raise IntegrationAuthorizationError("PostHog must be reconnected") from None
    if not isinstance(value, dict) or not all(
        isinstance(value.get(k), str) and value[k] for k in ("access_token", "refresh_token")
    ):
        raise IntegrationAuthorizationError("PostHog must be reconnected")
    return value


def _seal(integrations: Any, project_id: UUID, tokens: dict[str, Any]):
    return seal_credential(
        integrations,
        project_id=project_id,
        provider_key=POSTHOG_PROVIDER,
        value=json.dumps(tokens, separators=(",", ":")),
    )


def _tokens(payload: dict[str, Any], *, previous_refresh: str | None = None) -> dict[str, Any]:
    access = payload.get("access_token")
    refresh = payload.get("refresh_token") or previous_refresh
    if not isinstance(access, str) or not access or len(access) > 4096:
        raise IntegrationUpstreamError("PostHog did not return an access token")
    if not isinstance(refresh, str) or not refresh or len(refresh) > 4096:
        raise IntegrationAuthorizationError(
            "PostHog did not return durable access; connect PostHog again"
        )
    lifetime = payload.get("expires_in")
    lifetime = lifetime if type(lifetime) is int and 60 <= lifetime <= 31_536_000 else 3600
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": int(time.time()) + lifetime,
    }


def _team_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(i for i in value if type(i) is int and i > 0))[:50]


def _org_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(i) for i in value if isinstance(i, str) and ORG_ID.match(i)))[:10]


def _label(configuration: dict[str, Any]) -> str:
    region = str(configuration.get("region") or "").upper()
    name = configuration.get("selected_project_name") or configuration.get("email") or "PostHog"
    return f"{name} · {region} Cloud" if region else name


def _option(project: dict[str, Any], region: str) -> ProviderOption | None:
    project_id = project.get("id")
    if type(project_id) is not int or project_id <= 0:
        return None
    name = text(project.get("name")) or f"Project {project_id}"
    return ProviderOption(
        id=str(project_id), label=name, detail=f"{region.upper()} Cloud · id {project_id}"
    )


class PostHogConnections:
    def __init__(self, integrations: Any) -> None:
        self.integrations = integrations
        self.db = integrations._database

    @property
    def _client(self) -> httpx.AsyncClient:
        return self.integrations._client

    # -- authorization

    async def pending_project(self, *, state: str, clerk_user_id: str) -> UUID:
        """The project an authorization belongs to, also after it completed (a repeat)."""
        attempt = await self._attempt(state, clerk_user_id, include_used=True)
        return attempt.project_id

    async def _attempt(self, state: str, clerk_user_id: str, *, include_used: bool):
        if not state or len(state) > 256:
            raise IntegrationAuthorizationError("connection attempt has expired")
        attempt = await self.db.get_integration_auth_attempt(
            token_hash=hashlib.sha256(state.encode()).hexdigest(),
            provider_key=POSTHOG_PROVIDER,
            clerk_user_id=clerk_user_id,
            include_used=include_used,
        )
        if attempt is None:
            raise IntegrationAuthorizationError("connection attempt has expired")
        return attempt

    async def complete(self, *, state: str, code: str, clerk_user_id: str):
        """Exchange the code once; a repeated callback returns the connection it made."""
        integrations = self.integrations
        integrations._require_configured(POSTHOG_PROVIDER)
        state_hash = hashlib.sha256((state or "").encode()).hexdigest()
        attempt = await self.db.consume_integration_auth_attempt(
            token_hash=state_hash, provider_key=POSTHOG_PROVIDER, clerk_user_id=clerk_user_id
        )
        if attempt is None:
            used = await self._attempt(state, clerk_user_id, include_used=True)
            async with self.db.integration_refresh_lock(
                project_id=used.project_id, provider_key=POSTHOG_PROVIDER
            ):
                existing = await self.db.get_integration_connection(
                    project_id=used.project_id, provider_key=POSTHOG_PROVIDER
                )
            if existing is not None and existing.configuration.get("auth_attempt") == state_hash:
                return existing
            raise IntegrationAuthorizationError("connection attempt has expired")
        if not isinstance(code, str) or not 1 <= len(code) <= 2048:
            raise IntegrationAuthorizationError("PostHog did not return an authorization code")
        if attempt.pkce_verifier_ciphertext is None or integrations._cipher is None:
            raise IntegrationAuthorizationError("PostHog connection attempt is invalid")
        verifier = integrations._cipher.decrypt(
            attempt.pkce_verifier_ciphertext,
            context=f"auth:{attempt.project_id}:{POSTHOG_PROVIDER}",
        )
        settings = integrations._settings
        async with self.db.integration_refresh_lock(
            project_id=attempt.project_id, provider_key=POSTHOG_PROVIDER
        ):
            try:
                payload = await self._token_request(
                    POSTHOG_OAUTH,
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri(settings),
                        "client_id": client_id(settings),
                        "code_verifier": verifier,
                    },
                )
            except PostHogUnauthorized:
                raise IntegrationAuthorizationError(
                    "PostHog did not accept the authorization; connect PostHog again"
                ) from None
            tokens = _tokens(payload)
            scopes = {s for s in str(payload.get("scope") or "").split() if s}
            if not REQUIRED_SCOPES <= scopes:
                raise IntegrationAuthorizationError(
                    "PostHog did not grant project read access; connect again and approve it"
                )
            region = region_from_token(payload) or await self._probe_region(tokens["access_token"])
            if region is None:
                raise IntegrationUpstreamError(
                    "PostHog did not say whether this account is on US or EU Cloud; connect again"
                )
            host = REGION_HOSTS[region]
            identity = await self._identity(host, tokens["access_token"])
            teams = _team_ids(payload.get("scoped_teams"))
            previous = await self.db.get_integration_connection(
                project_id=attempt.project_id, provider_key=POSTHOG_PROVIDER
            )
            configuration: dict[str, Any] = {
                "region": region,
                "api_host": host,
                "oauth_issuer": POSTHOG_OAUTH,
                # The grant belongs to this client id; refreshes always present it.
                "oauth_client_id": client_id(settings),
                "granted_scopes": sorted(scopes),
                "granted_capabilities": capabilities_for(scopes),
                "scoped_teams": teams,
                "scoped_organizations": _org_ids(payload.get("scoped_organizations")),
                "email": identity.get("email"),
                "selected_project_id": None,
                "selected_project_name": None,
                "auth_attempt": state_hash,
            }
            await self._keep_or_pick_project(configuration, previous, tokens["access_token"])
            ciphertext, version = _seal(integrations, attempt.project_id, tokens)
            connection = await self.db.upsert_integration_connection(
                project_id=attempt.project_id,
                provider_key=POSTHOG_PROVIDER,
                external_account_id=(
                    f"{region}:{identity['uuid']}" if identity.get("uuid") else None
                ),
                external_account_label=_label(configuration),
                configuration=configuration,
                credential_ciphertext=ciphertext,
                credential_key_version=version,
                connected_by_clerk_user_id=clerk_user_id,
            )
        await integrations._record_activity(
            connection,
            "integration_connected",
            f"PostHog connected ({region.upper()} Cloud), read only.",
            suffix=str(uuid4()),
        )
        return connection

    async def _token_request(self, issuer: str, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{issuer}/oauth/token/",
                data=form,
                headers={"Accept": "application/json"},
                follow_redirects=False,
                timeout=20.0,
            )
        except httpx.HTTPError:
            raise IntegrationUpstreamError("PostHog could not be reached; try again") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code == 429:
            raise IntegrationRateLimitedError(
                "PostHog is rate-limiting sign-in right now; try again shortly.",
                retry_after=_retry_after(response.headers.get("retry-after")),
            )
        if response.status_code in {400, 401}:
            raise PostHogUnauthorized()
        if response.status_code != 200 or not isinstance(payload, dict):
            raise IntegrationUpstreamError(
                f"PostHog could not complete authorization ({response.status_code})"
            )
        return payload

    async def _probe_region(self, token: str) -> str | None:
        """Fallback when the token response names no region: ask each region who we are."""
        found = []
        for region, host in REGION_HOSTS.items():
            try:
                await PostHogReader(self._client, host, token).request("GET", "/api/users/@me/")
            except (IntegrationError, httpx.HTTPError):
                continue
            found.append(region)
        return found[0] if len(found) == 1 else None

    async def _identity(self, host: str, token: str) -> dict[str, Any]:
        try:
            me, _ = await PostHogReader(self._client, host, token).request("GET", "/api/users/@me/")
        except (IntegrationError, httpx.HTTPError):
            return {}  # user:read is optional; the connection works without a label
        uuid = me.get("uuid")
        return {
            "uuid": uuid if isinstance(uuid, str) and ORG_ID.match(uuid) else None,
            "email": text(me.get("email"), 320),
        }

    async def _keep_or_pick_project(self, configuration, previous, token) -> None:
        """Keep a prior selection on the same host that the new grant still reaches; else
        select the one project the founder chose on PostHog's consent screen."""
        teams = configuration["scoped_teams"]
        prior = previous.configuration if previous is not None else {}
        kept = prior.get("selected_project_id")
        if (
            kept
            and prior.get("api_host") == configuration["api_host"]
            and (not teams or int(kept) in teams)
        ):
            configuration["selected_project_id"] = kept
            configuration["selected_project_name"] = prior.get("selected_project_name")
            return
        if len(teams) == 1:
            configuration["selected_project_id"] = str(teams[0])
            try:
                project, _ = await PostHogReader(
                    self._client, configuration["api_host"], token
                ).request("GET", f"/api/projects/{teams[0]}/")
                configuration["selected_project_name"] = text(project.get("name"))
            except (IntegrationError, httpx.HTTPError):
                pass

    # -- tokens

    async def access_token(self, connection: Any, *, rejected: str | None = None) -> str:
        """A current access token; refresh under the lock when near expiry or rejected."""
        tokens = _credential(self.integrations, connection)
        if rejected is None and tokens.get("expires_at", 0) - time.time() > REFRESH_MARGIN_SECONDS:
            return tokens["access_token"]
        async with self.db.integration_refresh_lock(
            project_id=connection.project_id, provider_key=POSTHOG_PROVIDER
        ):
            # Re-read inside the lock: another worker may have refreshed already.
            current = await self.db.get_integration_connection(
                project_id=connection.project_id, provider_key=POSTHOG_PROVIDER
            )
            if current is None or current.id != connection.id:
                raise IntegrationAuthorizationError("PostHog connection changed; start again")
            tokens = _credential(self.integrations, current)
            fresh = (
                tokens["access_token"] != rejected
                if rejected is not None
                else tokens.get("expires_at", 0) - time.time() > REFRESH_MARGIN_SECONDS
            )
            if fresh:
                return tokens["access_token"]
            host = current.configuration.get("api_host")
            if host not in REGION_HOSTS.values():
                raise IntegrationAuthorizationError("PostHog must be reconnected")
            try:
                payload = await self._token_request(
                    host,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": tokens["refresh_token"],
                        # The client the grant was issued to, never whatever is current.
                        "client_id": current.configuration.get("oauth_client_id")
                        or client_id(self.integrations._settings),
                    },
                )
            except PostHogUnauthorized:
                await self.db.mark_integration_attention(
                    project_id=current.project_id,
                    provider_key=POSTHOG_PROVIDER,
                    error_code="reauthorization_required",
                )
                raise
            # A rotated refresh token is single-use: persist it before anything else runs.
            renewed = _tokens(payload, previous_refresh=tokens["refresh_token"])
            ciphertext, version = _seal(self.integrations, current.project_id, renewed)
            stored = await self.db.update_integration_credential(
                project_id=current.project_id,
                provider_key=POSTHOG_PROVIDER,
                connection_id=current.id,
                credential_ciphertext=ciphertext,
                credential_key_version=version,
            )
            if not stored:
                raise IntegrationAuthorizationError("PostHog connection changed; start again")
            return renewed["access_token"]

    async def _read(self, connection: Any, method: str, path: str, **request):
        """One read with a current token; a 401 refreshes once and retries once."""
        host = connection.configuration.get("api_host")
        token = await self.access_token(connection)
        try:
            return await PostHogReader(self._client, host, token).request(method, path, **request)
        except PostHogUnauthorized:
            token = await self.access_token(connection, rejected=token)
        try:
            return await PostHogReader(self._client, host, token).request(method, path, **request)
        except PostHogUnauthorized:
            await self.db.mark_integration_attention(
                project_id=connection.project_id,
                provider_key=POSTHOG_PROVIDER,
                error_code="reauthorization_required",
            )
            raise

    async def revoke(self, connection: Any) -> None:
        try:
            tokens = _credential(self.integrations, connection)
            host = connection.configuration.get("api_host")
            if host in REGION_HOSTS.values():
                await self._client.post(
                    f"{host}/oauth/revoke/",
                    data={
                        "token": tokens["refresh_token"],
                        "token_type_hint": "refresh_token",
                        "client_id": connection.configuration.get("oauth_client_id")
                        or client_id(self.integrations._settings),
                    },
                    follow_redirects=False,
                    timeout=10.0,
                )
        except Exception:  # noqa: BLE001 — local disconnection is authoritative
            return

    # -- project selection

    async def projects(self, *, project_id: UUID) -> list[ProviderOption]:
        """Projects the grant reaches on its own region host; ids are unique per host only."""
        connection = await self.integrations._connection(project_id, POSTHOG_PROVIDER)
        config = connection.configuration
        region = str(config.get("region") or "")
        options: list[ProviderOption] = []
        execution_key = f"integration:{uuid4()}"
        fingerprint = hashlib.sha256(f"{project_id}:{POSTHOG_PROVIDER}:projects".encode())
        receipt = {
            "connection": connection,
            "capability": "projects.list",
            "fingerprint": fingerprint.hexdigest(),
            "execution_key": execution_key,
            "run_id": None,
        }
        try:
            teams = _team_ids(config.get("scoped_teams"))
            if teams:
                for team in teams[:25]:
                    try:
                        project, _ = await self._read(connection, "GET", f"/api/projects/{team}/")
                    except ServiceCallRefused as exc:
                        if exc.code in {"permission_denied", "project_unavailable"}:
                            continue  # a scoped project the account can no longer read
                        raise
                    if option := _option(project, region):
                        options.append(option)
            else:
                paths = [
                    f"/api/organizations/{org}/projects/"
                    for org in _org_ids(config.get("scoped_organizations"))
                ] or ["/api/projects/"]
                for path in paths:
                    page, _ = await self._read(connection, "GET", path, params={"limit": 200})
                    rows = page.get("results") if isinstance(page.get("results"), list) else []
                    options += [
                        o for row in rows if isinstance(row, dict) if (o := _option(row, region))
                    ]
        except IntegrationError as exc:
            await record_call(
                self.db,
                **receipt,
                status="failed",
                error_code=getattr(exc, "code", "provider_request_failed"),
            )
            raise
        unique = list({option.id: option for option in options}.values())[:MAX_OPTIONS]
        unique.sort(key=lambda option: option.label.casefold())
        await record_call(
            self.db, **receipt, status="completed", response_summary={"count": len(unique)}
        )
        return unique

    async def select_project(self, *, project_id: UUID, option_id: str):
        options = await self.projects(project_id=project_id)
        option = next((item for item in options if item.id == option_id), None)
        if option is None:
            raise IntegrationAuthorizationError(
                "That PostHog project is not available to the connected account"
            )
        connection = await self.integrations._connection(project_id, POSTHOG_PROVIDER)
        configuration = {
            **connection.configuration,
            "selected_project_id": option.id,
            "selected_project_name": option.label,
        }
        updated = await self.db.update_integration_configuration(
            project_id=project_id,
            provider_key=POSTHOG_PROVIDER,
            configuration=configuration,
            external_account_label=_label(configuration),
        )
        await self.integrations._record_activity(
            updated,
            "integration_configured",
            f"PostHog now reads {option.label}.",
            suffix=hashlib.sha256(option.id.encode()).hexdigest()[:12],
        )
        return updated

    # -- gateway reads

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
        spec, request = request_for(operation, arguments)
        config = connection.configuration
        if spec.capability not in (config.get("granted_capabilities") or []):
            raise IntegrationAuthorizationError("PostHog did not grant this read")
        selected = config.get("selected_project_id")
        if not isinstance(selected, str) or not selected.isdigit():
            raise IntegrationAuthorizationError("Choose a PostHog project first")
        path = f"/api/projects/{selected}/{spec.resource}/"
        method = "POST" if spec.resource == "query" else "GET"
        fingerprint = hashlib.sha256(
            json.dumps(
                {"host": config.get("api_host"), "method": method, "path": path, **request},
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
            payload, request_id = await self._read(
                connection, method, path, params=request.get("params"), body=request.get("body")
            )
        except IntegrationError as exc:
            await record_call(
                self.db,
                **receipt,
                status="failed",
                error_code=getattr(exc, "code", "provider_request_failed"),
            )
            raise
        try:
            if method == "POST":
                result = project_query(payload, max_response_bytes=max_response_bytes)
                summary = {
                    "record_count": len(payload.get("results") or []),
                    "returned_count": len(result["rows"]),
                }
            else:
                result = project_list(
                    operation,
                    payload,
                    offset=request["params"]["offset"],
                    max_response_bytes=max_response_bytes,
                )
                summary = {
                    "record_count": len(payload.get("results") or []),
                    "returned_count": len(result["records"]),
                }
        except (ServiceResponseTooLarge, ServiceCallRefused) as exc:
            # PostHog answered; the outcome is known even though nothing usable fits.
            await record_call(
                self.db,
                **receipt,
                status="completed",
                response_summary={"returned_count": 0, "error": getattr(exc, "code", "too_large")},
                provider_request_id=request_id,
            )
            raise
        summary.update(has_more=result["has_more"], truncated=result["truncated"])
        await record_call(
            self.db,
            **receipt,
            status="completed",
            response_summary=summary,
            provider_request_id=request_id,
        )
        return result
