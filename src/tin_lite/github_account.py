"""A founder's own GitHub account, connected through Tin's GitHub OAuth App.

The GitHub App (`infra.github`) can only act on repositories that installed it, so it cannot
open a pull request on someone else's awesome list. This connection holds a user token with
the `public_repo` scope instead. Tin uses it for one bounded job: after the founder approves an
exact submission, fork that public list, commit one file change to a branch in the fork and
open one pull request, or open one issue. It never pushes to the founder's own repositories,
comments, stars or deletes anything.

Every write is looked up before it is made (the deterministic branch's pull request, or an
issue with the same title by the same author), so a retried or recovered attempt returns what
GitHub already holds instead of opening a second submission.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID, uuid4

import httpx

from tin_lite.connection_records import open_credential, seal_credential
from tin_lite.integrations import (
    GITHUB_USER_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationUpstreamError,
)

API = "https://api.github.com"
AUTHORIZE = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
SCOPE = "public_repo"
# A classic OAuth scope that includes public_repo also satisfies it.
ACCEPTED_SCOPES = frozenset({"public_repo", "repo"})
CALLBACK = "github-account"
MAX_FILE_BYTES = 1_000_000
FORK_READY_ATTEMPTS = 10
FORK_READY_SECONDS = 3.0


class SubmissionRefused(Exception):
    """GitHub refused a submission for a reason a retry will not change."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def oauth_ready(settings: Any) -> bool:
    return bool(
        getattr(settings, "github_oauth_client_id", None)
        and getattr(settings, "github_oauth_client_secret", None)
    )


