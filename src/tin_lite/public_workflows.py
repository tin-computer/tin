"""Explicit maintainer selection of public packages, not an auto-discovered plugin registry."""

from dataclasses import dataclass
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
class PublicWorkflow:
    id: UUID
    key: str


# Merging a package does not activate it. Add a reviewed package here to include it
# in the next catalog sync. Keep IDs and keys stable; never reuse a retired identity.
# Copyable example.* packages are deliberately not product Registry entries.
PUBLIC_WORKFLOWS: tuple[PublicWorkflow, ...] = (
    PublicWorkflow(UUID("a94f3a5d-d58c-4e91-b6bb-c1e047dc8372"), "brand.capture"),
    PublicWorkflow(UUID("0ddd88b9-6ded-44c3-9982-b7505c2e31b1"), "product.analytics_brief"),
    PublicWorkflow(UUID("543626b6-635f-4cef-b00e-e46225753c13"), "growth.score_quiz"),
    PublicWorkflow(UUID("ee456cef-e6a5-4da7-9447-a0834b5d107e"), "competitor.watch"),
    PublicWorkflow(UUID("1cd28320-650d-4204-9a13-09b813d77317"), "qa.buyer_trust"),
    PublicWorkflow(UUID("4bf8c067-1709-427d-a00f-b0b53c871751"), "organic.error_surface"),
    PublicWorkflow(UUID("2136b2ff-7570-40bf-97d3-e37889aea964"), "organic.mention_backlinks"),
    PublicWorkflow(UUID("7633e65c-d59d-4e96-a3f1-7e082d1cac5b"), "outreach.paying_segment"),
    PublicWorkflow(UUID("b0b2cb40-4c91-4283-adf2-43c11ec20b2f"), "outreach.speaking_shortlist"),
    PublicWorkflow(UUID("2dc0683b-74cc-4300-9dd3-dae5118c8a9a"), "outreach.syllabus_placement"),
    PublicWorkflow(UUID("ce0680ed-511c-4469-9913-726e33038d3d"), "outreach.marketplace_listings"),
    PublicWorkflow(UUID("bc37b3aa-51d9-4b56-98a2-eaa01e0cd3df"), "outreach.campus_events"),
    PublicWorkflow(UUID("85299178-953d-4f5f-bae7-4bf3b0822eff"), "content.release_announce"),
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
