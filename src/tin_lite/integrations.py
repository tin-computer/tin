from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import tarfile
import tempfile
import time
import zlib
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID, uuid4

import httpx
from anyio import Path as AsyncPath
from anyio import to_thread
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tin_lite.db import Database
from tin_lite.domain import (
    IntegrationAuthAttempt,
    IntegrationConnection,
    ProjectTestIdentity,
    SideEffectConflictError,
    Workflow,
)
from tin_lite.email_outreach import build_email_message, campaign_message_id
from tin_lite.repository_limits import (
    REPOSITORY_BLOB_FALLBACKS,
    REPOSITORY_DOWNLOAD_MAX_BYTES,
    REPOSITORY_MAX_BYTES,
    REPOSITORY_MAX_FILES,
)
from tin_lite.settings import Settings

logger = logging.getLogger(__name__)

GSC_PROVIDER = "analytics.gsc"
GITHUB_PROVIDER = "infra.github"
GOOGLE_WORKSPACE_PROVIDER = "workspace.google"
ADS_PROVIDER = "ads.google"
STRIPE_PROVIDER = "payments.stripe"
POSTHOG_PROVIDER = "analytics.posthog"
PROVIDER_KEYS = frozenset(
    {
        GSC_PROVIDER,
        GITHUB_PROVIDER,
        GOOGLE_WORKSPACE_PROVIDER,
        ADS_PROVIDER,
        STRIPE_PROVIDER,
        POSTHOG_PROVIDER,
    }
)
# Read-only Stripe capabilities; each names the Stripe resources its operation reads.
STRIPE_CAPABILITIES = (
    "subscriptions.read",
    "customers.read",
    "invoices.read",
    "prices.read",
    "charges.read",
)
# Read-only PostHog capabilities for the one project the founder selects.
POSTHOG_CAPABILITIES = ("query.read", "definitions.read", "insights.read")
ADS_CAPABILITIES = ("account.read", "campaigns.read", "campaigns.write")
# Google's ManagerLinkStatus values, lower-cased for the connection's configuration.
ADS_LINK_STATES = {
    "ACTIVE": "active",
    "PENDING": "pending",
    "REFUSED": "refused",
    "CANCELED": "canceled",
    "INACTIVE": "inactive",
}
ADS_LINK_MESSAGES = {
    "ManagerLinkError.TOO_MANY_INVITES": (
        "Google Ads refused the invitation: this account already has too many open manager "
        "invitations. Decline an old one in Google Ads, then try again."
    ),
    "ManagerLinkError.CLIENT_HAS_NO_ADMIN_USER": (
        "Google Ads refused the invitation: the account has no admin user to accept it."
    ),
    "ManagerLinkError.ACCOUNTS_NOT_COMPATIBLE_FOR_LINKING": (
        "Google Ads refused the invitation: that account cannot be linked to a manager account."
    ),
    "ManagerLinkError.TOO_MANY_ACCOUNTS": (
        "Tin's manager account cannot take another client account right now."
    ),
    "ManagerLinkError.SUSPENDED_ACCOUNT_CANNOT_ADD_CLIENTS": (
        "Tin's manager account cannot send invitations right now."
    ),
    "RequestError.INVALID_CUSTOMER_ID": "That is not a Google Ads customer id.",
    "AuthorizationError.USER_PERMISSION_DENIED": (
        "Tin's manager account is not allowed to invite that customer id."
    ),
}
ADS_ALREADY_LINKED = {
    "ManagerLinkError.ALREADY_INVITED_BY_THIS_MANAGER",
    "ManagerLinkError.ALREADY_MANAGED_BY_THIS_MANAGER",
    "ManagerLinkError.ALREADY_MANAGED_IN_HIERARCHY",
}
GOOGLE_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GOOGLE_IDENTITY_SCOPES = frozenset({"openid", "email", "profile"})
WORKSPACE_CAPABILITY_SCOPES = {
    "gmail.messages.read": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail.history.read": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail.messages.send": "https://www.googleapis.com/auth/gmail.send",
    "calendar.events.read": "https://www.googleapis.com/auth/calendar.events.readonly",
}
WORKSPACE_DEFAULT_CAPABILITIES = (
    "gmail.messages.read",
    "gmail.history.read",
    "gmail.messages.send",
    "calendar.events.read",
)
GITHUB_OPEN_PULL_REQUEST_LIMIT = 20
GITHUB_OPEN_PULL_REQUEST_FILE_LIMIT = 100
GITHUB_OPEN_PULL_REQUEST_EVIDENCE_MAX_BYTES = 250_000
GSC_FILTER_DIMENSIONS = frozenset({"query", "page", "country", "device", "searchAppearance"})
GSC_FILTER_OPERATORS = frozenset(
    {"equals", "notEquals", "contains", "notContains", "includingRegex", "excludingRegex"}
)
GSC_MAX_FILTERS = 5
GSC_MAX_START_ROW = 100_000
# Serialized size (json.dumps defaults, as service bounds measure) of the smallest row Search
# Console can return. A response within a byte bound has at most bound // this many rows.
GSC_MIN_ROW_BYTES = 70


class IntegrationError(RuntimeError):
    """A safe integration failure that may be shown to a project member."""


class IntegrationNotConfiguredError(IntegrationError):
    pass


class IntegrationAuthorizationError(IntegrationError):
    pass


class IntegrationUpstreamError(IntegrationError):
    pass


class IntegrationInputError(IntegrationError):
    """The person's own input was rejected before anything was stored."""


class ServiceResponseTooLarge(IntegrationError):
    """A received response exceeded its declared byte bound; the outcome is known, not uncertain."""


