"""Explicit maintainer selection of public packages, not an auto-discovered plugin registry."""

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any
from uuid import UUID

from tin_lite.community import (
    REPOSITORY_ROOT,
    CheckoutStorage,
    ContributedPackage,
    validate,
)
from tin_lite.workflow_packages import decode_workflow_source


@dataclass(frozen=True)
class PublicMCPExposure:
    """Presentation only; never part of a pinned execution definition."""

    name: str
    destructive: bool
    open_world: bool


@dataclass(frozen=True)
class PublicWorkflow:
    id: UUID
    key: str
    public_mcp: PublicMCPExposure | None = None

    @property
    def definition_path(self) -> str:
        return ContributedPackage(
            self.key, REPOSITORY_ROOT / "workflow_packages" / self.key
        ).definition_path

    @cached_property
    def definition(self) -> dict[str, Any]:
        # Read only the explicitly selected manifest through the shared safe decoder.
        # Never import package code or discover unregistered folders for tool exposure.
        raw = CheckoutStorage(REPOSITORY_ROOT)._read(self.definition_path)
        return decode_workflow_source(raw, definition_path=self.definition_path).definition

    @property
    def executor(self):
        return self.definition["executor"]

    @property
    def title(self):
        return self.definition["title"]

    @property
    def description(self):
        return self.definition["description"]


