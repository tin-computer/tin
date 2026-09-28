"""Bounded Google Ads REST adapter (API v25) acting under Tin's manager account.

Allowlisted endpoints, fixed request shapes from `google_ads_requests`, an opaque failure that
carries only the provider's error code, a credential-echo refusal, and a zero-cost usage receipt
per call. Retries cover only the provider's documented transient codes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from urllib.parse import quote

import httpx

from tin_lite.google_ads_requests import BOUNDS, bulk_body, client_link_body, customer_id
from tin_lite.usage_capture import begin_observation, observe_failure, observe_tool

MESSAGE = "Google Ads outcome could not be confirmed."
RESPONSE_BYTES = 4_000_000
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
SEGMENTS = frozenset(
    {
        "campaigns",
        "campaignBudgets",
        "sharedCriteria",
        "adGroupAds",
        "adGroupCriteria",
        "conversionActions",
        "recommendationSubscriptions:mutateRecommendationSubscription",
    }
)
RETRY_CODES = frozenset(
    {
        "InternalError.TRANSIENT_ERROR",
        "InternalError.INTERNAL_ERROR",
        "InternalError.DEADLINE_EXCEEDED",
        "QuotaError.RESOURCE_TEMPORARILY_EXHAUSTED",
    }
)
RETRY_DELAYS = (5, 10, 20)
# The request may have reached Google but its answer was lost or unreadable, so a write may
# have been applied: a timeout, a dropped connection, or a reply Tin could not accept.
UNCONFIRMED_CODES = frozenset({"timeout", "transport", "envelope"})
_VERSION = re.compile(r"^v\d{1,3}$")


class GoogleAdsError(RuntimeError):
    """Opaque Google Ads failure; only the provider's error code survives."""

    def __init__(self, code: str):
        super().__init__(MESSAGE)
        self.code = code


