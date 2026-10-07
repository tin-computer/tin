"""Closed collection contract shared by the extension and trusted activities."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

KEY = "connections.collect"
PROVIDER = "network.linkedin"
CAPABILITY = "connections.read"
WORKFLOW_ID = UUID("6f2915b7-bcae-4fbc-985f-7eea4df11d16")
POLICY = {
    "version": 1,
    "friends": 3,
    "pages_per_friend": 20,
    "people_per_friend": 200,
    "seconds_per_friend": 900,
    "job_seconds": 2700,
    "lease_seconds": 90,
    "max_page_records": 50,
    "max_batch_bytes": 200_000,
}
MODES = ("local_only", "cloud_only", "cloud_preferred")
TERMINAL = frozenset({"completed", "partial", "failed", "stopped"})
LOCAL_BACKUP_REASONS = frozenset({"cloud_unavailable", "session_expired", "cloud_failed"})
STOP_REASONS = frozenset({"challenge", "rate_limited", "account_changed", "access_denied"})


class CollectionError(ValueError):
    """Finite errors may be exposed without reflecting records or session material."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical_json(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def profile_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 700:
        raise CollectionError("invalid_profile")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.linkedin.com", "linkedin.com"}
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        raise CollectionError("invalid_profile")
    match = re.fullmatch(r"/in/([^/]+)/?", parsed.path)
    if not match:
        raise CollectionError("invalid_profile")
    identifier = unquote(match[1])
    if not identifier or len(identifier) > 300 or any(c in identifier for c in "/\\?#\x00"):
        raise CollectionError("invalid_profile")
    if any(ord(c) < 32 for c in identifier):
        raise CollectionError("invalid_profile")
    return "https://www.linkedin.com/in/" + quote(identifier, safe="-._~")


def enabled(settings, project_id: UUID | None) -> bool:
    if project_id is None:
        return False
    values = getattr(settings, "connection_collection_projects", ())
    return str(project_id) in {str(value) for value in values}


def require_enabled(settings, project_id: UUID):
    if not enabled(settings, project_id):
        raise CollectionError("collection_unavailable")


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CollectionInputs(ClosedModel):
    project_id: str
    friends: list[str] = Field(min_length=1, max_length=3)
    keywords: str = Field(default="", max_length=300)
    execution: Literal["local_only", "cloud_only", "cloud_preferred"] = "local_only"

    @field_validator("friends")
    @classmethod
    def friends_valid(cls, values):
        normalized = [profile_url(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise CollectionError("duplicate_friend")
        return normalized

    @field_validator("keywords")
    @classmethod
    def keywords_valid(cls, value):
        if any(ord(c) < 32 for c in value):
            raise CollectionError("invalid_keywords")
        return value.strip()


def validate_inputs(project_id: UUID, inputs: dict) -> dict:
    """Validate caller inputs with trusted project context; return caller fields only."""
    from tin_lite.workflow_inputs import WorkflowInputError

    if "project_id" in inputs:
        raise WorkflowInputError("project_id is bound by Tin and cannot be supplied as input")
    try:
        parsed = CollectionInputs.model_validate({**inputs, "project_id": str(project_id)})
    except ValidationError as exc:
        fields = {error["loc"][0] for error in exc.errors(include_input=False, include_url=False)}
        if "friends" in fields:
            message = (
                "Enter 1–3 different LinkedIn profile URLs for your first-degree connections, "
                "separated by commas. Names alone are not supported."
            )
        elif "keywords" in fields:
            message = "Enter a keyword query of up to 300 characters on one line."
        else:
            message = "Choose Local only, Cloud preferred or Cloud only for collection."
        raise WorkflowInputError(message) from None
    return parsed.model_dump(exclude={"project_id"})


INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "friends": {
            "type": "array",
            "title": "Friends' LinkedIn profile URLs",
            "description": (
                "Paste profile links for 1–3 first-degree connections, separated by commas. "
                "Open each friend's LinkedIn profile and copy the URL from the address bar."
            ),
            "minItems": 1,
            "maxItems": 3,
            "uniqueItems": True,
            "items": {"type": "string", "maxLength": 700},
        },
        "keywords": {
            "type": "string",
            "maxLength": 300,
            "default": "",
            "title": "Keywords (optional)",
            "description": (
                "Narrow the LinkedIn search within each friend's connections with a "
                "term such as founder, investor or designer. Leave blank to collect "
                "without a keyword filter. Only your second-degree connections are included."
            ),
        },
        "execution": {
            "type": "string",
            "enum": list(MODES),
            "default": "local_only",
            "title": "Collection mode",
            "description": (
                "Local only uses your Chrome browser. Cloud preferred uses cloud collection "
                "with a local backup when possible. "
                "Keep Chrome open and awake for local collection."
            ),
        },
    },
    "required": ["project_id", "friends"],
}


class Actor(ClosedModel):
    key: str = Field(min_length=1, max_length=1000)
    name: str = Field(default="", max_length=200)
    profile_url: str | None = None

    @field_validator("profile_url")
    @classmethod
    def url_valid(cls, value):
        return profile_url(value) if value is not None else None

    @field_validator("key")
    @classmethod
    def key_valid(cls, value):
        if value.startswith("https://"):
            return profile_url(value)
        if re.fullmatch(r"avatar:/[A-Za-z0-9_./%-]{1,900}", value):
            return value
        raise CollectionError("invalid_actor")


