"""Check contributed workflow packages in this checkout without running them.

A contributed package uses the same layout as a private workflow package, so the same
loader that pins a package at run time also reads it here. This module adds only what a
checkout needs on top of that: it reads bytes from disk, refuses file types a contributed
package has no reason to carry, and names files the manifest never declares.

Nothing here activates a workflow, registers one in the catalog, or reaches a database.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tin_lite.integrations import parse_integration_requirements
from tin_lite.procedures import load_pinned_codex_procedure
from tin_lite.workflow_code import load_code_package
from tin_lite.workflow_diagrams import check_presentation
from tin_lite.workflow_packages import (
    MAX_DEFINITION_BYTES,
    MAX_PACKAGE_FILES,
    decode_workflow_source,
    package_digest,
)
from tin_lite.workflow_prerequisites import parse_workflow_prerequisites

REPOSITORY_ROOT = Path(__file__).parents[2]
PACKAGE_DIRECTORY = "workflow_packages"
MANIFEST_NAME = "workflow.json"
CONTRIBUTED_SUFFIXES = {".json", ".md", ".txt", ".yaml", ".yml"}
CODE_SUFFIXES = CONTRIBUTED_SUFFIXES | {".py", ".csv"}


@dataclass(frozen=True)
class ContributedPackage:
    key: str
    path: Path

    @property
    def definition_path(self) -> str:
        return f"{PACKAGE_DIRECTORY}/{self.key}/{MANIFEST_NAME}"


class CheckoutStorage:
    """Serve package bytes from a checkout in place of code.storage."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def read_workflow_resource(self, *, repo_id: str, commit_sha: str, path: str) -> bytes:
        return self._read(path)

    async def read_canonical_artifact(self, *, repo_id: str, commit_sha: str, path: str) -> bytes:
        return self._read(path)

    def _read(self, path: str) -> bytes:
        target = self._root / path
        if not target.resolve().is_relative_to(self._root):
            raise ValueError(f"package resource leaves the checkout: {path}")
        if any(part.is_symlink() for part in (target, *target.parents) if part != self._root):
            raise ValueError(f"package resource is a symlink: {path}")
        if not target.is_file():
            raise ValueError(f"package resource is missing: {path}")
        # All package resources have smaller runtime limits. Bound reads before allocation;
        # the pinned loader below still checks each resource's precise limit and encoding.
        if target.stat().st_size > MAX_DEFINITION_BYTES:
            raise ValueError(f"package resource exceeds its byte limit: {path}")
        return target.read_bytes()


def discover(root: Path | None = None) -> list[ContributedPackage]:
    """List every candidate, including broken packages, ordered by key."""
    directory = root or REPOSITORY_ROOT / PACKAGE_DIRECTORY
    if directory.is_symlink():
        raise ValueError(f"contributed package directory must not be a symlink: {directory}")
    if not directory.is_dir():
        raise ValueError(
            f"contributed package directory is missing or not a directory: {directory}"
        )
    packages = [
        ContributedPackage(key=child.name, path=child)
        for child in sorted(directory.iterdir())
        if not (child.name == "README.md" and child.is_file() and not child.is_symlink())
    ]
    return packages


def _validate_metadata(definition: dict[str, Any]) -> None:
    """Validate new contributions without changing historical runtime contracts.

    Private-only rules (custom keys, procedure schedules, isolated sandboxes and capability
    restrictions) belong to activation, not this source contribution check.
    """
    if definition.get("executor") not in {"workflow.code", "codex.procedure"}:
        raise ValueError("contributed packages support workflow.code or codex.procedure")
    for key, maximum in (("title", 120), ("description", 2000), ("version", 40)):
        value = definition.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"workflow {key} must contain 1-{maximum} characters")
    if definition.get("kind", "workflow") != "workflow":
        raise ValueError("contributed packages define workflows, not tasks")
    modes = definition.get("schedule_modes")
    if (
        not isinstance(modes, list)
        or not modes
        or any(
            not isinstance(mode, str) or mode not in {"on_demand", "daily", "weekly", "monthly"}
            for mode in modes
        )
        or len(modes) != len(set(modes))
    ):
        raise ValueError(
            "schedule_modes must be a non-empty unique list of on_demand, daily or weekly"
        )
    review = definition.get("human_review")
    if review is not None:
        if not isinstance(review, dict) or set(review) - {
            "eligible",
            "reason",
            "summary",
            "defer_label",
            "queue_clause",
            "review_label",
        }:
            raise ValueError("unsupported review fields")
        if type(review.get("eligible")) is not bool or any(
            not isinstance(value, str) or len(value) > 1000
            for key, value in review.items()
            if key != "eligible"
        ):
            raise ValueError("review eligibility must be boolean and review copy bounded text")
    parse_integration_requirements(definition.get("integration_requirements"))
    parse_workflow_prerequisites(
        definition.get("prerequisites"), input_schema=definition["input_schema"]
    )
    # Every contribution and creator candidate shows how it runs.
    check_presentation(definition, required=True)


