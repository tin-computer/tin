"""First capture preparation and preservation, over ordinary project files and receipts."""

import hashlib
import re
from urllib.parse import urlsplit

from tin_lite import brand_contract as contract
from tin_lite.organic_audit import public_site
from tin_lite.procedure_documents import validate_document
from tin_lite.project_files import safe_project_file_path

PACKET_MAX = 32_000
PREPARATION = "brand_capture_preparation"


def public_url(value):
    try:
        parsed = urlsplit(value)
        public_site(f"{parsed.scheme}://{parsed.netloc}/")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Provide a public HTTPS product URL without credentials.") from exc
    if len(value) > 500 or parsed.fragment or any(ord(c) < 33 for c in value):
        raise ValueError("Provide a public HTTPS product URL without a fragment.")
    return value


def preparation(project_id, *, include_guide=False):
    result = {
        "kind": "source_selection",
        "next_tool": {"name": "get_brand_guide", "arguments": {"project_id": str(project_id)}},
        "instruction": (
            "Capture brand and design creates brand/BRAND.md and DESIGN.md in one review. "
            "Use an already supplied product URL or project source packet; a connected repository "
            "is optional. Ask only for missing sources or protected choices. Do not repeat "
            "permissions already given. Local files reach hosted capture only through an "
            "explicitly curated project packet. Existing compatible documents stay unchanged."
        ),
    }
    if include_guide:
        result["guide"] = {
            "workflow": contract.KEY,
            "outputs": [contract.BRAND_PATH, contract.DESIGN_PATH],
            "packet": "Optional Markdown in project Files, at most 32000 bytes: source URLs or "
            "pinned file references, attributed observations, useful excerpts, uncertainties, "
            "and explicit protected colors/fonts. Do not include credentials or transcripts.",
            "scope": "Capture for future marketing; document observed product design. No source "
            "edits, rebrand project, generated asset kit, ad placement or automatic refresh.",
            "intent": "capture preserves observed identity; develop permits modest supporting "
            "marketing guidance within explicit notes. Assessment never overrides preferences.",
            "review": "Read both proposals, then approve their exact pair. Neither is active "
            "before adoption. If both active files exist, use Files for small edits.",
        }
    return result


async def resolve_brand(storage, project, revision):
    """Read only the active guide at the caller's selected immutable project revision."""
    entry = await storage.read_output_destination(
        repo_id=project.state_repo_id, revision=revision, path=contract.BRAND_PATH
    )
    result = {
        "schema": "brand-context.v1",
        "revision": revision,
        "path": contract.BRAND_PATH,
        "status": "missing",
        "guide": None,
        "tokens": None,
        "assessment": None,
        "diagnostics": [],
    }
    if entry is None:
        return result
    raw = entry[1]
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        validate_document(raw, contract.BRAND_MAX)
        result["guide"] = raw.decode()
        result["tokens"] = contract.tokens(result["guide"])
        result["status"] = "available"
    except ValueError as exc:
        result["status"] = "brand_invalid"
        result["diagnostics"].append(str(exc))
        return result
    try:
        result["assessment"] = contract.assessment(result["guide"])
    except ValueError:
        result["diagnostics"].append(
            "Optional brand assessment is unavailable; the guide is valid."
        )
    return result