class CollectionSource(ClosedModel):
    friend_url: str
    friend_name: str = Field(max_length=200)
    actor: Actor
    first_degree: Literal[True]
    collection_url: str = Field(max_length=2500)
    query_id: str | None = Field(default=None, max_length=200)

    @field_validator("friend_url")
    @classmethod
    def friend_valid(cls, value):
        return profile_url(value)

    @field_validator("query_id")
    @classmethod
    def query_valid(cls, value):
        if value is not None and not re.fullmatch(
            r"voyagerSearchDashClusters\.[a-f0-9]{20,64}", value
        ):
            raise CollectionError("unsupported_search_contract")
        return value

    def scope(self, *, keywords: str) -> str:
        url = urlsplit(self.collection_url)
        if (
            url.scheme != "https"
            or url.netloc != "www.linkedin.com"
            or url.path.rstrip("/") != "/search/results/people"
            or url.fragment
        ):
            raise CollectionError("invalid_collection_view")
        params = parse_qs(url.query, keep_blank_values=True)
        if params.get("spellCorrectionEnabled") == ["true"]:
            params.pop("spellCorrectionEnabled")
        if params.get("prioritizeMessage") == ["false"]:
            params.pop("prioritizeMessage")
        if any(len(v) != 1 for v in params.values()):
            raise CollectionError("invalid_collection_view")
        try:
            friends = json.loads(params.get("connectionOf", [""])[0])
            if isinstance(friends, str):
                friends = [friends]
            degree = json.loads(params.get("network", [""])[0])
        except (ValueError, TypeError):
            raise CollectionError("invalid_collection_view") from None
        if (
            not isinstance(friends, list)
            or len(friends) != 1
            or not isinstance(friends[0], str)
            or not re.fullmatch(r"[A-Za-z0-9:_-]{1,256}", friends[0])
            or degree != ["S"]
            or params.get("keywords", [""])[0] != keywords
        ):
            raise CollectionError("filters_changed")
        # No other targeting filter may be silently introduced by a browser view.
        if set(params) - {"connectionOf", "network", "keywords", "origin", "page", "sid", "trk"}:
            raise CollectionError("filters_changed")
        return friends[0]


class Person(ClosedModel):
    profile_url: str
    name: str = Field(min_length=1, max_length=200)
    headline: str = Field(default="", max_length=1000)
    location: str = Field(default="", max_length=300)
    degree: Literal["2nd"]
    visible_text: str = Field(default="", max_length=3000)

    @field_validator("profile_url")
    @classmethod
    def url_valid(cls, value):
        return profile_url(value)


class PageBatch(ClosedModel):
    generation: int = Field(ge=1)
    lease: str = Field(min_length=32, max_length=128)
    friend_index: int = Field(ge=0, le=2)
    page: int = Field(ge=1, le=20)
    actor_key: str = Field(max_length=1000)
    source: CollectionSource
    people: list[Person] = Field(max_length=50)
    next_page: bool
    observed_at: str = Field(max_length=40)

    @model_validator(mode="after")
    def valid_page(self):
        if len(canonical_json(self.model_dump()).encode()) > POLICY["max_batch_bytes"]:
            raise CollectionError("page_too_large")
        try:
            observed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except ValueError:
            raise CollectionError("invalid_observation") from None
        if observed.tzinfo is None or observed > datetime.now(UTC) + timedelta(minutes=2):
            raise CollectionError("invalid_observation")
        if len({p.profile_url for p in self.people}) != len(self.people):
            raise CollectionError("duplicate_page_record")
        if not self.people and self.next_page:
            raise CollectionError("unverified_empty_page")
        if self.actor_key != self.source.actor.key:
            raise CollectionError("account_changed")
        return self

    def fingerprint(self):
        return digest(sorted(p.profile_url for p in self.people))


def failure_transition(policy: str, mode: str, reason: str) -> str:
    """Execution location changes only for a declared recoverable failure."""
    if reason in STOP_REASONS:
        return "paused"
    if mode == "cloud" and policy == "cloud_preferred" and reason in LOCAL_BACKUP_REASONS:
        return "handoff_pending"
    return "paused" if reason in {"session_expired", "browser_unavailable"} else "failed"


def view_url(value: str, page: int) -> str:
    from urllib.parse import parse_qsl, urlencode

    u = urlsplit(value)
    values = [(k, v) for k, v in parse_qsl(u.query) if k != "page"]
    values.append(("page", str(page)))
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(values), ""))


def visible(settings, workflow, project_id):
    return workflow.executor != KEY or enabled(settings, project_id)


def cloud_ready(settings):
    # The image and worker lane are separate; the E2B account need not be.
    return bool(
        cloud_credential(settings)
        and getattr(settings, "integration_credential_key", None)
        and getattr(settings, "linkedin_cloud_template", None)
        and getattr(settings, "linkedin_cloud_enabled", False)
    )


def cloud_credential(settings):
    return getattr(settings, "linkedin_e2b_api_key", None) or getattr(settings, "e2b_api_key", None)


class LeaseCommand(ClosedModel):
    generation: int = Field(ge=1)
    lease: str = Field(min_length=32, max_length=128)