class ServiceCallRefused(IntegrationError):
    """The provider answered and refused one read: a known outcome with a Tin-authored message.

    The service gateway settles the step with `code` instead of treating it as uncertain, so a
    later step may try again. Messages never carry provider bodies or credentials.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class IntegrationRateLimitedError(ServiceCallRefused):
    """The provider rate-limited a read; retry it later under a new step."""

    def __init__(
        self, message: str, *, reason: str | None = None, retry_after: int | None = None
    ) -> None:
        super().__init__(message, code="rate_limited")
        self.reason, self.retry_after = reason, retry_after


class GitHubInstallationRequiredError(IntegrationAuthorizationError):
    """The authorizing user has no installation of the Tin app; send them to install it."""

    def __init__(self, message: str, *, install_url: str, project_id: UUID) -> None:
        super().__init__(message)
        self.install_url, self.project_id = install_url, project_id


class GitHubInstallationChoiceError(IntegrationAuthorizationError):
    """The authorizing user can reach several installations; one must be chosen explicitly."""

    def __init__(self, message: str, *, choices: list[dict[str, Any]], project_id: UUID) -> None:
        super().__init__(message)
        self.choices, self.project_id = choices, project_id


class GoogleAdsCallError(IntegrationUpstreamError):
    """A Google Ads request failed; `code` is the opaque error code, never provider text."""

    def __init__(self, code: str) -> None:
        super().__init__("Google Ads did not accept the request.")
        self.code = code


class IntegrationDeliveryUnknownError(IntegrationUpstreamError):
    """The provider call may have succeeded, so automatic replay must fail closed."""


@dataclass(frozen=True)
class IntegrationDefinition:
    key: str
    name: str
    badge: str
    description: str
    access_label: str
    capabilities: tuple[str, ...]
    unlocks: tuple[str, ...]
    # Where the founder creates the credential to paste, for key-entry providers.
    setup_url: str | None = None


@dataclass(frozen=True)
class IntegrationRequirement:
    """One immutable workflow dependency on project-owned provider capabilities."""

    provider_key: str
    capabilities: tuple[str, ...]
    required: bool = True

    def definition(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "capabilities": list(self.capabilities),
            "required": self.required,
        }


@dataclass(frozen=True)
class ConnectStart:
    authorization_url: str


@dataclass(frozen=True)
class ProviderOption:
    id: str
    label: str
    detail: str | None = None


@dataclass(frozen=True)
class GitHubFileChange:
    path: str
    content: str


@dataclass(frozen=True)
class GitHubPullRequestResult:
    repository: str
    branch: str
    number: int
    url: str


@dataclass(frozen=True)
class GitHubCommitResult:
    repository: str
    branch: str
    commit: str
    url: str


@dataclass(frozen=True)
class GitHubRepositoryFile:
    path: str
    content: str


@dataclass(frozen=True)
class GitHubRepositorySnapshot:
    repository: str
    default_branch: str
    head_sha: str
    files: tuple[GitHubRepositoryFile, ...]


@dataclass(frozen=True)
class GitHubRepositoryBinding:
    connection_id: UUID
    installation_id: int
    repository_id: int
    repository: str
    default_branch: str
    head_sha: str


@dataclass(frozen=True)
class GitHubRepositoryBundle:
    repository: str
    default_branch: str
    head_sha: str
    archive: bytes
    file_count: int
    complete: bool = True


@dataclass(frozen=True)
class GitHubOpenPullRequestEvidence:
    document: bytes
    pull_request_count: int
    file_count: int
    changed_paths: tuple[str, ...]
    truncated: bool


def registered_integrations() -> tuple[IntegrationDefinition, ...]:
    return (
        IntegrationDefinition(
            key=GSC_PROVIDER,
            name="Google Search Console",
            badge="SC",
            description="Read verified search performance and indexing signals.",
            access_label="Read only",
            capabilities=("sites.list", "search_analytics.read"),
            unlocks=("AI visibility evidence", "Weekly search brief"),
        ),
        IntegrationDefinition(
            key=GITHUB_PROVIDER,
            name="GitHub",
            badge="GH",
            description="Work on selected repositories through a narrowly scoped GitHub App.",
            access_label="Repository + pull-request write",
            capabilities=(
                "repositories.list",
                "contents.read",
                "contents.write",
                "pull_requests.read",
                "pull_requests.write",
            ),
            unlocks=("Repository-aware tasks", "Reviewable pull requests"),
        ),
        IntegrationDefinition(
            key=GOOGLE_WORKSPACE_PROVIDER,
            name="Google Workspace",
            badge="GW",
            description="Research relationships in Gmail and Calendar, then send approved email.",
            access_label="Gmail read + send · Calendar read",
            capabilities=tuple(WORKSPACE_CAPABILITY_SCOPES),
            unlocks=("Email shortlist", "Approved email campaigns"),
        ),
        IntegrationDefinition(
            key=ADS_PROVIDER,
            name="Google Ads",
            badge="GA",
            description=(
                "Link your Google Ads account to Tin's manager account so Tin can launch "
                "and look after one Search campaign you approve."
            ),
            access_label="Campaign read + write through Tin's manager account",
            capabilities=ADS_CAPABILITIES,
            unlocks=("Google Ads launch", "Google Ads monitor"),
        ),
        IntegrationDefinition(
            key=STRIPE_PROVIDER,
            name="Stripe",
            badge="ST",
            description=(
                "Read subscriptions, customers, invoices, prices and charges through a "
                "restricted key you create in Stripe with read permissions only."
            ),
            access_label="Read only · restricted key",
            capabilities=STRIPE_CAPABILITIES,
            unlocks=("Revenue and churn evidence", "Paying-customer research"),
            setup_url=_stripe_create_key_url(),
        ),
        IntegrationDefinition(
            key=POSTHOG_PROVIDER,
            name="PostHog",
            badge="PH",
            description=(
                "Run bounded HogQL reads and list event, property and insight definitions "
                "in one PostHog project you choose."
            ),
            access_label="Read only · one project",
            capabilities=POSTHOG_CAPABILITIES,
            unlocks=("Activation and funnel evidence", "Product analytics brief"),
        ),
    )


def _stripe_create_key_url() -> str:
    from tin_lite.stripe_connection import create_key_url

    return create_key_url()


def parse_integration_requirements(value: Any) -> tuple[IntegrationRequirement, ...]:
    """Validate the deliberately small integration contract stored in a workflow definition."""

    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("workflow integration_requirements must be a list")
    definitions = {item.key: item for item in registered_integrations()}
    requirements: list[IntegrationRequirement] = []
    seen_providers: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "provider_key",
            "capabilities",
            "required",
        }:
            raise ValueError("workflow integration requirement has unsupported fields")
        provider_key = item.get("provider_key")
        raw_capabilities = item.get("capabilities")
        required = item.get("required")
        from tin_lite.project_connections import CUSTOM_KEY, custom_definition

        if isinstance(provider_key, str) and CUSTOM_KEY.fullmatch(provider_key):
            definitions[provider_key] = custom_definition(provider_key)
        if not isinstance(provider_key, str) or provider_key not in definitions:
            raise ValueError("workflow integration requirement names an unknown provider")
        if provider_key in seen_providers:
            raise ValueError("workflow integration requirements contain a duplicate provider")
        if (
            not isinstance(raw_capabilities, list)
            or not raw_capabilities
            or any(not isinstance(capability, str) for capability in raw_capabilities)
            or len(set(raw_capabilities)) != len(raw_capabilities)
        ):
            raise ValueError("workflow integration capabilities must be a non-empty unique list")
        unknown = set(raw_capabilities) - set(definitions[provider_key].capabilities)
        if unknown:
            capability = sorted(unknown)[0]
            raise ValueError(
                f"workflow integration requirement names unsupported capability {capability}"
            )
        if not isinstance(required, bool):
            raise ValueError("workflow integration requirement required must be a boolean")
        requirements.append(
            IntegrationRequirement(
                provider_key=provider_key,
                capabilities=tuple(raw_capabilities),
                required=required,
            )
        )
        seen_providers.add(provider_key)
    return tuple(requirements)


async def load_pinned_integration_requirements(
    *,
    storage: Any,
    workflow: Workflow,
    commit_sha: str,
) -> tuple[IntegrationRequirement, ...]:
    """Resolve dependencies from the exact definition revision pinned by a run."""

    if workflow.current_commit_sha == commit_sha:
        definition = workflow.definition
    else:
        from tin_lite.workflow_packages import load_workflow_source

        source = await load_workflow_source(
            storage=storage,
            repo_id=workflow.definition_repo_id,
            commit_sha=commit_sha,
            definition_path=workflow.definition_path,
        )
        definition = source.definition
        if definition.get("key") != workflow.key:
            raise ValueError("pinned workflow definition key does not match its catalog row")
    return parse_integration_requirements(definition.get("integration_requirements"))


class CredentialCipher:
    version = "v1"

    def __init__(self, encoded_key: str) -> None:
        try:
            padded = encoded_key + "=" * (-len(encoded_key) % 4)
            key = base64.urlsafe_b64decode(padded)
        except (ValueError, TypeError) as exc:
            raise ValueError("integration credential key must be URL-safe base64") from exc
        if len(key) != 32:
            raise ValueError("integration credential key must decode to exactly 32 bytes")
        self._cipher = AESGCM(key)
        self.key_id = hashlib.sha256(key).hexdigest()[:24]

    def encrypt(self, value: str, *, context: str) -> bytes:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, value.encode(), context.encode())
        return nonce + ciphertext

    def decrypt(self, value: bytes, *, context: str) -> str:
        if len(value) < 29:
            raise IntegrationAuthorizationError("stored integration credential is invalid")
        try:
            plaintext = self._cipher.decrypt(value[:12], value[12:], context.encode())
        except Exception as exc:
            raise IntegrationAuthorizationError(
                "stored integration credential could not be opened"
            ) from exc
        return plaintext.decode()


class IntegrationService:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        google_ads: Any = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._google_ads = google_ads
        self._cipher = (
            CredentialCipher(settings.integration_credential_key.get_secret_value())
            if settings.integration_credential_key is not None
            else None
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def seal_test_identity_password(
        self, *, project_id: UUID, identity_id: UUID, password: str
    ) -> tuple[bytes, str]:
        """Encrypt a switchboard-minted product password; it never leaves Tin in clear."""
        if self._cipher is None:
            raise IntegrationNotConfiguredError("integration credential key is not configured")
        ciphertext = self._cipher.encrypt(
            password, context=_test_identity_context(project_id, identity_id)
        )
        return ciphertext, self._cipher.version

    def open_test_identity_password(self, identity: ProjectTestIdentity) -> str:
        if self._cipher is None:
            raise IntegrationNotConfiguredError("integration credential key is not configured")
        if identity.password_ciphertext is None:
            raise IntegrationAuthorizationError("test identity has no stored password")
        return self._cipher.decrypt(
            identity.password_ciphertext,
            context=_test_identity_context(identity.project_id, identity.id),
        )

    def is_configured(self, provider_key: str) -> bool:
        self._definition(provider_key)
        if provider_key.startswith("custom.api.") or provider_key == STRIPE_PROVIDER:
            return self._cipher is not None
        if provider_key in {GSC_PROVIDER, GOOGLE_WORKSPACE_PROVIDER}:
            return bool(
                self._cipher
                and self._settings.google_oauth_client_id
                and self._settings.google_oauth_client_secret
            )
        if provider_key == POSTHOG_PROVIDER:
            from tin_lite.posthog_connection import oauth_ready

            return self._cipher is not None and oauth_ready(self._settings)
        if provider_key == ADS_PROVIDER:
            from tin_lite.google_ads import manager_oauth_client

            client_id, client_secret = manager_oauth_client(self._settings)
            return bool(
                client_id
                and client_secret is not None
                and getattr(self._settings, "google_ads_manager_customer_id", None)
                and getattr(self._settings, "google_ads_manager_refresh_token", None)
            )
        return bool(
            self._settings.github_app_slug
            and self._settings.github_app_id
            and self._settings.github_app_client_id
            and self._settings.github_app_client_secret
            and self._settings.github_app_private_key_path
            and self._settings.github_app_private_key_path.is_file()
            and self._settings.github_webhook_secret
        )

    async def list_connections(self, project_id: UUID) -> list[IntegrationConnection]:
        return await self._database.list_integration_connections(project_id)

    @property
    def custom(self):
        from tin_lite.project_connections import ProjectConnections

        return ProjectConnections(self)

    @property
    def stripe(self):
        from tin_lite.stripe_connection import StripeConnections

        return StripeConnections(self)

    @property
    def posthog(self):
        from tin_lite.posthog_connection import PostHogConnections

        return PostHogConnections(self)

    def definitions(self, connections):
        from tin_lite.project_connections import CUSTOM_KEY, custom_definition

        return (
            *registered_integrations(),
            *(
                custom_definition(c.provider_key)
                for c in connections
                if CUSTOM_KEY.fullmatch(c.provider_key)
            ),
        )

    async def ensure_requirements(
        self,
        *,
        project_id: UUID,
        requirements: tuple[IntegrationRequirement, ...],
    ) -> None:
        """Fail closed before a run starts when a required project capability is unavailable."""

        for requirement in requirements:
            if not requirement.required:
                continue
            from tin_lite.project_connections import CUSTOM_KEY

            if CUSTOM_KEY.fullmatch(requirement.provider_key):
                await self.custom.ready(project_id, requirement)
                continue
            self._require_configured(requirement.provider_key)
            connection = await self._database.get_integration_connection(
                project_id=project_id,
                provider_key=requirement.provider_key,
            )
            definition = self._definition(requirement.provider_key)
            if connection is None:
                raise IntegrationAuthorizationError(
                    f"Connect {definition.name} before starting this workflow"
                )
            if connection.status != "connected":
                raise IntegrationAuthorizationError(
                    f"{definition.name} needs attention before starting this workflow"
                )
            if requirement.provider_key == GSC_PROVIDER:
                if "search_analytics.read" in requirement.capabilities and not _selected_string(
                    connection, "selected_site_url"
                ):
                    raise IntegrationAuthorizationError(
                        "Choose a Search Console property before starting this workflow"
                    )
                continue
            if requirement.provider_key == GOOGLE_WORKSPACE_PROVIDER:
                granted = connection.configuration.get("granted_capabilities", [])
                if not isinstance(granted, list) or not set(requirement.capabilities).issubset(
                    {item for item in granted if isinstance(item, str)}
                ):
                    raise IntegrationAuthorizationError(
                        "Google Workspace needs additional permission before starting this workflow"
                    )
                continue
            if requirement.provider_key == STRIPE_PROVIDER:
                missing = set(requirement.capabilities) - _granted(connection)
                if connection.credential_ciphertext is None or missing:
                    raise IntegrationAuthorizationError(
                        "Stripe's restricted key does not allow "
                        + ", ".join(sorted(missing) or ["these reads"])
                        + "; add the read permission in Stripe, then press Check again"
                    )
                continue
            if requirement.provider_key == POSTHOG_PROVIDER:
                if not _selected_string(connection, "selected_project_id"):
                    raise IntegrationAuthorizationError(
                        "Choose a PostHog project before starting this workflow"
                    )
                missing = set(requirement.capabilities) - _granted(connection)
                if connection.credential_ciphertext is None or missing:
                    raise IntegrationAuthorizationError(
                        "PostHog did not grant "
                        + ", ".join(sorted(missing) or ["these reads"])
                        + "; reconnect PostHog and approve the read access"
                    )
                continue
            if requirement.provider_key == ADS_PROVIDER:
                link = connection.configuration.get("link_status")
                if link != "active":
                    raise IntegrationAuthorizationError(
                        "Accept Tin's manager request in Google Ads before starting this workflow"
                        if link == "pending"
                        else "Reconnect Google Ads before starting this workflow"
                    )
                if (
                    "campaigns.write" in requirement.capabilities
                    and connection.configuration.get("write_opted_in") is not True
                ):
                    raise IntegrationAuthorizationError(
                        "Google Ads campaign changes must be explicitly enabled for this workflow"
                    )
                continue
            if any(
                capability in requirement.capabilities
                for capability in (
                    "contents.read",
                    "contents.write",
                    "pull_requests.read",
                    "pull_requests.write",
                )
            ) and not _selected_string(connection, "selected_repository"):
                raise IntegrationAuthorizationError(
                    "Choose a GitHub repository before starting this workflow"
                )
            if any(
                capability in requirement.capabilities
                for capability in ("contents.write", "pull_requests.write")
            ):
                permissions = connection.configuration.get("permissions")
                if (
                    connection.configuration.get("write_opted_in") is not True
                    or not isinstance(permissions, dict)
                    or permissions.get("contents") != "write"
                    or permissions.get("pull_requests") != "write"
                ):
                    raise IntegrationAuthorizationError(
                        "GitHub write access must be explicitly enabled for this workflow"
                    )

    async def start_connect(
        self,
        *,
        project_id: UUID,
        provider_key: str,
        clerk_user_id: str,
        capabilities: tuple[str, ...] | list[str] | None = None,
    ) -> ConnectStart:
        if provider_key.startswith("custom.api."):
            raise IntegrationError("Use the secure Custom API form in project Integrations.")
        self._require_configured(provider_key)
        definition = self._definition(provider_key)
        if provider_key == STRIPE_PROVIDER:
            # A restricted key is pasted only in Tin's own page, never through chat or MCP.
            from tin_lite.product_urls import dashboard_url

            return ConnectStart(
                authorization_url=(
                    f"{dashboard_url(self._settings)}/connect?project={project_id}"
                    f"&providers={STRIPE_PROVIDER}"
                )
            )
        if capabilities is None:
            requested_capabilities = (
                WORKSPACE_DEFAULT_CAPABILITIES
                if provider_key == GOOGLE_WORKSPACE_PROVIDER
                else definition.capabilities
            )
        else:
            requested_capabilities = tuple(dict.fromkeys(capabilities))
            if not requested_capabilities or not set(requested_capabilities).issubset(
                set(definition.capabilities)
            ):
                raise IntegrationAuthorizationError(
                    "requested integration capabilities are empty or unsupported"
                )
        if provider_key == GOOGLE_WORKSPACE_PROVIDER:
            existing = await self._database.get_integration_connection(
                project_id=project_id, provider_key=provider_key
            )
            if existing is not None:
                granted = existing.configuration.get("granted_capabilities", [])
                if isinstance(granted, list):
                    requested_capabilities = tuple(
                        dict.fromkeys(
                            [
                                *[item for item in granted if isinstance(item, str)],
                                *requested_capabilities,
                            ]
                        )
                    )
        pkce = {GSC_PROVIDER, GOOGLE_WORKSPACE_PROVIDER, POSTHOG_PROVIDER}
        state = secrets.token_urlsafe(32)
        state_hash = _sha256(state)
        verifier_ciphertext = None
        if provider_key in pkce:
            verifier = secrets.token_urlsafe(64)
            assert self._cipher is not None
            verifier_ciphertext = self._cipher.encrypt(
                verifier, context=f"auth:{project_id}:{provider_key}"
            )
        await self._database.create_integration_auth_attempt(
            token_hash=state_hash,
            project_id=project_id,
            provider_key=provider_key,
            clerk_user_id=clerk_user_id,
            pkce_verifier_ciphertext=verifier_ciphertext,
            requested_capabilities=requested_capabilities,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        if provider_key in pkce:
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
        if provider_key == POSTHOG_PROVIDER:
            from tin_lite.posthog_connection import authorization_url

            return ConnectStart(
                authorization_url=authorization_url(
                    self._settings, state=state, challenge=challenge
                )
            )
        if provider_key in {GSC_PROVIDER, GOOGLE_WORKSPACE_PROVIDER}:
            query = urlencode(
                {
                    "client_id": self._settings.google_oauth_client_id,
                    "redirect_uri": self._callback_url("google"),
                    "response_type": "code",
                    "scope": " ".join(sorted(_google_scopes(provider_key, requested_capabilities))),
                    "access_type": "offline",
                    "include_granted_scopes": "true",
                    "prompt": "consent",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
            return ConnectStart(
                authorization_url=f"https://accounts.google.com/o/oauth2/v2/auth?{query}"
            )
        # GitHub only issues a code on a fresh install, so the connection starts with user
        # authorization; Tin then finds the installation itself and only sends the user to
        # the install page when there is none.
        return ConnectStart(authorization_url=self._github_authorize_url(state))

    async def complete_google(
        self, *, state: str, code: str, clerk_user_id: str
    ) -> IntegrationConnection:
        attempt = await self._consume_google_attempt(state=state, clerk_user_id=clerk_user_id)
        self._require_configured(attempt.provider_key)
        if attempt.pkce_verifier_ciphertext is None or self._cipher is None:
            raise IntegrationAuthorizationError("Google connection attempt is invalid")
        verifier = self._cipher.decrypt(
            attempt.pkce_verifier_ciphertext,
            context=f"auth:{attempt.project_id}:{attempt.provider_key}",
        )
        response = await self._client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": self._settings.google_oauth_client_id,
                "client_secret": self._settings.google_oauth_client_secret.get_secret_value(),
                "redirect_uri": self._callback_url("google"),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
        payload = _provider_json(response, provider="Google")
        refresh_token = payload.get("refresh_token")
        existing = await self._database.get_integration_connection(
            project_id=attempt.project_id, provider_key=attempt.provider_key
        )
        reused_existing_credential = False
        if not isinstance(refresh_token, str) or not refresh_token:
            if existing is not None and existing.credential_ciphertext is not None:
                credential = existing.credential_ciphertext
                reused_existing_credential = True
            else:
                raise IntegrationAuthorizationError(
                    "Google did not return durable access; reconnect and approve offline access"
                )
        else:
            credential = self._cipher.encrypt(
                refresh_token,
                context=f"credential:{attempt.project_id}:{attempt.provider_key}",
            )
        requested_scopes = _google_scopes(attempt.provider_key, attempt.requested_capabilities)
        raw_scopes = payload.get("scope")
        granted_scopes = (
            requested_scopes
            if raw_scopes is None
            else {item for item in str(raw_scopes).split() if item}
        )
        if attempt.provider_key == GSC_PROVIDER and GOOGLE_SCOPE not in granted_scopes:
            raise IntegrationAuthorizationError(
                "Google did not grant every requested Tin capability"
            )
        external_account_id = None
        external_account_label = "Search Console connected"
        configuration: dict[str, Any] = {
            "selected_site_url": None,
            "granted_scope": GOOGLE_SCOPE,
        }
        if attempt.provider_key == GOOGLE_WORKSPACE_PROVIDER:
            access_token = payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise IntegrationUpstreamError("Google did not return an access token")
            identity_response = await self._client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            identity = _provider_json(identity_response, provider="Google identity")
            subject = identity.get("sub")
            email = identity.get("email")
            if not isinstance(subject, str) or not isinstance(email, str):
                raise IntegrationAuthorizationError("Google account identity is unavailable")
            if (
                reused_existing_credential
                and existing is not None
                and existing.external_account_id != subject
            ):
                raise IntegrationAuthorizationError(
                    "Google switched accounts without returning durable access; reconnect again"
                )
            granted_capabilities = _workspace_capabilities_for_scopes(granted_scopes)
            if not set(attempt.requested_capabilities).issubset(granted_capabilities):
                raise IntegrationAuthorizationError(
                    "Google did not grant every requested Tin capability"
                )
            external_account_id = subject
            external_account_label = email
            configuration = {
                "email": email,
                "granted_capabilities": sorted(granted_capabilities),
                "granted_scopes": sorted(granted_scopes),
            }
        connection = await self._database.upsert_integration_connection(
            project_id=attempt.project_id,
            provider_key=attempt.provider_key,
            external_account_id=external_account_id,
            external_account_label=external_account_label,
            configuration=configuration,
            credential_ciphertext=credential,
            credential_key_version=self._cipher.version,
            connected_by_clerk_user_id=clerk_user_id,
        )
        await self._record_activity(
            connection,
            "integration_connected",
            f"{self._definition(attempt.provider_key).name} connected.",
            suffix=str(uuid4()),
        )
        return connection

    async def pending_google(self, *, state: str, clerk_user_id: str) -> IntegrationAuthAttempt:
        if not state or len(state) > 256:
            raise IntegrationAuthorizationError("connection attempt has expired")
        attempt = await self._database.get_google_integration_auth_attempt(
            token_hash=_sha256(state), clerk_user_id=clerk_user_id
        )
        if attempt is None:
            raise IntegrationAuthorizationError("connection attempt has expired")
        return attempt

    async def pending_project(self, *, state: str, provider_key: str, clerk_user_id: str) -> UUID:
        self._definition(provider_key)
        if not state or len(state) > 256:
            raise IntegrationAuthorizationError("connection attempt has expired")
        attempt = await self._database.get_integration_auth_attempt(
            token_hash=_sha256(state),
            provider_key=provider_key,
            clerk_user_id=clerk_user_id,
        )
        if attempt is None:
            raise IntegrationAuthorizationError("connection attempt has expired")
        return attempt.project_id

    async def start_github_authorization(
        self,
        *,
        project_id: UUID,
        installation_id: int,
        setup_action: str | None,
        clerk_user_id: str,
        state: str | None = None,
    ) -> ConnectStart:
        """Bind an installation GitHub already holds to this project.

        GitHub only issues an OAuth code on a fresh install. When the app is already installed
        it returns with an installation id and no code, so Tin asks the signed-in GitHub user to
        authorize separately, remembers the chosen installation on the attempt, and completes
        through the same verified path once the code arrives.
        """
        self._require_configured(GITHUB_PROVIDER)
        if installation_id <= 0:
            raise IntegrationAuthorizationError("GitHub installation was not completed")
        if state and len(state) <= 256:
            # The install-page attempt is retired; the authorization attempt replaces it.
            await self._database.consume_integration_auth_attempt(
                token_hash=_sha256(state),
                provider_key=GITHUB_PROVIDER,
                clerk_user_id=clerk_user_id,
            )
        definition = self._definition(GITHUB_PROVIDER)
        new_state = secrets.token_urlsafe(32)
        await self._database.create_integration_auth_attempt(
            token_hash=_sha256(new_state),
            project_id=project_id,
            provider_key=GITHUB_PROVIDER,
            clerk_user_id=clerk_user_id,
            pkce_verifier_ciphertext=None,
            requested_capabilities=tuple(definition.capabilities),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            context={"installation_id": installation_id, "setup_action": setup_action},
        )
        return ConnectStart(authorization_url=self._github_authorize_url(new_state))

    async def complete_github(
        self,
        *,
        state: str,
        code: str,
        installation_id: int | None,
        setup_action: str | None,
        clerk_user_id: str,
    ) -> IntegrationConnection:
        self._require_configured(GITHUB_PROVIDER)
        attempt = await self._consume_attempt(
            state=state, provider_key=GITHUB_PROVIDER, clerk_user_id=clerk_user_id
        )
        remembered = attempt.context.get("installation_id")
        if installation_id is None and isinstance(remembered, int) and remembered > 0:
            installation_id = remembered
            setup_action = setup_action or attempt.context.get("setup_action")
        oauth_response = await self._client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": self._settings.github_app_client_id,
                "client_secret": self._settings.github_app_client_secret.get_secret_value(),
                "code": code,
                "redirect_uri": self._callback_url("github"),
            },
        )
        oauth_payload = _provider_json(oauth_response, provider="GitHub authorization")
        user_token = oauth_payload.get("access_token")
        if not isinstance(user_token, str) or not user_token:
            raise IntegrationAuthorizationError("GitHub authorization could not be verified")
        if installation_id is None:
            installation_id = await self._github_resolve_user_installation(
                user_token, attempt=attempt
            )
        access_response = await self._client.get(
            f"https://api.github.com/user/installations/{installation_id}/repositories",
            headers=self._github_headers(user_token),
            params={"per_page": 1},
        )
        if access_response.status_code in {401, 403, 404}:
            raise IntegrationAuthorizationError(
                "The signed-in GitHub user cannot access that installation"
            )
        _provider_json(access_response, provider="GitHub")
        response = await self._client.get(
            f"https://api.github.com/app/installations/{installation_id}",
            headers=self._github_headers(await self._github_jwt()),
        )
        payload = _provider_json(response, provider="GitHub")
        account = payload.get("account")
        account_label = account.get("login") if isinstance(account, dict) else None
        permissions = payload.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        write_enabled = (
            permissions.get("contents") == "write" and permissions.get("pull_requests") == "write"
        )
        if not write_enabled:
            raise IntegrationAuthorizationError(
                "The GitHub installation must grant Contents and Pull requests write access"
            )
        del user_token
        connection = await self._database.upsert_integration_connection(
            project_id=attempt.project_id,
            provider_key=GITHUB_PROVIDER,
            external_account_id=str(installation_id),
            external_account_label=(
                str(account_label) if account_label else f"Installation {installation_id}"
            ),
            configuration={
                "selected_repository": None,
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                },
                "write_opted_in": True,
                "setup_action": setup_action,
            },
            credential_ciphertext=None,
            credential_key_version=None,
            connected_by_clerk_user_id=clerk_user_id,
        )
        await self._record_activity(
            connection,
            "integration_connected",
            "GitHub connected with repository and pull-request write access.",
            suffix=str(uuid4()),
        )
        return connection

    async def google_sites(self, *, project_id: UUID) -> list[ProviderOption]:
        connection = await self._connection(project_id, GSC_PROVIDER)
        access_token = await self._google_access_token(connection)
        execution_key = f"integration:{uuid4()}"
        fingerprint = _sha256(f"{project_id}:{GSC_PROVIDER}:sites.list")
        try:
            response = await self._client.get(
                "https://www.googleapis.com/webmasters/v3/sites",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            payload = _provider_json(response, provider="Google Search Console")
            entries = payload.get("siteEntry", [])
            options = [
                ProviderOption(
                    id=str(item["siteUrl"]),
                    label=str(item["siteUrl"]),
                    detail=str(item.get("permissionLevel", "")) or None,
                )
                for item in entries
                if isinstance(item, dict) and item.get("siteUrl")
            ]
            options.sort(key=lambda option: option.label.casefold())
        except IntegrationError:
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                connection_id=connection.id,
                provider_key=GSC_PROVIDER,
                capability="sites.list",
                request_fingerprint=fingerprint,
                status="failed",
                error_code="provider_request_failed",
            )
            raise
        await self._database.record_integration_call(
            execution_key=execution_key,
            project_id=project_id,
            connection_id=connection.id,
            provider_key=GSC_PROVIDER,
            capability="sites.list",
            request_fingerprint=fingerprint,
            status="completed",
            response_summary={"count": len(options)},
            provider_request_id=response.headers.get("x-guploader-uploadid"),
        )
        return options

    async def github_repositories(self, *, project_id: UUID) -> list[ProviderOption]:
        connection = await self._connection(project_id, GITHUB_PROVIDER)
        installation_id = _installation_id(connection)
        token = await self._github_installation_token(installation_id)
        execution_key = f"integration:{uuid4()}"
        fingerprint = _sha256(f"{project_id}:{GITHUB_PROVIDER}:repositories.list")
        try:
            response = await self._client.get(
                "https://api.github.com/installation/repositories?per_page=100",
                headers=self._github_headers(token),
            )
            payload = _provider_json(response, provider="GitHub")
            repositories = payload.get("repositories", [])
            options = [
                ProviderOption(
                    id=str(item["full_name"]),
                    label=str(item["full_name"]),
                    detail=("private" if item.get("private") else "public"),
                )
                for item in repositories
                if isinstance(item, dict) and item.get("full_name")
            ]
            options.sort(key=lambda option: option.label.casefold())
        except IntegrationError:
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                connection_id=connection.id,
                provider_key=GITHUB_PROVIDER,
                capability="repositories.list",
                request_fingerprint=fingerprint,
                status="failed",
                error_code="provider_request_failed",
            )
            raise
        await self._database.record_integration_call(
            execution_key=execution_key,
            project_id=project_id,
            connection_id=connection.id,
            provider_key=GITHUB_PROVIDER,
            capability="repositories.list",
            request_fingerprint=fingerprint,
            status="completed",
            response_summary={"count": len(options)},
            provider_request_id=response.headers.get("x-github-request-id"),
        )
        return options

    async def github_repository_snapshot(
        self,
        *,
        project_id: UUID,
        execution_key: str,
        run_id: UUID | None = None,
    ) -> GitHubRepositorySnapshot:
        """Read one bounded, immutable source snapshot without exposing an installation token."""
        if not execution_key or len(execution_key) > 200:
            raise IntegrationError("GitHub execution key is invalid")
        connection = await self._connection(project_id, GITHUB_PROVIDER)
        repository = connection.configuration.get("selected_repository")
        permissions = connection.configuration.get("permissions", {})
        if (
            not isinstance(repository, str)
            or repository.count("/") != 1
            or not isinstance(permissions, dict)
            or permissions.get("contents") != "write"
        ):
            raise IntegrationAuthorizationError(
                "Choose a GitHub repository with contents access first"
            )
        fingerprint = _sha256(
            _canonical_json({"repository": repository, "selector": "site-source-v1"})
        )
        token = await self._github_installation_token(_installation_id(connection))
        headers = self._github_headers(token)
        repository_path = quote(repository, safe="/")
        async with self._database.integration_call_lock(execution_key):
            existing = await self._database.get_integration_call_receipt(execution_key)
            if existing is not None and (
                existing.project_id != project_id
                or existing.run_id != run_id
                or existing.provider_key != GITHUB_PROVIDER
                or existing.capability != "contents.read"
                or existing.request_fingerprint != fingerprint
            ):
                raise SideEffectConflictError("GitHub execution key belongs to a different request")
            summary = existing.response_summary if existing is not None else None
            head_sha = summary.get("head_sha") if isinstance(summary, dict) else None
            default_branch = summary.get("default_branch") if isinstance(summary, dict) else None
            paths = summary.get("paths") if isinstance(summary, dict) else None
            request_id = existing.provider_request_id if existing is not None else None
            if (
                existing is None
                or existing.status != "completed"
                or not isinstance(head_sha, str)
                or not isinstance(default_branch, str)
                or not isinstance(paths, list)
                or any(not isinstance(path, str) for path in paths)
            ):
                await self._database.record_integration_call(
                    execution_key=execution_key,
                    project_id=project_id,
                    run_id=run_id,
                    connection_id=connection.id,
                    provider_key=GITHUB_PROVIDER,
                    capability="contents.read",
                    request_fingerprint=fingerprint,
                    status="started",
                )
                try:
                    repo_response = await self._client.get(
                        f"https://api.github.com/repos/{repository_path}", headers=headers
                    )
                    repo_payload = _provider_json(repo_response, provider="GitHub")
                    default_branch = repo_payload.get("default_branch")
                    if not isinstance(default_branch, str) or not _safe_github_ref(default_branch):
                        raise IntegrationUpstreamError("GitHub returned an invalid default branch")
                    ref_response = await self._client.get(
                        (
                            f"https://api.github.com/repos/{repository_path}/git/ref/heads/"
                            f"{quote(default_branch, safe='')}"
                        ),
                        headers=headers,
                    )
                    ref_payload = _provider_json(ref_response, provider="GitHub")
                    target = ref_payload.get("object")
                    head_sha = target.get("sha") if isinstance(target, dict) else None
                    if not isinstance(head_sha, str) or not head_sha:
                        raise IntegrationUpstreamError(
                            "GitHub default branch did not resolve to a commit"
                        )
                    tree_response = await self._client.get(
                        f"https://api.github.com/repos/{repository_path}/git/trees/{head_sha}",
                        headers=headers,
                        params={"recursive": "1"},
                    )
                    tree_payload = _provider_json(tree_response, provider="GitHub")
                    tree = tree_payload.get("tree")
                    if not isinstance(tree, list) or tree_payload.get("truncated") is True:
                        raise IntegrationUpstreamError(
                            "GitHub repository tree is unavailable or too large"
                        )
                    paths = _site_source_paths(tree)
                    if not paths:
                        raise IntegrationAuthorizationError(
                            "The selected repository has no supported website source files"
                        )
                    request_id = tree_response.headers.get("x-github-request-id")
                    await self._database.record_integration_call(
                        execution_key=execution_key,
                        project_id=project_id,
                        run_id=run_id,
                        connection_id=connection.id,
                        provider_key=GITHUB_PROVIDER,
                        capability="contents.read",
                        request_fingerprint=fingerprint,
                        status="completed",
                        response_summary={
                            "repository": repository,
                            "default_branch": default_branch,
                            "head_sha": head_sha,
                            "paths": paths,
                        },
                        provider_request_id=request_id,
                    )
                except IntegrationError:
                    await self._database.record_integration_call(
                        execution_key=execution_key,
                        project_id=project_id,
                        run_id=run_id,
                        connection_id=connection.id,
                        provider_key=GITHUB_PROVIDER,
                        capability="contents.read",
                        request_fingerprint=fingerprint,
                        status="failed",
                        error_code="provider_request_failed",
                    )
                    raise

        files: list[GitHubRepositoryFile] = []
        total_bytes = 0
        assert isinstance(head_sha, str)
        assert isinstance(default_branch, str)
        assert isinstance(paths, list)
        for path in paths:
            content_response = await self._client.get(
                (
                    f"https://api.github.com/repos/{repository_path}/contents/"
                    f"{quote(path, safe='/')}"
                ),
                headers=headers,
                params={"ref": head_sha},
            )
            payload = _provider_json(content_response, provider="GitHub")
            encoded = payload.get("content")
            encoding = payload.get("encoding")
            if not isinstance(encoded, str) or encoding != "base64":
                continue
            try:
                raw = base64.b64decode("".join(encoded.split()), validate=True)
                content = raw.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            if not content or len(raw) > 80_000 or total_bytes + len(raw) > 180_000:
                continue
            files.append(GitHubRepositoryFile(path=path, content=content))
            total_bytes += len(raw)
        if not files:
            raise IntegrationAuthorizationError(
                "The selected repository has no readable website source files"
            )
        return GitHubRepositorySnapshot(
            repository=repository,
            default_branch=default_branch,
            head_sha=head_sha,
            files=tuple(files),
        )

    async def github_repository_binding(
        self, *, project_id: UUID, expected_repository: str
    ) -> GitHubRepositoryBinding:
        """Read the exact selected target without cloning, dispatching, or writing to it.

        A preview is not a durable run binding. Execution must resolve this again and
        retain its trusted identity through snapshot preparation and delivery.
        """
        connection = await self._connection(project_id, GITHUB_PROVIDER)
        if (
            not isinstance(expected_repository, str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", expected_repository
            )
            or expected_repository.split("/")[-1] in {".", ".."}
        ):
            raise IntegrationAuthorizationError("Choose an explicit owner/repository on GitHub")

        def identity(value):
            permissions = value.configuration.get("permissions", {}) if value else {}
            if (
                value is None
                or value.project_id != project_id
                or value.provider_key != GITHUB_PROVIDER
                or value.status != "connected"
                or value.configuration.get("selected_repository") != expected_repository
                or value.configuration.get("write_opted_in") is not True
                or not isinstance(permissions, dict)
                or permissions.get("contents") != "write"
                or permissions.get("pull_requests") != "write"
                or _installation_id(value) <= 0
            ):
                raise IntegrationAuthorizationError(
                    "Select this repository in GitHub and enable pull-request access first"
                )
            return value.id, value.external_account_id, value.updated_at

        original = identity(connection)
        repository_path = quote(expected_repository, safe="/")
        try:
            token = await self._github_installation_token(_installation_id(connection))
            headers = self._github_headers(token)
            response = await self._client.get(
                f"https://api.github.com/repos/{repository_path}", headers=headers
            )
            payload = _provider_json(response, provider="GitHub")
            repository_id, repository = payload.get("id"), payload.get("full_name")
            branch = payload.get("default_branch")
            if (
                type(repository_id) is not int
                or repository_id <= 0
                or not isinstance(repository, str)
                or repository.casefold() != expected_repository.casefold()
                or not isinstance(branch, str)
                or not _safe_github_ref(branch)
                or any(ord(c) <= 32 or ord(c) == 127 for c in branch)
                or payload.get("archived") is True
                or payload.get("disabled") is True
            ):
                raise IntegrationUpstreamError(
                    "GitHub returned an unavailable or different repository"
                )
            response = await self._client.get(
                f"https://api.github.com/repos/{repository_path}/git/ref/heads/"
                f"{quote(branch, safe='')}",
                headers=headers,
            )
            target = _provider_json(response, provider="GitHub").get("object")
            if (
                not isinstance(target, dict)
                or target.get("type") != "commit"
                or not isinstance(target.get("sha"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", target["sha"])
            ):
                raise IntegrationUpstreamError("GitHub default branch did not resolve to a commit")
        except httpx.HTTPError as exc:
            raise IntegrationUpstreamError(
                "GitHub repository could not be read. Try again."
            ) from exc
        current = await self._database.get_integration_connection(
            project_id=project_id, provider_key=GITHUB_PROVIDER
        )
        if identity(current) != original:
            raise IntegrationAuthorizationError(
                "GitHub connection changed. Prepare the target again."
            )
        return GitHubRepositoryBinding(
            connection_id=connection.id,
            installation_id=_installation_id(connection),
            repository_id=repository_id,
            repository=repository,
            default_branch=branch,
            head_sha=target["sha"],
        )

    async def _check_github_binding(self, project_id, connection, binding, *, head=True):
        current = await self.github_repository_binding(
            project_id=project_id, expected_repository=binding.repository
        )
        if (
            connection.id != binding.connection_id
            or current.connection_id != binding.connection_id
            or current.installation_id != binding.installation_id
            or current.repository_id != binding.repository_id
            or current.repository != binding.repository
            or current.default_branch != binding.default_branch
            or (head and current.head_sha != binding.head_sha)
        ):
            raise IntegrationAuthorizationError(
                "GitHub target changed after preparation. Start a new workflow run."
            )

    async def _github_validate_recovery(self, *, connection, binding, branch, files, complete):
        """Recover only our exact change (or a subset), never an unrelated branch."""
        token = await self._github_installation_token(_installation_id(connection))
        headers = self._github_headers(token)
        root = f"https://api.github.com/repos/{quote(binding.repository, safe='/')}"
        response = await self._client.get(
            f"{root}/compare/{binding.head_sha}...{quote(branch, safe='')}", headers=headers
        )
        comparison = _provider_json(response, provider="GitHub")
        changes = comparison.get("files")
        expected = {item.path: item.content for item in files}
        if (
            comparison.get("status") not in {"ahead", "identical"}
            or comparison.get("merge_base_commit", {}).get("sha") != binding.head_sha
            or not isinstance(changes, list)
            or len(changes) > len(files)
            or any(
                not isinstance(item, dict)
                or item.get("status") not in {"modified", "added"}
                or item.get("filename") not in expected
                for item in changes
            )
            or (complete and {item["filename"] for item in changes} != set(expected))
        ):
            raise IntegrationAuthorizationError("The result branch contains unexpected changes")
        for item in changes:
            response = await self._client.get(
                f"{root}/contents/{quote(item['filename'], safe='/')}",
                headers=headers,
                params={"ref": branch},
            )
            payload = _provider_json(response, provider="GitHub")
            try:
                actual = base64.b64decode(
                    "".join(payload["content"].split()), validate=True
                ).decode()
            except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
                raise IntegrationUpstreamError("The result branch cannot be verified") from exc
            if actual != expected[item["filename"]]:
                raise IntegrationAuthorizationError(
                    "The result branch was changed outside this run"
                )

    async def _github_validate_base_advance(self, *, connection, binding, current_sha, files):
        """Content delivery alone may retain its prepared base after unrelated commits."""
        token = await self._github_installation_token(_installation_id(connection))
        response = await self._client.get(
            f"https://api.github.com/repos/{quote(binding.repository, safe='/')}/compare/"
            f"{binding.head_sha}...{current_sha}",
            headers=self._github_headers(token),
        )
        comparison = _provider_json(response, provider="GitHub")
        changes = comparison.get("files")
        # GitHub caps compare file lists at 300. At that boundary we cannot prove
        # completeness. A rewritten default branch is not an unrelated advance.
        if (
            comparison.get("status") != "ahead"
            or comparison.get("merge_base_commit", {}).get("sha") != binding.head_sha
            or not isinstance(changes, list)
            or len(changes) >= 300
            or any(
                not isinstance(item, dict) or not isinstance(item.get("filename"), str)
                for item in changes
            )
        ):
            raise IntegrationAuthorizationError(
                "The repository advance could not be verified; the draft is safe in Tin"
            )
        touched = {
            p
            for item in changes
            for p in (item["filename"], item.get("previous_filename"))
            if isinstance(p, str)
        }
        if any(
            path == change.path
            or path.startswith(change.path + "/")
            or change.path.startswith(path + "/")
            for path in touched
            for change in files
        ):
            raise IntegrationAuthorizationError(
                "The article destination changed in GitHub; "
                "review its changes before starting a fresh draft"
            )

    async def github_markdown_file(
        self, *, project_id: UUID, binding: GitHubRepositoryBinding, path: str
    ) -> str | None:
        """Read one exact Markdown file (or absence) at the bound GitHub commit."""
        if not _safe_github_path(path) or not path.endswith(".md"):
            raise IntegrationAuthorizationError("Choose a Markdown content file")
        connection = await self._connection(project_id, GITHUB_PROVIDER)
        await self._check_github_binding(project_id, connection, binding, head=False)
        token = await self._github_installation_token(_installation_id(connection))
        root = f"https://api.github.com/repos/{quote(binding.repository, safe='/')}"
        # Contents can dereference symlinks. Check the Git modes and all parents first.
        tree_response = await self._client.get(
            f"{root}/git/trees/{binding.head_sha}",
            headers=self._github_headers(token),
            params={"recursive": "1"},
        )
        tree = _provider_json(tree_response, provider="GitHub")
        if not isinstance(tree.get("tree"), list) or tree.get("truncated") is True:
            raise IntegrationUpstreamError("The Markdown destination tree is incomplete")
        entries = {item.get("path"): item for item in tree["tree"] if isinstance(item, dict)}
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = entries.get("/".join(parts[:index]))
            if parent and (parent.get("type") != "tree" or parent.get("mode") != "040000"):
                raise IntegrationAuthorizationError(
                    "The Markdown destination has a non-directory parent"
                )
        target = entries.get(path)
        if target is None:
            return None
        if target.get("type") != "blob" or target.get("mode") not in {"100644", "100755"}:
            raise IntegrationAuthorizationError("Choose a regular Markdown file")
        response = await self._client.get(
            f"{root}/contents/{quote(path, safe='/')}",
            headers=self._github_headers(token),
            params={"ref": binding.head_sha},
        )
        payload = _provider_json(response, provider="GitHub")
        if (
            payload.get("type") != "file"
            or payload.get("path") != path
            or payload.get("encoding") != "base64"
            or payload.get("sha") != target.get("sha")
            or payload.get("target")
            or type(payload.get("size")) is not int
            or payload["size"] > 100_000
        ):
            raise IntegrationAuthorizationError("Choose a regular Markdown file under 100 KB")
        try:
            raw = base64.b64decode("".join(payload["content"].split()), validate=True)
            if len(raw) > 100_000:
                raise ValueError("oversized file")
            return raw.decode("utf-8")
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise IntegrationUpstreamError("The Markdown destination could not be read") from exc

    async def github_repository_bundle(
        self,
        *,
        project_id: UUID,
        execution_key: str,
        run_id: UUID | None = None,
        expected_binding: GitHubRepositoryBinding | None = None,
    ) -> GitHubRepositoryBundle:
        """Build a bounded immutable repository archive without exposing GitHub auth to E2B."""
        if not execution_key or len(execution_key) > 200:
            raise IntegrationError("GitHub execution key is invalid")
        connection = await self._connection(project_id, GITHUB_PROVIDER)
        if expected_binding is not None:
            await self._check_github_binding(project_id, connection, expected_binding)
        repository = connection.configuration.get("selected_repository")
        permissions = connection.configuration.get("permissions", {})
        if (
            not isinstance(repository, str)
            or repository.count("/") != 1
            or not isinstance(permissions, dict)
            or permissions.get("contents") not in {"read", "write"}
        ):
            raise IntegrationAuthorizationError(
                "Choose a GitHub repository with contents access first"
            )
        selector = {"repository": repository, "selector": "procedure-repository-v2"}
        fingerprint = _sha256(_canonical_json(selector))
        token = await self._github_installation_token(_installation_id(connection))
        headers = self._github_headers(token)
        repository_path = quote(repository, safe="/")
        async with self._database.integration_call_lock(execution_key):
            existing = await self._database.get_integration_call_receipt(execution_key)
            if existing is not None and (
                existing.project_id != project_id
                or existing.run_id != run_id
                or existing.provider_key != GITHUB_PROVIDER
                or existing.capability != "contents.read"
                or existing.request_fingerprint != fingerprint
            ):
                raise SideEffectConflictError("GitHub execution key belongs to a different request")
            summary = existing.response_summary if existing is not None else None
            head_sha = summary.get("head_sha") if isinstance(summary, dict) else None
            default_branch = summary.get("default_branch") if isinstance(summary, dict) else None
            request_id = existing.provider_request_id if existing is not None else None
            if (
                existing is None
                or existing.status != "completed"
                or not isinstance(head_sha, str)
                or not isinstance(default_branch, str)
            ):
                await self._database.record_integration_call(
                    execution_key=execution_key,
                    project_id=project_id,
                    run_id=run_id,
                    connection_id=connection.id,
                    provider_key=GITHUB_PROVIDER,
                    capability="contents.read",
                    request_fingerprint=fingerprint,
                    status="started",
                )
                try:
                    repo_response = await self._client.get(
                        f"https://api.github.com/repos/{repository_path}", headers=headers
                    )
                    repo_payload = _provider_json(repo_response, provider="GitHub")
                    default_branch = repo_payload.get("default_branch")
                    if not isinstance(default_branch, str) or not _safe_github_ref(default_branch):
                        raise IntegrationUpstreamError("GitHub returned an invalid default branch")
                    ref_response = await self._client.get(
                        (
                            f"https://api.github.com/repos/{repository_path}/git/ref/heads/"
                            f"{quote(default_branch, safe='')}"
                        ),
                        headers=headers,
                    )
                    ref_payload = _provider_json(ref_response, provider="GitHub")
                    target = ref_payload.get("object")
                    head_sha = target.get("sha") if isinstance(target, dict) else None
                    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
                        raise IntegrationUpstreamError(
                            "GitHub default branch did not resolve to a commit"
                        )
                    request_id = ref_response.headers.get("x-github-request-id")
                    await self._database.record_integration_call(
                        execution_key=execution_key,
                        project_id=project_id,
                        run_id=run_id,
                        connection_id=connection.id,
                        provider_key=GITHUB_PROVIDER,
                        capability="contents.read",
                        request_fingerprint=fingerprint,
                        status="completed",
                        response_summary={
                            "repository": repository,
                            "default_branch": default_branch,
                            "head_sha": head_sha,
                        },
                        provider_request_id=request_id,
                    )
                except IntegrationError:
                    await self._database.record_integration_call(
                        execution_key=execution_key,
                        project_id=project_id,
                        run_id=run_id,
                        connection_id=connection.id,
                        provider_key=GITHUB_PROVIDER,
                        capability="contents.read",
                        request_fingerprint=fingerprint,
                        status="failed",
                        error_code="provider_request_failed",
                    )
                    raise

        assert isinstance(head_sha, str)
        assert isinstance(default_branch, str)
        if expected_binding is not None and (
            head_sha != expected_binding.head_sha
            or default_branch != expected_binding.default_branch
        ):
            raise IntegrationAuthorizationError("The pinned repository snapshot changed")
        tree_response = await self._client.get(
            f"https://api.github.com/repos/{repository_path}/git/trees/{head_sha}",
            headers=headers,
            params={"recursive": "1"},
        )
        tree_payload = _provider_json(tree_response, provider="GitHub")
        raw_tree = tree_payload.get("tree")
        if not isinstance(raw_tree, list) or tree_payload.get("truncated") is True:
            raise IntegrationUpstreamError("GitHub repository tree is unavailable or too large")
        blobs = [
            item
            for item in raw_tree
            if isinstance(item, dict)
            and item.get("type") == "blob"
            and item.get("mode") in {"100644", "100755"}
            and isinstance(item.get("path"), str)
            and _safe_github_source_path(item["path"])
            and isinstance(item.get("sha"), str)
            and isinstance(item.get("size"), int)
            and 0 <= item["size"] <= 2_000_000
        ]
        total_bytes = sum(item["size"] for item in blobs)
        if not blobs:
            raise IntegrationAuthorizationError(
                "The selected repository has no eligible files for a procedure workspace"
            )
        if len(blobs) > REPOSITORY_MAX_FILES or total_bytes > REPOSITORY_MAX_BYTES:
            raise IntegrationAuthorizationError(
                "The selected repository is outside the procedure workspace limits: "
                f"{len(blobs):,} files / {total_bytes:,} bytes; "
                f"this workflow allows {REPOSITORY_MAX_FILES:,} files / "
                f"{REPOSITORY_MAX_BYTES:,} bytes"
            )
        wanted = {item["path"]: item for item in blobs}
        # One tarball request instead of one API call per file. Every file is checked
        # against its blob hash in the pinned tree, so the archive is exactly head_sha.
        with tempfile.SpooledTemporaryFile(max_size=16_000_000) as download:
            await self._github_tarball(repository_path, head_sha, headers, download)
            contents = await to_thread.run_sync(_verified_tarball_blobs, download, wanted)
        missing = [item for path, item in wanted.items() if path not in contents]
        if len(missing) > REPOSITORY_BLOB_FALLBACKS:
            raise IntegrationUpstreamError("GitHub repository tarball is missing pinned files")
        for item in missing:
            # export-ignore drops a path from the tarball and export-subst rewrites it.
            contents[item["path"]] = await self._github_blob_content(repository_path, headers, item)
        archive = await to_thread.run_sync(_repository_archive, blobs, contents)
        return GitHubRepositoryBundle(
            repository=repository,
            default_branch=default_branch,
            head_sha=head_sha,
            archive=archive,
            file_count=len(blobs),
            complete=len(blobs)
            == len(
                [
                    item
                    for item in raw_tree
                    if not isinstance(item, dict) or item.get("type") != "tree"
                ]
            ),
        )

    async def github_open_pull_requests(
        self,
        *,
        project_id: UUID,
        execution_key: str,
        base_branch: str,
        run_id: UUID | None = None,
        expected_binding: GitHubRepositoryBinding | None = None,
    ) -> GitHubOpenPullRequestEvidence:
        """Read bounded open-PR evidence without exposing GitHub auth to E2B."""
        if not execution_key or len(execution_key) > 200:
            raise IntegrationError("GitHub execution key is invalid")
        if not _safe_github_ref(base_branch):
            raise IntegrationError("GitHub base branch is invalid")
        connection = await self._connection(project_id, GITHUB_PROVIDER)
        if expected_binding is not None:
            await self._check_github_binding(project_id, connection, expected_binding)
        repository = connection.configuration.get("selected_repository")
        permissions = connection.configuration.get("permissions", {})
        if (
            not isinstance(repository, str)
            or repository.count("/") != 1
            or not isinstance(permissions, dict)
            or permissions.get("pull_requests") != "write"
        ):
            raise IntegrationAuthorizationError(
                "Choose a GitHub repository with pull-request access first"
            )
        fingerprint = _sha256(
            _canonical_json(
                {
                    "repository": repository,
                    "base_branch": base_branch,
                    "selector": "procedure-open-pull-requests-v1",
                }
            )
        )
        async with self._database.integration_call_lock(execution_key):
            existing = await self._database.get_integration_call_receipt(execution_key)
            if existing is not None and (
                existing.project_id != project_id
                or existing.run_id != run_id
                or existing.provider_key != GITHUB_PROVIDER
                or existing.capability != "pull_requests.read"
                or existing.request_fingerprint != fingerprint
            ):
                raise SideEffectConflictError("GitHub execution key belongs to a different request")
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection_id=connection.id,
                provider_key=GITHUB_PROVIDER,
                capability="pull_requests.read",
                request_fingerprint=fingerprint,
                status="started",
            )
            try:
                evidence, request_id = await self._github_read_open_pull_requests(
                    connection=connection,
                    repository=repository,
                    base_branch=base_branch,
                )
            except IntegrationError:
                await self._database.record_integration_call(
                    execution_key=execution_key,
                    project_id=project_id,
                    run_id=run_id,
                    connection_id=connection.id,
                    provider_key=GITHUB_PROVIDER,
                    capability="pull_requests.read",
                    request_fingerprint=fingerprint,
                    status="failed",
                    error_code="provider_request_failed",
                )
                raise
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection_id=connection.id,
                provider_key=GITHUB_PROVIDER,
                capability="pull_requests.read",
                request_fingerprint=fingerprint,
                status="completed",
                response_summary={
                    "repository": repository,
                    "base_branch": base_branch,
                    "pull_request_count": evidence.pull_request_count,
                    "file_count": evidence.file_count,
                    "truncated": evidence.truncated,
                    "evidence_sha256": hashlib.sha256(evidence.document).hexdigest(),
                },
                provider_request_id=request_id,
            )
            return evidence

    async def workspace_search_messages(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        connection_id: UUID | None = None,
        external_account_id: str | None = None,
        query: str,
        max_results: int,
        execution_key: str,
    ) -> dict[str, Any]:
        _gmail_search_request(query, max_results)
        connection = await self._workspace_connection(
            project_id=project_id,
            capability="gmail.messages.read",
            connection_id=connection_id,
            external_account_id=external_account_id,
        )
        fingerprint = _sha256(_canonical_json({"query": query.strip(), "max_results": max_results}))
        try:
            access_token = await self._google_access_token(connection)
            response = await self._client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"q": query.strip(), "maxResults": max_results},
            )
            payload = _provider_json(response, provider="Gmail")
            raw_messages = payload.get("messages", [])
            if not isinstance(raw_messages, list):
                raw_messages = []
            messages = [
                {"id": item["id"], "thread_id": item["threadId"]}
                for item in raw_messages
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("threadId"), str)
            ][:max_results]
            result = {
                "messages": messages,
                "result_size_estimate": min(int(payload.get("resultSizeEstimate", 0)), 100_000),
                "truncated": len(messages) >= max_results,
            }
        except (IntegrationError, TypeError, ValueError):
            await self._record_workspace_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection=connection,
                capability="gmail.messages.read",
                fingerprint=fingerprint,
                status="failed",
                error_code="provider_request_failed",
            )
            raise
        await self._record_workspace_call(
            execution_key=execution_key,
            project_id=project_id,
            run_id=run_id,
            connection=connection,
            capability="gmail.messages.read",
            fingerprint=fingerprint,
            status="completed",
            response_summary={"message_count": len(messages)},
            provider_request_id=response.headers.get("x-guploader-uploadid"),
        )
        return result

    async def workspace_get_thread(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        connection_id: UUID | None = None,
        external_account_id: str | None = None,
        thread_id: str,
        execution_key: str,
    ) -> dict[str, Any]:
        _gmail_thread_request(thread_id)
        connection = await self._workspace_connection(
            project_id=project_id,
            capability="gmail.messages.read",
            connection_id=connection_id,
            external_account_id=external_account_id,
        )
        fingerprint = _sha256(_canonical_json({"thread_id": thread_id}))
        try:
            access_token = await self._google_access_token(connection)
            response = await self._client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "format": "full",
                    "fields": (
                        "id,historyId,messages(id,threadId,internalDate,labelIds,snippet,"
                        "payload(headers,mimeType,body/data,parts(headers,mimeType,body/data,"
                        "parts(headers,mimeType,body/data))))"
                    ),
                },
            )
            payload = _provider_json(response, provider="Gmail")
            raw_messages = payload.get("messages", [])
            if not isinstance(raw_messages, list):
                raw_messages = []
            messages = [
                _workspace_message(item) for item in raw_messages if isinstance(item, dict)
            ][:50]
            result = {
                "thread_id": str(payload.get("id", thread_id)),
                "messages": messages,
                "truncated": len(raw_messages) > len(messages),
            }
        except (IntegrationError, UnicodeDecodeError, ValueError):
            await self._record_workspace_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection=connection,
                capability="gmail.messages.read",
                fingerprint=fingerprint,
                status="failed",
                error_code="provider_request_failed",
            )
            raise
        await self._record_workspace_call(
            execution_key=execution_key,
            project_id=project_id,
            run_id=run_id,
            connection=connection,
            capability="gmail.messages.read",
            fingerprint=fingerprint,
            status="completed",
            response_summary={"message_count": len(messages)},
            provider_request_id=response.headers.get("x-guploader-uploadid"),
        )
        return result

    async def workspace_list_calendar_events(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        connection_id: UUID | None = None,
        external_account_id: str | None = None,
        time_min: str,
        time_max: str,
        query: str,
        max_results: int,
        execution_key: str,
    ) -> dict[str, Any]:
        start, end = _calendar_request(time_min, time_max, query, max_results)
        connection = await self._workspace_connection(
            project_id=project_id,
            capability="calendar.events.read",
            connection_id=connection_id,
            external_account_id=external_account_id,
        )
        fingerprint = _sha256(
            _canonical_json(
                {
                    "time_min": time_min,
                    "time_max": time_max,
                    "query": query,
                    "max_results": max_results,
                }
            )
        )
        try:
            access_token = await self._google_access_token(connection)
            response = await self._client.get(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "q": query or None,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": max_results,
                },
            )
            payload = _provider_json(response, provider="Google Calendar")
            raw_items = payload.get("items", [])
            if not isinstance(raw_items, list):
                raw_items = []
            events = [
                _workspace_calendar_event(item) for item in raw_items if isinstance(item, dict)
            ][:max_results]
            result = {
                "events": events,
                "time_zone": payload.get("timeZone"),
                "truncated": bool(payload.get("nextPageToken")),
            }
        except (IntegrationError, ValueError):
            await self._record_workspace_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection=connection,
                capability="calendar.events.read",
                fingerprint=fingerprint,
                status="failed",
                error_code="provider_request_failed",
            )
            raise
        await self._record_workspace_call(
            execution_key=execution_key,
            project_id=project_id,
            run_id=run_id,
            connection=connection,
            capability="calendar.events.read",
            fingerprint=fingerprint,
            status="completed",
            response_summary={"event_count": len(events)},
            provider_request_id=response.headers.get("x-guploader-uploadid"),
        )
        return result

    async def workspace_send_message(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        connection_id: UUID | None = None,
        external_account_id: str | None = None,
        execution_key: str,
        recipient_email: str,
        recipient_name: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, str]:
        if not execution_key or len(execution_key) > 200:
            raise IntegrationError("Gmail send execution key is invalid")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient_email):
            raise IntegrationError("Gmail recipient is invalid")
        if not subject.strip() or len(subject) > 300 or not body.strip() or len(body) > 20_000:
            raise IntegrationError("Gmail subject or body is invalid")
        if thread_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            raise IntegrationError("Gmail thread ID is invalid")
        connection = await self._workspace_connection(
            project_id=project_id,
            capability="gmail.messages.send",
            connection_id=connection_id,
            external_account_id=external_account_id,
        )
        sender_email = connection.configuration.get("email")
        if not isinstance(sender_email, str) or "@" not in sender_email:
            raise IntegrationAuthorizationError("Google Workspace account email is unavailable")
        rfc_message_id = campaign_message_id(execution_key)
        message = build_email_message(
            sender_email=sender_email,
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            subject=subject.strip(),
            body=body.strip(),
            message_id=rfc_message_id,
            in_reply_to=in_reply_to,
        )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        request_value = {
            "recipient_email": recipient_email,
            "subject": subject.strip(),
            "body_sha256": hashlib.sha256(body.strip().encode()).hexdigest(),
            "thread_id": thread_id,
            "rfc_message_id": rfc_message_id,
        }
        fingerprint = _sha256(_canonical_json(request_value))
        async with self._database.integration_call_lock(execution_key):
            existing = await self._database.get_integration_call_receipt(execution_key)
            is_retry = existing is not None
            if existing is not None:
                if (
                    existing.project_id != project_id
                    or existing.run_id != run_id
                    or existing.provider_key != GOOGLE_WORKSPACE_PROVIDER
                    or existing.capability != "gmail.messages.send"
                    or existing.request_fingerprint != fingerprint
                ):
                    raise SideEffectConflictError(
                        "Gmail execution key belongs to a different request"
                    )
                if existing.status == "completed" and existing.response_summary is not None:
                    return {
                        **_gmail_send_result(existing.response_summary),
                        "rfc_message_id": rfc_message_id,
                    }
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection_id=connection.id,
                provider_key=GOOGLE_WORKSPACE_PROVIDER,
                capability="gmail.messages.send",
                request_fingerprint=fingerprint,
                status="started",
            )
            access_token = await self._google_access_token(connection)
            recovered = await self._gmail_find_message(
                access_token=access_token,
                rfc_message_id=rfc_message_id,
            )
            provider_request_id: str | None = None
            if recovered is None:
                if is_retry:
                    await self._database.record_integration_call(
                        execution_key=execution_key,
                        project_id=project_id,
                        run_id=run_id,
                        connection_id=connection.id,
                        provider_key=GOOGLE_WORKSPACE_PROVIDER,
                        capability="gmail.messages.send",
                        request_fingerprint=fingerprint,
                        status="unknown",
                        error_code="delivery_unconfirmed",
                    )
                    raise IntegrationDeliveryUnknownError(
                        "Gmail delivery could not be confirmed; Tin will not resend automatically"
                    )
                try:
                    response = await self._client.post(
                        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                        headers={"Authorization": f"Bearer {access_token}"},
                        json={
                            "raw": raw,
                            **({"threadId": thread_id} if thread_id is not None else {}),
                        },
                    )
                    payload = _provider_json(response, provider="Gmail")
                    recovered = _gmail_send_result(payload)
                    provider_request_id = response.headers.get("x-guploader-uploadid")
                except IntegrationError as exc:
                    await self._database.record_integration_call(
                        execution_key=execution_key,
                        project_id=project_id,
                        run_id=run_id,
                        connection_id=connection.id,
                        provider_key=GOOGLE_WORKSPACE_PROVIDER,
                        capability="gmail.messages.send",
                        request_fingerprint=fingerprint,
                        status="unknown",
                        error_code="delivery_unconfirmed",
                    )
                    raise IntegrationDeliveryUnknownError(
                        "Gmail delivery could not be confirmed; Tin will not resend automatically"
                    ) from exc
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection_id=connection.id,
                provider_key=GOOGLE_WORKSPACE_PROVIDER,
                capability="gmail.messages.send",
                request_fingerprint=fingerprint,
                status="completed",
                response_summary={**recovered, "rfc_message_id": rfc_message_id},
                provider_request_id=provider_request_id,
            )
            return {**recovered, "rfc_message_id": rfc_message_id}

    async def workspace_thread_has_reply(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        connection_id: UUID | None = None,
        external_account_id: str | None = None,
        thread_id: str,
        after: datetime,
        execution_key: str,
    ) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            raise IntegrationError("Gmail thread ID is invalid")
        connection = await self._workspace_connection(
            project_id=project_id,
            capability="gmail.history.read",
            connection_id=connection_id,
            external_account_id=external_account_id,
        )
        sender_email = connection.configuration.get("email")
        if not isinstance(sender_email, str):
            raise IntegrationAuthorizationError("Google Workspace account email is unavailable")
        fingerprint = _sha256(_canonical_json({"thread_id": thread_id, "after": after.isoformat()}))
        try:
            access_token = await self._google_access_token(connection)
            response = await self._client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "format": "metadata",
                    "metadataHeaders": "From",
                    "fields": "messages(id,internalDate,payload/headers)",
                },
            )
            payload = _provider_json(response, provider="Gmail")
            raw_messages = payload.get("messages", [])
            if not isinstance(raw_messages, list):
                raw_messages = []
            replied = any(
                _gmail_message_is_reply(item, sender_email=sender_email, after=after)
                for item in raw_messages
                if isinstance(item, dict)
            )
        except (IntegrationError, ValueError):
            await self._record_workspace_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection=connection,
                capability="gmail.history.read",
                fingerprint=fingerprint,
                status="failed",
                error_code="provider_request_failed",
            )
            raise
        await self._record_workspace_call(
            execution_key=execution_key,
            project_id=project_id,
            run_id=run_id,
            connection=connection,
            capability="gmail.history.read",
            fingerprint=fingerprint,
            status="completed",
            response_summary={"replied": replied},
            provider_request_id=response.headers.get("x-guploader-uploadid"),
        )
        return replied

    async def search_console_analytics(
        self,
        *,
        project_id: UUID,
        start_date: str,
        end_date: str,
        dimensions: tuple[str, ...] = ("date",),
        row_limit: int = 1000,
        start_row: int = 0,
        dimension_filters: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        execution_key: str | None = None,
        run_id: UUID | None = None,
        expected_site_url: str | None = None,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        connection = await self._connection(project_id, GSC_PROVIDER)
        selected_site = connection.configuration.get("selected_site_url")
        if not isinstance(selected_site, str) or not selected_site:
            raise IntegrationAuthorizationError("Choose a Search Console property first")
        if expected_site_url is not None and selected_site != expected_site_url:
            raise IntegrationAuthorizationError("The selected Search Console property changed")
        filters = _search_console_request(
            start_date, end_date, dimensions, row_limit, start_row, dimension_filters
        )
        sent_limit = row_limit
        if max_response_bytes is not None:
            # Lossless: a response that fits the bound cannot hold more rows than this.
            sent_limit = max(1, min(row_limit, max_response_bytes // GSC_MIN_ROW_BYTES))
        request_body: dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": list(dimensions),
            "rowLimit": sent_limit,
        }
        if start_row:
            request_body["startRow"] = start_row
        if filters:
            request_body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
        fingerprint = _sha256(_canonical_json({"site": selected_site, **request_body}))
        receipt_key = execution_key or f"integration:{uuid4()}"
        access_token = await self._google_access_token(connection)
        try:
            response = await self._client.post(
                (
                    "https://www.googleapis.com/webmasters/v3/sites/"
                    f"{quote(selected_site, safe='')}/searchAnalytics/query"
                ),
                headers={"Authorization": f"Bearer {access_token}"},
                json=request_body,
            )
            payload = _provider_json(response, provider="Google Search Console")
        except IntegrationError:
            await self._database.record_integration_call(
                execution_key=receipt_key,
                project_id=project_id,
                run_id=run_id,
                connection_id=connection.id,
                provider_key=GSC_PROVIDER,
                capability="search_analytics.read",
                request_fingerprint=fingerprint,
                status="failed",
                error_code="provider_request_failed",
            )
            raise
        rows = payload.get("rows", [])
        truncated = False
        if max_response_bytes is not None:
            payload = _fit_search_console_rows(
                payload,
                max_response_bytes=max_response_bytes,
                start_row=start_row,
                clamped=sent_limit < row_limit,
                sent_limit=sent_limit,
            )
            truncated = bool(payload.get("truncated"))
        summary: dict[str, Any] = {"row_count": len(rows) if isinstance(rows, list) else 0}
        if truncated:
            summary["returned_row_count"] = len(payload["rows"])
            summary["truncated"] = True
        await self._database.record_integration_call(
            execution_key=receipt_key,
            project_id=project_id,
            run_id=run_id,
            connection_id=connection.id,
            provider_key=GSC_PROVIDER,
            capability="search_analytics.read",
            request_fingerprint=fingerprint,
            status="completed",
            response_summary=summary,
            provider_request_id=response.headers.get("x-guploader-uploadid"),
        )
        if max_response_bytes is not None and not payload.get("rows") and rows:
            raise ServiceResponseTooLarge("A single Search Console row exceeds the response bound.")
        return payload

    async def github_create_pull_request(
        self,
        *,
        project_id: UUID,
        execution_key: str,
        title: str,
        body: str,
        files: tuple[GitHubFileChange, ...],
        base_branch: str | None = None,
        expected_base_sha: str | None = None,
        run_id: UUID | None = None,
        expected_binding: GitHubRepositoryBinding | None = None,
        allow_unrelated_base_advance: bool = False,
    ) -> GitHubPullRequestResult:
        if allow_unrelated_base_advance and (
            expected_binding is None or expected_base_sha != expected_binding.head_sha
        ):
            raise IntegrationError(
                "Content delivery requires its exact prepared repository binding"
            )
        if not execution_key or len(execution_key) > 200:
            raise IntegrationError("GitHub execution key is invalid")
        if not title.strip() or len(title) > 200 or len(body) > 20_000:
            raise IntegrationError("GitHub pull-request title or body is invalid")
        if not 1 <= len(files) <= 10:
            raise IntegrationError("GitHub pull requests must contain between 1 and 10 files")
        if sum(len(item.content.encode()) for item in files) > 512_000:
            raise IntegrationError("GitHub pull-request files exceed the 512 KB limit")
        for item in files:
            if not _safe_github_path(item.path):
                raise IntegrationError("GitHub pull request contains an unsafe file path")
        branch = f"tin/{hashlib.sha256(execution_key.encode()).hexdigest()[:16]}"

        def request_fingerprint(repository):
            value = {
                "repository": repository,
                "branch": branch,
                "base_branch": base_branch,
                "expected_base_sha": expected_base_sha,
                "title": title.strip(),
                "body": body,
                "files": [{"path": item.path, "content": item.content} for item in files],
            }
            if expected_binding is not None:
                value["binding"] = {
                    "connection_id": str(expected_binding.connection_id),
                    "installation_id": expected_binding.installation_id,
                    "repository_id": expected_binding.repository_id,
                }
            if allow_unrelated_base_advance:
                value["allow_unrelated_base_advance"] = True
            return _sha256(_canonical_json(value))

        if expected_binding is not None:
            saved = await self._database.get_integration_call_receipt(execution_key)
            if saved and saved.status == "completed" and saved.response_summary is not None:
                if (
                    saved.project_id != project_id
                    or saved.run_id != run_id
                    or saved.connection_id != expected_binding.connection_id
                    or saved.provider_key != GITHUB_PROVIDER
                    or saved.capability != "pull_requests.write"
                    or saved.request_fingerprint != request_fingerprint(expected_binding.repository)
                ):
                    raise SideEffectConflictError(
                        "GitHub execution key belongs to a different request"
                    )
                # Reading an already committed effect needs no new provider permission
                # and must survive later disconnection. This path makes no GitHub call.
                return _github_pull_result(saved.response_summary)
        connection = await self._connection(project_id, GITHUB_PROVIDER)
        if expected_binding is not None:
            # Ambiguous delivery still needs the same provider identity to reconcile.
            await self._check_github_binding(project_id, connection, expected_binding, head=False)
        repository = connection.configuration.get("selected_repository")
        permissions = connection.configuration.get("permissions", {})
        if (
            not isinstance(repository, str)
            or repository.count("/") != 1
            or connection.configuration.get("write_opted_in") is not True
            or not isinstance(permissions, dict)
            or permissions.get("contents") != "write"
            or permissions.get("pull_requests") != "write"
        ):
            raise IntegrationAuthorizationError(
                "Choose a GitHub repository with write access first"
            )
        fingerprint = request_fingerprint(repository)
        async with self._database.integration_call_lock(execution_key):
            existing = await self._database.get_integration_call_receipt(execution_key)
            if existing is not None:
                if (
                    existing.project_id != project_id
                    or existing.run_id != run_id
                    or existing.provider_key != GITHUB_PROVIDER
                    or existing.capability != "pull_requests.write"
                    or existing.request_fingerprint != fingerprint
                ):
                    raise SideEffectConflictError(
                        "GitHub execution key belongs to a different request"
                    )
                if existing.status == "completed" and existing.response_summary is not None:
                    return _github_pull_result(existing.response_summary)
                recovered, request_id = await self._github_find_open_pull_request(
                    connection=connection,
                    repository=repository,
                    branch=branch,
                    base_branch=base_branch,
                )
                if recovered is not None:
                    if expected_binding is not None:
                        await self._github_validate_recovery(
                            connection=connection,
                            binding=expected_binding,
                            branch=branch,
                            files=files,
                            complete=True,
                        )
                    summary = {
                        "repository": recovered.repository,
                        "branch": recovered.branch,
                        "number": recovered.number,
                        "url": recovered.url,
                    }
                    await self._database.record_integration_call(
                        execution_key=execution_key,
                        project_id=project_id,
                        run_id=run_id,
                        connection_id=connection.id,
                        provider_key=GITHUB_PROVIDER,
                        capability="pull_requests.write",
                        request_fingerprint=fingerprint,
                        status="completed",
                        response_summary=summary,
                        provider_request_id=request_id,
                    )
                    return recovered
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection_id=connection.id,
                provider_key=GITHUB_PROVIDER,
                capability="pull_requests.write",
                request_fingerprint=fingerprint,
                status="started",
            )
            try:
                result, request_id = await self._github_write_pull_request(
                    connection=connection,
                    repository=repository,
                    branch=branch,
                    title=title.strip(),
                    body=body,
                    files=files,
                    base_branch=base_branch,
                    expected_base_sha=expected_base_sha,
                    **({"expected_binding": expected_binding} if expected_binding else {}),
                    **(
                        {"allow_unrelated_base_advance": True}
                        if allow_unrelated_base_advance
                        else {}
                    ),
                )
            except IntegrationError:
                await self._database.record_integration_call(
                    execution_key=execution_key,
                    project_id=project_id,
                    run_id=run_id,
                    connection_id=connection.id,
                    provider_key=GITHUB_PROVIDER,
                    capability="pull_requests.write",
                    request_fingerprint=fingerprint,
                    status="failed",
                    error_code="provider_request_failed",
                )
                raise
            summary = {
                "repository": result.repository,
                "branch": result.branch,
                "number": result.number,
                "url": result.url,
            }
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection_id=connection.id,
                provider_key=GITHUB_PROVIDER,
                capability="pull_requests.write",
                request_fingerprint=fingerprint,
                status="completed",
                response_summary=summary,
                provider_request_id=request_id,
            )
            return result

    async def github_commit_files(
        self,
        *,
        project_id: UUID,
        execution_key: str,
        message: str,
        files: tuple[GitHubFileChange, ...],
        run_id: UUID | None = None,
        base_branch: str | None = None,
        expected_binding: GitHubRepositoryBinding | None = None,
    ) -> GitHubCommitResult:
        """Commit reviewed files straight onto the default branch: no branch, no PR."""
        if not execution_key or len(execution_key) > 200:
            raise IntegrationError("GitHub execution key is invalid")
        if not message.strip() or len(message) > 200:
            raise IntegrationError("GitHub commit message is invalid")
        if not 1 <= len(files) <= 10:
            raise IntegrationError("GitHub commits must contain between 1 and 10 files")
        if sum(len(item.content.encode()) for item in files) > 512_000:
            raise IntegrationError("GitHub commit files exceed the 512 KB limit")
        for item in files:
            if not _safe_github_path(item.path):
                raise IntegrationError("GitHub commit contains an unsafe file path")

        def request_fingerprint(repository):
            value = {
                "repository": repository,
                "direct_commit": True,
                "base_branch": base_branch,
                "message": message.strip(),
                "files": [{"path": item.path, "content": item.content} for item in files],
            }
            if expected_binding is not None:
                value["binding"] = {
                    "connection_id": str(expected_binding.connection_id),
                    "installation_id": expected_binding.installation_id,
                    "repository_id": expected_binding.repository_id,
                }
            return _sha256(_canonical_json(value))

        def check(receipt, fingerprint):
            if (
                receipt.project_id != project_id
                or receipt.run_id != run_id
                or receipt.provider_key != GITHUB_PROVIDER
                or receipt.capability != "contents.write"
                or receipt.request_fingerprint != fingerprint
            ):
                raise SideEffectConflictError("GitHub execution key belongs to a different request")

        if expected_binding is not None:
            saved = await self._database.get_integration_call_receipt(execution_key)
            if saved and saved.status == "completed" and saved.response_summary is not None:
                check(saved, request_fingerprint(expected_binding.repository))
                if saved.connection_id != expected_binding.connection_id:
                    raise SideEffectConflictError(
                        "GitHub execution key belongs to a different request"
                    )
                # A committed effect survives later disconnection without a GitHub call.
                return _github_commit_result(saved.response_summary)
        connection = await self._connection(project_id, GITHUB_PROVIDER)
        if expected_binding is not None:
            # The default branch moves on its own; only the repository identity must hold.
            await self._check_github_binding(project_id, connection, expected_binding, head=False)
        repository = connection.configuration.get("selected_repository")
        permissions = connection.configuration.get("permissions", {})
        if (
            not isinstance(repository, str)
            or repository.count("/") != 1
            or connection.configuration.get("write_opted_in") is not True
            or not isinstance(permissions, dict)
            or permissions.get("contents") != "write"
        ):
            raise IntegrationAuthorizationError(
                "Choose a GitHub repository with write access first"
            )
        fingerprint = request_fingerprint(repository)

        async def record(status, **extra):
            await self._database.record_integration_call(
                execution_key=execution_key,
                project_id=project_id,
                run_id=run_id,
                connection_id=connection.id,
                provider_key=GITHUB_PROVIDER,
                capability="contents.write",
                request_fingerprint=fingerprint,
                status=status,
                **extra,
            )

        async with self._database.integration_call_lock(execution_key):
            existing = await self._database.get_integration_call_receipt(execution_key)
            if existing is not None:
                check(existing, fingerprint)
                if existing.status == "completed" and existing.response_summary is not None:
                    return _github_commit_result(existing.response_summary)
                recovered, request_id = await self._github_recover_commit(
                    connection=connection,
                    repository=repository,
                    files=files,
                    base_branch=base_branch,
                )
                if recovered is not None:
                    await record(
                        "completed",
                        response_summary=asdict(recovered),
                        provider_request_id=request_id,
                    )
                    return recovered
            await record("started")
            try:
                result, request_id = await self._github_write_commit(
                    connection=connection,
                    repository=repository,
                    message=message.strip(),
                    files=files,
                    base_branch=base_branch,
                )
            except IntegrationError:
                await record("failed", error_code="provider_request_failed")
                raise
            await record(
                "completed", response_summary=asdict(result), provider_request_id=request_id
            )
            return result

    async def _github_default_branch(self, *, headers, repository_path, base_branch):
        repo_response = await self._client.get(
            f"https://api.github.com/repos/{repository_path}", headers=headers
        )
        repo_payload = _provider_json(repo_response, provider="GitHub")
        default_branch = repo_payload.get("default_branch")
        resolved = base_branch or default_branch
        if not isinstance(resolved, str) or not _safe_github_ref(resolved):
            raise IntegrationAuthorizationError("GitHub base branch is invalid")
        if resolved != default_branch:
            raise IntegrationAuthorizationError(
                "Direct publishing writes only to the repository's default branch"
            )
        return resolved

    async def _github_current_file(self, *, headers, content_url, branch):
        """(sha, decoded content) of a file on ``branch``; (None, None) when absent."""
        response = await self._client.get(content_url, headers=headers, params={"ref": branch})
        if response.status_code == 404:
            return None, None
        payload = _provider_json(response, provider="GitHub")
        encoded = payload.get("content")
        content = None
        if isinstance(encoded, str):
            try:
                content = base64.b64decode(encoded.replace("\n", ""), validate=True).decode()
            except (ValueError, UnicodeDecodeError):
                content = None
        return payload.get("sha"), content

    async def _github_latest_commit(self, *, headers, repository_path, repository, branch, path):
        response = await self._client.get(
            f"https://api.github.com/repos/{repository_path}/commits",
            headers=headers,
            params={"sha": branch, "path": path, "per_page": 1},
        )
        commits = _provider_list(response, provider="GitHub")
        head = commits[0] if commits and isinstance(commits[0], dict) else {}
        return _github_commit_result(
            {
                "repository": repository,
                "branch": branch,
                "commit": head.get("sha"),
                "url": head.get("html_url"),
            }
        ), response.headers.get("x-github-request-id")

    async def _github_recover_commit(self, *, connection, repository, files, base_branch):
        """After an uncertain write, our exact files already on the branch are the commit."""
        token = await self._github_installation_token(_installation_id(connection))
        headers = self._github_headers(token)
        repository_path = quote(repository, safe="/")
        branch = await self._github_default_branch(
            headers=headers, repository_path=repository_path, base_branch=base_branch
        )
        for change in files:
            _sha, current = await self._github_current_file(
                headers=headers,
                content_url=(
                    f"https://api.github.com/repos/{repository_path}/contents/"
                    f"{quote(change.path, safe='/')}"
                ),
                branch=branch,
            )
            if current != change.content:
                return None, None
        return await self._github_latest_commit(
            headers=headers,
            repository_path=repository_path,
            repository=repository,
            branch=branch,
            path=files[0].path,
        )

    async def _github_write_commit(
        self,
        *,
        connection: IntegrationConnection,
        repository: str,
        message: str,
        files: tuple[GitHubFileChange, ...],
        base_branch: str | None,
    ) -> tuple[GitHubCommitResult, str | None]:
        token = await self._github_installation_token(_installation_id(connection))
        headers = self._github_headers(token)
        repository_path = quote(repository, safe="/")
        branch = await self._github_default_branch(
            headers=headers, repository_path=repository_path, base_branch=base_branch
        )
        result = None
        request_id = None
        for change in files:
            content_url = (
                f"https://api.github.com/repos/{repository_path}/contents/"
                f"{quote(change.path, safe='/')}"
            )
            current_sha, current = await self._github_current_file(
                headers=headers, content_url=content_url, branch=branch
            )
            if current == change.content:
                continue
            update_payload: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(change.content.encode()).decode(),
                "branch": branch,
            }
            if isinstance(current_sha, str) and current_sha:
                update_payload["sha"] = current_sha
            update_response = await self._client.put(
                content_url, headers=headers, json=update_payload
            )
            update_payload = _provider_json(update_response, provider="GitHub")
            request_id = update_response.headers.get("x-github-request-id") or request_id
            commit = update_payload.get("commit")
            commit = commit if isinstance(commit, dict) else {}
            result = _github_commit_result(
                {
                    "repository": repository,
                    "branch": branch,
                    "commit": commit.get("sha"),
                    "url": commit.get("html_url"),
                }
            )
        if result is None:
            # Every file already held this exact content; report the commit that did it.
            result, request_id = await self._github_latest_commit(
                headers=headers,
                repository_path=repository_path,
                repository=repository,
                branch=branch,
                path=files[0].path,
            )
        return result, request_id

    async def _github_find_open_pull_request(
        self,
        *,
        connection: IntegrationConnection,
        repository: str,
        branch: str,
        base_branch: str | None,
    ) -> tuple[GitHubPullRequestResult | None, str | None]:
        token = await self._github_installation_token(_installation_id(connection))
        headers = self._github_headers(token)
        repository_path = quote(repository, safe="/")
        resolved_base = base_branch
        if resolved_base is None:
            repo_response = await self._client.get(
                f"https://api.github.com/repos/{repository_path}", headers=headers
            )
            repo_payload = _provider_json(repo_response, provider="GitHub")
            candidate = repo_payload.get("default_branch")
            if not isinstance(candidate, str) or not _safe_github_ref(candidate):
                raise IntegrationAuthorizationError("GitHub base branch is invalid")
            resolved_base = candidate
        owner = repository.split("/", 1)[0]
        response = await self._client.get(
            f"https://api.github.com/repos/{repository_path}/pulls",
            headers=headers,
            # A lost creation response must also recover a PR closed before retry.
            params={"state": "all", "head": f"{owner}:{branch}", "base": resolved_base},
        )
        request_id = response.headers.get("x-github-request-id")
        pulls = _provider_list(response, provider="GitHub")
        if not pulls:
            return None, request_id
        payload = pulls[0]
        if not isinstance(payload, dict):
            raise IntegrationUpstreamError("GitHub returned an invalid pull request")
        number = payload.get("number")
        url = payload.get("html_url")
        if (
            not isinstance(number, int)
            or not isinstance(url, str)
            or not _safe_github_pull_url(url, repository=repository, number=number)
        ):
            raise IntegrationUpstreamError("GitHub returned an invalid pull request")
        return (
            GitHubPullRequestResult(
                repository=repository,
                branch=branch,
                number=number,
                url=url,
            ),
            request_id,
        )

    async def _github_read_open_pull_requests(
        self,
        *,
        connection: IntegrationConnection,
        repository: str,
        base_branch: str,
    ) -> tuple[GitHubOpenPullRequestEvidence, str | None]:
        token = await self._github_installation_token(_installation_id(connection))
        headers = self._github_headers(token)
        repository_path = quote(repository, safe="/")
        response = await self._client.get(
            f"https://api.github.com/repos/{repository_path}/pulls",
            headers=headers,
            params={
                "state": "open",
                "base": base_branch,
                "sort": "updated",
                "direction": "desc",
                "per_page": GITHUB_OPEN_PULL_REQUEST_LIMIT,
            },
        )
        pulls = _provider_list(response, provider="GitHub")
        truncated = _github_has_next_page(response)
        evidence_pulls: list[dict[str, Any]] = []
        changed_paths: set[str] = set()
        file_count = 0
        for raw_pull in pulls[:GITHUB_OPEN_PULL_REQUEST_LIMIT]:
            if not isinstance(raw_pull, dict):
                raise IntegrationUpstreamError("GitHub returned an invalid pull request")
            number = raw_pull.get("number")
            url = raw_pull.get("html_url")
            title = raw_pull.get("title")
            if (
                not isinstance(number, int)
                or not isinstance(url, str)
                or not _safe_github_pull_url(url, repository=repository, number=number)
                or not isinstance(title, str)
            ):
                raise IntegrationUpstreamError("GitHub returned an invalid pull request")
            remaining_files = GITHUB_OPEN_PULL_REQUEST_FILE_LIMIT - file_count
            raw_files: list[Any] = []
            files_response: httpx.Response | None = None
            if remaining_files > 0:
                files_response = await self._client.get(
                    f"https://api.github.com/repos/{repository_path}/pulls/{number}/files",
                    headers=headers,
                    params={"per_page": min(100, remaining_files)},
                )
                raw_files = _provider_list(files_response, provider="GitHub")
                if _github_has_next_page(files_response):
                    truncated = True
            else:
                truncated = True
            files: list[dict[str, str]] = []
            for raw_file in raw_files[:remaining_files]:
                if not isinstance(raw_file, dict):
                    raise IntegrationUpstreamError("GitHub returned an invalid pull-request file")
                path = raw_file.get("filename")
                if not isinstance(path, str) or not _safe_github_source_path(path):
                    truncated = True
                    continue
                changed_paths.add(path)
                item = {
                    "path": path,
                    "status": _bounded_provider_text(raw_file.get("status"), 40),
                }
                previous_path = raw_file.get("previous_filename")
                if isinstance(previous_path, str) and _safe_github_source_path(previous_path):
                    item["previous_path"] = previous_path
                    changed_paths.add(previous_path)
                patch = raw_file.get("patch")
                if isinstance(patch, str) and patch:
                    bounded_patch = _bounded_provider_text(patch, 1_000)
                    item["patch"] = bounded_patch
                    if bounded_patch != patch:
                        item["patch_truncated"] = "true"
                        truncated = True
                files.append(item)
                file_count += 1
            head = raw_pull.get("head")
            user = raw_pull.get("user")
            evidence_pulls.append(
                {
                    "number": number,
                    "title": _bounded_provider_text(title, 500),
                    "body": _bounded_provider_text(raw_pull.get("body"), 3_000),
                    "url": url,
                    "author": _bounded_provider_text(
                        user.get("login") if isinstance(user, dict) else "", 100
                    ),
                    "draft": raw_pull.get("draft") is True,
                    "updated_at": _bounded_provider_text(raw_pull.get("updated_at"), 80),
                    "head_ref": _bounded_provider_text(
                        head.get("ref") if isinstance(head, dict) else "", 255
                    ),
                    "head_sha": _bounded_provider_text(
                        head.get("sha") if isinstance(head, dict) else "", 80
                    ),
                    "files": files,
                }
            )
        if len(pulls) > GITHUB_OPEN_PULL_REQUEST_LIMIT:
            truncated = True
        document: dict[str, Any] = {
            "version": 1,
            "trust": "untrusted_reference_data",
            "repository": repository,
            "base_branch": base_branch,
            "limits": {
                "pull_requests": GITHUB_OPEN_PULL_REQUEST_LIMIT,
                "files": GITHUB_OPEN_PULL_REQUEST_FILE_LIMIT,
                "bytes": GITHUB_OPEN_PULL_REQUEST_EVIDENCE_MAX_BYTES,
            },
            "truncated": truncated,
            "pull_requests": evidence_pulls,
        }
        encoded = _canonical_json(document).encode()
        if len(encoded) > GITHUB_OPEN_PULL_REQUEST_EVIDENCE_MAX_BYTES:
            document["truncated"] = True
            for pull in reversed(evidence_pulls):
                for item in reversed(pull["files"]):
                    item.pop("patch", None)
                    item.pop("patch_truncated", None)
                    encoded = _canonical_json(document).encode()
                    if len(encoded) <= GITHUB_OPEN_PULL_REQUEST_EVIDENCE_MAX_BYTES:
                        break
                if len(encoded) <= GITHUB_OPEN_PULL_REQUEST_EVIDENCE_MAX_BYTES:
                    break
        if len(encoded) > GITHUB_OPEN_PULL_REQUEST_EVIDENCE_MAX_BYTES:
            raise IntegrationUpstreamError("GitHub pull-request evidence exceeds its safe limit")
        return (
            GitHubOpenPullRequestEvidence(
                document=encoded,
                pull_request_count=len(evidence_pulls),
                file_count=file_count,
                changed_paths=tuple(sorted(changed_paths)),
                truncated=bool(document["truncated"]),
            ),
            response.headers.get("x-github-request-id"),
        )

    async def _github_write_pull_request(
        self,
        *,
        connection: IntegrationConnection,
        repository: str,
        branch: str,
        title: str,
        body: str,
        files: tuple[GitHubFileChange, ...],
        base_branch: str | None,
        expected_base_sha: str | None,
        expected_binding: GitHubRepositoryBinding | None = None,
        allow_unrelated_base_advance: bool = False,
    ) -> tuple[GitHubPullRequestResult, str | None]:
        token = await self._github_installation_token(_installation_id(connection))
        headers = self._github_headers(token)
        repository_path = quote(repository, safe="/")
        repo_response = await self._client.get(
            f"https://api.github.com/repos/{repository_path}", headers=headers
        )
        repo_payload = _provider_json(repo_response, provider="GitHub")
        resolved_base = base_branch or repo_payload.get("default_branch")
        if not isinstance(resolved_base, str) or not _safe_github_ref(resolved_base):
            raise IntegrationAuthorizationError("GitHub base branch is invalid")
        ref_response = await self._client.get(
            (
                f"https://api.github.com/repos/{repository_path}/git/ref/heads/"
                f"{quote(resolved_base, safe='')}"
            ),
            headers=headers,
        )
        ref_payload = _provider_json(ref_response, provider="GitHub")
        target = ref_payload.get("object")
        base_sha = target.get("sha") if isinstance(target, dict) else None
        if not isinstance(base_sha, str) or not base_sha:
            raise IntegrationUpstreamError("GitHub base branch did not resolve to a commit")
        if expected_base_sha is not None and base_sha != expected_base_sha:
            if not allow_unrelated_base_advance or expected_binding is None:
                raise IntegrationAuthorizationError(
                    "The GitHub repository changed after analysis; start a new workflow run"
                )
            await self._github_validate_base_advance(
                connection=connection, binding=expected_binding, current_sha=base_sha, files=files
            )
            # Keep the immutable prepared branch base. GitHub's PR merge preserves
            # later unrelated commits; retry never rewrites the approved content.
            base_sha = expected_base_sha
        open_pull_requests, _request_id = await self._github_read_open_pull_requests(
            connection=connection,
            repository=repository,
            base_branch=resolved_base,
        )
        overlapping_paths = sorted(
            {item.path for item in files}.intersection(open_pull_requests.changed_paths)
        )
        if overlapping_paths:
            preview = ", ".join(overlapping_paths[:3])
            if len(overlapping_paths) > 3:
                preview += ", …"
            raise IntegrationAuthorizationError(
                "An open GitHub pull request already changes "
                f"{preview}; resolve it or choose a non-overlapping improvement"
            )
        if expected_binding is not None:
            if open_pull_requests.truncated:
                raise IntegrationAuthorizationError("Open pull-request evidence is incomplete")
            await self._check_github_binding(
                connection.project_id,
                connection,
                expected_binding,
                head=not allow_unrelated_base_advance,
            )
        create_ref = await self._client.post(
            f"https://api.github.com/repos/{repository_path}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        if create_ref.status_code != 422:
            _provider_json(create_ref, provider="GitHub")
        elif expected_binding is not None:
            await self._github_validate_recovery(
                connection=connection,
                binding=expected_binding,
                branch=branch,
                files=files,
                complete=False,
            )
        commit_message = f"Tin: {title}"[:200]
        for change in files:
            content_url = (
                f"https://api.github.com/repos/{repository_path}/contents/"
                f"{quote(change.path, safe='/')}"
            )
            current_response = await self._client.get(
                content_url,
                headers=headers,
                params={"ref": branch},
            )
            current_sha = None
            if current_response.status_code == 200:
                current_payload = _provider_json(current_response, provider="GitHub")
                current_sha = current_payload.get("sha")
                encoded_current = current_payload.get("content")
                if isinstance(encoded_current, str):
                    try:
                        current_content = base64.b64decode(
                            encoded_current.replace("\n", ""), validate=True
                        ).decode()
                    except (ValueError, UnicodeDecodeError):
                        current_content = None
                    if current_content == change.content:
                        continue
            elif current_response.status_code != 404:
                _provider_json(current_response, provider="GitHub")
            update_payload: dict[str, Any] = {
                "message": commit_message,
                "content": base64.b64encode(change.content.encode()).decode(),
                "branch": branch,
            }
            if isinstance(current_sha, str) and current_sha:
                update_payload["sha"] = current_sha
            update_response = await self._client.put(
                content_url,
                headers=headers,
                json=update_payload,
            )
            _provider_json(update_response, provider="GitHub")
        if expected_binding is not None:
            await self._github_validate_recovery(
                connection=connection,
                binding=expected_binding,
                branch=branch,
                files=files,
                complete=True,
            )
        pull_response = await self._client.post(
            f"https://api.github.com/repos/{repository_path}/pulls",
            headers=headers,
            json={
                "title": title,
                "body": body,
                "head": branch,
                "base": resolved_base,
            },
        )
        request_id = pull_response.headers.get("x-github-request-id")
        if pull_response.status_code == 422:
            owner = repository.split("/", 1)[0]
            existing_response = await self._client.get(
                f"https://api.github.com/repos/{repository_path}/pulls",
                headers=headers,
                params={"state": "open", "head": f"{owner}:{branch}", "base": resolved_base},
            )
            existing = _provider_list(existing_response, provider="GitHub")
            pull_payload = existing[0] if existing else None
        else:
            pull_payload = _provider_json(pull_response, provider="GitHub")
        if not isinstance(pull_payload, dict):
            raise IntegrationUpstreamError("GitHub did not return the pull request")
        number = pull_payload.get("number")
        url = pull_payload.get("html_url")
        if (
            not isinstance(number, int)
            or not isinstance(url, str)
            or not _safe_github_pull_url(url, repository=repository, number=number)
        ):
            raise IntegrationUpstreamError("GitHub returned an invalid pull request")
        return (
            GitHubPullRequestResult(
                repository=repository,
                branch=branch,
                number=number,
                url=url,
            ),
            request_id,
        )

    async def select_option(
        self, *, project_id: UUID, provider_key: str, option_id: str
    ) -> IntegrationConnection:
        if provider_key == POSTHOG_PROVIDER:
            return await self.posthog.select_project(project_id=project_id, option_id=option_id)
        if provider_key == GSC_PROVIDER:
            options = await self.google_sites(project_id=project_id)
            config_key = "selected_site_url"
        elif provider_key == GITHUB_PROVIDER:
            options = await self.github_repositories(project_id=project_id)
            config_key = "selected_repository"
        else:
            raise IntegrationError("unknown integration provider")
        option = next((item for item in options if item.id == option_id), None)
        if option is None:
            raise IntegrationAuthorizationError(
                "That selection is not available to the connected account"
            )
        connection = await self._connection(project_id, provider_key)
        configuration = dict(connection.configuration)
        configuration[config_key] = option.id
        updated = await self._database.update_integration_configuration(
            project_id=project_id,
            provider_key=provider_key,
            configuration=configuration,
            external_account_label=option.label,
        )
        await self._record_activity(
            updated,
            "integration_configured",
            f"{self._definition(provider_key).name} now uses {option.label}.",
            suffix=_sha256(option.id)[:12],
        )
        return updated

    async def disconnect(self, *, project_id: UUID, provider_key: str) -> bool:
        definition = self._definition(provider_key)
        if provider_key == ADS_PROVIDER:
            connection = await self._database.get_integration_connection(
                project_id=project_id, provider_key=provider_key
            )
            if connection is not None:
                await self._cancel_google_ads_link(connection)
        if provider_key == POSTHOG_PROVIDER:
            connection = await self._database.get_integration_connection(
                project_id=project_id, provider_key=provider_key
            )
            if connection is not None:
                # Best effort, like Google: local disconnection is authoritative.
                await self.posthog.revoke(connection)
        if provider_key in {GSC_PROVIDER, GOOGLE_WORKSPACE_PROVIDER}:
            connection = await self._database.get_integration_connection(
                project_id=project_id, provider_key=provider_key
            )
            if connection is not None and connection.credential_ciphertext is not None:
                try:
                    assert self._cipher is not None
                    refresh_token = self._cipher.decrypt(
                        connection.credential_ciphertext,
                        context=f"credential:{project_id}:{provider_key}",
                    )
                    await self._client.post(
                        "https://oauth2.googleapis.com/revoke",
                        params={"token": refresh_token},
                    )
                except Exception:
                    # Local disconnection is authoritative even when upstream revocation is down.
                    logger.warning(
                        "Google token revocation unavailable for project integration",
                        extra={"project_id": str(project_id), "provider_key": provider_key},
                    )
        deleted = await self._database.delete_integration_connection(
            project_id=project_id, provider_key=provider_key
        )
        if deleted:
            await self._database.record_integration_activity(
                project_id=project_id,
                event_type="integration_disconnected",
                summary=f"{definition.name} disconnected.",
                details={"kind": "integrations", "provider_key": provider_key},
                dedupe_key=f"integration:{project_id}:{provider_key}:disconnect:{uuid4()}",
            )
        return deleted

    async def handle_github_webhook(
        self,
        *,
        signature: str,
        delivery_id: str,
        event_type: str,
        body: bytes,
    ) -> bool:
        self._require_configured(GITHUB_PROVIDER)
        webhook_secret = self._settings.github_webhook_secret
        if webhook_secret is None:
            raise IntegrationNotConfiguredError("GitHub webhooks are not configured")
        expected = (
            "sha256="
            + hmac.new(webhook_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
        )
        if not hmac.compare_digest(signature, expected):
            raise IntegrationAuthorizationError("GitHub webhook signature is invalid")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise IntegrationAuthorizationError("GitHub webhook payload is invalid") from exc
        if not isinstance(payload, dict):
            raise IntegrationAuthorizationError("GitHub webhook payload is invalid")
        installation = payload.get("installation")
        installation_id = installation.get("id") if isinstance(installation, dict) else None
        connections = (
            await self._database.list_integration_connections_by_external_id(
                provider_key=GITHUB_PROVIDER,
                external_account_id=str(installation_id),
            )
            if isinstance(installation_id, int)
            else []
        )
        project_id = connections[0].project_id if len(connections) == 1 else None
        action = payload.get("action")
        removed = payload.get("repositories_removed", [])
        removed_names = {
            item.get("full_name")
            for item in removed
            if isinstance(item, dict) and item.get("full_name")
        }
        accepted = await self._database.record_integration_webhook_delivery(
            provider_key=GITHUB_PROVIDER,
            delivery_id=delivery_id,
            project_id=project_id,
            event_type=event_type[:120],
            payload_sha256=hashlib.sha256(body).hexdigest(),
            status="accepted" if connections else "ignored",
        )
        if not accepted:
            return False
        for connection in connections:
            needs_attention = (
                event_type == "installation" and action in {"deleted", "suspend"}
            ) or (
                event_type == "installation_repositories"
                and connection.configuration.get("selected_repository") in removed_names
            )
            if not needs_attention:
                continue
            await self._database.mark_integration_attention(
                project_id=connection.project_id,
                provider_key=GITHUB_PROVIDER,
                error_code="github_installation_changed",
            )
            await self._database.record_integration_activity(
                project_id=connection.project_id,
                event_type="integration_needs_attention",
                summary="GitHub needs attention after its repository access changed.",
                details={
                    "kind": "integrations",
                    "provider_key": GITHUB_PROVIDER,
                    "delivery_id": delivery_id,
                },
                dedupe_key=f"integration:github:webhook:{delivery_id}",
            )
        return True

    async def _gmail_find_message(
        self, *, access_token: str, rfc_message_id: str
    ) -> dict[str, str] | None:
        response = await self._client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": f"in:anywhere rfc822msgid:{rfc_message_id}", "maxResults": 1},
        )
        payload = _provider_json(response, provider="Gmail")
        messages = payload.get("messages", [])
        if not isinstance(messages, list) or not messages:
            return None
        first = messages[0]
        if not isinstance(first, dict):
            return None
        return _gmail_send_result(first)

    # ------------------------------------------------------------------ Google Ads

    @property
    def google_ads(self):
        """Tin's manager-account client; built once from settings, injectable for tests."""
        if self._google_ads is None:
            from tin_lite.google_ads import api_from_settings

            self._google_ads = api_from_settings(self._settings)
        if self._google_ads is None:
            raise IntegrationNotConfiguredError(
                "Google Ads is not configured on this Tin deployment"
            )
        return self._google_ads

    async def connect_google_ads(
        self, *, project_id: UUID, customer_id: str, clerk_user_id: str
    ) -> IntegrationConnection:
        """Record the founder's account and send Tin's manager invitation to it.

        Nothing about the founder's Google identity is stored; accepting the invitation inside
        their own Google Ads account is the proof of ownership.
        """
        self._require_configured(ADS_PROVIDER)
        from tin_lite.google_ads import GoogleAdsError
        from tin_lite.google_ads_requests import customer_id as normalize_customer_id

        try:
            account = normalize_customer_id(customer_id)
        except ValueError as exc:
            raise IntegrationError(
                "Enter the ten-digit Google Ads customer id, for example 123-456-7890"
            ) from exc
        manager = normalize_customer_id(self._settings.google_ads_manager_customer_id)
        if account == manager:
            raise IntegrationError("Enter your own Google Ads customer id, not Tin's")
        existing = await self._database.get_integration_connection(
            project_id=project_id, provider_key=ADS_PROVIDER
        )
        if (
            existing is not None
            and existing.external_account_id not in {None, account}
            and existing.configuration.get("link_status") == "active"
        ):
            raise IntegrationAuthorizationError(
                "Disconnect the linked Google Ads account before connecting another one"
            )
        configuration = {
            **(dict(existing.configuration) if existing is not None else {}),
            "customer_id": account,
            "manager_customer_id": manager,
            "link_status": "pending",
            "manager_link_id": None,
            "write_opted_in": True,
            "health": {},
            "invited_at": datetime.now(UTC).isoformat(),
        }
        connection = await self._database.upsert_integration_connection(
            project_id=project_id,
            provider_key=ADS_PROVIDER,
            external_account_id=account,
            external_account_label=f"Google Ads {_format_customer_id(account)}",
            configuration=configuration,
            credential_ciphertext=None,
            credential_key_version=None,
            connected_by_clerk_user_id=clerk_user_id,
        )
        execution_key = f"integration:{uuid4()}"
        fingerprint = _sha256(f"{project_id}:{ADS_PROVIDER}:link:{account}")
        try:
            result = await self.google_ads.client_link(account)
        except GoogleAdsError as exc:
            await self._record_ads_call(
                execution_key=execution_key,
                connection=connection,
                capability="account.read",
                fingerprint=fingerprint,
                status="completed" if exc.code in ADS_ALREADY_LINKED else "failed",
                response_summary={"code": exc.code},
                error_code=None if exc.code in ADS_ALREADY_LINKED else exc.code[:120],
            )
            if exc.code in ADS_ALREADY_LINKED:
                return await self.google_ads_link_status(project_id=project_id)
            raise IntegrationUpstreamError(
                ADS_LINK_MESSAGES.get(
                    exc.code,
                    "Google Ads could not send the manager invitation. Check the customer id "
                    "and try again.",
                )
            ) from None
        await self._record_ads_call(
            execution_key=execution_key,
            connection=connection,
            capability="account.read",
            fingerprint=fingerprint,
            status="completed",
            response_summary={"resource_name": result.get("resource_name")},
            provider_request_id=result.get("provider_request_id"),
        )
        configuration["manager_link_id"] = _manager_link_id(result.get("resource_name"))
        updated = await self._database.update_integration_configuration(
            project_id=project_id, provider_key=ADS_PROVIDER, configuration=configuration
        )
        await self._record_activity(
            updated,
            "integration_connected",
            f"Google Ads invitation sent to {_format_customer_id(account)}. Accept it in "
            "Google Ads under Admin, Access and security, Managers.",
            suffix=f"{account}:{configuration['invited_at']}",
        )
        return updated

    async def google_ads_link_status(self, *, project_id: UUID) -> IntegrationConnection:
        """Read the manager link from Tin's side and record the founder's answer."""
        from tin_lite.google_ads import GoogleAdsError
        from tin_lite.google_ads_requests import QUERIES

        connection = await self._connection(project_id, ADS_PROVIDER)
        account = _ads_account(connection)
        manager = connection.configuration.get("manager_customer_id") or ""
        execution_key = f"integration:{uuid4()}"
        fingerprint = _sha256(f"{project_id}:{ADS_PROVIDER}:link_status:{account}")
        try:
            result = await self.google_ads.search(manager, QUERIES["client_link_status"](account))
        except GoogleAdsError as exc:
            await self._record_ads_call(
                execution_key=execution_key,
                connection=connection,
                capability="account.read",
                fingerprint=fingerprint,
                status="failed",
                error_code=exc.code[:120],
            )
            raise IntegrationUpstreamError(
                "Google Ads did not answer the link status check. Try again in a minute."
            ) from None
        rows = result.get("rows") or []
        raw = None
        for row in rows:
            link = row.get("customerClientLink") if isinstance(row, dict) else None
            if isinstance(link, dict) and isinstance(link.get("status"), str):
                raw = link
                # Prefer an active link over stale refused or canceled rows.
                if link["status"] == "ACTIVE":
                    break
        status = ADS_LINK_STATES.get(raw["status"], "pending") if raw else "missing"
        await self._record_ads_call(
            execution_key=execution_key,
            connection=connection,
            capability="account.read",
            fingerprint=fingerprint,
            status="completed",
            response_summary={"link_status": status, "rows": len(rows)},
            provider_request_id=result.get("provider_request_id"),
        )
        previous = connection.configuration.get("link_status")
        configuration = {
            **dict(connection.configuration),
            "link_status": status,
            "manager_link_id": (
                str(raw.get("managerLinkId"))
                if raw and raw.get("managerLinkId") is not None
                else connection.configuration.get("manager_link_id")
            ),
            "link_checked_at": datetime.now(UTC).isoformat(),
        }
        updated = await self._database.update_integration_configuration(
            project_id=project_id, provider_key=ADS_PROVIDER, configuration=configuration
        )
        if status in {"refused", "canceled", "inactive", "missing"}:
            await self._database.mark_integration_attention(
                project_id=project_id,
                provider_key=ADS_PROVIDER,
                error_code=f"manager_link_{status}",
            )
            updated = await self._connection(project_id, ADS_PROVIDER)
        if status == "active" and previous != "active":
            await self._record_activity(
                updated,
                "integration_configured",
                f"Google Ads account {_format_customer_id(account)} is linked to Tin's manager "
                "account.",
                suffix=f"{account}:active",
            )
        return updated

    async def google_ads_health(self, *, project_id: UUID) -> IntegrationConnection:
        """Account status, billing and whether any conversion action records data."""
        from tin_lite.google_ads import GoogleAdsError
        from tin_lite.google_ads_requests import QUERIES

        connection = await self._connection(project_id, ADS_PROVIDER)
        if connection.configuration.get("link_status") != "active":
            raise IntegrationAuthorizationError(
                "Accept Tin's manager request in Google Ads before checking the account"
            )
        account = _ads_account(connection)
        reads: dict[str, list] = {}
        for name, query in (
            ("account", QUERIES["account"]()),
            ("billing", QUERIES["billing"]()),
            ("conversions", QUERIES["conversion_actions"](30)),
        ):
            execution_key = f"integration:{uuid4()}"
            fingerprint = _sha256(f"{project_id}:{ADS_PROVIDER}:health:{name}:{account}")
            try:
                result = await self.google_ads.search(account, query)
            except GoogleAdsError as exc:
                await self._record_ads_call(
                    execution_key=execution_key,
                    connection=connection,
                    capability="account.read",
                    fingerprint=fingerprint,
                    status="failed",
                    error_code=exc.code[:120],
                )
                if exc.code.startswith("AuthorizationError."):
                    raise IntegrationAuthorizationError(
                        "Google Ads has not granted Tin's manager account access yet"
                    ) from None
                raise IntegrationUpstreamError(
                    "Google Ads did not answer the account check. Try again in a minute."
                ) from None
            reads[name] = result.get("rows") or []
            await self._record_ads_call(
                execution_key=execution_key,
                connection=connection,
                capability="account.read",
                fingerprint=fingerprint,
                status="completed",
                response_summary={"rows": len(reads[name])},
                provider_request_id=result.get("provider_request_id"),
            )
        health = ads_health_summary(reads)
        configuration = {**dict(connection.configuration), "health": health}
        return await self._database.update_integration_configuration(
            project_id=project_id,
            provider_key=ADS_PROVIDER,
            configuration=configuration,
            external_account_label=(
                f"{health['descriptive_name']} · {_format_customer_id(account)}"
                if health.get("descriptive_name")
                else None
            ),
        )

    async def refresh_google_ads(self, *, project_id: UUID) -> IntegrationConnection:
        connection = await self.google_ads_link_status(project_id=project_id)
        if connection.configuration.get("link_status") == "active":
            connection = await self.google_ads_health(project_id=project_id)
        return connection

    async def google_ads_account(self, *, project_id: UUID) -> str:
        """The linked customer id for a run; refuses unless the manager link is active."""
        connection = await self._connection(project_id, ADS_PROVIDER)
        if connection.status != "connected":
            raise IntegrationAuthorizationError("Google Ads needs attention")
        if connection.configuration.get("link_status") != "active":
            raise IntegrationAuthorizationError("Accept Tin's manager request in Google Ads first")
        return _ads_account(connection)

    async def google_ads_call(
        self,
        *,
        project_id: UUID,
        kind: str,
        request: dict[str, Any],
        execution_key: str,
        run_id: UUID | None = None,
        expected_customer_id: str | None = None,
    ) -> dict[str, Any]:
        """One receipted Google Ads request for a run: search, bulk mutate or resource mutate.

        `kind` is search | mutate | mutate_resource; `request` carries `query`, or `operations`
        and `validate_only`, or `segment` and `body`. Writes require the connection's explicit
        opt-in. Provider failures surface as GoogleAdsCallError with the opaque error code so
        the owning receipt can keep it; nothing else from the provider leaves this method.
        """
        from tin_lite.google_ads import GoogleAdsError

        connection = await self._connection(project_id, ADS_PROVIDER)
        account = await self.google_ads_account(project_id=project_id)
        if expected_customer_id is not None and account != expected_customer_id:
            raise IntegrationAuthorizationError(
                "The linked Google Ads account changed after this run started"
            )
        write = kind in {"mutate", "mutate_resource"}
        if write and connection.configuration.get("write_opted_in") is not True:
            raise IntegrationAuthorizationError("Google Ads campaign changes are not enabled")
        capability = "campaigns.write" if write else "campaigns.read"
        fingerprint = _sha256(_canonical_json({"kind": kind, "account": account, **request}))
        try:
            if kind == "search":
                result = await self.google_ads.search(account, request["query"])
            elif kind == "mutate":
                result = await self.google_ads.mutate(
                    account,
                    request["operations"],
                    validate_only=bool(request.get("validate_only", False)),
                )
            elif kind == "mutate_resource":
                result = await self.google_ads.mutate_resource(
                    account, request["segment"], request["body"]
                )
            else:
                raise IntegrationError("unknown Google Ads request kind")
        except GoogleAdsError as exc:
            await self._record_ads_call(
                execution_key=execution_key,
                run_id=run_id,
                connection=connection,
                capability=capability,
                fingerprint=fingerprint,
                status="failed",
                error_code=exc.code[:120],
            )
            raise GoogleAdsCallError(exc.code) from None
        await self._record_ads_call(
            execution_key=execution_key,
            run_id=run_id,
            connection=connection,
            capability=capability,
            fingerprint=fingerprint,
            status="completed",
            response_summary={
                "rows": len(result.get("rows") or []),
                "results": len(result.get("results") or []),
            },
            provider_request_id=result.get("provider_request_id"),
        )
        return {**result, "customer_id": account}

    async def _cancel_google_ads_link(self, connection: IntegrationConnection) -> None:
        """Best effort: the local disconnect is authoritative even when Google is down."""
        link = connection.configuration.get("link_status")
        manager_link_id = connection.configuration.get("manager_link_id")
        if link not in {"pending", "active"} or not manager_link_id:
            return
        try:
            await self.google_ads.client_link_update(
                _ads_account(connection),
                str(manager_link_id),
                "CANCELED" if link == "pending" else "INACTIVE",
            )
        except Exception:
            logger.warning(
                "Google Ads manager link could not be ended",
                extra={"project_id": str(connection.project_id)},
            )

    async def _record_ads_call(
        self,
        *,
        execution_key: str,
        connection: IntegrationConnection,
        capability: str,
        fingerprint: str,
        status: str,
        run_id: UUID | None = None,
        response_summary: dict[str, Any] | None = None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._database.record_integration_call(
            execution_key=execution_key,
            project_id=connection.project_id,
            run_id=run_id,
            connection_id=connection.id,
            provider_key=ADS_PROVIDER,
            capability=capability,
            request_fingerprint=fingerprint,
            status=status,
            response_summary=response_summary,
            provider_request_id=provider_request_id,
            error_code=error_code,
        )

    async def _google_access_token(self, connection: IntegrationConnection) -> str:
        if connection.credential_ciphertext is None or self._cipher is None:
            raise IntegrationAuthorizationError("Google integration must be reconnected")
        async with self._database.integration_refresh_lock(
            project_id=connection.project_id, provider_key=connection.provider_key
        ):
            refresh_token = self._cipher.decrypt(
                connection.credential_ciphertext,
                context=f"credential:{connection.project_id}:{connection.provider_key}",
            )
            response = await self._client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self._settings.google_oauth_client_id,
                    "client_secret": self._settings.google_oauth_client_secret.get_secret_value(),
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            try:
                payload = _provider_json(response, provider="Google")
            except IntegrationUpstreamError:
                if response.status_code in {400, 401}:
                    await self._database.mark_integration_attention(
                        project_id=connection.project_id,
                        provider_key=connection.provider_key,
                        error_code="reauthorization_required",
                    )
                raise
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise IntegrationUpstreamError("Google did not return an access token")
        return token

    async def _github_installation_token(self, installation_id: int) -> str:
        response = await self._client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers=self._github_headers(await self._github_jwt()),
        )
        payload = _provider_json(response, provider="GitHub")
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise IntegrationUpstreamError("GitHub did not return an installation token")
        return token

    async def _github_jwt(self) -> str:
        private_key_path = self._settings.github_app_private_key_path
        if private_key_path is None:
            raise IntegrationNotConfiguredError("GitHub is not configured")
        try:
            private_key = serialization.load_pem_private_key(
                await AsyncPath(private_key_path).read_bytes(), password=None
            )
        except (OSError, ValueError) as exc:
            raise IntegrationNotConfiguredError("GitHub App private key is unavailable") from exc
        now = int(time.time())
        header = _base64url_json({"alg": "RS256", "typ": "JWT"})
        claims = _base64url_json(
            {"iat": now - 30, "exp": now + 540, "iss": self._settings.github_app_id}
        )
        signing_input = f"{header}.{claims}".encode()
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{_base64url(signature)}"

    def _github_authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._settings.github_app_client_id,
                "redirect_uri": self._callback_url("github"),
                "state": state,
            }
        )
        return f"https://github.com/login/oauth/authorize?{query}"

    async def _github_install_url(self, attempt: IntegrationAuthAttempt) -> str:
        """A fresh install-page attempt for the same project; GitHub returns code and id."""
        state = secrets.token_urlsafe(32)
        await self._database.create_integration_auth_attempt(
            token_hash=_sha256(state),
            project_id=attempt.project_id,
            provider_key=GITHUB_PROVIDER,
            clerk_user_id=attempt.clerk_user_id,
            pkce_verifier_ciphertext=None,
            requested_capabilities=attempt.requested_capabilities,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        query = urlencode({"state": state})
        return f"https://github.com/apps/{self._settings.github_app_slug}/installations/new?{query}"

    async def _github_resolve_user_installation(
        self, user_token: str, *, attempt: IntegrationAuthAttempt
    ) -> int:
        """The one installation of this app the authorizing user can reach.

        None means the user must install the app: a fresh install-page attempt is returned
        so the callback comes back with a code. Several means the member must choose; the
        choice completes through a remembered-installation authorization.
        """
        response = await self._client.get(
            "https://api.github.com/user/installations",
            headers=self._github_headers(user_token),
            params={"per_page": 100},
        )
        payload = _provider_json(response, provider="GitHub")
        wanted = str(self._settings.github_app_id)
        found = [
            item
            for item in payload.get("installations", [])
            if isinstance(item, dict)
            and str(item.get("app_id")) == wanted
            and isinstance(item.get("id"), int)
        ]
        if not found:
            raise GitHubInstallationRequiredError(
                "Install the Tin GitHub App on the account that owns your repository",
                install_url=await self._github_install_url(attempt),
                project_id=attempt.project_id,
            )
        if len(found) > 1:
            choices = sorted(
                (
                    {
                        "installation_id": int(item["id"]),
                        "account": str((item.get("account") or {}).get("login") or item["id"]),
                    }
                    for item in found
                ),
                key=lambda choice: choice["account"],
            )
            accounts = ", ".join(choice["account"] for choice in choices)
            raise GitHubInstallationChoiceError(
                f"Choose which GitHub account to connect; the Tin app is installed on {accounts}",
                choices=choices,
                project_id=attempt.project_id,
            )
        return int(found[0]["id"])

    async def _github_tarball(
        self, repository_path: str, head_sha: str, headers: dict[str, str], sink: Any
    ) -> None:
        url = f"https://api.github.com/repos/{repository_path}/tarball/{head_sha}"
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code == 200:
                await _download_bounded(response, sink)
                return
            location = response.headers.get("location", "")
            if not response.is_redirect or not location:
                raise IntegrationUpstreamError(
                    f"GitHub could not complete the request ({response.status_code})"
                )
        target = urlsplit(location)
        if (
            target.scheme != "https"
            or target.hostname != "codeload.github.com"
            or target.port not in {None, 443}
            or target.username is not None
            or target.password is not None
        ):
            raise IntegrationUpstreamError("GitHub redirected the repository tarball unexpectedly")
        # The signed codeload URL carries its own short-lived grant; never forward the token.
        public = {key: value for key, value in headers.items() if key != "Authorization"}
        async with self._client.stream("GET", location, headers=public) as response:
            if response.status_code != 200:
                raise IntegrationUpstreamError(
                    f"GitHub could not complete the request ({response.status_code})"
                )
            await _download_bounded(response, sink)

    async def _github_blob_content(
        self, repository_path: str, headers: dict[str, str], item: dict[str, Any]
    ) -> bytes:
        blob_response = await self._client.get(
            f"https://api.github.com/repos/{repository_path}/git/blobs/{item['sha']}",
            headers=headers,
        )
        blob_payload = _provider_json(blob_response, provider="GitHub")
        encoded = blob_payload.get("content")
        if not isinstance(encoded, str) or blob_payload.get("encoding") != "base64":
            raise IntegrationUpstreamError("GitHub repository blob is unreadable")
        try:
            content = base64.b64decode("".join(encoded.split()), validate=True)
        except ValueError as exc:
            raise IntegrationUpstreamError("GitHub repository blob is invalid") from exc
        if len(content) != item["size"] or _git_blob_sha(content, item["sha"]) != item["sha"]:
            raise IntegrationUpstreamError("GitHub repository blob does not match the pinned tree")
        return content

    def _github_headers(self, token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _consume_attempt(
        self, *, state: str, provider_key: str, clerk_user_id: str
    ) -> IntegrationAuthAttempt:
        if not state or len(state) > 256:
            raise IntegrationAuthorizationError("connection attempt has expired")
        attempt = await self._database.consume_integration_auth_attempt(
            token_hash=_sha256(state),
            provider_key=provider_key,
            clerk_user_id=clerk_user_id,
        )
        if attempt is None:
            raise IntegrationAuthorizationError("connection attempt has expired")
        return attempt

    async def _consume_google_attempt(
        self, *, state: str, clerk_user_id: str
    ) -> IntegrationAuthAttempt:
        if not state or len(state) > 256:
            raise IntegrationAuthorizationError("connection attempt has expired")
        attempt = await self._database.consume_google_integration_auth_attempt(
            token_hash=_sha256(state), clerk_user_id=clerk_user_id
        )
        if attempt is None:
            raise IntegrationAuthorizationError("connection attempt has expired")
        return attempt

    async def _connection(self, project_id: UUID, provider_key: str) -> IntegrationConnection:
        connection = await self._database.get_integration_connection(
            project_id=project_id, provider_key=provider_key
        )
        if connection is None:
            raise IntegrationAuthorizationError("integration is not connected")
        return connection

    async def _workspace_connection(
        self,
        *,
        project_id: UUID,
        capability: str,
        connection_id: UUID | None = None,
        external_account_id: str | None = None,
    ) -> IntegrationConnection:
        connection = await self._connection(project_id, GOOGLE_WORKSPACE_PROVIDER)
        if connection_id is not None and connection.id != connection_id:
            raise IntegrationAuthorizationError(
                "Google Workspace connection changed after this run started"
            )
        if (
            external_account_id is not None
            and connection.external_account_id != external_account_id
        ):
            raise IntegrationAuthorizationError(
                "Google Workspace account changed after this run started"
            )
        granted = connection.configuration.get("granted_capabilities", [])
        if connection.status != "connected" or not isinstance(granted, list):
            raise IntegrationAuthorizationError("Google Workspace is not connected")
        if capability not in granted:
            raise IntegrationAuthorizationError(
                "Google Workspace does not grant the required capability"
            )
        return connection

    async def _record_workspace_call(
        self,
        *,
        execution_key: str,
        project_id: UUID,
        run_id: UUID,
        connection: IntegrationConnection,
        capability: str,
        fingerprint: str,
        status: str,
        response_summary: dict[str, Any] | None = None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._database.record_integration_call(
            execution_key=execution_key,
            project_id=project_id,
            run_id=run_id,
            connection_id=connection.id,
            provider_key=GOOGLE_WORKSPACE_PROVIDER,
            capability=capability,
            request_fingerprint=fingerprint,
            status=status,
            response_summary=response_summary,
            provider_request_id=provider_request_id,
            error_code=error_code,
        )

    async def _record_activity(
        self,
        connection: IntegrationConnection,
        event_type: str,
        summary: str,
        *,
        suffix: str,
    ) -> None:
        await self._database.record_integration_activity(
            project_id=connection.project_id,
            event_type=event_type,
            summary=summary,
            details={
                "kind": "integrations",
                "provider_key": connection.provider_key,
                "connection_id": str(connection.id),
            },
            dedupe_key=(
                f"integration:{connection.project_id}:{connection.provider_key}:"
                f"{event_type}:{suffix}"
            ),
        )

    def _callback_url(self, provider: str) -> str:
        return (
            f"{self._settings.switchboard_public_url.rstrip('/')}/integrations/callback/{provider}"
        )

    def _require_configured(self, provider_key: str) -> None:
        if not self.is_configured(provider_key):
            raise IntegrationNotConfiguredError(
                f"{self._definition(provider_key).name} is not configured on this Tin deployment"
            )

    @staticmethod
    def _definition(provider_key: str) -> IntegrationDefinition:
        from tin_lite.project_connections import CUSTOM_KEY, custom_definition

        if CUSTOM_KEY.fullmatch(provider_key):
            return custom_definition(provider_key)
        definition = next(
            (item for item in registered_integrations() if item.key == provider_key), None
        )
        if definition is None:
            raise IntegrationError("unknown integration provider")
        return definition


def _format_customer_id(value: str) -> str:
    return f"{value[:3]}-{value[3:6]}-{value[6:]}" if len(value) == 10 else value


def _ads_account(connection: IntegrationConnection) -> str:
    account = connection.configuration.get("customer_id") or connection.external_account_id
    if not isinstance(account, str) or not account.isdigit() or len(account) != 10:
        raise IntegrationAuthorizationError("Google Ads is not connected to an account")
    return account


def _manager_link_id(resource_name: Any) -> str | None:
    if not isinstance(resource_name, str) or "~" not in resource_name:
        return None
    tail = resource_name.rsplit("~", 1)[-1]
    return tail if tail.isdigit() else None


def ads_health_summary(reads: dict[str, list]) -> dict[str, Any]:
    """Bounded facts from the account, billing and conversion-action queries."""

    def first(rows, key):
        for row in rows:
            value = row.get(key) if isinstance(row, dict) else None
            if isinstance(value, dict):
                return value
        return {}

    customer = first(reads.get("account") or [], "customer")
    tracking = customer.get("conversionTrackingSetting") or {}
    billing_statuses = sorted(
        {
            str((row.get("billingSetup") or {}).get("status"))
            for row in reads.get("billing") or []
            if isinstance(row, dict) and isinstance(row.get("billingSetup"), dict)
        }
    )
    actions = []
    for row in reads.get("conversions") or []:
        if not isinstance(row, dict) or not isinstance(row.get("conversionAction"), dict):
            continue
        action = row["conversionAction"]
        metrics = row.get("metrics") or {}
        try:
            conversions = float(metrics.get("allConversions") or 0)
        except (TypeError, ValueError):
            conversions = 0.0
        actions.append(
            {
                "resource_name": action.get("resourceName"),
                "id": str(action.get("id") or ""),
                "name": str(action.get("name") or "")[:120],
                "category": action.get("category"),
                "status": action.get("status"),
                "type": action.get("type"),
                "primary_for_goal": bool(action.get("primaryForGoal")),
                "conversions_30d": conversions,
            }
        )
    actions.sort(key=lambda item: -item["conversions_30d"])
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "descriptive_name": str(customer.get("descriptiveName") or "")[:120],
        "account_status": customer.get("status"),
        "currency_code": customer.get("currencyCode"),
        "time_zone": customer.get("timeZone"),
        "auto_tagging_enabled": bool(customer.get("autoTaggingEnabled")),
        "conversion_tracking_status": tracking.get("conversionTrackingStatus"),
        "accepted_customer_data_terms": bool(tracking.get("acceptedCustomerDataTerms")),
        "conversion_tracking_id": str(tracking.get("conversionTrackingId") or ""),
        "billing_statuses": billing_statuses,
        "billing_approved": "APPROVED" in billing_statuses,
        "conversion_actions": actions[:50],
        "conversion_actions_with_data": sum(1 for a in actions if a["conversions_30d"] >= 1),
    }


def _test_identity_context(project_id: UUID, identity_id: UUID) -> str:
    return f"test-identity:{project_id}:{identity_id}"


def _google_scopes(provider_key: str, capabilities: tuple[str, ...]) -> set[str]:
    if provider_key == GSC_PROVIDER:
        return {GOOGLE_SCOPE}
    if provider_key != GOOGLE_WORKSPACE_PROVIDER:
        raise IntegrationError("provider does not use Google OAuth")
    try:
        scopes = {WORKSPACE_CAPABILITY_SCOPES[capability] for capability in capabilities}
    except KeyError as exc:
        raise IntegrationAuthorizationError(
            "requested Google Workspace capability is unsupported"
        ) from exc
    return scopes | set(GOOGLE_IDENTITY_SCOPES)


def _workspace_capabilities_for_scopes(scopes: set[str]) -> set[str]:
    return {
        capability for capability, scope in WORKSPACE_CAPABILITY_SCOPES.items() if scope in scopes
    }


def _aware_datetime(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrationError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise IntegrationError(f"{field} must include a timezone")
    return parsed


def _workspace_message(value: dict[str, Any]) -> dict[str, Any]:
    payload = value.get("payload")
    headers: dict[str, str] = {}
    if isinstance(payload, dict):
        for item in payload.get("headers", []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            header_value = item.get("value")
            if isinstance(name, str) and isinstance(header_value, str):
                lowered = name.casefold()
                if lowered in {"from", "to", "cc", "subject", "date", "message-id"}:
                    headers[lowered.replace("-", "_")] = _bounded_provider_text(header_value, 2_000)
    raw_labels = value.get("labelIds", [])
    labels = raw_labels if isinstance(raw_labels, list) else []
    return {
        "id": str(value.get("id", ""))[:128],
        "thread_id": str(value.get("threadId", ""))[:128],
        "internal_date": str(value.get("internalDate", ""))[:32],
        "labels": [str(item)[:100] for item in labels[:30]],
        "headers": headers,
        "snippet": _bounded_provider_text(value.get("snippet", ""), 4_000),
        "body": _workspace_message_body(payload) if isinstance(payload, dict) else "",
    }


def _workspace_message_body(payload: dict[str, Any]) -> str:
    plain: list[str] = []
    html: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType")
        body = part.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, str) and mime_type in {"text/plain", "text/html"}:
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", errors="replace"
                )
            except (ValueError, binascii.Error):
                decoded = ""
            (plain if mime_type == "text/plain" else html).append(decoded)
        for child in part.get("parts", []) if isinstance(part.get("parts"), list) else []:
            if isinstance(child, dict):
                visit(child)

    visit(payload)
    content = "\n\n".join(plain or html)
    return _bounded_provider_text(content, 40_000)


def _workspace_calendar_event(value: dict[str, Any]) -> dict[str, Any]:
    def moment(field: str) -> str | None:
        item = value.get(field)
        if not isinstance(item, dict):
            return None
        raw = item.get("dateTime", item.get("date"))
        return str(raw)[:64] if isinstance(raw, str) else None

    raw_attendees = value.get("attendees", [])
    attendees = raw_attendees if isinstance(raw_attendees, list) else []
    return {
        "id": str(value.get("id", ""))[:256],
        "status": str(value.get("status", ""))[:40],
        "summary": _bounded_provider_text(value.get("summary", ""), 2_000),
        "description": _bounded_provider_text(value.get("description", ""), 4_000),
        "location": _bounded_provider_text(value.get("location", ""), 1_000),
        "start": moment("start"),
        "end": moment("end"),
        "organizer": _calendar_email(value.get("organizer")),
        "attendees": [
            email
            for email in (_calendar_email(item) for item in attendees[:50])
            if email is not None
        ],
    }


def _calendar_email(value: Any) -> str | None:
    email = value.get("email") if isinstance(value, dict) else None
    return email.casefold()[:320] if isinstance(email, str) and "@" in email else None


def _gmail_send_result(value: dict[str, Any]) -> dict[str, str]:
    message_id = value.get("id")
    thread_id = value.get("threadId", value.get("thread_id"))
    if (
        not isinstance(message_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id)
        or not isinstance(thread_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id)
    ):
        raise IntegrationUpstreamError("Gmail returned an invalid message receipt")
    return {"id": message_id, "thread_id": thread_id}


def _gmail_message_is_reply(value: dict[str, Any], *, sender_email: str, after: datetime) -> bool:
    raw_internal_date = value.get("internalDate")
    try:
        internal_date = datetime.fromtimestamp(int(str(raw_internal_date)) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return False
    if internal_date <= after.astimezone(UTC):
        return False
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return False
    for header in payload.get("headers", []):
        if not isinstance(header, dict) or str(header.get("name", "")).casefold() != "from":
            continue
        _name, address = parseaddr(str(header.get("value", "")))
        return bool(address and address.casefold() != sender_email.casefold())
    return False


def _provider_json(response: httpx.Response, *, provider: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise IntegrationUpstreamError(f"{provider} returned an invalid response") from exc
    if response.is_error:
        raise IntegrationUpstreamError(
            f"{provider} could not complete the request ({response.status_code})"
        )
    if not isinstance(payload, dict):
        raise IntegrationUpstreamError(f"{provider} returned an invalid response")
    return payload


def _provider_list(response: httpx.Response, *, provider: str) -> list[Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise IntegrationUpstreamError(f"{provider} returned an invalid response") from exc
    if response.is_error:
        raise IntegrationUpstreamError(
            f"{provider} could not complete the request ({response.status_code})"
        )
    if not isinstance(payload, list):
        raise IntegrationUpstreamError(f"{provider} returned an invalid response")
    return payload


def _installation_id(connection: IntegrationConnection) -> int:
    try:
        return int(connection.external_account_id or "")
    except ValueError as exc:
        raise IntegrationAuthorizationError("GitHub installation is invalid") from exc


def _granted(connection: IntegrationConnection) -> set[str]:
    granted = connection.configuration.get("granted_capabilities", [])
    return (
        {item for item in granted if isinstance(item, str)} if isinstance(granted, list) else set()
    )


def _selected_string(connection: IntegrationConnection, key: str) -> str | None:
    value = connection.configuration.get(key)
    return value if isinstance(value, str) and value else None


def _search_console_filters(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > GSC_MAX_FILTERS:
        raise IntegrationError(f"Search Console accepts at most {GSC_MAX_FILTERS} filters")
    filters = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"dimension", "operator", "expression"}
            or item["dimension"] not in GSC_FILTER_DIMENSIONS
            or item["operator"] not in GSC_FILTER_OPERATORS
            or not isinstance(item["expression"], str)
            or not 1 <= len(item["expression"]) <= 4096
        ):
            raise IntegrationError("Search Console filters are unsupported")
        filters.append(
            {
                "dimension": item["dimension"],
                "operator": item["operator"],
                "expression": item["expression"],
            }
        )
    return filters


def _search_console_request(
    start_date: str,
    end_date: str,
    dimensions: tuple[str, ...] = ("date",),
    row_limit: int = 1000,
    start_row: int = 0,
    dimension_filters: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> list[dict[str, str]]:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except (TypeError, ValueError) as exc:
        raise IntegrationError("Search Console dates must use YYYY-MM-DD") from exc
    if end < start or (end - start).days > 366:
        raise IntegrationError("Search Console date range must span at most 366 days")
    allowed_dimensions = {"date", "query", "page", "country", "device", "searchAppearance"}
    if (
        not isinstance(dimensions, (list, tuple))
        or not dimensions
        or len(dimensions) > 3
        or any(not isinstance(item, str) or item not in allowed_dimensions for item in dimensions)
    ):
        raise IntegrationError("Search Console dimensions are unsupported")
    if type(row_limit) is not int or not 1 <= row_limit <= 25_000:
        raise IntegrationError("Search Console row limit must be between 1 and 25000")
    if type(start_row) is not int or not 0 <= start_row <= GSC_MAX_START_ROW:
        raise IntegrationError(
            f"Search Console start row must be between 0 and {GSC_MAX_START_ROW}"
        )
    return _search_console_filters(dimension_filters)


def _gmail_search_request(query: str, max_results: int) -> None:
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise IntegrationError("Gmail search query must contain 1-500 characters")
    if type(max_results) is not int or not 1 <= max_results <= 100:
        raise IntegrationError("Gmail search result limit must be between 1 and 100")


def _gmail_thread_request(thread_id: str) -> None:
    if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        raise IntegrationError("Gmail thread ID is invalid")


def _calendar_request(
    time_min: str, time_max: str, query: str, max_results: int
) -> tuple[datetime, datetime]:
    start = _aware_datetime(time_min, field="Calendar start")
    end = _aware_datetime(time_max, field="Calendar end")
    if end <= start or end - start > timedelta(days=366):
        raise IntegrationError("Calendar range must be positive and at most 366 days")
    if (
        not isinstance(query, str)
        or len(query) > 500
        or type(max_results) is not int
        or not 1 <= max_results <= 250
    ):
        raise IntegrationError("Calendar query or result limit is invalid")
    return start, end


# The adapters' own value checks, by service operation, so the service gateway can refuse a
# malformed call before a receipt exists instead of recording an uncertain provider attempt.
GOOGLE_ARGUMENT_CHECKS = {
    "search_analytics.read": _search_console_request,
    "gmail.messages.search": _gmail_search_request,
    "gmail.thread.read": _gmail_thread_request,
    "calendar.events.list": _calendar_request,
}


def check_google_arguments(operation: str, arguments: dict[str, Any]) -> None:
    """Gateway hook: raise IntegrationError, or TypeError for a missing argument."""
    check = GOOGLE_ARGUMENT_CHECKS.get(operation)
    if check is not None:
        check(**arguments)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


def _fit_search_console_rows(
    payload: dict[str, Any],
    *,
    max_response_bytes: int,
    start_row: int,
    clamped: bool,
    sent_limit: int,
) -> dict[str, Any]:
    """Keep Google's leading rows that fit the bound and say where the next page starts."""
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise IntegrationError("Search Console returned an unexpected response")
    more = clamped and len(rows) >= sent_limit
    if not more and _json_size(payload) <= max_response_bytes:
        return payload
    # Budget the envelope with the widest metadata it can carry, then add rows in order.
    envelope = {
        **{k: v for k, v in payload.items() if k != "rows"},
        "rows": [],
        "truncated": True,
        "next_start_row": start_row + len(rows),
    }
    used = _json_size(envelope)
    kept = 0
    for index, row in enumerate(rows):
        used += _json_size(row) + (2 if index else 0)  # ", " between rows
        if used > max_response_bytes:
            break
        kept += 1
    return {**envelope, "rows": rows[:kept], "next_start_row": start_row + kept}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _base64url_json(value: dict[str, Any]) -> str:
    return _base64url(json.dumps(value, separators=(",", ":")).encode())


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bounded_provider_text(value: Any, max_bytes: int) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.replace("\x00", "").encode("utf-8")
    if len(raw) <= max_bytes:
        return raw.decode("utf-8")
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _github_has_next_page(response: httpx.Response) -> bool:
    return 'rel="next"' in response.headers.get("link", "").casefold()


def _site_source_paths(tree: list[Any]) -> list[str]:
    allowed_suffixes = {
        ".css",
        ".html",
        ".htm",
        ".js",
        ".jsx",
        ".json",
        ".py",
        ".svelte",
        ".toml",
        ".ts",
        ".tsx",
        ".vue",
    }
    preferred_names = {
        "app.html",
        "astro.config.mjs",
        "index.html",
        "layout.jsx",
        "layout.tsx",
        "manifest.json",
        "next.config.js",
        "next.config.mjs",
        "package.json",
        "page.jsx",
        "page.tsx",
        "pyproject.toml",
        "robots.txt",
        "sitemap.xml",
        "vite.config.js",
        "vite.config.ts",
    }
    candidates: list[tuple[tuple[int, int, int, str], str]] = []
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = item.get("path")
        size = item.get("size")
        if (
            not isinstance(path, str)
            or not _safe_github_path(path)
            or not isinstance(size, int)
            or not 1 <= size <= 80_000
        ):
            continue
        pure = PurePosixPath(path)
        lowered = path.casefold()
        name = pure.name.casefold()
        if pure.suffix.casefold() not in allowed_suffixes and name not in {
            "robots.txt",
            "sitemap.xml",
        }:
            continue
        if any(
            part.casefold()
            in {
                ".git",
                ".venv",
                "build",
                "coverage",
                "dist",
                "node_modules",
                "vendor",
            }
            for part in pure.parts
        ):
            continue
        if any(label in lowered for label in (".env", "credential", "private-key", "secret")):
            continue
        preferred = 0 if name in preferred_names else 1
        site_area = (
            0
            if any(
                part.casefold() in {"app", "pages", "public", "src", "static", "web"}
                for part in pure.parts[:-1]
            )
            else 1
        )
        depth = len(pure.parts)
        candidates.append(((preferred, site_area, depth, lowered), path))
    candidates.sort(key=lambda item: item[0])
    selected: list[str] = []
    selected_bytes = 0
    sizes = {
        item["path"]: item["size"]
        for item in tree
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("size"), int)
    }
    for _rank, path in candidates:
        size = sizes[path]
        if selected_bytes + size > 180_000:
            continue
        selected.append(path)
        selected_bytes += size
        if len(selected) == 20:
            break
    return selected


def _safe_github_path(value: str) -> bool:
    if (
        not value
        or len(value) > 300
        or "\\" in value
        or "//" in value
        or value.endswith("/")
        or any(ord(character) < 32 for character in value)
    ):
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    return not value.startswith(".github/workflows/")


def _safe_github_source_path(value: str) -> bool:
    if (
        not value
        or len(value) > 300
        or "\\" in value
        or "//" in value
        or value.endswith("/")
        or any(ord(character) < 32 for character in value)
    ):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


async def _download_bounded(response: httpx.Response, sink: Any) -> None:
    declared = response.headers.get("content-length", "")
    total = 0
    if declared.isdigit() and int(declared) > REPOSITORY_DOWNLOAD_MAX_BYTES:
        total = int(declared)
    else:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > REPOSITORY_DOWNLOAD_MAX_BYTES:
                break
            sink.write(chunk)
    if total > REPOSITORY_DOWNLOAD_MAX_BYTES:
        raise IntegrationAuthorizationError(
            "The selected repository is outside the procedure workspace limits: its archive "
            f"exceeds {REPOSITORY_DOWNLOAD_MAX_BYTES:,} bytes"
        )
    sink.seek(0)


def _git_blob_sha(content: bytes, expected: str) -> str:
    digest = hashlib.sha256() if len(expected) == 64 else hashlib.sha1(usedforsecurity=False)
    digest.update(b"blob %d\0" % len(content))
    digest.update(content)
    return digest.hexdigest()


def _verified_tarball_blobs(fileobj: Any, wanted: dict[str, dict[str, Any]]) -> dict[str, bytes]:
    """Read the tree's eligible files from a GitHub tarball; skip anything unverified."""
    contents: dict[str, bytes] = {}
    root: str | None = None
    try:
        with tarfile.open(fileobj=fileobj, mode="r|gz") as archive:
            for member in archive:
                head, _, path = member.name.partition("/")
                root = head if root is None else root
                if not head or head != root:
                    raise IntegrationUpstreamError("GitHub repository tarball layout is invalid")
                item = wanted.get(path)
                if (
                    item is None
                    or path in contents
                    or not member.isreg()
                    or member.size != item["size"]
                ):
                    continue
                handle = archive.extractfile(member)
                content = handle.read() if handle is not None else b""
                if (
                    len(content) == item["size"]
                    and _git_blob_sha(content, item["sha"]) == item["sha"]
                ):
                    contents[path] = content
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise IntegrationUpstreamError("GitHub repository tarball is unreadable") from exc
    return contents


def _repository_archive(blobs: list[dict[str, Any]], contents: dict[str, bytes]) -> bytes:
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for item in sorted(blobs, key=lambda value: value["path"]):
            content = contents[item["path"]]
            info = tarfile.TarInfo(name=item["path"])
            info.size = len(content)
            info.mode = 0o755 if item["mode"] == "100755" else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return archive_buffer.getvalue()


def _safe_github_ref(value: str) -> bool:
    forbidden = ("..", "~", "^", ":", "?", "*", "[", "\\", "@{")
    return bool(
        value
        and len(value) <= 200
        and not value.startswith(("/", "."))
        and not value.endswith(("/", ".", ".lock"))
        and "//" not in value
        and not any(item in value for item in forbidden)
    )


def _github_pull_result(value: dict[str, Any]) -> GitHubPullRequestResult:
    repository = value.get("repository")
    branch = value.get("branch")
    number = value.get("number")
    url = value.get("url")
    if (
        not isinstance(repository, str)
        or not isinstance(branch, str)
        or not isinstance(number, int)
        or not isinstance(url, str)
        or not _safe_github_pull_url(url, repository=repository, number=number)
    ):
        raise IntegrationUpstreamError("stored GitHub pull-request receipt is invalid")
    return GitHubPullRequestResult(
        repository=repository,
        branch=branch,
        number=number,
        url=url,
    )


def _github_commit_result(value: dict[str, Any]) -> GitHubCommitResult:
    repository = value.get("repository")
    branch = value.get("branch")
    commit = value.get("commit")
    url = value.get("url")
    if (
        not isinstance(repository, str)
        or not isinstance(branch, str)
        or not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not isinstance(url, str)
        or not _safe_github_commit_url(url, repository=repository, commit=commit)
    ):
        raise IntegrationUpstreamError("GitHub did not return a valid commit")
    return GitHubCommitResult(repository=repository, branch=branch, commit=commit, url=url)


def _safe_github_commit_url(value: str, *, repository: str, commit: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and parsed.path == f"/{repository}/commit/{commit}"
        and not parsed.query
        and not parsed.fragment
    )


def _safe_github_pull_url(value: str, *, repository: str, number: int) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and parsed.path == f"/{repository}/pull/{number}"
        and not parsed.query
        and not parsed.fragment
    )