class BrandCaptureSources:
    def __init__(self, *, database, storage, integrations=None):
        self.db, self.storage, self.integrations = database, storage, integrations

    async def inspect(self, project_id, inputs, *, revision=None, require_source=True):
        project = await self.db.get_project(project_id)
        if project is None:
            raise LookupError("Project not found.")
        if revision is None:
            repo = await self.storage.get_repo(project.state_repo_id)
            revision = await self.storage.head_sha(repo, project.canonical_branch)
        documents = {}
        for path, limit in (
            (contract.BRAND_PATH, contract.BRAND_MAX),
            (contract.DESIGN_PATH, contract.DESIGN_MAX),
        ):
            entry = await self.storage.read_output_destination(
                repo_id=project.state_repo_id, revision=revision, path=path
            )
            if entry is not None:
                try:
                    validate_document(entry[1], limit)
                    if path == contract.BRAND_PATH:
                        contract.tokens(entry[1].decode())
                except ValueError as exc:
                    raise ValueError(f"{path} cannot be carried forward: {exc}") from exc
            documents[path] = {
                "present": entry is not None,
                "sha256": hashlib.sha256(entry[1]).hexdigest() if entry else None,
                "bytes": len(entry[1]) if entry else 0,
            }
        if require_source and all(d["present"] for d in documents.values()):
            raise ValueError(
                "Both brand/BRAND.md and DESIGN.md already exist. Use Files for small "
                "changes; automatic refresh is not part of first capture."
            )
        packet = None
        if path := inputs.get("source_path"):
            if (
                not safe_project_file_path(path)
                or not path.endswith(".md")
                or path.startswith(("brand/proposals/", ".tin-lite/"))
            ):
                raise ValueError("Choose a Markdown source packet in this project's Files.")
            entry = await self.storage.read_output_destination(
                repo_id=project.state_repo_id, revision=revision, path=path
            )
            if entry is None:
                raise ValueError("The selected brand source packet does not exist.")
            validate_document(entry[1], PACKET_MAX)
            packet = {
                "path": path,
                "sha256": hashlib.sha256(entry[1]).hexdigest(),
                "bytes": len(entry[1]),
            }
        url = inputs.get("product_url") or ""
        if not url:
            memory = await self.storage.read_output_destination(
                repo_id=project.state_repo_id, revision=revision, path="wiki/INDEX.md"
            )
            if memory:
                candidates = re.findall(
                    r"(?im)^\s*[-*]?\s*(?:website|product url|site):\s*(https://[^\s<>]+)",
                    memory[1].decode(),
                )
                if len(set(candidates)) == 1:
                    url = candidates[0]
        if url:
            url = public_url(url)
        connection = (
            await self.db.get_integration_connection(
                project_id=project_id, provider_key="infra.github"
            )
            if inputs.get("include_repository", True)
            else None
        )
        repository = connection.configuration.get("selected_repository") if connection else None
        if connection and not repository:
            raise ValueError("Select a repository or turn off repository evidence.")
        if require_source and not (url or packet or repository):
            raise ValueError(
                "Provide a product URL, an attributed source packet in Files, or a "
                "selected repository before capture. Call get_brand_guide for help."
            )
        return {
            "schema": "brand-capture-preparation.v1",
            "project_revision": revision,
            "documents": documents,
            "product_url": url,
            "source_packet": packet,
            "repository": repository,
            "intent": inputs.get("intent", "capture"),
            "notes": inputs.get("notes", ""),
            "contract": contract.VALIDATOR,
        }

    async def saved(self, run_id):
        receipt = await self.db.get_effect(f"{run_id}:{PREPARATION}")
        return receipt.result if receipt and receipt.status == "completed" else None

    async def prepare(self, run, procedure):
        from tin_lite.procedure_repository import select_repository

        key = f"{run.id}:{PREPARATION}"
        async with self.db.effect_lock(key, PREPARATION) as (conn, saved):
            if saved and saved.status == "completed":
                return saved.result
            use_repository = await select_repository(self.db, run, procedure)
            context = await self.inspect(run.project_id, run.input or {})
            if bool(context["repository"]) != use_repository:
                raise ValueError(
                    "Repository selection changed before preparation. Start a new run."
                )
            if use_repository:
                bundle = await self.integrations.github_repository_bundle(
                    project_id=run.project_id,
                    run_id=run.id,
                    execution_key=f"{run.id}:procedure_repository_workspace",
                )
                if bundle.repository != context["repository"]:
                    raise ValueError("Repository selection changed during preparation.")
                context["source_snapshot"] = {
                    "repository": bundle.repository,
                    "head_sha": bundle.head_sha,
                    "file_count": bundle.file_count,
                    "complete": bundle.complete,
                }
            await self.db.start_effect(conn, execution_key=key, operation=PREPARATION)
            await self.db.complete_effect(conn, execution_key=key, result=context)
            return context


async def validate_pair(storage, project, run, brand, design):
    """New files use the contract; any existing destination must be carried byte-for-byte."""
    generated_sources = []
    for path, raw, limit, validate_new in (
        (contract.BRAND_PATH, brand, contract.BRAND_MAX, contract.validate_new_brand),
        (contract.DESIGN_PATH, design, contract.DESIGN_MAX, contract.validate_new_design),
    ):
        validate_document(raw, limit)
        before = await storage.read_output_destination(
            repo_id=project.state_repo_id, revision=run.expected_head_sha, path=path
        )
        if before is not None:
            if before[1] != raw:
                raise ValueError(f"First capture must carry existing {path} forward unchanged")
            if path == contract.BRAND_PATH:
                contract.tokens(raw.decode())
        else:
            validate_new(raw.decode())
            generated_sources.append(contract.sources(raw.decode()))
    # A carried document may use ordinary relative links or another citation convention.
    # Only newly generated documents share our source-ID namespace.
    if len(generated_sources) == 2:
        brand_sources, design_sources = generated_sources
        if any(
            brand_sources[k] != design_sources[k]
            for k in brand_sources.keys() & design_sources.keys()
        ):
            raise ValueError("The same source ID cannot identify different sources across the pair")
