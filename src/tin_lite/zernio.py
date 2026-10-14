"""Zernio, the provider behind LinkedIn posting: fixed, bounded operations on Tin's account.

Tin holds one Zernio key (`TIN_LITE_ZERNIO_API_KEY`) on the switchboard. It never reaches a
sandbox, package, project file, log or Temporal history. Every operation names a fixed path,
and callers pass the profile and account IDs held by the project's connection, never IDs
taken from a request: one key acts on every project's LinkedIn, so that mapping is the
boundary. See docs/linkedin-posting.md.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from tin_lite.connection_records import read_bounded_json
from tin_lite.integrations import (
    IntegrationAuthorizationError,
    IntegrationDeliveryRefusedError,
    IntegrationDeliveryUnknownError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationUpstreamError,
    ServiceCallRefused,
)

logger = logging.getLogger(__name__)

API = "https://zernio.com/api/v1"
MAX_RESPONSE_BYTES = 256_000
TIMEOUT_SECONDS = 20.0
# Zernio's media host refuses Python's default user agent.
USER_AGENT = "Tin (+https://tin.computer)"
# Posting now; comments and analytics are asked for once so later reads need no second consent.
SCOPES = "posting,analytics,comments"
ID = re.compile(r"^[0-9a-f]{24}$")
ORGANIZATION_URN = re.compile(r"^urn:li:organization:[0-9]{1,20}$")
POST_URN = re.compile(r"^urn:li:(share|ugcPost|activity):[0-9]{1,30}$")
URL_POST_URN = re.compile(r"/(urn:li:(?:share|ugcPost|activity):[0-9]{1,30})(?:/|$)")
PERSON_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CONNECT_HOSTS = {"www.linkedin.com", "linkedin.com", "zernio.com"}
POST_HOSTS = {"www.linkedin.com", "linkedin.com"}
# Presigned uploads go to Zernio's Cloudflare R2 bucket; posts name the media host's copy.
UPLOAD_HOST_SUFFIX = ".r2.cloudflarestorage.com"
MEDIA_HOST = "media.zernio.com"
MEDIA_TIMEOUT_SECONDS = 60.0
POST_STATUSES = {"draft", "scheduled", "publishing", "published", "partial", "failed", "cancelled"}
ERROR_CATEGORIES = {
    "auth_expired",
    "user_content",
    "user_abuse",
    "account_issue",
    "platform_rejected",
    "platform_error",
    "platform_rate_limit",
    "quota_exhausted",
    "system_error",
    "unknown",
}
UNAVAILABLE = "LinkedIn posting is temporarily unavailable"
REFUSED = (
    "LinkedIn refused the post before publishing it, so nothing was posted. "
    "Check its text and images, then preview it again."
)
DUPLICATE = (
    "LinkedIn already has this exact post from the last 24 hours, so Tin did not send it again."
)
RESULTS_UNAVAILABLE = "LinkedIn results are temporarily unavailable"
ANALYTICS_REFUSED = (
    "LinkedIn didn't let Tin read this account's results. "
    "Reconnect LinkedIn posting under Integrations."
)
ACCOUNT_GONE = (
    "The LinkedIn account isn't connected anymore. Reconnect LinkedIn posting under Integrations."
)
# What Tin keeps of one post's numbers; LinkedIn reports saves and sends for profiles only, and
# clicks for pages only.
METRICS = ("impressions", "reach", "reactions", "comments", "shares", "clicks", "saves", "sends")
MAX_METRIC = 10**12


def ready(settings: Any) -> bool:
    return getattr(settings, "zernio_api_key", None) is not None


def profile_name(project_id: UUID) -> str:
    return f"tin:{project_id}"


def zernio_id(value: Any) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise IntegrationUpstreamError("Zernio returned an invalid identifier")
    return value


def linkedin_account(value: Any) -> dict[str, Any] | None:
    """What Tin keeps of one Zernio account, or None when it is not a usable LinkedIn one."""

    if not isinstance(value, dict) or value.get("platform") != "linkedin":
        return None
    account_id = value.get("_id")
    metadata = value.get("metadata")
    if not isinstance(account_id, str) or not ID.fullmatch(account_id):
        return None
    if not isinstance(metadata, dict):
        return None
    kind = metadata.get("accountType")
    if kind == "organization":
        organization = metadata.get("organizationInfo")
        if not isinstance(organization, dict):
            return None
        urn = organization.get("urn")
        if not isinstance(urn, str) or not ORGANIZATION_URN.fullmatch(urn):
            return None
        name = organization.get("name") or value.get("displayName")
    elif kind == "personal":
        profile = metadata.get("profileData")
        person = profile.get("id") if isinstance(profile, dict) else None
        if not isinstance(person, str) or not PERSON_ID.fullmatch(person):
            return None
        urn = f"urn:li:person:{person}"
        name = value.get("displayName")
    else:
        return None
    if not isinstance(name, str) or not name.strip():
        name = "LinkedIn page" if kind == "organization" else "LinkedIn profile"
    expires = value.get("tokenExpiresAt")
    return {
        "id": account_id,
        "urn": urn,
        "kind": kind,
        "name": " ".join(name.split())[:100],
        "active": value.get("isActive") is True and value.get("enabled", True) is not False,
        # A sign-in gives the account a new token, so this moves when someone signs in again.
        "token_expires_at": expires if isinstance(expires, str) and len(expires) <= 40 else None,
    }


def _timestamp(value: Any) -> str | None:
    """An ISO time Zernio sent, normalised to UTC, or None."""

    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def post_urn(value: Any, url: Any = None) -> str | None:
    """A LinkedIn post's URN, from Zernio's platform post ID or else from its link."""

    if isinstance(value, str) and POST_URN.fullmatch(value):
        return value
    if _https(url, POST_HOSTS):
        found = URL_POST_URN.search(urlsplit(url).path)
        if found:
            return found.group(1)
    return None


def metrics(value: Any) -> dict[str, Any]:
    """One reading's numbers: whole counts, and LinkedIn's engagement rate as a percentage."""

    values = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for name in METRICS:
        raw = values.get("likes" if name == "reactions" else name)
        result[name] = raw if type(raw) is int and 0 <= raw < MAX_METRIC else 0
    rate = values.get("engagementRate")
    result["engagement_rate"] = (
        round(float(rate), 2)
        if type(rate) in {int, float} and 0 <= rate <= 10_000 and rate == rate
        else None
    )
    return result


def _https(url: Any, hosts: set[str] | None = None, *, suffix: str | None = None) -> bool:
    if not isinstance(url, str) or len(url) > 4096:
        return False
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and (
        host in (hosts or set()) or (suffix is not None and host.endswith(suffix))
    )


def post_state(value: Any) -> dict[str, Any]:
    """What Tin keeps of one Zernio post: its ID, status and the LinkedIn link once published."""

    if not isinstance(value, dict):
        raise IntegrationDeliveryUnknownError(UNAVAILABLE)
    post_id = value.get("_id")
    if not isinstance(post_id, str) or not ID.fullmatch(post_id):
        # Zernio answered a write without naming the post; whether it exists is unknown.
        raise IntegrationDeliveryUnknownError(UNAVAILABLE)
    platforms = value.get("platforms")
    entry = next(
        (
            item
            for item in (platforms if isinstance(platforms, list) else [])
            if isinstance(item, dict) and item.get("platform") == "linkedin"
        ),
        {},
    )
    status = value.get("status")
    url = entry.get("platformPostUrl")
    category = entry.get("errorCategory")
    url = url if _https(url, POST_HOSTS) else None
    return {
        "post_id": post_id,
        "status": status if status in POST_STATUSES else "unknown",
        "url": url,
        "error_category": category if category in ERROR_CATEGORIES else None,
        # What listening reads the post by, and the time its readings count from.
        "urn": post_urn(entry.get("platformPostId"), url),
        "published_at": _timestamp(entry.get("publishedAt")),
    }


class Zernio:
    def __init__(self, client: httpx.AsyncClient, settings: Any) -> None:
        key = getattr(settings, "zernio_api_key", None)
        if key is None:
            raise IntegrationNotConfiguredError(
                "LinkedIn posting is not configured on this Tin deployment"
            )
        self._client = client
        self._key = key.get_secret_value()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        write: bool = False,
    ) -> tuple[int, Any]:
        """One request; returns status and parsed body for 2xx and 4xx answers.

        A write whose answer is lost raises IntegrationDeliveryUnknownError, so callers holding
        a receipt never send it again on their own. Reads fail as ordinary upstream errors.
        """

        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        lost = IntegrationDeliveryUnknownError if write else IntegrationUpstreamError
        try:
            async with self._client.stream(
                method,
                f"{API}{path}",
                params=params,
                json=body,
                headers=headers,
                follow_redirects=False,
                timeout=TIMEOUT_SECONDS,
            ) as response:
                status = response.status_code
                if status >= 500:
                    raise lost(UNAVAILABLE)
                try:
                    payload = await read_bounded_json(
                        response, maximum=MAX_RESPONSE_BYTES, provider="Zernio"
                    )
                except (IntegrationUpstreamError, ServiceCallRefused):
                    if 200 <= status < 300:
                        raise lost(UNAVAILABLE) from None
                    payload = None
        except httpx.HTTPError:
            raise lost(UNAVAILABLE) from None
        if status == 429:
            raw = response.headers.get("retry-after", "")
            raise IntegrationRateLimitedError(
                "Zernio is rate limiting LinkedIn posting; try again shortly",
                retry_after=min(int(raw), 3600) if raw.isdigit() else None,
            )
        if status == 401:
            # Tin's own key was refused; nothing a founder can fix.
            logger.warning("Zernio refused Tin's API key (%s %s)", method, path)
            raise IntegrationUpstreamError(UNAVAILABLE)
        if not (200 <= status < 300 or 400 <= status < 500):
            raise lost(UNAVAILABLE)
        return status, payload

    async def ensure_profile(self, project_id: UUID) -> str:
        """The project's one profile: found by its unique name, or created once."""

        found = await self.find_profile(project_id)
        if found is not None:
            return found
        name = profile_name(project_id)
        status, payload = await self._call(
            "POST",
            "/profiles",
            body={"name": name, "externalId": str(project_id), "description": "Tin project"},
            idempotency_key=f"tin-profile-{project_id}",
        )
        if status in {200, 201} and isinstance(payload, dict):
            profile = payload.get("profile")
            if isinstance(profile, dict):
                return zernio_id(profile.get("_id"))
        if status == 409:
            details = payload.get("details") if isinstance(payload, dict) else None
            existing = details.get("existingProfileId") if isinstance(details, dict) else None
            if isinstance(existing, str) and ID.fullmatch(existing):
                return existing
        if 400 <= status < 500:
            # Another request may have made it meanwhile; Zernio refuses a duplicate with 409
            # or 422.
            found = await self.find_profile(project_id)
            if found is not None:
                return found
        logger.warning("Zernio did not create the project profile (status %s)", status)
        raise IntegrationUpstreamError(UNAVAILABLE)

    async def find_profile(self, project_id: UUID) -> str | None:
        status, payload = await self._call(
            "GET", "/profiles", params={"name": profile_name(project_id), "limit": "2"}
        )
        if status != 200 or not isinstance(payload, dict):
            raise IntegrationUpstreamError(UNAVAILABLE)
        profiles = payload.get("profiles")
        if not isinstance(profiles, list):
            raise IntegrationUpstreamError(UNAVAILABLE)
        for profile in profiles:
            if (
                isinstance(profile, dict)
                and profile.get("name") == profile_name(project_id)
                and profile.get("externalId", str(project_id)) == str(project_id)
            ):
                return zernio_id(profile.get("_id"))
        return None

    async def connect_url(self, profile_id: str, *, redirect: str) -> str:
        status, payload = await self._call(
            "GET",
            "/connect/linkedin",
            params={"profileId": zernio_id(profile_id), "scopes": SCOPES, "redirect_url": redirect},
        )
        url = payload.get("authUrl") if status == 200 and isinstance(payload, dict) else None
        if not isinstance(url, str) or len(url) > 4096:
            raise IntegrationUpstreamError(UNAVAILABLE)
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in CONNECT_HOSTS:
            raise IntegrationUpstreamError(UNAVAILABLE)
        return url

    async def accounts(self, profile_id: str) -> list[dict[str, Any]]:
        """The LinkedIn accounts in this profile, and only those."""

        profile_id = zernio_id(profile_id)
        status, payload = await self._call(
            "GET", "/accounts", params={"profileId": profile_id, "platform": "linkedin"}
        )
        if status != 200 or not isinstance(payload, dict):
            raise IntegrationUpstreamError(UNAVAILABLE)
        values = payload.get("accounts")
        if not isinstance(values, list):
            raise IntegrationUpstreamError(UNAVAILABLE)
        accounts = []
        for value in values[:100]:
            owner = value.get("profileId") if isinstance(value, dict) else None
            owner_id = owner.get("_id") if isinstance(owner, dict) else owner
            if owner_id is not None and owner_id != profile_id:
                continue
            account = linkedin_account(value)
            if account is not None:
                accounts.append(account)
        return accounts

    async def disconnect_account(self, account_id: str) -> None:
        status, _ = await self._call("DELETE", f"/accounts/{zernio_id(account_id)}", write=True)
        if status not in {200, 204, 404}:
            raise IntegrationUpstreamError(UNAVAILABLE)

    async def delete_profile(self, profile_id: str) -> None:
        status, _ = await self._call("DELETE", f"/profiles/{zernio_id(profile_id)}", write=True)
        if status not in {200, 204, 404}:
            raise IntegrationAuthorizationError(
                "Zernio kept the profile; disconnect its accounts first"
            )

    async def upload_media(self, *, filename: str, media_type: str, content: bytes) -> str:
        """Put approved bytes in Zernio's storage; returns the public URL a post names.

        An upload publishes nothing, so repeating one after a lost answer leaves only an
        unused file.
        """

        status, payload = await self._call(
            "POST",
            "/media/presign",
            body={"filename": filename, "contentType": media_type, "size": len(content)},
        )
        values = payload if status == 200 and isinstance(payload, dict) else {}
        upload, public = values.get("uploadUrl"), values.get("publicUrl")
        if not _https(upload, suffix=UPLOAD_HOST_SUFFIX) or not _https(public, {MEDIA_HOST}):
            raise IntegrationUpstreamError(UNAVAILABLE)
        try:
            response = await self._client.put(
                upload,
                content=content,
                headers={"Content-Type": media_type, "User-Agent": USER_AGENT},
                follow_redirects=False,
                timeout=MEDIA_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise IntegrationUpstreamError(UNAVAILABLE) from None
        if not 200 <= response.status_code < 300:
            raise IntegrationUpstreamError(UNAVAILABLE)
        return public

    async def create_post(
        self, *, account_id: str, text: str, media: list[dict[str, Any]], idempotency_key: str
    ) -> dict[str, Any]:
        """Publish now as one connected account.

        Zernio returns the original post for a repeated `Idempotency-Key` within 24 hours, so a
        caller holding a receipt resends the same key after a lost answer. A refusal raises
        IntegrationDeliveryRefusedError: nothing was created.
        """

        body: dict[str, Any] = {
            "content": text,
            "platforms": [{"platform": "linkedin", "accountId": zernio_id(account_id)}],
            "publishNow": True,
        }
        if media:
            body["mediaItems"] = [
                {
                    "type": "image",
                    "url": item["url"],
                    **({"altText": item["alt_text"]} if item.get("alt_text") else {}),
                }
                for item in media
            ]
        status, payload = await self._call(
            "POST", "/posts", body=body, idempotency_key=idempotency_key, write=True
        )
        if status in {200, 201, 207} and isinstance(payload, dict):
            return post_state(payload.get("post"))
        code = payload.get("code") if isinstance(payload, dict) else None
        if status == 409 and code == "idempotency_conflict":
            # The first request with this key is still being processed.
            raise IntegrationDeliveryUnknownError(UNAVAILABLE)
        if status == 404:
            raise IntegrationAuthorizationError(
                "The LinkedIn account is no longer connected. "
                "Reconnect LinkedIn posting under Integrations, then preview the post again."
            )
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else None
        logger.warning("Zernio refused a LinkedIn post (status %s, code %s)", status, code)
        raise IntegrationDeliveryRefusedError(
            DUPLICATE if status == 409 else REFUSED, status=status
        )

    async def get_post(self, post_id: str) -> dict[str, Any]:
        status, payload = await self._call("GET", f"/posts/{zernio_id(post_id)}")
        if status != 200 or not isinstance(payload, dict):
            raise IntegrationUpstreamError(UNAVAILABLE)
        try:
            return post_state(payload.get("post"))
        except IntegrationDeliveryUnknownError:
            raise IntegrationUpstreamError(UNAVAILABLE) from None

    async def post_analytics(self, account_id: str, urn: str) -> dict[str, Any] | None:
        """One post's numbers now, read live from LinkedIn; None once the post is gone."""

        if not POST_URN.fullmatch(urn):
            raise ValueError("LinkedIn post URN is invalid")
        status, payload = await self._call(
            "GET",
            f"/accounts/{zernio_id(account_id)}/linkedin-post-analytics",
            params={"urn": urn},
        )
        code = payload.get("code") if isinstance(payload, dict) else None
        if status == 200 and isinstance(payload, dict) and payload.get("postUrn", urn) == urn:
            return metrics(payload.get("analytics"))
        if status == 404 and code == "post_not_found":
            return None
        if status == 404:
            raise IntegrationAuthorizationError(ACCOUNT_GONE)
        if status == 403:
            raise IntegrationAuthorizationError(ANALYTICS_REFUSED)
        # 402 is Tin's own Zernio plan, and anything else is no answer Tin can use.
        logger.warning("Zernio did not return LinkedIn post analytics (status %s)", status)
        raise IntegrationUpstreamError(RESULTS_UNAVAILABLE)

    async def page_posts(self, account_id: str) -> list[dict[str, Any]]:
        """A page's most recent posts on LinkedIn, including those made outside Tin."""

        status, payload = await self._call("GET", f"/accounts/{zernio_id(account_id)}/posts")
        if status == 404:
            raise IntegrationAuthorizationError(ACCOUNT_GONE)
        if status in {401, 403}:
            raise IntegrationAuthorizationError(ANALYTICS_REFUSED)
        values = payload.get("posts") if status == 200 and isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise IntegrationUpstreamError(RESULTS_UNAVAILABLE)
        posts = []
        for value in values[:50]:
            if not isinstance(value, dict):
                continue
            url = value.get("permalink")
            urn = post_urn(value.get("id"), url)
            published = _timestamp(value.get("createdTime"))
            if urn is None or published is None:
                continue
            text = value.get("message")
            posts.append(
                {
                    "urn": urn,
                    "url": url if _https(url, POST_HOSTS) else None,
                    "published_at": published,
                    "text": text[:2000] if isinstance(text, str) else "",
                }
            )
        return posts

    async def follower_counts(self, account_ids: list[str]) -> dict[str, int]:
        """Each account's follower count as Zernio last read it; missing when it has none."""

        ids = [zernio_id(value) for value in account_ids]
        if not ids:
            return {}
        status, payload = await self._call(
            "GET", "/accounts/follower-stats", params={"accountIds": ",".join(ids)}
        )
        values = payload.get("accounts") if status == 200 and isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise IntegrationUpstreamError(RESULTS_UNAVAILABLE)
        counts = {}
        for value in values:
            if not isinstance(value, dict) or value.get("_id") not in ids:
                continue
            followers = value.get("currentFollowers")
            if type(followers) is int and 0 <= followers < 2**31:
                counts[value["_id"]] = followers
        return counts