def _check_files(package: ContributedPackage, *, root: Path) -> str:
    if package.path.is_symlink():
        raise ValueError("package directory is a symlink; contributed packages hold regular files")
    if not package.path.is_dir():
        raise ValueError("a contributed package must be a directory containing workflow.json")
    present = sorted(
        path for path in package.path.rglob("*") if path.is_file() or path.is_symlink()
    )
    if len(present) > MAX_PACKAGE_FILES:
        raise ValueError(
            f"a package holds at most {MAX_PACKAGE_FILES} files, this one holds {len(present)}"
        )
    for path in present:
        relative = path.relative_to(package.path).as_posix()
        if path.is_symlink():
            raise ValueError(f"{relative} is a symlink; contributed packages hold regular files")
    source = decode_workflow_source(
        CheckoutStorage(root)._read(package.definition_path),
        definition_path=package.definition_path,
    )
    _validate_metadata(source.definition)
    executor = source.definition["executor"]
    suffixes = CODE_SUFFIXES if executor == "workflow.code" else CONTRIBUTED_SUFFIXES
    declared = {root / path for path in [package.definition_path, *source.resource_paths.values()]}
    for path in present:
        relative = path.relative_to(package.path).as_posix()
        if path.suffix.lower() not in suffixes:
            allowed = " ".join(sorted(suffixes))
            raise ValueError(
                f"{relative} is not a file type a contributed package carries (allowed: {allowed})"
            )
        if path not in declared:
            raise ValueError(f"{relative} sits in the package but the manifest never lists it")
    return executor


async def validate(package: ContributedPackage, *, root: Path | None = None) -> None:
    """Raise ValueError when a contributed package would not load."""
    executor = _check_files(package, root=root or REPOSITORY_ROOT)
    loader = load_code_package if executor == "workflow.code" else load_pinned_codex_procedure
    await loader(
        storage=CheckoutStorage(root or REPOSITORY_ROOT),
        repo_id=PACKAGE_DIRECTORY,
        commit_sha="checkout",
        definition_path=package.definition_path,
    )


async def validate_files(files: dict[str, bytes], *, definition_path: str):
    """Validate an in-memory candidate with the same contribution and runtime checks.

    Source remains data. No import, extraction, activation or execution is performed.
    """
    if not 1 <= len(files) <= MAX_PACKAGE_FILES or definition_path not in files:
        raise ValueError("candidate requires a bounded package and its manifest")
    source = decode_workflow_source(files[definition_path], definition_path=definition_path)
    _validate_metadata(source.definition)
    executor = source.definition["executor"]
    suffixes = CODE_SUFFIXES if executor == "workflow.code" else CONTRIBUTED_SUFFIXES
    fingerprint = package_digest(files, definition_path=definition_path)
    for path, raw in files.items():
        if Path(path).suffix.lower() not in suffixes:
            raise ValueError("candidate contains an unsupported resource type")
        if not raw or len(raw) > MAX_DEFINITION_BYTES:
            raise ValueError("candidate resource exceeds its byte limit")

    async def read(*, path, **_):
        return files[path]

    loader = load_code_package if executor == "workflow.code" else load_pinned_codex_procedure
    await loader(
        storage=SimpleNamespace(read_workflow_resource=read),
        repo_id=PACKAGE_DIRECTORY,
        commit_sha="candidate",
        definition_path=definition_path,
    )
    return source.definition, fingerprint


class PrivateCopyError(ValueError):
    """The public package is valid, but its custom.* copy would not activate privately."""


def private_key(key: str) -> str:
    """Name the custom.* copy a contributor activates in their own project."""
    return "custom." + re.sub(r"[^a-z0-9_]", "_", key.split(".", 1)[-1].lower())


async def validate_private_copy(package: ContributedPackage, *, root: Path | None = None) -> None:
    """Raise ValueError when the package's custom.* copy would not activate privately.

    Mirrors private activation without a database: the package is re-rooted in memory under
    its custom.* key, then checked by the private policy and the pinned loader. Prerequisite
    workflow keys are only resolvable against a project, so activation still checks those.
    """
    from tin_lite.private_workflows import validate_private_definition

    root = root or REPOSITORY_ROOT
    storage = CheckoutStorage(root)
    source = decode_workflow_source(
        storage._read(package.definition_path), definition_path=package.definition_path
    )
    key = private_key(package.key)
    prefix = f"{PACKAGE_DIRECTORY}/{package.key}/"
    definition_path = f"{PACKAGE_DIRECTORY}/{key}/{MANIFEST_NAME}"
    manifest = json.loads(storage._read(package.definition_path))
    manifest["definition"]["key"] = key
    files = {definition_path: json.dumps(manifest).encode()}
    for path in source.resource_paths.values():
        files[f"{PACKAGE_DIRECTORY}/{key}/{path.removeprefix(prefix)}"] = storage._read(path)
    private = decode_workflow_source(files[definition_path], definition_path=definition_path)
    validate_private_definition(private.definition)

    async def read(*, path, **_):
        return files[path]

    executor = private.definition["executor"]
    loader = load_code_package if executor == "workflow.code" else load_pinned_codex_procedure
    await loader(
        storage=SimpleNamespace(read_workflow_resource=read),
        repo_id=PACKAGE_DIRECTORY,
        commit_sha="private-copy",
        definition_path=definition_path,
    )


async def validate_all(
    root: Path | None = None, *, private: bool = False, only: str | None = None
) -> list[tuple[ContributedPackage, Exception | None]]:
    """Check every contributed package, or only one key, and report results in key order.

    With private, a package must also activate as a custom.* copy in a Tin project.
    """
    directory = (root / PACKAGE_DIRECTORY) if root else None
    results: list[tuple[ContributedPackage, Exception | None]] = []
    packages = discover(directory)
    if only is not None:
        packages = [package for package in packages if package.key == only]
        if not packages:
            raise ValueError(f"no contributed package named {only}")
    for package in packages:
        try:
            await validate(package, root=root)
            if private:
                try:
                    await validate_private_copy(package, root=root)
                except Exception as error:  # noqa: BLE001 - reported like any failure
                    raise PrivateCopyError(f"as {private_key(package.key)}: {error}") from error
        except Exception as error:  # noqa: BLE001 - the report names every failure
            results.append((package, error))
        else:
            results.append((package, None))
    return results