# Merging a package does not activate it. Add a reviewed package here to include it
# in the next catalog sync. Keep IDs and keys stable; never reuse a retired identity.
# Copyable example.* packages are deliberately not product Registry entries.
PUBLIC_WORKFLOWS: tuple[PublicWorkflow, ...] = (
    PublicWorkflow(
        UUID("7c9ec5f4-d6e5-4800-9628-c7789f82b619"),
        "social.content_plan",
        PublicMCPExposure("start_social_content_plan", destructive=True, open_world=False),
    ),
    PublicWorkflow(
        UUID("5ad36b92-1f48-4ad2-a8a5-3c7f0bd7e905"),
        "social.post_batch",
        PublicMCPExposure("start_social_post_batch", destructive=True, open_world=False),
    ),
    PublicWorkflow(UUID("3eebd981-bc95-4c50-86a1-0643155270bc"), "social.x_compose"),
    PublicWorkflow(
        UUID("a94f3a5d-d58c-4e91-b6bb-c1e047dc8372"),
        "brand.capture",
        PublicMCPExposure("start_brand_capture", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("0ddd88b9-6ded-44c3-9982-b7505c2e31b1"),
        "product.analytics_brief",
        PublicMCPExposure("start_product_analytics_brief", destructive=True, open_world=False),
    ),
    PublicWorkflow(
        UUID("543626b6-635f-4cef-b00e-e46225753c13"),
        "growth.score_quiz",
        PublicMCPExposure("start_score_quiz", destructive=True, open_world=False),
    ),
    PublicWorkflow(
        UUID("ee456cef-e6a5-4da7-9447-a0834b5d107e"),
        "competitor.watch",
        PublicMCPExposure("start_competitor_watch", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("1cd28320-650d-4204-9a13-09b813d77317"),
        "qa.buyer_trust",
        PublicMCPExposure("start_buyer_trust_audit", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("4bf8c067-1709-427d-a00f-b0b53c871751"),
        "organic.error_surface",
        PublicMCPExposure("start_error_surface_research", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("2136b2ff-7570-40bf-97d3-e37889aea964"),
        "organic.mention_backlinks",
        PublicMCPExposure("start_mention_backlinks", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("7633e65c-d59d-4e96-a3f1-7e082d1cac5b"),
        "outreach.paying_segment",
        PublicMCPExposure("start_paying_segment_analysis", destructive=True, open_world=False),
    ),
    PublicWorkflow(
        UUID("b0b2cb40-4c91-4283-adf2-43c11ec20b2f"),
        "outreach.speaking_shortlist",
        PublicMCPExposure("start_speaking_shortlist", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("2dc0683b-74cc-4300-9dd3-dae5118c8a9a"),
        "outreach.syllabus_placement",
        PublicMCPExposure("start_syllabus_placement", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("ce0680ed-511c-4469-9913-726e33038d3d"),
        "outreach.marketplace_listings",
        PublicMCPExposure("start_marketplace_listings", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("bc37b3aa-51d9-4b56-98a2-eaa01e0cd3df"),
        "outreach.campus_events",
        PublicMCPExposure("start_campus_events", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("85299178-953d-4f5f-bae7-4bf3b0822eff"),
        "content.release_announce",
        PublicMCPExposure("start_release_announcements", destructive=True, open_world=False),
    ),
    PublicWorkflow(
        UUID("a233f94b-03d8-42f1-8843-ff12c1d6f006"),
        "outreach.awesome_lists",
        PublicMCPExposure("start_awesome_lists", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("d1fa829d-c40b-4e4b-b039-93ed9c3f1766"),
        "outreach.community_threads",
        PublicMCPExposure("start_community_threads", destructive=True, open_world=True),
    ),
    PublicWorkflow(
        UUID("5411afb9-0a11-4970-b096-724cc7fb1ab7"),
        "outreach.newsletter_placements",
        PublicMCPExposure("start_newsletter_placements", destructive=True, open_world=True),
    ),
    PublicWorkflow(UUID("234d05ce-c9e6-46c5-8299-5ec3973645ef"), "competitor.sunset_rescue"),
    PublicWorkflow(UUID("4cfd20c4-6aaa-46d7-a5c1-1f67032a4358"), "growth.framework_starter"),
    PublicWorkflow(UUID("cf62caaa-fc6f-4a32-b0bd-8babceec534d"), "organic.traffic_snapshot"),
    PublicWorkflow(UUID("51b9959f-3f5c-4df3-b417-f6fbf12d19fc"), "organic.content_efficacy"),
    PublicWorkflow(UUID("d6a097d3-056f-4ffd-af85-3205f32f58f8"), "organic.site_architecture"),
    PublicWorkflow(UUID("896b8e66-ea09-4dda-ac11-7a1824282349"), "content.blog_index"),
    PublicWorkflow(UUID("0dd4b124-cd4d-4558-b3c9-21d00d820dfb"), "growth.acquisition_analytics"),
    PublicWorkflow(UUID("5e1f2809-6673-4ff9-be16-d0420bf05e69"), "organic.prompt_panel"),
)


@dataclass(frozen=True)
class PackagePublication:
    id: UUID
    key: str
    definition_path: str
    definition: dict[str, Any]
    files: dict[str, bytes]

    @property
    def executor(self):
        return self.definition["executor"]

    @property
    def title(self):
        return self.definition["title"]

    @property
    def description(self):
        return self.definition["description"]

    @property
    def version_label(self):
        return self.definition["version"]


async def load_public_workflows(*, root: Path | None = None) -> tuple[PackagePublication, ...]:
    """Validate selected sources before catalog sync writes anything. Never import author code."""
    root = root or REPOSITORY_ROOT
    storage = CheckoutStorage(root)
    publications = []
    for selection in PUBLIC_WORKFLOWS:
        package = ContributedPackage(
            key=selection.key, path=root / "workflow_packages" / selection.key
        )
        await validate(package, root=root)
        raw = storage._read(package.definition_path)
        source = decode_workflow_source(raw, definition_path=package.definition_path)
        # Keep the versioned manifest, not only its normalized runtime definition.
        # Resources stay at package-relative locations inside the immutable registry commit.
        files = {package.definition_path: raw}
        files.update({path: storage._read(path) for path in source.resource_paths.values()})
        publications.append(
            PackagePublication(
                selection.id, selection.key, package.definition_path, source.definition, files
            )
        )
    return tuple(publications)