class _Refused(Exception):
    def __init__(self, kind: str, status_code: int | None = None, code: str | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code
        self.code = code or (f"http_{status_code}" if status_code else kind)


def _credential_forms(secret: str) -> set[str]:
    return {
        secret,
        json.dumps(secret, ensure_ascii=False)[1:-1],
        json.dumps(secret, ensure_ascii=True)[1:-1],
        quote(secret, safe=""),
        base64.b64encode(secret.encode()).decode(),
    }


def _error_code(payload) -> str | None:
    """`Category.VALUE` from the first GoogleAdsFailure error, never its message."""
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    for detail in details or []:
        for item in (detail.get("errors") if isinstance(detail, dict) else None) or []:
            code = item.get("errorCode") if isinstance(item, dict) else None
            if isinstance(code, dict) and code:
                key, value = next(iter(code.items()))
                if isinstance(key, str) and isinstance(value, str) and key and value:
                    return f"{key[0].upper()}{key[1:]}.{value}"
    return None


class ManagerTokenSource:
    """Mint short-lived access tokens from the manager account's refresh token."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, transport=None):
        if not all(isinstance(v, str) and v for v in (client_id, client_secret, refresh_token)):
            raise ValueError("Google Ads manager credentials are incomplete.")
        self._client_id, self._client_secret = client_id, client_secret
        self._refresh_token = refresh_token
        self._transport = transport
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def secrets(self) -> set[str]:
        forms = _credential_forms(self._refresh_token) | _credential_forms(self._client_secret)
        if self._token:
            forms |= _credential_forms(self._token)
        return forms

    async def __call__(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=False, trust_env=False, transport=self._transport
            ) as client:
                response = await client.post(
                    TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
            if response.status_code != 200:
                raise _Refused("http", response.status_code, code="token_refresh")
            try:
                payload = response.json()
            except ValueError:
                raise _Refused("api", response.status_code, code="token_refresh") from None
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise _Refused("api", response.status_code, code="token_refresh")
            expires = payload.get("expires_in")
            lifetime = expires if type(expires) is int and expires > 0 else 3600
            self._token = token
            self._expires_at = time.monotonic() + max(lifetime - 300, 60)
            return token


class GoogleAdsApi:
    def __init__(
        self,
        *,
        manager_customer_id: str,
        token_source,
        api_version: str = "v25",
        developer_token: str | None = None,
        transport=None,
        base_url: str = "https://googleads.googleapis.com",
        sleep=asyncio.sleep,
    ):
        self._manager = customer_id(manager_customer_id)
        self._tokens = token_source
        if not isinstance(api_version, str) or not _VERSION.fullmatch(api_version):
            raise ValueError("Google Ads API version looks like v25.")
        self._version = api_version
        if developer_token is not None and (
            not isinstance(developer_token, str) or not developer_token
        ):
            raise ValueError("Developer token must be non-empty when given.")
        self._developer_token = developer_token
        self._transport = transport
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ValueError("Google Ads base URL must be https.")
        self._base = base_url.rstrip("/")
        self._sleep = sleep

    @property
    def manager_customer_id(self) -> str:
        return self._manager

    # ------------------------------------------------------------ public calls

    async def search(self, customer_id_value: str, query: str) -> dict:
        cid = customer_id(customer_id_value)
        if not isinstance(query, str) or not query.startswith("SELECT ") or len(query) > 8000:
            raise ValueError("Search takes one fixed GAQL statement.")
        body = {"query": query}

        async def once(client, headers):
            return await self._paginate_search(client, headers, cid=cid, body=body)

        return await self._call("googleAds:search", once)

    async def mutate(
        self, customer_id_value: str, operations: list[dict], *, validate_only: bool = False
    ) -> dict:
        cid = customer_id(customer_id_value)
        body = bulk_body(operations, validate_only=validate_only)

        async def once(client, headers):
            payload, request_id = await self._post(
                client, f"customers/{cid}/googleAds:mutate", body, headers
            )
            results = payload.get("mutateOperationResponses")
            if results is None and validate_only:
                results = []
            if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
                raise _Refused("api", 200, code="envelope")
            if not validate_only and len(results) != len(operations):
                raise _Refused("api", 200, code="envelope")
            return {"results": results, "provider_request_id": request_id}

        return await self._call("googleAds:mutate", once)

    async def mutate_resource(self, customer_id_value: str, segment: str, body: dict) -> dict:
        cid = customer_id(customer_id_value)
        if segment not in SEGMENTS:
            raise ValueError("Unknown Google Ads mutate resource.")
        if not isinstance(body, dict) or not isinstance(body.get("operations"), list):
            raise ValueError("Resource mutates carry an operations list.")
        path = f"customers/{cid}/{segment}" + ("" if ":" in segment else ":mutate")

        async def once(client, headers):
            payload, request_id = await self._post(client, path, body, headers)
            results = payload.get("results")
            if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
                raise _Refused("api", 200, code="envelope")
            return {"results": results, "provider_request_id": request_id}

        return await self._call(segment, once)

    async def client_link(self, customer_id_value: str) -> dict:
        cid = customer_id(customer_id_value)
        body = client_link_body(cid)
        path = f"customers/{self._manager}/customerClientLinks:mutate"

        async def once(client, headers):
            payload, request_id = await self._post(client, path, body, headers)
            result = payload.get("result")
            name = result.get("resourceName") if isinstance(result, dict) else None
            if not isinstance(name, str) or not name.startswith(f"customers/{self._manager}/"):
                raise _Refused("api", 200, code="envelope")
            return {"resource_name": name, "provider_request_id": request_id}

        return await self._call("customerClientLinks:mutate", once)

    async def client_link_update(
        self, customer_id_value: str, manager_link_id: str, status: str
    ) -> dict:
        """End a manager link from Tin's side: CANCELED while pending, INACTIVE once active."""
        cid = customer_id(customer_id_value)
        if status not in {"CANCELED", "INACTIVE"} or not str(manager_link_id).isdigit():
            raise ValueError("Unsupported manager link update.")
        body = {
            "operation": {
                "update": {
                    "resourceName": (
                        f"customers/{self._manager}/customerClientLinks/{cid}~{manager_link_id}"
                    ),
                    "status": status,
                },
                "updateMask": "status",
            }
        }
        path = f"customers/{self._manager}/customerClientLinks:mutate"

        async def once(client, headers):
            payload, request_id = await self._post(client, path, body, headers)
            result = payload.get("result")
            name = result.get("resourceName") if isinstance(result, dict) else None
            if not isinstance(name, str):
                raise _Refused("api", 200, code="envelope")
            return {"resource_name": name, "provider_request_id": request_id}

        return await self._call("customerClientLinks:mutate", once)

    # ------------------------------------------------------------ transport

    async def _paginate_search(self, client, headers, *, cid, body):
        rows: list[dict] = []
        request_id = None
        token = None
        for _page in range(50):
            page_body = dict(body, **({"pageToken": token} if token else {}))
            payload, request_id = await self._post(
                client, f"customers/{cid}/googleAds:search", page_body, headers
            )
            results = payload.get("results", [])
            if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
                raise _Refused("api", 200, code="envelope")
            rows.extend(results)
            if len(rows) > BOUNDS["search_rows"]:
                raise _Refused("api", 200, code="envelope")
            token = payload.get("nextPageToken")
            if not token:
                return {"rows": rows, "provider_request_id": request_id}
            if not isinstance(token, str) or len(token) > 4000:
                raise _Refused("api", 200, code="envelope")
        raise _Refused("api", 200, code="envelope")

    async def _call(self, endpoint: str, once) -> dict:
        observation = await begin_observation("google_ads", "tool", endpoint)
        try:
            async with asyncio.timeout(50):
                result = await self._with_retries(once)
        except _Refused as refused:
            await observe_failure(observation, kind=refused.kind, status_code=refused.status_code)
            raise GoogleAdsError(refused.code) from None
        except (TimeoutError, httpx.TimeoutException):
            await observe_failure(observation, kind="timeout")
            raise GoogleAdsError("timeout") from None
        except (httpx.TransportError, OSError):
            await observe_failure(observation, kind="connection")
            raise GoogleAdsError("transport") from None
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            KeyError,
            ArithmeticError,
            UnicodeError,
            RecursionError,
        ):
            await observe_failure(observation, kind="api")
            raise GoogleAdsError("envelope") from None
        await observe_tool(observation, {"cost": "0"})
        return result

    async def _with_retries(self, once):
        token = await self._tokens()
        headers = {
            "Authorization": f"Bearer {token}",
            "login-customer-id": self._manager,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": "Tin-GoogleAds/1",
        }
        if self._developer_token:
            headers["developer-token"] = self._developer_token
        attempt = 0
        while True:
            async with httpx.AsyncClient(
                timeout=45, follow_redirects=False, trust_env=False, transport=self._transport
            ) as client:
                try:
                    return await once(client, headers)
                except _Refused as refused:
                    retryable = refused.code in RETRY_CODES or refused.status_code == 503
                    if not retryable or attempt >= len(RETRY_DELAYS):
                        raise
            await self._sleep(RETRY_DELAYS[attempt])
            attempt += 1

    async def _post(self, client, path: str, body: dict, headers: dict) -> tuple[dict, str | None]:
        content = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        if len(content) > 4_000_000:
            raise _Refused("api", None, code="request_too_large")
        async with client.stream(
            "POST", f"{self._base}/{self._version}/{path}", content=content, headers=headers
        ) as response:
            status = response.status_code
            request_id = response.headers.get("request-id")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise _Refused("http", status, code="envelope")
            if 300 <= status < 400:
                raise _Refused("http", status, code="transport")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > RESPONSE_BYTES:
                    raise _Refused("http", status, code="envelope")
        text = raw.decode("utf-8", "replace")
        secrets = getattr(self._tokens, "secrets", None) or set()
        secrets = set(secrets) | _credential_forms(headers["Authorization"].removeprefix("Bearer "))
        if self._developer_token:
            secrets |= _credential_forms(self._developer_token)
        if any(form in text for form in secrets):
            # A credential echo never reaches a receipt, evidence or a log line.
            raise _Refused("api", status, code="envelope")
        try:
            payload = json.loads(text) if text else {}
        except ValueError:
            raise _Refused(
                "api", status, code="envelope" if status == 200 else f"http_{status}"
            ) from None
        if status != 200:
            code = _error_code(payload)
            raise _Refused("http", status, code=code or f"http_{status}")
        if not isinstance(payload, dict):
            raise _Refused("api", status, code="envelope")
        return payload, request_id


