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
    PublicWorkflow(UUID("0ddd88b9-6ded-44c3-9982-b7505c2e31b1"), "product.analytics_brief"),
    PublicWorkflow(UUID("fed8f9ce-1a81-4da8-a544-100a5167898f"), "growth.dev_challenge"),
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
