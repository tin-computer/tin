"""Exact reviewed Markdown delivery; a continuation, not another drafting engine."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tin_lite import content_draft
from tin_lite.content_plan import item_kind
from tin_lite.content_programs import ContentPrograms
from tin_lite.domain import RunStatus
from tin_lite.integrations import (
    GitHubFileChange,
    GitHubRepositoryBinding,
    IntegrationAuthorizationError,
)
from tin_lite.organic_audit import canonical_json
from tin_lite.project_files import ProjectFileService, safe_project_file_path
from tin_lite.repository_limits import describe_omissions

WORKFLOW = "tin.content_draft_delivery"
OPERATION = "content_draft_delivery_v1"
CHOICE_OPERATION = "content_draft_delivery_choice_v1"
DRAFT_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000031")
PUBLIC_ARTICLE_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000009")
ANSWER_PAGE_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000005")
# content.refresh: approved replacements on an existing page (see content_refresh.py).
REFRESH_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000044")
REFRESH_KIND = "refresh"
# Runs whose approval may choose a repository delivery for that one document.
CHOICE_WORKFLOW_IDS = frozenset(
    {DRAFT_WORKFLOW_ID, PUBLIC_ARTICLE_WORKFLOW_ID, ANSWER_PAGE_WORKFLOW_ID, REFRESH_WORKFLOW_ID}
)
REPOSITORY_MODES = frozenset({"github_pr", "github_commit"})
APPROVAL_CHOICES = ("github_pr", "github_commit", "none")
# Approved pages that Tin adapts into the site's own format (content.deliver) instead of
# committing their Markdown as it is, when Codex API execution is on for the project.
ADAPTED_WORKFLOW_IDS = frozenset({PUBLIC_ARTICLE_WORKFLOW_ID, ANSWER_PAGE_WORKFLOW_ID})
ADAPTER = "repository"
ADAPTATION_OPERATION = "content_draft_adaptation_v1"
ADAPTED_PATH = "Repository-adapted page"
# The largest reviewed document each workflow saves; the Markdown publisher reads it whole.
DOCUMENT_MAX_BYTES = {
    ANSWER_PAGE_WORKFLOW_ID: 150_000,
    PUBLIC_ARTICLE_WORKFLOW_ID: 300_000,
    REFRESH_WORKFLOW_ID: 40_000,
}


def settings_path(program_id):
    return f"content/plans/{UUID(str(program_id))}/delivery.json"


def delivery_key(run_id):
    return f"content-draft:{UUID(str(run_id))}:delivery"


def preparation_key(run_id):
    return f"{delivery_key(run_id)}:prepare"


def github_key(run_id):
    return f"{delivery_key(run_id)}:github"


# A refresh retried after the default branch moved reads the repository again under a new
# attempt's keys, up to this many times.
MAX_REFRESH_ATTEMPTS = 20


def refresh_keys(run_id, attempt):
    """The repository read and GitHub write keys of one refresh delivery attempt."""
    suffix = f":{attempt}" if attempt else ""
    return f"{run_id}:refresh_repository{suffix}", f"{github_key(run_id)}{suffix}"


def choice_key(run_id):
    return f"{delivery_key(run_id)}:choice"


def remember_request_id(run_id, mode, settings_revision):
    """One settings save per approval attempt at one project revision.

    A retry at the same revision replays the save; a later attempt, after a stale head or
    after the founder picked another delivery, is a new request rather than a conflict.
    """
    return uuid5(NAMESPACE_URL, f"tin:delivery-choice:{run_id}:{mode}:{settings_revision}")


def approval_label(mode):
    return "Approve & publish" if mode == "github_commit" else "Approve & open PR"


def chosen_mode(intent):
    return (intent.get("settings") or {}).get("mode")


def adaptation_start_key(run_id):
    """The start key of the one content.deliver run an approval may start."""
    return f"approval-delivery:{UUID(str(run_id))}"


def adaptable(settings, run):
    """Whether approving this page adapts it to the site instead of committing its Markdown."""
    from tin_lite.codex_api import api_enabled

    return getattr(run, "workflow_id", None) in ADAPTED_WORKFLOW_IDS and api_enabled(
        settings, run.project_id
    )


def adapted(intent):
    return bool(intent) and intent.get("adapter") == ADAPTER


class AdaptationRefused(ValueError):
    """The adaptation was refused and recorded; retrying the same start cannot help."""


def adaptation_refusal(exc):
    """Why the adaptation could not start, when retrying the same start cannot help; else None.

    Credits and limits point to the two ways to try again once they are fixed. Every message
    here is Tin's own; provider payloads never reach the receipt.
    """
    from tin_lite.billing_contracts import BillingError
    from tin_lite.integrations import IntegrationAuthorizationError, IntegrationNotConfiguredError
    from tin_lite.run_service import WorkflowExecutorUnavailableError
    from tin_lite.workflow_prerequisites import PrerequisiteError

    if isinstance(exc, BillingError):
        return (
            f"Tin could not start adapting this page. {exc} The approved page stays in Tin; "
            "retry delivery or use Prepare PR once that is fixed."
        )[:500]
    if isinstance(
        exc,
        (
            ValueError,
            LookupError,
            PrerequisiteError,
            WorkflowExecutorUnavailableError,
            IntegrationAuthorizationError,
            IntegrationNotConfiguredError,
        ),
    ):
        return f"Tin could not start adapting this page. {exc}"[:500]
    return None


def publish_sentence(mode, *, route_missing=False):
    """What Publish does for an adapted page, in the founder's words.

    Tin merges a pull request that adds only the page, or the page and the route the founder
    chose for these pages. Until a route is chosen, a commit-to-main setting still leaves the
    first pull request open.
    """
    if route_missing and mode == "github_commit":
        return (
            "Tin adapts it to your site and opens a pull request, "
            "since you have not chosen where these pages live yet"
        )
    if mode == "github_commit":
        return "Tin adapts it to your site and commits it to main"
    return "Tin adapts it to your site and opens a pull request"


def about_usd(value):
    """A cost preview as the card shows it: whole dollars, or cents under a dollar."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return f"${amount:.2f}" if amount < 1 else f"${int(amount + 0.5)}"