def manager_oauth_client(settings) -> tuple:
    """The OAuth client that minted the manager refresh token: the dedicated Google Ads pair
    when set, otherwise Tin's Google OAuth client."""
    client_id = getattr(settings, "google_ads_oauth_client_id", None)
    client_secret = getattr(settings, "google_ads_oauth_client_secret", None)
    if client_id and client_secret is not None:
        return client_id, client_secret
    return (
        getattr(settings, "google_oauth_client_id", None),
        getattr(settings, "google_oauth_client_secret", None),
    )


def api_from_settings(settings, *, transport=None) -> GoogleAdsApi | None:
    manager = getattr(settings, "google_ads_manager_customer_id", None)
    refresh = getattr(settings, "google_ads_manager_refresh_token", None)
    client_id, client_secret = manager_oauth_client(settings)
    if not manager or refresh is None or not client_id or client_secret is None:
        return None
    developer = getattr(settings, "google_ads_developer_token", None)
    return GoogleAdsApi(
        manager_customer_id=manager,
        token_source=ManagerTokenSource(
            client_id,
            client_secret.get_secret_value(),
            refresh.get_secret_value(),
            transport=transport,
        ),
        api_version=getattr(settings, "google_ads_api_version", "v25") or "v25",
        developer_token=developer.get_secret_value() if developer is not None else None,
        transport=transport,
    )
