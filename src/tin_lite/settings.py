from __future__ import annotations

import os
from base64 import urlsafe_b64decode
from binascii import Error as Base64Error
from functools import cached_property
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration with credentials bound to the supplied variable names."""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    temporal_endpoint: str = Field(alias="TEMPORAL_ENDPOINT")
    temporal_api_key: SecretStr = Field(alias="TEMPORAL_API_KEY")
    temporal_namespace: str = Field(alias="TEMPORAL_NAMESPACE")
    e2b_api_key: SecretStr = Field(alias="E2B_API_KEY")

    database_host: str = Field(alias="DATABASE_HOST")
    database_port: int = Field(alias="DATABASE_PORT")
    database_username: str = Field(alias="DATABASE_USERNAME")
    database_password: SecretStr = Field(alias="DATABASE_PASSWORD")
    database_name: str = Field(alias="DATABASE")

    migration_host: str = Field(alias="MIGRATION_HOST")
    migration_port: int = Field(alias="MIGRATION_PORT")
    migration_username: str = Field(alias="MIGRATION_USERNAME")
    migration_password: SecretStr = Field(alias="MIGRATION_PASSWORD")
    migration_database: str = Field(alias="MIGRATION_DATABASE")

    code_storage_api_key: SecretStr = Field(alias="CODE_STORAGE_API_KEY")

    clerk_publishable_key: str = Field(alias="CLERK_PUBLISHABLE_KEY")
    clerk_secret_key: SecretStr = Field(alias="CLERK_SECRET_KEY")
    clerk_jwt_key: SecretStr | None = Field(default=None, alias="CLERK_JWT_KEY")
    clerk_authorized_parties_raw: str = Field(alias="CLERK_AUTHORIZED_PARTIES")
    mcp_oauth_client_ids_raw: str = Field(default="", alias="TIN_LITE_MCP_OAUTH_CLIENT_IDS")

    luna_api_key: SecretStr | None = Field(default=None, alias="TIN_LITE_LUNA_API_KEY")
    posthog_api_key: str | None = Field(default=None, alias="TIN_LITE_POSTHOG_API_KEY")
    # Welcome email on a person's first agent connection (Resend). Off when the key is absent.
    resend_api_key: SecretStr | None = Field(default=None, alias="TIN_LITE_RESEND_API_KEY")
    welcome_email_from: str = Field(
        default="Tin <hello@tin.computer>", alias="TIN_LITE_WELCOME_EMAIL_FROM"
    )
    # Replies land in the shared hello@ mailbox, which the founders read.
    welcome_email_reply_to: str | None = Field(
        default="Tin <hello@tin.computer>", alias="TIN_LITE_WELCOME_EMAIL_REPLY_TO"
    )
    posthog_host: str = Field(default="https://us.i.posthog.com", alias="TIN_LITE_POSTHOG_HOST")
    luna_model: str = Field(default="gpt-6-luna", alias="TIN_LITE_LUNA_MODEL")
    luna_base_url: str = Field(default="https://api.openai.com/v1", alias="TIN_LITE_LUNA_BASE_URL")
    luna_timeout_seconds: float = Field(default=90, alias="TIN_LITE_LUNA_TIMEOUT")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_workspace_id: str | None = Field(default=None, alias="ANTHROPIC_WORKSPACE_ID")
    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY")
    billing_enabled: bool = Field(default=False, alias="TIN_LITE_BILLING_ENABLED")
    # Browser sign-ups see a locked dashboard until their coding agent sets up the first
    # workflow. Off once browser onboarding exists, or for a test account.
    browser_lock_enabled: bool = Field(default=True, alias="TIN_LITE_BROWSER_LOCK_ENABLED")
    billing_test_enabled: bool = Field(default=False, alias="TIN_LITE_BILLING_TEST_ENABLED")
    billing_hosted_defaults_enabled: bool = Field(
        default=False, alias="TIN_LITE_BILLING_HOSTED_DEFAULTS_ENABLED"
    )
    billing_welcome_credits_enabled: bool = Field(
        default=False, alias="TIN_LITE_BILLING_WELCOME_CREDITS_ENABLED"
    )
    stripe_secret_key: SecretStr | None = Field(default=None, alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: SecretStr | None = Field(default=None, alias="STRIPE_WEBHOOK_SECRET")
    # The creative studio's fal credential stays on the switchboard; sandboxes reach voice
    # synthesis only through the run-bound studio grant.
    fal_key: SecretStr | None = Field(default=None, alias="FAL_KEY")
    studio_max_voice_lines_per_run: int = Field(
        default=24, ge=1, le=200, alias="TIN_LITE_STUDIO_MAX_VOICE_LINES"
    )
    studio_max_voice_characters_per_run: int = Field(
        default=3000, ge=1, le=50_000, alias="TIN_LITE_STUDIO_MAX_VOICE_CHARACTERS"
    )
    dataforseo_login: SecretStr | None = Field(default=None, alias="DATAFORSEO_LOGIN")
    dataforseo_password: SecretStr | None = Field(default=None, alias="DATAFORSEO_PASSWORD")
    # Operator-run Google Ads Keyword Planner service (gak). An HTTPS origin, or plain HTTP on
    # loopback only when the private-address flag is set explicitly.
    gak_url: str | None = Field(default=None, alias="TIN_LITE_GAK_URL")
    gak_token: SecretStr | None = Field(default=None, alias="TIN_LITE_GAK_TOKEN")
    gak_allow_private: bool = Field(default=False, alias="TIN_LITE_GAK_ALLOW_PRIVATE")
    organic_audit_max_cost_usd: float = Field(
        default=0, ge=0, le=25, allow_inf_nan=False, alias="TIN_LITE_ORGANIC_AUDIT_MAX_COST_USD"
    )
    keyword_plan_max_cost_usd: float = Field(
        default=0, ge=0, le=25, allow_inf_nan=False, alias="TIN_LITE_KEYWORD_PLAN_MAX_COST_USD"
    )
    content_plan_max_cost_usd: float = Field(
        default=0, ge=0, le=5, allow_inf_nan=False, alias="TIN_LITE_CONTENT_PLAN_MAX_COST_USD"
    )
    paid_ads_max_cost_usd: float = Field(
        default=0, ge=0, le=25, allow_inf_nan=False, alias="TIN_LITE_PAID_ADS_MAX_COST_USD"
    )
    # Tin's Google Ads manager account. The refresh token is a deployment credential minted
    # once by an operator against the Google OAuth client below; customers link their own
    # account to the manager by accepting an invitation, so no customer token is ever stored.
    google_ads_manager_customer_id: str | None = Field(
        default=None, alias="TIN_LITE_GOOGLE_ADS_MANAGER_CUSTOMER_ID"
    )
    google_ads_manager_refresh_token: SecretStr | None = Field(
        default=None, alias="TIN_LITE_GOOGLE_ADS_MANAGER_REFRESH_TOKEN"
    )
    google_ads_developer_token: SecretStr | None = Field(
        default=None, alias="TIN_LITE_GOOGLE_ADS_DEVELOPER_TOKEN"
    )
    # The OAuth client the manager refresh token was minted against. Defaults to Tin's Google
    # OAuth client; set both when the token came from another client (a migration case).
    google_ads_oauth_client_id: str | None = Field(
        default=None, alias="TIN_LITE_GOOGLE_ADS_OAUTH_CLIENT_ID"
    )
    google_ads_oauth_client_secret: SecretStr | None = Field(
        default=None, alias="TIN_LITE_GOOGLE_ADS_OAUTH_CLIENT_SECRET"
    )
    google_ads_api_version: str = Field(
        default="v25", pattern=r"^v\d{1,3}$", alias="TIN_LITE_GOOGLE_ADS_API_VERSION"
    )

    integration_credential_key: SecretStr | None = Field(
        default=None, alias="TIN_LITE_INTEGRATION_CREDENTIAL_KEY"
    )
    google_oauth_client_id: str | None = Field(
        default=None, alias="TIN_LITE_GOOGLE_OAUTH_CLIENT_ID"
    )
    google_oauth_client_secret: SecretStr | None = Field(
        default=None, alias="TIN_LITE_GOOGLE_OAUTH_CLIENT_SECRET"
    )
    # PostHog OAuth uses a Client ID Metadata Document Tin serves at a fixed path on
    # TIN_LITE_PUBLIC_URL; PostHog fetches it, so there is no client secret to configure.
    posthog_oauth_enabled: bool = Field(default=False, alias="TIN_LITE_POSTHOG_OAUTH_ENABLED")
    # Optional phvt_ token from PostHog organization settings; it links the client to that
    # organization. It is published in the metadata document, so it is not a secret.
    posthog_oauth_verification_token: str | None = Field(
        default=None,
        alias="TIN_LITE_POSTHOG_OAUTH_VERIFICATION_TOKEN",
        pattern=r"^phvt_[A-Za-z0-9_-]{8,200}$",
    )
    github_app_slug: str | None = Field(default=None, alias="TIN_LITE_GITHUB_APP_SLUG")
    github_app_id: str | None = Field(default=None, alias="TIN_LITE_GITHUB_APP_ID")
    github_app_client_id: str | None = Field(default=None, alias="TIN_LITE_GITHUB_APP_CLIENT_ID")
    github_app_client_secret: SecretStr | None = Field(
        default=None, alias="TIN_LITE_GITHUB_APP_CLIENT_SECRET"
    )
    github_app_private_key_path: Path | None = Field(
        default=None, alias="TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH"
    )
    github_webhook_secret: SecretStr | None = Field(
        default=None, alias="TIN_LITE_GITHUB_WEBHOOK_SECRET"
    )
    # Bearer secret the tin repository's contributor gate sends; unset disables the check.
    contributor_check_token: SecretStr | None = Field(
        default=None, alias="TIN_LITE_CONTRIBUTOR_CHECK_TOKEN"
    )
    # Tin-owned, receive-only phone number test identities give to products that ask for one.
    # Twilio posts each inbound SMS to the switchboard; Tin never sends from the number.
    twilio_auth_token: SecretStr | None = Field(default=None, alias="TWILIO_AUTH_TOKEN")
    test_phone_number: str | None = Field(
        default=None, alias="TIN_LITE_TEST_PHONE_NUMBER", pattern=r"^\+[1-9][0-9]{6,14}$"
    )
    twilio_webhook_url_raw: str | None = Field(default=None, alias="TIN_LITE_TWILIO_WEBHOOK_URL")

    code_storage_org: str = Field(default="tin", alias="TIN_LITE_CODE_STORAGE_ORG")
    e2b_template: str = Field(default="tin-lite-codex", alias="TIN_LITE_E2B_TEMPLATE")
    e2b_isolated_template: str | None = Field(default=None, alias="TIN_LITE_E2B_ISOLATED_TEMPLATE")
    e2b_browser_api_template: str = Field(
        default="tin-lite-codex-browser-api", alias="TIN_LITE_E2B_BROWSER_API_TEMPLATE"
    )
    e2b_studio_api_template: str = Field(
        default="tin-lite-codex-studio-api", alias="TIN_LITE_E2B_STUDIO_API_TEMPLATE"
    )
    private_workflow_projects_raw: str = Field(
        default="", alias="TIN_LITE_PRIVATE_WORKFLOW_PROJECTS"
    )
    # Any project may run private workflows; requires billing so each run spends credits.
    private_workflows_open: bool = Field(default=False, alias="TIN_LITE_PRIVATE_WORKFLOWS_OPEN")
    codex_api_projects_raw: str = Field(default="", alias="TIN_LITE_CODEX_API_PROJECTS")
    e2b_browser_template: str = Field(
        default="tin-lite-codex-browser", alias="TIN_LITE_E2B_BROWSER_TEMPLATE"
    )
    e2b_studio_template: str = Field(
        default="tin-lite-codex-studio", alias="TIN_LITE_E2B_STUDIO_TEMPLATE"
    )
    task_queue: str = Field(default="tin-lite-checkpoint-a", alias="TIN_LITE_TASK_QUEUE")
    # On SIGTERM the worker stops polling and in-flight activities get this long to
    # finish before Temporal cancels them. HTTP (including the Codex relay those
    # activities call) keeps serving meanwhile; systemd TimeoutStopSec must exceed it.
    worker_graceful_shutdown_seconds: int = Field(
        default=300, ge=0, alias="TIN_LITE_WORKER_GRACEFUL_SHUTDOWN_SECONDS"
    )
    switchboard_public_url: str = Field(
        default="http://127.0.0.1:8000", alias="TIN_LITE_PUBLIC_URL"
    )
    app_url: str | None = Field(default=None, alias="TIN_LITE_APP_URL")
    private_fonts_stylesheet_url: str | None = Field(
        default=None, alias="TIN_LITE_PRIVATE_FONTS_STYLESHEET_URL"
    )
    # Temporary inbound compatibility during an origin migration, never advertised.
    legacy_public_url: str | None = Field(default=None, alias="TIN_LITE_LEGACY_PUBLIC_URL")
    forward_proxy_url: SecretStr | None = Field(default=None, alias="TIN_LITE_PROXY_URL")
    proxy_grant_dir: Path | None = Field(default=None, alias="TIN_LITE_PROXY_GRANT_DIR")

    egress_allow_hosts_raw: str = Field(default="", alias="TIN_LITE_EGRESS_ALLOW_HOSTS")
    sandbox_timeout_seconds: int = Field(default=900, alias="TIN_LITE_SANDBOX_TIMEOUT")

    @model_validator(mode="after")
    def secure_forward_proxy(self) -> Settings:
        if self.forward_proxy_url is not None:
            from tin_lite.proxy_grants import validate_proxy_url

            validate_proxy_url(self.forward_proxy_url.get_secret_value())
            if self.proxy_grant_dir is None or not self.proxy_grant_dir.is_absolute():
                raise ValueError("TIN_LITE_PROXY_GRANT_DIR must be an absolute directory path")
            if not 1 <= self.sandbox_timeout_seconds <= 3600:
                raise ValueError("proxied sandboxes require a timeout between 1 and 3600 seconds")
        return self

    @field_validator("app_url", "legacy_public_url")
    @classmethod
    def validate_app_url(cls, value: str | None) -> str | None:
        from tin_lite.product_urls import validate_origin

        return validate_origin(value) if value is not None else None

    @field_validator("gak_url")
    @classmethod
    def validate_gak_url(cls, value: str | None) -> str | None:
        from tin_lite.gak import validate_base_url

        return validate_base_url(value) if value is not None else None

    @field_validator("private_fonts_stylesheet_url")
    @classmethod
    def validate_private_fonts_stylesheet(cls, value: str | None) -> str | None:
        from urllib.parse import urlsplit

        if value is None:
            return None
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
            or any(char.isspace() or char in "\\<>\"'" for char in value)
        ):
            raise ValueError("Private font stylesheet must be a credential-free HTTPS URL")
        return value

    @property
    def private_workflow_projects(self):
        from uuid import UUID

        return frozenset(
            UUID(value.strip())
            for value in self.private_workflow_projects_raw.split(",")
            if value.strip()
        )

    @property
    def codex_api_projects(self):
        from uuid import UUID

        return frozenset(
            UUID(value.strip()) for value in self.codex_api_projects_raw.split(",") if value.strip()
        )

    @property
    def test_phone_enabled(self) -> bool:
        return self.twilio_auth_token is not None and self.test_phone_number is not None

    @property
    def twilio_webhook_url(self) -> str:
        return self.twilio_webhook_url_raw or (
            f"{self.switchboard_public_url.rstrip('/')}/webhooks/twilio/sms"
        )

    @model_validator(mode="after")
    def reject_api_key_auth(self) -> Settings:
        if (self.codex_api_projects or self.billing_hosted_defaults_enabled) and (
            self.luna_api_key is None or not self.e2b_isolated_template
        ):
            raise ValueError("Codex API pilot requires the server OpenAI key and isolated template")
        if self.billing_hosted_defaults_enabled and not (
            self.billing_enabled and self.billing_welcome_credits_enabled
        ):
            raise ValueError("Hosted credit defaults require billing and welcome credits enabled")
        if self.private_workflows_open and not self.billing_enabled:
            raise ValueError("Open private workflows require billing enabled")
        if self.billing_test_enabled and self.stripe_secret_key is not None:
            if not self.stripe_secret_key.get_secret_value().startswith(("sk_test_", "rk_test_")):
                raise ValueError("Tin billing currently accepts Stripe test keys only")
        if "OPENAI_API_KEY" in os.environ or "CODEX_API_KEY" in os.environ:
            raise ValueError(
                "OPENAI_API_KEY and CODEX_API_KEY are forbidden; Luna uses its explicit "
                "switchboard-only OpenAI setting"
            )
        if not self.clerk_publishable_key.startswith(("pk_test_", "pk_live_")):
            raise ValueError("CLERK_PUBLISHABLE_KEY is not a Clerk publishable key")
        if not self.clerk_authorized_parties:
            raise ValueError("CLERK_AUTHORIZED_PARTIES must contain at least one origin")
        google_values = (self.google_oauth_client_id, self.google_oauth_client_secret)
        if any(value is not None for value in google_values) and not all(
            value is not None for value in google_values
        ):
            raise ValueError(
                "TIN_LITE_GOOGLE_OAUTH_CLIENT_ID and "
                "TIN_LITE_GOOGLE_OAUTH_CLIENT_SECRET must be configured together"
            )
        github_values = (
            self.github_app_slug,
            self.github_app_id,
            self.github_app_client_id,
            self.github_app_client_secret,
            self.github_app_private_key_path,
            self.github_webhook_secret,
        )
        if any(value is not None for value in github_values) and not all(
            value is not None for value in github_values
        ):
            raise ValueError(
                "TIN_LITE_GITHUB_APP_SLUG, TIN_LITE_GITHUB_APP_ID, "
                "TIN_LITE_GITHUB_APP_CLIENT_ID, TIN_LITE_GITHUB_APP_CLIENT_SECRET, "
                "TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH, and "
                "TIN_LITE_GITHUB_WEBHOOK_SECRET must be configured together"
            )
        if self.integration_credential_key is None and self.google_oauth_client_id is not None:
            raise ValueError(
                "TIN_LITE_INTEGRATION_CREDENTIAL_KEY is required when Google OAuth is configured"
            )
        if self.posthog_oauth_enabled:
            from urllib.parse import urlsplit

            public = urlsplit(self.switchboard_public_url)
            if self.integration_credential_key is None:
                raise ValueError(
                    "TIN_LITE_INTEGRATION_CREDENTIAL_KEY is required when PostHog OAuth is enabled"
                )
            if public.scheme != "https" or public.hostname in {None, "localhost", "127.0.0.1"}:
                raise ValueError(
                    "TIN_LITE_POSTHOG_OAUTH_ENABLED requires an https TIN_LITE_PUBLIC_URL that "
                    "PostHog can fetch the client metadata document from"
                )
        ads_values = (self.google_ads_manager_customer_id, self.google_ads_manager_refresh_token)
        if any(value is not None for value in ads_values):
            if not all(value is not None for value in ads_values):
                raise ValueError(
                    "TIN_LITE_GOOGLE_ADS_MANAGER_CUSTOMER_ID and "
                    "TIN_LITE_GOOGLE_ADS_MANAGER_REFRESH_TOKEN must be configured together"
                )
            ads_client = (self.google_ads_oauth_client_id, self.google_ads_oauth_client_secret)
            if any(value is not None for value in ads_client) and not all(
                value is not None for value in ads_client
            ):
                raise ValueError(
                    "TIN_LITE_GOOGLE_ADS_OAUTH_CLIENT_ID and "
                    "TIN_LITE_GOOGLE_ADS_OAUTH_CLIENT_SECRET must be configured together"
                )
            if self.google_oauth_client_id is None and self.google_ads_oauth_client_id is None:
                raise ValueError(
                    "TIN_LITE_GOOGLE_ADS_MANAGER_REFRESH_TOKEN requires the Google OAuth client "
                    "it was minted against"
                )
            digits = self.google_ads_manager_customer_id.replace("-", "")
            if not digits.isdigit() or len(digits) != 10:
                raise ValueError(
                    "TIN_LITE_GOOGLE_ADS_MANAGER_CUSTOMER_ID must be a ten-digit customer id"
                )
        if (self.gak_url is None) != (self.gak_token is None):
            raise ValueError("TIN_LITE_GAK_URL and TIN_LITE_GAK_TOKEN must be configured together")
        if self.gak_token is not None and len(self.gak_token.get_secret_value()) < 32:
            raise ValueError("TIN_LITE_GAK_TOKEN must be at least 32 characters")
        if (
            self.gak_url is not None
            and self.gak_url.startswith("http://")
            and not self.gak_allow_private
        ):
            raise ValueError(
                "TIN_LITE_GAK_URL over plain HTTP requires TIN_LITE_GAK_ALLOW_PRIVATE=true"
            )
        return self

    @cached_property
    def egress_allow_hosts(self) -> tuple[str, ...]:
        return tuple(
            item.strip() for item in self.egress_allow_hosts_raw.split(",") if item.strip()
        )

    @cached_property
    def clerk_authorized_parties(self) -> tuple[str, ...]:
        return tuple(
            item.strip().rstrip("/")
            for item in self.clerk_authorized_parties_raw.split(",")
            if item.strip()
        )

    @cached_property
    def mcp_oauth_client_ids(self) -> frozenset[str]:
        # OAuth client IDs (including CIMD URLs) are opaque, case-sensitive identifiers.
        return frozenset(
            item.strip() for item in self.mcp_oauth_client_ids_raw.split(",") if item.strip()
        )

    @cached_property
    def clerk_frontend_api_url(self) -> str:
        try:
            encoded = self.clerk_publishable_key.split("_", 2)[2]
            encoded += "=" * (-len(encoded) % 4)
            decoded = urlsafe_b64decode(encoded).decode("ascii")
        except (IndexError, UnicodeDecodeError, Base64Error, ValueError) as exc:
            raise ValueError("CLERK_PUBLISHABLE_KEY has an invalid payload") from exc
        if not decoded.endswith("$"):
            raise ValueError("CLERK_PUBLISHABLE_KEY has an invalid payload")
        host = decoded[:-1]
        if not host or any(character in host for character in "/?#"):
            raise ValueError("CLERK_PUBLISHABLE_KEY has an invalid frontend host")
        return f"https://{host}"

    @property
    def runtime_dsn(self) -> str:
        return self._postgres_dsn(
            host=self.database_host,
            port=self.database_port,
            username=self.database_username,
            password=self.database_password,
            database=self.database_name,
        )

    @property
    def migration_dsn(self) -> str:
        return self._postgres_dsn(
            host=self.migration_host,
            port=self.migration_port,
            username=self.migration_username,
            password=self.migration_password,
            database=self.migration_database,
        )

    @staticmethod
    def _postgres_dsn(
        *, host: str, port: int, username: str, password: SecretStr, database: str
    ) -> str:
        user = quote(username, safe="")
        secret = quote(password.get_secret_value(), safe="")
        name = quote(database, safe="")
        return f"postgresql://{user}:{secret}@{host}:{port}/{name}?sslmode=require"


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