def markdown_path(value):
    return (
        isinstance(value, str)
        and safe_project_file_path(value)
        and bool(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_./-]{0,239}\.md", value))
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


class DeliverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["draft_only", "github_pr", "github_commit"] = "draft_only"
    repository: str = Field(default="", max_length=140)
    path_pattern: str = Field(default="content/blog/{slug}.md", max_length=240)
    frontmatter: dict[str, str | bool | int] = Field(default_factory=dict, max_length=20)
    # Updates are deliberately mapped to actual files, never guessed from a public URL.
    item_paths: dict[str, str] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def valid(self):
        if self.mode in REPOSITORY_MODES and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", self.repository
        ):
            raise ValueError("Choose the connected owner/repository.")
        pattern = self.path_pattern
        if pattern.count("{slug}") != 1 or not markdown_path(pattern.replace("{slug}", "article")):
            raise ValueError("Use a Markdown path with one {slug}, such as content/blog/{slug}.md.")
        if any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) or not markdown_path(path)
            for key, path in self.item_paths.items()
        ):
            raise ValueError("Map article IDs to explicit Markdown repository paths.")
        for key, value in self.frontmatter.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key):
                raise ValueError("Frontmatter field names must be simple identifiers.")
            if isinstance(value, str) and (
                len(value) > 1000
                or "\x00" in value
                or any(
                    token not in {"title", "date", "slug"}
                    for token in re.findall(r"\{([^{}]*)\}", value)
                )
            ):
                raise ValueError("Frontmatter supports only {title}, {date}, and {slug} values.")
        return self


def slug_for(candidate, fallback):
    slug = re.sub(r"[^a-z0-9]+", "-", candidate.casefold()).strip("-")[:100].rstrip("-")
    return slug or fallback


def destination(settings, selected):
    item = selected["item"]
    explicit = settings.item_paths.get(item["id"])
    if item["action"] == "update_page" and not explicit:
        raise ValueError(
            "Map this existing-page article to its Markdown file in delivery settings first."
        )
    candidate = urlsplit(item.get("destination", "")).path.rstrip("/").rsplit("/", 1)[-1]
    candidate = candidate or item["title"]
    slug = slug_for(candidate, item["id"])
    path = explicit or settings.path_pattern.replace("{slug}", slug)
    if not markdown_path(path):
        raise ValueError("The selected article needs a valid Markdown destination.")
    return path, slug


def document_destination(settings, title, run_id):
    """A reviewed document outside a content plan lands at the pattern's slug path."""
    slug = slug_for(title, str(run_id))
    path = settings.path_pattern.replace("{slug}", slug)
    if not markdown_path(path):
        raise ValueError("The reviewed document needs a valid Markdown destination.")
    return path, slug


def document_body(raw):
    """The exact reviewed Markdown of a public article or answer page, plus its title."""
    article = raw.decode("utf-8").strip() + "\n"
    title = re.search(r"(?m)^# (.+)$", article)
    if title is None:
        raise ValueError("The reviewed document needs a title heading before delivery.")
    return article, title.group(1).strip()


def display_title(raw):
    """A saved document's own heading as a one-line label, or None to keep its file name."""
    try:
        _, title = document_body(raw)
    except ValueError:  # includes undecodable bytes
        return None
    title = re.sub(r"[`*_]", "", "".join(c for c in title if c.isprintable()))
    return " ".join(title.split())[:160].strip() or None


def summary_line(raw):
    """One sentence from a saved document that says what it contains, or None.

    The first paragraph or list item after the title, skipping notes set entirely in
    emphasis, quotes, tables, rules and code. A list item keeps its section's name.
    """
    try:
        lines = raw.decode("utf-8").splitlines()
    except (AttributeError, UnicodeDecodeError):
        return None
    if lines and lines[0].strip() == "---":  # front matter
        closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
        lines = lines[closing + 1 :]
    section, fenced = None, False
    for line in lines:
        text = line.strip()
        if text.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not text or text.startswith(("|", ">", "<", "![", "---", "***")):
            continue
        heading = re.match(r"(#+)\s+(.*)", text)
        if heading:
            section = heading.group(2).strip() if len(heading.group(1)) == 2 else section
            continue
        if re.fullmatch(r"([*_]).+\1", text):
            continue
        item = re.match(r"(?:[-*+]|\d+[.)])\s+(.*)", text)
        body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", item.group(1) if item else text)
        body = " ".join(re.sub(r"[`*_]", "", body).split())
        sentence = re.match(r"(.+?[.!?])(?:\s|$)", body)
        body = sentence.group(1) if sentence else body
        if item and section:
            body = f"{section}: {body}"
        return body[:240].strip() or None
    return None


_COUNT_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_ANNOUNCED_KINDS = {
    "breaking": ("breaking change", "breaking changes"),
    "feature": ("feature", "features"),
    "features": ("feature", "features"),
    "improvement": ("improvement", "improvements"),
    "improvements": ("improvement", "improvements"),
    "fix": ("fix", "fixes"),
    "fixes": ("fix", "fixes"),
}


def _spoken_list(items):
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def release_line(raw):
    """What a release announcement draft announces and which drafts it holds, or None.

    Only a document whose first section is "What shipped", listed as counted groups
    ("### Features (6)"), qualifies; its later sections are the channel drafts.
    """
    try:
        text = raw.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None
    sections = re.split(r"(?m)^## +", text)[1:]
    if not sections or sections[0].splitlines()[0].strip().casefold() != "what shipped":
        return None
    counts = []
    for name, number in re.findall(r"(?m)^### +([A-Za-z ]+?) +\((\d+)\) *$", sections[0]):
        kind = _ANNOUNCED_KINDS.get(name.strip().casefold())
        if kind is None or int(number) < 1:
            return None
        amount = int(number)
        spoken = _COUNT_WORDS[amount] if amount < len(_COUNT_WORDS) else str(amount)
        counts.append(f"{spoken} {kind[0] if amount == 1 else kind[1]}")
    if not counts:
        return None
    channels = []
    for section in sections[1:]:
        heading = " ".join(section.splitlines()[0].split())
        if heading.casefold().startswith("newsletter"):
            heading = "your newsletter"
        if heading and heading not in channels:
            channels.append(heading)
    drafts = f", with drafts for {_spoken_list(channels)}" if channels else ""
    return f"Announces {_spoken_list(counts)}{drafts}."[:240]


def review_line(raw):
    """The one sentence a decision card shows for a saved document, or None."""
    return release_line(raw) or summary_line(raw)


