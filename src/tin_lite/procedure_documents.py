"""The bounded two-document result contract; no workflow-specific brand policy."""

from dataclasses import dataclass, replace

from tin_lite.brand_contract import VALIDATOR as BRAND_VALIDATOR
from tin_lite.project_files import credential_findings, safe_project_file_path

MAX_DOCUMENT_BYTES = 64_000


@dataclass(frozen=True)
class DocumentPair:
    companion_path: str
    companion_max_bytes: int
    companion_label: str
    destinations: tuple[str, str]

    def resolve(self, run_id):
        return replace(self, companion_path=self.companion_path.replace("{run_id}", str(run_id)))


def document_path(path):
    return (
        isinstance(path, str)
        and safe_project_file_path(path)
        and path.endswith(".md")
        and path != "wiki/INDEX.md"
        and path.split("/")[0] not in {".tin-lite", "registry", "procedures", "workflow_packages"}
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def validate_document_paths(paths):
    if len(set(paths)) != 4 or any(a.startswith(b + "/") for a in paths for b in paths if a != b):
        raise ValueError("reviewed document paths must be distinct and cannot contain each other")


def parse_document_pair(output, definition):
    """One required companion and two fixed destinations, applied by existing review."""
    companion = output.get("companion")
    destinations = output.get("apply_on_approval")
    if companion is None and destinations is None:
        return None
    if (
        output.get("kind") != "project.artifact"
        or output.get("media_type") != "text/markdown"
        or output.get("validator") not in {None, BRAND_VALIDATOR}
        or "section" in output
        or not isinstance(companion, dict)
        or set(companion) != {"path_template", "max_bytes", "label"}
        or not isinstance(destinations, dict)
        or set(destinations) != {"primary", "companion"}
        or (definition.get("human_review") or {}).get("eligible") is not True
    ):
        raise ValueError("reviewed documents require two Markdown outputs and human review")
    paths = [output.get("path_template"), companion["path_template"]]
    if "path" in output or any(
        not isinstance(p, str)
        or p.count("{run_id}") != 1
        or "{" in p.replace("{run_id}", "run")
        or "}" in p.replace("{run_id}", "run")
        or not document_path(p.replace("{run_id}", "run"))
        for p in paths
    ):
        raise ValueError("reviewed documents require safe run-owned Markdown paths")
    target_paths = (destinations["primary"], destinations["companion"])
    if any(not document_path(p) or "{" in p or "}" in p for p in target_paths):
        raise ValueError("reviewed document destinations must be ordinary fixed Markdown paths")
    all_paths = [p.replace("{run_id}", "run") for p in paths] + list(target_paths)
    validate_document_paths(all_paths)
    if any(
        type(n) is not int or not 1 <= n <= MAX_DOCUMENT_BYTES
        for n in (output.get("max_bytes"), companion["max_bytes"])
    ):
        raise ValueError("reviewed documents are bounded to 64000 bytes each")
    label = companion["label"]
    if not isinstance(label, str) or not label.strip() or len(label) > 80:
        raise ValueError("companion label must contain 1–80 characters")
    return DocumentPair(paths[1], companion["max_bytes"], label, target_paths)


def validate_document(content, maximum):
    if not 0 < len(content) <= maximum:
        raise ValueError("document is missing or exceeds its declared limit")
    text = content.decode("utf-8")
    if not text.strip() or "\x00" in text or credential_findings(text):
        raise ValueError("document must contain ordinary UTF-8 text without credentials")