def authorization_url(settings: Any, *, state: str, challenge: str, redirect: str) -> str:
    query = urlencode(
        {
            "client_id": settings.github_oauth_client_id,
            "redirect_uri": redirect,
            "scope": SCOPE,
            "state": state,
            "allow_signup": "false",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE}?{query}"


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo(full_name: str) -> str:
    owner, name = full_name.split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise IntegrationUpstreamError("GitHub returned an invalid response") from None


def _safe_html_url(value: Any, *, repository: str, kind: str, number: int) -> str:
    expected = f"/{repository}/{kind}/{number}".lower()
    if (
        isinstance(value, str)
        and urlsplit(value).scheme == "https"
        and urlsplit(value).hostname == "github.com"
        and urlsplit(value).path.lower() == expected
    ):
        return value
    raise IntegrationUpstreamError("GitHub returned an unexpected submission link")


def branch_name(execution_key: str) -> str:
    return f"tin/awesome-{hashlib.sha256(execution_key.encode()).hexdigest()[:12]}"


class GitHubAccounts:
    def __init__(self, integrations: Any) -> None:
        self.integrations = integrations
        self.db = integrations._database

    @property
    def _client(self) -> httpx.AsyncClient:
        return self.integrations._client

    # -- authorization

    async def pending_project(self, *, state: str, clerk_user_id: str) -> UUID:
        attempt = await self._attempt(state, clerk_user_id, include_used=True)
        return attempt.project_id

    async def _attempt(self, state: str, clerk_user_id: str, *, include_used: bool):
        if not state or len(state) > 256:
            raise IntegrationAuthorizationError("connection attempt has expired")
        attempt = await self.db.get_integration_auth_attempt(
            token_hash=hashlib.sha256(state.encode()).hexdigest(),
            provider_key=GITHUB_USER_PROVIDER,
            clerk_user_id=clerk_user_id,
            include_used=include_used,
        )
        if attempt is None:
            raise IntegrationAuthorizationError("connection attempt has expired")
        return attempt

    async def complete(self, *, state: str, code: str, clerk_user_id: str):
        """Exchange the code once; a repeated callback returns the connection it made."""
        integrations = self.integrations
        integrations._require_configured(GITHUB_USER_PROVIDER)
        state_hash = hashlib.sha256((state or "").encode()).hexdigest()
        attempt = await self.db.consume_integration_auth_attempt(
            token_hash=state_hash, provider_key=GITHUB_USER_PROVIDER, clerk_user_id=clerk_user_id
        )
        if attempt is None:
            used = await self._attempt(state, clerk_user_id, include_used=True)
            existing = await self.db.get_integration_connection(
                project_id=used.project_id, provider_key=GITHUB_USER_PROVIDER
            )
            if existing is not None and existing.configuration.get("auth_attempt") == state_hash:
                return existing
            raise IntegrationAuthorizationError("connection attempt has expired")
        if not isinstance(code, str) or not 1 <= len(code) <= 2048:
            raise IntegrationAuthorizationError("GitHub did not return an authorization code")
        if attempt.pkce_verifier_ciphertext is None or integrations._cipher is None:
            raise IntegrationAuthorizationError("GitHub connection attempt is invalid")
        verifier = integrations._cipher.decrypt(
            attempt.pkce_verifier_ciphertext,
            context=f"auth:{attempt.project_id}:{GITHUB_USER_PROVIDER}",
        )
        settings = integrations._settings
        try:
            response = await self._client.post(
                TOKEN_URL,
                data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret.get_secret_value(),
                    "code": code,
                    "redirect_uri": integrations._callback_url(CALLBACK),
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
                follow_redirects=False,
                timeout=20.0,
            )
        except httpx.HTTPError:
            raise IntegrationUpstreamError("GitHub could not be reached; try again") from None
        payload = _json(response)
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if response.is_error or not isinstance(token, str) or not token:
            raise IntegrationAuthorizationError(
                "GitHub did not accept the authorization; connect your GitHub account again"
            )
        scopes = {s.strip() for s in str(payload.get("scope") or "").split(",") if s.strip()}
        if not scopes & ACCEPTED_SCOPES:
            raise IntegrationAuthorizationError(
                "GitHub did not grant public repository access; connect again and approve it"
            )
        user = await self._identity(token)
        ciphertext, version = seal_credential(
            integrations,
            project_id=attempt.project_id,
            provider_key=GITHUB_USER_PROVIDER,
            value=token,
        )
        connection = await self.db.upsert_integration_connection(
            project_id=attempt.project_id,
            provider_key=GITHUB_USER_PROVIDER,
            external_account_id=str(user["id"]),
            external_account_label=f"@{user['login']}",
            configuration={
                "login": user["login"],
                "granted_scopes": sorted(scopes),
                "granted_capabilities": list(
                    integrations._definition(GITHUB_USER_PROVIDER).capabilities
                ),
                "auth_attempt": state_hash,
            },
            credential_ciphertext=ciphertext,
            credential_key_version=version,
            connected_by_clerk_user_id=clerk_user_id,
        )
        await integrations._record_activity(
            connection,
            "integration_connected",
            f"GitHub account @{user['login']} connected for approved list submissions.",
            suffix=str(uuid4()),
        )
        return connection

    async def _identity(self, token: str) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{API}/user", headers=_headers(token), timeout=20)
        except httpx.HTTPError:
            raise IntegrationUpstreamError("GitHub could not be reached; try again") from None
        payload = _json(response)
        if (
            response.is_error
            or not isinstance(payload, dict)
            or not isinstance(payload.get("id"), int)
            or not isinstance(payload.get("login"), str)
        ):
            raise IntegrationAuthorizationError("GitHub did not identify the signed-in account")
        return {"id": payload["id"], "login": payload["login"]}

    async def revoke(self, connection: Any) -> None:
        settings = self.integrations._settings
        try:
            token = open_credential(self.integrations, connection)
            # Only this connection's token. The grant is the founder's whole authorization of
            # Tin's app, shared by every project they connected with the same account;
            # deleting it would disconnect those projects too.
            await self._client.request(
                "DELETE",
                f"{API}/applications/{settings.github_oauth_client_id}/token",
                json={"access_token": token},
                auth=(
                    settings.github_oauth_client_id,
                    settings.github_oauth_client_secret.get_secret_value(),
                ),
                headers={"Accept": "application/vnd.github+json"},
                timeout=10.0,
            )
        except Exception:  # noqa: BLE001 — local disconnection is authoritative
            return

    # -- bounded operations

    async def connection(self, project_id: UUID):
        self.integrations._require_configured(GITHUB_USER_PROVIDER)
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=GITHUB_USER_PROVIDER
        )
        if connection is None:
            raise IntegrationAuthorizationError("Connect your GitHub account first")
        if connection.status != "connected":
            raise IntegrationAuthorizationError("Reconnect your GitHub account")
        return connection

    async def _call(self, connection, method: str, path: str, **request) -> httpx.Response:
        token = open_credential(self.integrations, connection)
        try:
            response = await self._client.request(
                method,
                f"{API}{path}",
                headers=_headers(token),
                follow_redirects=False,
                timeout=30.0,
                **request,
            )
        except httpx.HTTPError:
            raise IntegrationUpstreamError("GitHub could not be reached; try again") from None
        if response.status_code == 401:
            await self.db.mark_integration_attention(
                project_id=connection.project_id,
                provider_key=GITHUB_USER_PROVIDER,
                error_code="reauthorization_required",
            )
            raise IntegrationAuthorizationError("Reconnect your GitHub account")
        if response.status_code == 429 or (
            response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise IntegrationUpstreamError("GitHub rate limited the request; try again later")
        if response.status_code >= 500:
            raise IntegrationUpstreamError(
                f"GitHub could not complete the request ({response.status_code})"
            )
        return response

    async def repository(self, connection, full_name: str) -> dict[str, Any]:
        response = await self._call(connection, "GET", f"/repos/{_repo(full_name)}")
        if response.status_code == 404:
            raise SubmissionRefused("The list's repository was not found.")
        value = _json(response)
        if response.is_error or not isinstance(value, dict):
            raise IntegrationUpstreamError("GitHub did not return the list's repository")
        if value.get("private"):
            raise SubmissionRefused("The list's repository is not public.")
        if value.get("archived"):
            raise SubmissionRefused("The list's repository is archived.")
        branch = value.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise IntegrationUpstreamError("GitHub did not name the list's default branch")
        return {
            "full_name": value.get("full_name") or full_name,
            "default_branch": branch,
            "has_issues": bool(value.get("has_issues")),
        }

    async def _head(self, connection, full_name: str, branch: str) -> str | None:
        response = await self._call(
            connection,
            "GET",
            f"/repos/{_repo(full_name)}/git/ref/heads/{quote(branch, safe='/')}",
        )
        if response.status_code in {404, 409}:
            return None
        value = _json(response)
        sha = (value.get("object") or {}).get("sha") if isinstance(value, dict) else None
        if response.is_error or not isinstance(sha, str):
            raise IntegrationUpstreamError("GitHub did not return the branch head")
        return sha

    async def _file(self, connection, full_name: str, path: str, ref: str) -> dict | None:
        response = await self._call(
            connection,
            "GET",
            f"/repos/{_repo(full_name)}/contents/{quote(path)}",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        value = _json(response)
        if response.is_error or not isinstance(value, dict) or value.get("type") != "file":
            raise SubmissionRefused("The list file is not a single file.")
        if not isinstance(value.get("size"), int) or value["size"] > MAX_FILE_BYTES:
            raise SubmissionRefused("The list file is larger than Tin edits.")
        if value.get("encoding") != "base64" or not isinstance(value.get("content"), str):
            raise SubmissionRefused("The list file could not be read.")
        try:
            content = base64.b64decode(value["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise SubmissionRefused("The list file is not UTF-8 text.") from None
        return {"content": content, "blob_sha": value.get("sha")}

    async def list_file(self, connection, full_name: str, path: str) -> dict[str, Any]:
        """The list file at the default branch's current head, pinned to that commit."""
        repository = await self.repository(connection, full_name)
        head = await self._head(connection, full_name, repository["default_branch"])
        if head is None:
            raise SubmissionRefused("The list's default branch was not found.")
        found = await self._file(connection, full_name, path, head)
        if found is None:
            raise SubmissionRefused(f"The list has no {path}.")
        return {**repository, **found, "base_commit": head}

    async def _existing_pull(self, connection, upstream: str, login: str, branch: str):
        response = await self._call(
            connection,
            "GET",
            f"/repos/{_repo(upstream)}/pulls",
            params={"head": f"{login}:{branch}", "state": "all", "per_page": 5},
        )
        values = _json(response)
        if response.is_error or not isinstance(values, list):
            raise IntegrationUpstreamError("GitHub did not list the list's pull requests")
        for value in values:
            if isinstance(value, dict) and isinstance(value.get("number"), int):
                return {
                    "number": value["number"],
                    "url": _safe_html_url(
                        value.get("html_url"),
                        repository=upstream,
                        kind="pull",
                        number=value["number"],
                    ),
                }
        return None

    async def _fork(self, connection, upstream: str) -> str:
        response = await self._call(
            connection,
            "POST",
            f"/repos/{_repo(upstream)}/forks",
            json={"default_branch_only": True},
        )
        if response.status_code in {403, 404, 422}:
            raise SubmissionRefused("GitHub did not allow forking this list.")
        value = _json(response)
        fork = value.get("full_name") if isinstance(value, dict) else None
        if response.is_error or not isinstance(fork, str) or "/" not in fork:
            raise IntegrationUpstreamError("GitHub did not return the fork")
        for attempt in range(FORK_READY_ATTEMPTS):
            ready = await self._call(connection, "GET", f"/repos/{_repo(fork)}")
            if ready.status_code == 200:
                return fork
            await asyncio.sleep(FORK_READY_SECONDS * (attempt + 1) / 2)
        raise IntegrationUpstreamError("GitHub is still creating the fork; try again")

    async def _branch(self, connection, fork: str, branch: str, base: str, default: str) -> None:
        body = {"ref": f"refs/heads/{branch}", "sha": base}
        for attempt in range(2):
            response = await self._call(
                connection, "POST", f"/repos/{_repo(fork)}/git/refs", json=body
            )
            if response.status_code == 201:
                return
            if response.status_code == 422 and await self._head(connection, fork, branch):
                return
            if attempt == 0 and response.status_code == 422:
                # An older fork may not have the list's newest commit yet.
                await self._call(
                    connection,
                    "POST",
                    f"/repos/{_repo(fork)}/merge-upstream",
                    json={"branch": default},
                )
                continue
        raise IntegrationUpstreamError("GitHub did not create the submission branch")

    async def submit_pull_request(
        self,
        connection,
        *,
        execution_key: str,
        upstream: str,
        default_branch: str,
        base_commit: str,
        path: str,
        content: str,
        title: str,
        body: str,
        commit_message: str,
    ) -> dict[str, Any]:
        login = connection.configuration.get("login")
        if not isinstance(login, str) or not login:
            raise IntegrationAuthorizationError("Reconnect your GitHub account")
        branch = branch_name(execution_key)
        existing = await self._existing_pull(connection, upstream, login, branch)
        if existing:
            return {**existing, "branch": branch, "recovered": True}
        fork = await self._fork(connection, upstream)
        await self._branch(connection, fork, branch, base_commit, default_branch)
        current = await self._file(connection, fork, path, branch)
        if current is None:
            raise SubmissionRefused(f"The fork has no {path}.")
        if current["content"] != content:
            response = await self._call(
                connection,
                "PUT",
                f"/repos/{_repo(fork)}/contents/{quote(path)}",
                json={
                    "message": commit_message,
                    "content": base64.b64encode(content.encode()).decode(),
                    "sha": current["blob_sha"],
                    "branch": branch,
                },
            )
            if response.status_code not in {200, 201}:
                raise IntegrationUpstreamError("GitHub did not save the list change")
        response = await self._call(
            connection,
            "POST",
            f"/repos/{_repo(upstream)}/pulls",
            json={
                "title": title,
                "body": body,
                "head": f"{login}:{branch}",
                "base": default_branch,
                "maintainer_can_modify": True,
            },
        )
        if response.status_code == 422:
            existing = await self._existing_pull(connection, upstream, login, branch)
            if existing:
                return {**existing, "branch": branch, "recovered": True}
            raise SubmissionRefused("GitHub refused the pull request.")
        if response.status_code in {403, 404}:
            raise SubmissionRefused("The list does not accept pull requests from this account.")
        value = _json(response)
        if (
            response.is_error
            or not isinstance(value, dict)
            or not isinstance(value.get("number"), int)
        ):
            raise IntegrationUpstreamError("GitHub did not confirm the pull request")
        return {
            "number": value["number"],
            "url": _safe_html_url(
                value.get("html_url"), repository=upstream, kind="pull", number=value["number"]
            ),
            "branch": branch,
            "fork": fork,
        }

    async def _existing_issue(self, connection, upstream: str, login: str, title: str):
        response = await self._call(
            connection,
            "GET",
            f"/repos/{_repo(upstream)}/issues",
            params={"creator": login, "state": "all", "per_page": 50},
        )
        values = _json(response)
        if response.is_error or not isinstance(values, list):
            raise IntegrationUpstreamError("GitHub did not list the list's issues")
        for value in values:
            if (
                isinstance(value, dict)
                and value.get("title") == title
                and "pull_request" not in value
                and isinstance(value.get("number"), int)
            ):
                return {
                    "number": value["number"],
                    "url": _safe_html_url(
                        value.get("html_url"),
                        repository=upstream,
                        kind="issues",
                        number=value["number"],
                    ),
                }
        return None

    async def open_issue(self, connection, *, upstream: str, title: str, body: str):
        login = connection.configuration.get("login")
        if not isinstance(login, str) or not login:
            raise IntegrationAuthorizationError("Reconnect your GitHub account")
        existing = await self._existing_issue(connection, upstream, login, title)
        if existing:
            return {**existing, "recovered": True}
        response = await self._call(
            connection,
            "POST",
            f"/repos/{_repo(upstream)}/issues",
            json={"title": title, "body": body},
        )
        if response.status_code in {403, 404, 410, 422}:
            raise SubmissionRefused("The list does not accept issues from this account.")
        value = _json(response)
        if (
            response.is_error
            or not isinstance(value, dict)
            or not isinstance(value.get("number"), int)
        ):
            raise IntegrationUpstreamError("GitHub did not confirm the issue")
        return {
            "number": value["number"],
            "url": _safe_html_url(
                value.get("html_url"), repository=upstream, kind="issues", number=value["number"]
            ),
        }