def new_page_header(settings, title, date, slug, page_metadata=None):
    values = {"title": title, "date": date, "slug": slug}
    metadata = {
        **(page_metadata or {}),
        **{
            key: re.sub(r"\{(title|date|slug)\}", lambda m: values[m[1]], value)
            if isinstance(value, str)
            else value
            for key, value in settings.frontmatter.items()
        },
    }
    if not metadata:
        return ""
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n\n"


def page_frontmatter(article):
    """Split a reviewed document's own plain frontmatter (an answer page's search listing)
    from its copy, so delivery writes one merged header instead of two."""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n+", article, re.S)
    if not match:
        return {}, article
    metadata = yaml.safe_load(match[1])
    if not isinstance(metadata, dict) or not all(
        isinstance(value, str | int | float | bool) for value in metadata.values()
    ):
        raise ValueError("The reviewed document's frontmatter is unsupported.")
    return metadata, article[match.end() :]


def article_body(raw, context):
    content_draft.validate_artifact(raw, context)
    if context.get("output_validator") == content_draft.EDITORIAL_VALIDATOR and raw.startswith(
        b"# Content assessment\n"
    ):
        raise ValueError("This run recorded an editorial assessment, not an article for delivery.")
    if context.get("output_validator") in content_draft.CLEAN_VALIDATORS:
        article = raw.decode("utf-8").strip() + "\n"
    else:
        body = raw.decode("utf-8")[4:].split("\n---\n", 1)[1]
        article = body.split("\n## Verification notes\n", 1)[0].strip() + "\n"
    title = re.search(r"(?m)^# (.+)$", article).group(1).strip()
    return article, title


def render_file(raw, context, settings, existing):
    article, title = article_body(raw, context)
    path, slug = destination(settings, context)
    updating = context["item"]["action"] == "update_page"
    if updating != (existing is not None):
        raise ValueError(
            "The update destination does not exist. Check its mapped file."
            if updating
            else "This new-page destination already exists. Choose another path."
        )
    header = ""
    if existing is not None and existing.startswith(("---\n", "---\r\n")):
        frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", existing, re.S)
        if not frontmatter or not isinstance(yaml.safe_load(frontmatter[1]), dict):
            raise ValueError("The existing Markdown frontmatter is unsupported.")
        header = frontmatter[0] + "\n"
    elif existing is not None and existing.startswith(("+++", "\ufeff")):
        raise ValueError("This Markdown file uses unsupported metadata. Keep it draft-only.")
    elif not updating:
        header = new_page_header(settings, title, context["due_date"], slug)
    return path, header + article, title


def delivery_projection(run, intent, receipt):
    """One card shape for both repository modes; ``receipt`` is a dict or None."""
    mode = intent["settings"]["mode"]
    receipt = receipt or {}
    completed = receipt.get("status") == "completed"
    return {
        "repository": intent["settings"]["repository"],
        "path": intent["path"],
        "mode": mode,
        "status": receipt.get("status")
        or ("pending" if run.review_decision == "approved" else "awaiting_review"),
        "error": receipt.get("error_message"),
        "pull_request": receipt.get("result") if completed and mode == "github_pr" else None,
        "commit": receipt.get("result") if completed and mode == "github_commit" else None,
        "approval_label": approval_label(mode),
    }


# A content.generate answer page reaches the site through website.change (a metered adaptation
# at the founder's chosen route), never as Markdown in content/answers/.
WEBSITE_CHANGE = "website.change"


class ContentDelivery:
    def __init__(self, *, database, storage=None, integrations=None):
        self.db, self.storage, self.integrations = database, storage, integrations
        self.programs = ContentPrograms(database=database, storage=storage)

    async def settings(self, *, project_id, program_id, revision=None):
        await self.programs.configured(project_id, program_id)
        project = await self.db.get_project(project_id)
        if revision is None:
            repo = await self.storage.get_repo(project.state_repo_id)
            revision = await self.storage.head_sha(repo, project.canonical_branch)
        raw = await self.storage.read_canonical_artifact_if_exists(
            repo_id=project.state_repo_id, commit_sha=revision, path=settings_path(program_id)
        )
        if raw is not None and len(raw) > 32_000:
            raise ValueError("Delivery settings exceed 32 KB.")
        settings = DeliverySettings.model_validate_json(raw) if raw else DeliverySettings()
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key="infra.github"
        )
        return {
            "revision": revision,
            "path": settings_path(program_id),
            "settings": settings.model_dump(),
            "available_repository": (
                connection.configuration.get("selected_repository")
                if connection and connection.status == "connected"
                else None
            ),
        }

    async def save_settings(
        self,
        *,
        project_id,
        program_id,
        settings,
        request_id,
        expected_revision,
        actor,
        client_id=None,
    ):
        await self.programs.configured(project_id, program_id)
        # A replay must not require an integration that was subsequently disconnected.
        prior = await self.db.get_project_file_change(project_id=project_id, request_id=request_id)
        if settings.mode in REPOSITORY_MODES and not prior:
            if self.integrations is None:
                raise ValueError("Connect GitHub before enabling delivery.")
            await self.integrations.github_repository_binding(
                project_id=project_id, expected_repository=settings.repository
            )
        project = await self.db.get_project(project_id)
        return asdict(
            await ProjectFileService(database=self.db, storage=self.storage).commit(
                project=project,
                actor_clerk_user_id=actor,
                client_id=client_id,
                request_id=request_id,
                expected_revision=expected_revision,
                message="Update content delivery settings",
                changes=[
                    {
                        "operation": "upsert",
                        "path": settings_path(program_id),
                        "content": canonical_json(settings.model_dump()).decode(),
                    }
                ],
            )
        )

    async def pin(self, *, project_id, selected):
        configured = await self.settings(
            project_id=project_id,
            program_id=UUID(selected["program_id"]),
            revision=selected["project_revision"],
        )
        settings = DeliverySettings.model_validate(configured["settings"])
        if settings.mode == "draft_only":
            return None
        kind = item_kind(selected["item"])
        # A refresh edits the page's own source, and an answer page is adapted to the site at
        # its route: neither has a Markdown destination path.
        path = None if kind != content_draft.ARTICLE else destination(settings, selected)[0]
        if self.integrations is None:
            raise ValueError("Connect GitHub before drafting for repository delivery.")
        binding = await self.integrations.github_repository_binding(
            project_id=project_id, expected_repository=settings.repository
        )
        return {
            **(
                {"kind": REFRESH_KIND}
                if kind == content_draft.REFRESH
                else {"adapter": ADAPTER, "via": WEBSITE_CHANGE}
                if kind == content_draft.ANSWER
                else {}
            ),
            "settings": settings.model_dump(),
            "settings_revision": configured["revision"],
            "path": path,
            **({"route": selected.get("page_route")} if kind == content_draft.ANSWER else {}),
            "repository_id": binding.repository_id,
            "connection_id": str(binding.connection_id),
            "installation_id": binding.installation_id,
        }

    async def intent(self, run):
        if run.workflow_id not in CHOICE_WORKFLOW_IDS:
            return None
        choice = await self.db.get_effect(choice_key(run.id))
        if choice and choice.status == "completed" and choice.result:
            return choice.result if chosen_mode(choice.result) in REPOSITORY_MODES else None
        if run.workflow_id != DRAFT_WORKFLOW_ID or run.executor != "codex.procedure":
            return None
        selected = await self.db.get_effect(content_draft.selection_key(run.id))
        return (
            (selected.result or {}).get("delivery")
            if selected and selected.status == "completed"
            else None
        )

    async def plan_kind(self, run):
        """What a content.generate run drafted: an article, an answer page or a page refresh.

        Read from the run's prepared context, which records a kind only for an answer or a
        refresh (content.generate 1.9.0). Other workflows have none.
        """
        if run.workflow_id != DRAFT_WORKFLOW_ID:
            return None
        prepared = await self.db.get_effect(content_draft.receipt_key(run.id))
        return content_draft.context_kind(
            prepared.result if prepared and prepared.status == "completed" else None
        )

    async def program_for(self, run):
        """The program whose delivery settings govern this run: (program_id, selection)."""
        if run.workflow_id == DRAFT_WORKFLOW_ID:
            selected = await self.db.get_effect(content_draft.selection_key(run.id))
            if not selected or selected.status != "completed" or not selected.result:
                raise ValueError("This draft has no saved article selection.")
            return UUID(str(selected.result["program_id"])), selected.result
        if run.project_workflow_id is None:
            raise ValueError(
                "Save this workflow to the project before choosing where it publishes."
            )
        return run.project_workflow_id, None

    async def choose(
        self, *, run, mode, remember=False, actor=None, adapt=False, trigger_source="manual"
    ):
        """Record the reviewer's delivery pick for one document; optionally keep it.

        `adapt` (the caller checked `adaptable`) sends an answer page or public article
        through content.deliver instead of the Markdown publisher; `mode` still says
        whether its pull request stays open or Tin merges it.
        """
        if run.workflow_id not in CHOICE_WORKFLOW_IDS:
            raise ValueError("This run does not publish to a repository.")
        if mode not in APPROVAL_CHOICES:
            raise ValueError("Choose a pull request, publishing now, or no delivery.")
        existing = await self.db.get_effect(choice_key(run.id))
        existing = existing.result if existing and existing.status == "completed" else None
        if run.review_decision == "approved":
            return existing  # The approval already happened; its delivery choice stands.
        kind = await self.plan_kind(run)
        if run.workflow_id == REFRESH_WORKFLOW_ID or kind == content_draft.REFRESH:
            # A page refresh, from content.refresh or a content.generate refresh item.
            return await self.choose_refresh(run=run, mode=mode, remember=remember, actor=actor)
        if kind == content_draft.ANSWER and mode in REPOSITORY_MODES:
            # An answer page always goes to the site through website.change at its route.
            return await self.choose_adaptation(
                run=run,
                mode=mode,
                remember=remember,
                actor=actor,
                trigger_source=trigger_source,
            )
        if adapt and run.workflow_id in ADAPTED_WORKFLOW_IDS and mode in REPOSITORY_MODES:
            return await self.choose_adaptation(
                run=run,
                mode=mode,
                remember=remember,
                actor=actor,
                trigger_source=trigger_source,
            )
        if run.workflow_id == DRAFT_WORKFLOW_ID:
            from tin_lite.organic_content import intent_for

            system_intent = await intent_for(self.db, run)
            if system_intent and system_intent.get("mode") == "github_pr":
                if mode == "github_commit":
                    raise ValueError(
                        "This organic system adapts the article into a reviewable PR. "
                        "Choose a PR or keep the draft in Tin."
                    )
                # The parent owns repository adaptation; never also invoke the
                # generic exact-Markdown publisher for this approval.
                return await self.record_choice(
                    run,
                    {
                        "system_delivery": system_intent,
                        "mode": mode,
                        "chosen_by": actor,
                    },
                )
        if mode == "none" and run.workflow_id != DRAFT_WORKFLOW_ID and not run.project_workflow_id:
            # Keeping a one-off page in Tin leaves nothing to configure or remember.
            return await self.record_choice(
                run,
                {
                    "settings": DeliverySettings().model_dump(),
                    "settings_revision": None,
                    "path": None,
                    "chosen_by": actor,
                },
            )
        program_id, selected = await self.program_for(run)
        configured = await self.settings(project_id=run.project_id, program_id=program_id)
        base = DeliverySettings.model_validate(configured["settings"])
        if mode == "none":
            chosen = DeliverySettings.model_validate({**base.model_dump(), "mode": "draft_only"})
            record = {
                "settings": chosen.model_dump(),
                "settings_revision": configured["revision"],
                "path": None,
            }
        else:
            chosen = DeliverySettings.model_validate(
                {
                    **base.model_dump(),
                    "mode": mode,
                    "repository": base.repository or configured["available_repository"] or "",
                }
            )
            if selected is not None:
                path, _ = destination(chosen, selected)
            else:
                _, _, title = await self.document_source(run)
                path, _ = document_destination(chosen, title, run.id)
            if self.integrations is None:
                raise ValueError("Connect GitHub before publishing to a repository.")
            binding = await self.integrations.github_repository_binding(
                project_id=run.project_id, expected_repository=chosen.repository
            )
            record = {
                "settings": chosen.model_dump(),
                "settings_revision": configured["revision"],
                "path": path,
                "repository_id": binding.repository_id,
                "connection_id": str(binding.connection_id),
                "installation_id": binding.installation_id,
            }
        record["chosen_by"] = actor
        if remember and chosen.model_dump() != base.model_dump():
            await self.save_settings(
                project_id=run.project_id,
                program_id=program_id,
                settings=chosen,
                request_id=remember_request_id(run.id, mode, configured["revision"]),
                expected_revision=configured["revision"],
                actor=actor,
            )
        return await self.record_choice(run, record)

    async def saved_mode(self, run):
        """The delivery the founder saved for this page's workflow: commit or pull request.

        Only an explicit commit-to-main setting commits; draft-only and unsaved pages open
        a pull request when the founder presses Publish.
        """
        try:
            program_id, _ = await self.program_for(run)
            configured = await self.settings(project_id=run.project_id, program_id=program_id)
        except (ValueError, LookupError):
            return "github_pr"
        return "github_commit" if configured["settings"]["mode"] == "github_commit" else "github_pr"

    async def choose_adaptation(self, *, run, mode, remember, actor, trigger_source):
        """Pin the repository and the founder's delivery for a page Tin adapts to the site.

        The page is not copied as-is, so no Markdown destination is computed here: the
        adaptation picks the site's own folder. A page outside a saved workflow still
        adapts; only remembering the pick needs one.
        """
        try:
            program_id, _ = await self.program_for(run)
            configured = await self.settings(project_id=run.project_id, program_id=program_id)
        except (ValueError, LookupError):
            program_id, configured = None, None
        if configured is not None:
            base = DeliverySettings.model_validate(configured["settings"])
            available = configured["available_repository"]
        else:
            base = DeliverySettings()
            connection = await self.db.get_integration_connection(
                project_id=run.project_id, provider_key="infra.github"
            )
            available = (
                connection.configuration.get("selected_repository")
                if connection and connection.status == "connected"
                else None
            )
        chosen = DeliverySettings.model_validate(
            {**base.model_dump(), "mode": mode, "repository": base.repository or available or ""}
        )
        if self.integrations is None:
            raise ValueError("Connect GitHub before publishing to a repository.")
        binding = await self.integrations.github_repository_binding(
            project_id=run.project_id, expected_repository=chosen.repository
        )
        from tin_lite.page_routes import PageRouteService

        answer = await self.plan_kind(run) == content_draft.ANSWER
        record = {
            "adapter": ADAPTER,
            **({"via": WEBSITE_CHANGE} if answer else {}),
            "settings": chosen.model_dump(),
            "settings_revision": configured["revision"] if configured else None,
            "path": None,
            # Where the founder chose these pages live, pinned at approval; None when unset.
            "route": await PageRouteService(database=self.db, storage=self.storage).route_for(
                run, page_type="answer_page" if answer else None
            ),
            "repository_id": binding.repository_id,
            "connection_id": str(binding.connection_id),
            "installation_id": binding.installation_id,
            "chosen_by": actor,
            "trigger_source": trigger_source if trigger_source in {"manual", "mcp"} else "manual",
        }
        if remember and program_id is not None and chosen.model_dump() != base.model_dump():
            await self.save_settings(
                project_id=run.project_id,
                program_id=program_id,
                settings=chosen,
                request_id=remember_request_id(run.id, mode, configured["revision"]),
                expected_revision=configured["revision"],
                actor=actor,
            )
        return await self.record_choice(run, record)

    async def choose_refresh(self, *, run, mode, remember, actor):
        """Pin the repository and delivery for a page refresh's approved replacements.

        A refresh edits the page's existing source, so no destination path is computed:
        Tin finds the approved old text in the repository when it delivers. A refresh
        started outside a saved workflow uses the connected repository and cannot
        remember the pick.
        """
        try:
            program_id, _ = await self.program_for(run)
            configured = await self.settings(project_id=run.project_id, program_id=program_id)
        except (ValueError, LookupError):
            program_id, configured = None, None
        if configured is not None:
            base = DeliverySettings.model_validate(configured["settings"])
            available = configured["available_repository"]
        else:
            base = DeliverySettings()
            connection = await self.db.get_integration_connection(
                project_id=run.project_id, provider_key="infra.github"
            )
            available = (
                connection.configuration.get("selected_repository")
                if connection and connection.status == "connected"
                else None
            )
        if mode == "none":
            chosen = DeliverySettings.model_validate({**base.model_dump(), "mode": "draft_only"})
            record = {"kind": REFRESH_KIND, "settings": chosen.model_dump(), "path": None}
        else:
            chosen = DeliverySettings.model_validate(
                {
                    **base.model_dump(),
                    "mode": mode,
                    "repository": base.repository or available or "",
                }
            )
            if self.integrations is None:
                raise ValueError("Connect GitHub before publishing to a repository.")
            binding = await self.integrations.github_repository_binding(
                project_id=run.project_id, expected_repository=chosen.repository
            )
            record = {
                "kind": REFRESH_KIND,
                "settings": chosen.model_dump(),
                "settings_revision": configured["revision"] if configured else None,
                "path": None,
                "repository_id": binding.repository_id,
                "connection_id": str(binding.connection_id),
                "installation_id": binding.installation_id,
            }
        record["chosen_by"] = actor
        if (
            remember
            and program_id is not None
            and mode != "none"
            and chosen.model_dump() != base.model_dump()
        ):
            await self.save_settings(
                project_id=run.project_id,
                program_id=program_id,
                settings=chosen,
                request_id=uuid5(NAMESPACE_URL, f"tin:delivery-choice:{run.id}:{mode}"),
                expected_revision=configured["revision"],
                actor=actor,
            )
        return await self.record_choice(run, record)

    async def deliver_refresh(self, run, intent):
        """Apply exactly the approved replacements to the site's source, then commit or open a PR.

        Tin finds each approved old text in the repository itself and changes nothing else
        (content_refresh.plan_patch verifies it). Text the source does not hold verbatim, or
        holds in several unrelated places, stops the delivery with the reason.
        """
        from tin_lite import content_refresh
        from tin_lite.content_refresh_sources import ContentRefreshSources
        from tin_lite.technical_build_profile import archive_files

        if run.status != RunStatus.SUCCEEDED or run.review_decision != "approved":
            raise ValueError("Approve this refresh before publishing it.")
        direct_commit = chosen_mode(intent) == "github_commit"
        key = delivery_key(run.id)
        async with self.db.effect_lock(key, OPERATION) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
            try:
                if self.integrations is None:
                    raise ValueError("GitHub is unavailable. Reconnect it, then retry delivery.")
                context = await ContentRefreshSources(database=self.db, storage=self.storage).saved(
                    run.id
                )
                if not context or not context.get("page"):
                    raise ValueError("This refresh has no pinned page.")
                raw, _, _ = await self.document_source(run)
                items = content_refresh.validate_document(raw, context)
                binding = await self.integrations.github_repository_binding(
                    project_id=run.project_id, expected_repository=intent["settings"]["repository"]
                )
                if str(binding.repository_id) != str(intent["repository_id"]) or str(
                    binding.connection_id
                ) != str(intent["connection_id"]):
                    raise ValueError(
                        "The GitHub connection changed after this refresh was approved."
                    )
                read_key, write_key = refresh_keys(
                    run.id, await self.refresh_attempt(run.id, binding.head_sha)
                )
                bundle = await self.integrations.github_repository_bundle(
                    project_id=run.project_id,
                    run_id=run.id,
                    execution_key=read_key,
                    expected_binding=binding,
                )
                if not getattr(bundle, "complete", True):
                    named = describe_omissions(getattr(bundle, "missing", ()), limit=3)
                    raise ValueError(
                        "Tin couldn't read every file in the repository"
                        + (f" ({named})" if named else "")
                        + ", so it will not guess where the page's text lives. Change it by hand."
                    )
                # Read only the source files plan_patch searches; large media and built
                # files were never part of the snapshot.
                changed = content_refresh.plan_patch(
                    archive_files(bundle.archive, select=content_refresh.searched), items
                )
                files = tuple(
                    GitHubFileChange(path=path, content=text) for path, text in changed.items()
                )
                page = context["page"]["path"]
                title = f"Refresh: {page}"[:200]
                if direct_commit:
                    result = await self.integrations.github_commit_files(
                        project_id=run.project_id,
                        run_id=run.id,
                        execution_key=write_key,
                        message=title,
                        files=files,
                        base_branch=binding.default_branch,
                        expected_binding=binding,
                    )
                else:
                    result = await self.integrations.github_create_pull_request(
                        project_id=run.project_id,
                        run_id=run.id,
                        execution_key=write_key,
                        title=title,
                        body=content_refresh.patch_body(context, items),
                        files=files,
                        base_branch=binding.default_branch,
                        expected_base_sha=binding.head_sha,
                        expected_binding=binding,
                        allow_unrelated_base_advance=True,
                    )
                result = {
                    **asdict(result),
                    "page": context["page"]["url"],
                    "changed_paths": sorted(changed),
                    "replacements_sha256": content_refresh.document_digest(items),
                    # A commit is live on the default branch now; a PR once it merges.
                    "delivered_at": datetime.now(UTC).isoformat(),
                }
                if direct_commit:
                    short = result["commit"][:7]
                    event = {
                        "event_type": "content_refresh_commit_ready",
                        "summary": f"The approved refresh of {page} is committed as {short}.",
                        "external_label": f"View commit {short}",
                    }
                else:
                    event = {
                        "event_type": "content_refresh_pull_request_ready",
                        "summary": f"The approved refresh of {page} is ready as PR "
                        f"#{result['number']}.",
                        "external_label": f"Review PR #{result['number']}",
                    }
                async with conn.transaction():
                    await self.db.complete_effect(conn, execution_key=key, result=result)
                    await self.db.add_activity(
                        run_id=run.id,
                        event_type=event["event_type"],
                        audience="product",
                        summary=event["summary"],
                        details={
                            "kind": "runs",
                            "status": "succeeded",
                            "external_url": result["url"],
                            "external_label": event["external_label"],
                            "repository": result["repository"],
                            "artifact_ref": run.artifact_ref,
                        },
                        dedupe_key=f"{key}:ready",
                        conn=conn,
                    )
            except Exception as exc:
                error = (
                    str(exc)[:500]
                    if isinstance(exc, (ValueError, IntegrationAuthorizationError))
                    else "GitHub delivery could not be confirmed. "
                    "Retry delivery; the approved refresh is safe."
                )
                await self.db.fail_effect(conn, execution_key=key, error_message=error)
                raise

    async def refresh_attempt(self, run_id, head_sha):
        """Which delivery attempt a refresh uses now.

        An attempt that committed, opened a pull request, or may have done either keeps its
        keys, so a retry replays or recovers it. When the attempt's write failed or never
        started and the default branch has moved since its repository read, a new attempt
        reads the current head instead of replaying the old one.
        """
        for attempt in range(MAX_REFRESH_ATTEMPTS):
            read_key, write_key = refresh_keys(run_id, attempt)
            write = await self.db.get_integration_call_receipt(write_key)
            if write is not None and write.status != "failed":
                return attempt
            read = await self.db.get_integration_call_receipt(read_key)
            summary = read.response_summary if read is not None else None
            if read is None or (
                write is None
                and (
                    read.status != "completed"
                    or (isinstance(summary, dict) and summary.get("head_sha") == head_sha)
                )
            ):
                return attempt
        raise ValueError(
            "This refresh failed to deliver too many times. Apply it by hand or ask your "
            "coding agent."
        )

    async def record_choice(self, run, record):
        key = choice_key(run.id)
        async with self.db.effect_lock(key, CHOICE_OPERATION) as (conn, receipt):
            if receipt and receipt.status == "completed":
                if receipt.result != record:
                    # Not yet approved: the reviewer changed their mind before approving.
                    await conn.execute(
                        "UPDATE effect_receipts SET result = $2::jsonb, updated_at = now() "
                        "WHERE execution_key = $1",
                        key,
                        json.dumps(record),
                    )
                return record
            await self.db.start_effect(conn, execution_key=key, operation=CHOICE_OPERATION)
            await self.db.complete_effect(conn, execution_key=key, result=record)
        return record

    async def document_source(self, run):
        """The exact reviewed Markdown of a non-plan run (public article, answer page)."""
        if not run.canonical_commit_sha or not run.artifact_path:
            raise ValueError("The reviewed document's publication proof is unavailable.")
        project = await self.db.get_project(run.project_id)
        raw = await self.storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=run.canonical_commit_sha,
            path=run.artifact_path,
        )
        limit = DOCUMENT_MAX_BYTES.get(run.workflow_id, 80_000)
        if len(raw) > limit:
            raise ValueError(f"The reviewed document is larger than {limit // 1000} KB.")
        article, title = document_body(raw)
        return raw, article, title

    async def status(self, run):
        from tin_lite import content_repository_delivery as repository_delivery

        if repository_delivery.adapts(run):
            facts = await repository_delivery.adaptation_facts(self.db, [run.id])
            return repository_delivery.child_projection(run, facts.get(run.id))
        intent = await self.intent(run)
        if adapted(intent):
            start = await self.db.get_effect(delivery_key(run.id))
            start = (
                {
                    "status": start.status,
                    "result": start.result,
                    "error_message": start.error_message,
                }
                if start
                else None
            )
            return await repository_delivery.adapted_status(self.db, run, intent, start)
        if not intent:
            if run.workflow_id != DRAFT_WORKFLOW_ID:
                return None
            from tin_lite.content_editorial_judgment import NO_DRAFT, saved
            from tin_lite.organic_content import delivery_status, intent_for

            system_intent = await intent_for(self.db, run)
            if not system_intent or system_intent.get("mode") != "github_pr":
                return None
            judgment = await saved(self.db, run)
            if judgment and judgment["outcome"] in NO_DRAFT:
                return None
            return await delivery_status(self.db, run, system_intent)
        if run.workflow_id == DRAFT_WORKFLOW_ID:
            from tin_lite.content_editorial_judgment import NO_DRAFT, saved

            judgment = await saved(self.db, run)
            if judgment and judgment["outcome"] in NO_DRAFT:
                return None
        receipt = await self.db.get_effect(delivery_key(run.id))
        return delivery_projection(
            run,
            intent,
            {
                "status": receipt.status,
                "error_message": receipt.error_message,
                "result": receipt.result,
            }
            if receipt
            else None,
        )

    async def statuses(self, runs):
        """A bounded Postgres read for card polling; never filesystem/provider reads."""
        from tin_lite import content_repository_delivery as repository_delivery
        from tin_lite.content_programs import decoded

        candidates = [
            r for r in runs if r.workflow_id in CHOICE_WORKFLOW_IDS or repository_delivery.adapts(r)
        ]
        if not candidates:
            return {}
        keys = [
            key
            for run in candidates
            if run.workflow_id in CHOICE_WORKFLOW_IDS
            for key in (
                content_draft.selection_key(run.id),
                delivery_key(run.id),
                choice_key(run.id),
                f"{run.id}:procedure_canonical_commit",
            )
        ]
        rows = await self.db.pool.fetch(
            "SELECT execution_key, status, result, error_message FROM effect_receipts "
            "WHERE execution_key = ANY($1::text[])",
            keys,
        )
        receipts = {
            r["execution_key"]: {**dict(r), "result": decoded(r["result"] or {})} for r in rows
        }
        output = {}
        children = await repository_delivery.adaptation_facts(
            self.db,
            [run.id for run in candidates if repository_delivery.adapts(run)],
        )
        for run in candidates:
            if repository_delivery.adapts(run):
                if run.id in children:
                    output[run.id] = repository_delivery.child_projection(run, children[run.id])
                continue
            if run.workflow_id == DRAFT_WORKFLOW_ID:
                from tin_lite.content_editorial_judgment import no_draft

                publication = receipts.get(f"{run.id}:procedure_canonical_commit", {})
                if publication.get("status") == "completed" and no_draft(publication.get("result")):
                    continue
            choice = receipts.get(choice_key(run.id), {})
            if choice.get("status") == "completed" and choice.get("result"):
                intent = (
                    choice["result"] if chosen_mode(choice["result"]) in REPOSITORY_MODES else None
                )
            elif run.workflow_id == DRAFT_WORKFLOW_ID and run.executor == "codex.procedure":
                selected = receipts.get(content_draft.selection_key(run.id), {})
                intent = (
                    selected.get("result", {}).get("delivery")
                    if selected.get("status") == "completed"
                    else None
                )
            else:
                intent = None
            selected = receipts.get(content_draft.selection_key(run.id), {})
            system_intent = selected.get("result", {}).get("system_delivery")
            if (
                selected.get("status") == "completed"
                and system_intent
                and system_intent.get("mode") == "github_pr"
            ):
                from tin_lite.organic_content import delivery_status

                output[run.id] = await delivery_status(self.db, run, system_intent)
                continue
            if not intent:
                continue
            if adapted(intent):
                output[run.id] = await repository_delivery.adapted_status(
                    self.db, run, intent, receipts.get(delivery_key(run.id))
                )
                continue
            output[run.id] = delivery_projection(run, intent, receipts.get(delivery_key(run.id)))
        return output

    async def adapt(self, run_id, *, start):
        """Start the one content.deliver run an adapted page's approval asks for.

        `start(run, intent)` admits and dispatches it (the ordinary run service, billing and
        Temporal start). The start receipt shares the Markdown publisher's key under another
        operation, so one approval can never run both. A refused admission (credits, limits,
        a changed connection) is recorded as a failed delivery the founder can retry and
        raised as AdaptationRefused; an uncertain one raises as it is, so the activity
        retries the same idempotent start.
        """
        run = await self.db.get_run(UUID(str(run_id)))
        if run is None:
            raise LookupError("Page not found.")
        intent = await self.intent(run)
        if not adapted(intent):
            return None
        if run.status != RunStatus.SUCCEEDED or run.review_decision != "approved":
            raise ValueError("Approve this page before publishing it.")
        key = delivery_key(run.id)
        async with self.db.effect_lock(key, ADAPTATION_OPERATION) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            await self.db.start_effect(conn, execution_key=key, operation=ADAPTATION_OPERATION)
            try:
                child = await start(run, intent)
            except Exception as exc:
                refused = adaptation_refusal(exc)
                await self.db.fail_effect(
                    conn,
                    execution_key=key,
                    error_message=refused
                    or "Tin could not confirm that the adaptation started. "
                    "Retry delivery; the approved page is safe in Tin.",
                )
                if refused:
                    raise AdaptationRefused(refused) from exc
                raise
            result = {
                "run_id": str(child.id),
                "repository": intent["settings"]["repository"],
                "mode": chosen_mode(intent),
            }
            async with conn.transaction():
                await self.db.complete_effect(conn, execution_key=key, result=result)
                await self.db.add_activity(
                    run_id=run.id,
                    event_type="content_adaptation_started",
                    audience="product",
                    summary="Tin is adapting the approved page to your site.",
                    details={
                        "kind": "runs",
                        "status": "running",
                        "delivery_run_id": str(child.id),
                        "repository": result["repository"],
                    },
                    dedupe_key=f"{key}:started",
                    conn=conn,
                )
            return result

    async def deliver(self, run_id):
        run = await self.db.get_run(UUID(str(run_id)))
        if run is None:
            raise LookupError("Draft not found.")
        intent = await self.intent(run)
        if not intent or adapted(intent):
            # An adapted page ships through content.deliver (see adapt), never as plain Markdown.
            return
        if run.workflow_id == DRAFT_WORKFLOW_ID:
            from tin_lite.content_editorial_judgment import NO_DRAFT, saved

            judgment = await saved(self.db, run)
            if judgment and judgment["outcome"] in NO_DRAFT:
                return  # No copy, review, or supplier effect exists to deliver.
        if intent.get("kind") == REFRESH_KIND:
            return await self.deliver_refresh(run, intent)
        if run.status != RunStatus.SUCCEEDED or run.review_decision != "approved":
            raise ValueError("Approve this draft before publishing it.")
        direct_commit = chosen_mode(intent) == "github_commit"
        key = delivery_key(run.id)
        async with self.db.effect_lock(key, OPERATION) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
            try:
                prepared = await self.prepare(run, intent, conn=conn)
                binding = GitHubRepositoryBinding(
                    **{
                        **prepared["binding"],
                        "connection_id": UUID(prepared["binding"]["connection_id"]),
                    }
                )
                files = (GitHubFileChange(path=prepared["path"], content=prepared["content"]),)
                if direct_commit:
                    # Same rendered file, committed onto the default branch; no branch, no PR.
                    result = await self.integrations.github_commit_files(
                        project_id=run.project_id,
                        run_id=run.id,
                        execution_key=github_key(run.id),
                        message=f"Content: {prepared['title']}"[:200],
                        files=files,
                        base_branch=binding.default_branch,
                        expected_binding=binding,
                    )
                else:
                    result = await self.integrations.github_create_pull_request(
                        project_id=run.project_id,
                        run_id=run.id,
                        execution_key=github_key(run.id),
                        title=f"Content: {prepared['title']}"[:200],
                        body="Add the reviewed article. Tin metadata and verification notes "
                        "are excluded.\n\nThis PR is unmerged. Review the site preview and "
                        "editorial checks before merging.",
                        files=files,
                        base_branch=binding.default_branch,
                        expected_base_sha=binding.head_sha,
                        expected_binding=binding,
                        allow_unrelated_base_advance=True,
                    )
                result = {
                    **asdict(result),
                    "path": prepared["path"],
                    "draft_revision": prepared["draft_revision"],
                    "draft_sha256": prepared["draft_sha256"],
                    "content_sha256": prepared["content_sha256"],
                }
                if direct_commit:
                    short = result["commit"][:7]
                    event = {
                        "event_type": "content_draft_commit_ready",
                        "summary": f"Reviewed article is published as commit {short}.",
                        "external_label": f"View commit {short}",
                    }
                else:
                    event = {
                        "event_type": "content_draft_pull_request_ready",
                        "summary": f"Reviewed article is ready as PR #{result['number']}.",
                        "external_label": f"Review PR #{result['number']}",
                    }
                async with conn.transaction():
                    await self.db.complete_effect(conn, execution_key=key, result=result)
                    await self.db.add_activity(
                        run_id=run.id,
                        event_type=event["event_type"],
                        audience="product",
                        summary=event["summary"],
                        details={
                            "kind": "runs",
                            "status": "succeeded",
                            "external_url": result["url"],
                            "external_label": event["external_label"],
                            "repository": result["repository"],
                            "program_id": (run.input or {}).get("program_id"),
                            "artifact_ref": run.artifact_ref,
                        },
                        dedupe_key=f"{key}:ready",
                        conn=conn,
                    )
            except Exception as exc:
                # Only bounded product errors; no provider payloads or credentials.
                error = (
                    str(exc)[:500]
                    if isinstance(exc, (ValueError, IntegrationAuthorizationError))
                    else "GitHub delivery could not be confirmed. "
                    "Retry delivery; the approved draft is safe."
                )
                await self.db.fail_effect(conn, execution_key=key, error_message=error)
                raise

    async def prepare(self, run, intent, *, conn=None):
        key = preparation_key(run.id)
        async with self.db.effect_lock(key, OPERATION, conn=conn) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            if self.integrations is None:
                raise ValueError("GitHub is unavailable. Reconnect it, then retry delivery.")
            settings = DeliverySettings.model_validate(intent["settings"])
            if run.workflow_id != DRAFT_WORKFLOW_ID:
                raw, article, title = await self.document_source(run)
                context = None
            else:
                project = await self.db.get_project(run.project_id)
                context = await self.db.get_effect(content_draft.receipt_key(run.id))
                canonical = await self.db.get_effect(f"{run.id}:procedure_canonical_commit")
                if (
                    not context
                    or context.status != "completed"
                    or not canonical
                    or canonical.status != "completed"
                    or run.canonical_commit_sha != canonical.result["canonical_commit_sha"]
                    or run.artifact_path != content_draft.PATH_TEMPLATE.format(run_id=run.id)
                ):
                    raise ValueError("The reviewed draft's publication proof is unavailable.")
                raw = await self.storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=run.canonical_commit_sha,
                    path=run.artifact_path,
                )
                if len(raw) > 80_000:
                    raise ValueError("The reviewed draft exceeds its size limit.")
            binding = await self.integrations.github_repository_binding(
                project_id=run.project_id, expected_repository=settings.repository
            )
            if any(
                str(getattr(binding, field)) != str(intent[field])
                for field in ("repository_id", "installation_id", "connection_id")
            ):
                raise ValueError("The GitHub connection changed after this draft started.")
            existing = await self.integrations.github_markdown_file(
                project_id=run.project_id, binding=binding, path=intent["path"]
            )
            if context is None:
                if existing is not None:
                    raise ValueError(
                        "This destination already exists in the repository. Choose another path."
                    )
                path, slug = document_destination(settings, title, run.id)
                date = run.created_at.date().isoformat() if run.created_at else ""
                metadata, article = page_frontmatter(article)
                content = new_page_header(settings, title, date, slug, metadata) + article
            else:
                path, content, title = render_file(raw, context.result, settings, existing)
            if path != intent["path"]:
                raise ValueError("The draft destination changed after admission.")
            prepared = {
                "binding": {**asdict(binding), "connection_id": str(binding.connection_id)},
                "path": path,
                "content": content,
                "title": title,
                "draft_revision": run.canonical_commit_sha,
                "draft_sha256": hashlib.sha256(raw).hexdigest(),
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
            await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
            await self.db.complete_effect(conn, execution_key=key, result=prepared)
            return prepared
