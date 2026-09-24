# ruff: noqa: E501
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tin_lite.activities import TinActivities
from tin_lite.domain import MEMORY_INDEX_PATH, EffectReceipt, ProjectTestIdentity, RunStatus
from tin_lite.memory import (
    MemoryGardener,
    MemorySource,
    extract_owned_section,
    splice_owned_section,
)
from tin_lite.procedures import (
    CODE_MAP_SECTION,
    FEATURE_MAP_SECTION,
    MEMORY_SECTION_PARENT,
    MEMORY_SECTION_VALIDATOR,
    PRODUCT_AUDIT_VALIDATOR,
    OutputSection,
    PinnedCodexProcedure,
    SandboxProfile,
    TestIdentityPolicy,
    product_audit_summary,
    validate_codex_procedure_definition,
    validate_memory_section,
    validate_procedure_artifact,
)
from tin_lite.skills import load_skill_suite

ROOT = Path(__file__).parents[1]
FEATURE_SECTION = OutputSection(MEMORY_SECTION_PARENT, FEATURE_MAP_SECTION, 24_000)
CODE_SECTION = OutputSection(MEMORY_SECTION_PARENT, CODE_MAP_SECTION, 16_000)

FEATURE_MAP = """### Feature map (verified 2026-09-04, via product.deep_dive, depth standard, lenses docs, live, code)

**Product**
- One-liner: Tin runs growth workflows for founders.
- Target user: Solo founder
- Core action: Start a workflow · https://lite.tin.computer/#workflows · observed

**Features**
#### Workflows
- [live] Registry — lists every callable template · https://lite.tin.computer/#workflows · observed · https://lite.tin.computer/#workflows
- [documented-not-verified] Slack digest — posts a weekly brief to Slack · https://tin.computer/docs · inferred · "Get a weekly brief in Slack"

**Onboarding flow**
1. https://lite.tin.computer/sign-in — enter the email code — landed on Chat

**Plans and gating**
- none

**Integrations**
- Google Workspace — mailbox for outreach · [live] · https://lite.tin.computer/#integrations

**Reconciliation**
- Slack digest — docs: "Get a weekly brief in Slack" · code: none · live: not seen · verdict: [documented-not-verified]

**Gaps and rough edges**
- none

**Test footprint**
- Workflow configuration — tin-qa weekly brief 14:32Z · https://lite.tin.computer/#workflows · created 14:32Z · not deleted

**Not proven**
- none

**Verification record**
- Account: founder+tin-1a2b3c4d@example.org (existing), status active
- Executed live: Start a workflow and 12 screens
- Code map: none
- Budget: depth standard, 12/30 screens, 6/12 docs pages, frontier left 0
- Walls: none
"""

CODE_MAP = """### Code map (verified 2026-09-04, via product.code_map, commit abcdef123456, depth standard)

**Stack**
- Web — FastAPI 0.116 · pyproject.toml:12

**Surfaces**
#### Product shell
- [exposed] Chat — sends founder messages to Luna · /#chat · observed · src/tin_lite/static/app.js:1001
- [internal-only] Broker — serves pooled Codex auth to sandboxes · /internal/broker/auth · observed · src/tin_lite/api.py:2414

**Plans and gating**
- none

**Integrations**
- Clerk — identity · src/tin_lite/auth.py:1 · src/tin_lite/auth.py:1

**Flags and env gates**
- none

**In code but likely not surfaced**
- none

**Not read**
- web/ — budget

**Verification record**
- Commit: abcdef1234567890abcdef1234567890abcdef12
- Files opened: 40
- Budget: depth standard, 2/80 surfaces, 40/150 files
- Focus: none
"""

BASE_INDEX = (
    "# Test memory\n\nIntro line.\n\n## Architecture\n\n- Durable architecture facts.\n\n"
    f"{MEMORY_SECTION_PARENT}\n\n{CODE_MAP}\n## Sources\n\n- code.storage://projects/test@c/DESIGN.md\n"
)


def _index(
    *sections: str,
    intro: str = "# Test memory\n\nIntro line.\n\n## Architecture\n\n- Durable architecture facts.\n\n",
) -> bytes:
    body = "\n".join(sections)
    return (
        f"{intro}{MEMORY_SECTION_PARENT}\n\n{body}\n## Sources\n\n"
        "- code.storage://projects/test@c/DESIGN.md\n"
    ).encode()


def _spec(section: OutputSection, **overrides) -> PinnedCodexProcedure:
    values = {
        "workflow_key": "product.deep_dive",
        "prompt": "Map the product.",
        "entry_skill": "product-deep-dive",
        "skill_files": {"product-deep-dive/SKILL.md": b"---\nname: product-deep-dive\n---\n"},
        "output_path": MEMORY_INDEX_PATH,
        "output_media_type": "text/markdown",
        "output_validator": MEMORY_SECTION_VALIDATOR,
        "output_max_bytes": 100_000,
        "output_section": section,
    }
    values.update(overrides)
    return PinnedCodexProcedure(**values)


def test_memory_section_validator_accepts_only_a_section_replacement() -> None:
    base = BASE_INDEX.encode()
    # Adding the feature map next to the existing code map leaves everything else intact.
    validate_procedure_artifact(
        _index(CODE_MAP, FEATURE_MAP), spec=_spec(FEATURE_SECTION), base=base
    )
    # Replacing the code map in full is the code lens's own write.
    rewritten = CODE_MAP.replace("Files opened: 40", "Files opened: 41")
    validate_memory_section(_index(rewritten), section=CODE_SECTION, base=base)
    # A base without `## Product` accepts the parent being introduced before Sources.
    no_product = b"# Test memory\n\n## Architecture\n\n- Durable architecture facts.\n\n## Sources\n\n- ref\n"
    validate_memory_section(
        b"# Test memory\n\n## Architecture\n\n- Durable architecture facts.\n\n## Product\n\n"
        + FEATURE_MAP.encode()
        + b"\n## Sources\n\n- ref\n",
        section=FEATURE_SECTION,
        base=no_product,
    )
    # A missing index accepts only the skeleton plus the section.
    skeleton = (
        b"# Test memory\n\n## Product\n\n"
        + FEATURE_MAP.encode()
        + b"\n## Sources\n\n- No durable sources yet.\n"
    )
    validate_memory_section(skeleton, section=FEATURE_SECTION, base=None)

    with pytest.raises(ValueError, match="only the skeleton"):
        validate_memory_section(
            b"# Test memory\n\n## Extra\n\n- x\n\n## Product\n\n"
            + FEATURE_MAP.encode()
            + b"\n## Sources\n\n- No durable sources yet.\n",
            section=FEATURE_SECTION,
            base=None,
        )
    with pytest.raises(ValueError, match="changed outside"):
        validate_memory_section(
            _index(
                CODE_MAP, FEATURE_MAP, intro="# Test memory\n\n## Architecture\n\n- Edited.\n\n"
            ),
            section=FEATURE_SECTION,
            base=base,
        )
    with pytest.raises(ValueError, match="changed outside"):
        # Touching the other owned section is an outside edit for this procedure.
        validate_memory_section(
            _index(CODE_MAP.replace("Files opened: 40", "Files opened: 99"), FEATURE_MAP),
            section=FEATURE_SECTION,
            base=base,
        )
    with pytest.raises(ValueError, match="more than once"):
        validate_memory_section(
            _index(CODE_MAP, FEATURE_MAP, FEATURE_MAP), section=FEATURE_SECTION, base=base
        )
    with pytest.raises(ValueError, match="has no ### Feature map"):
        validate_memory_section(_index(CODE_MAP), section=FEATURE_SECTION, base=base)
    with pytest.raises(ValueError, match="must sit under"):
        validate_memory_section(
            b"# Test memory\n\n## Architecture\n\n"
            + FEATURE_MAP.encode()
            + b"\n## Product\n\n## Sources\n\n- ref\n",
            section=FEATURE_SECTION,
            base=None,
        )
    with pytest.raises(ValueError, match="exceeds 24000 bytes"):
        validate_memory_section(
            _index(CODE_MAP, FEATURE_MAP.replace("- none\n", "- " + "x" * 30_000 + "\n", 1)),
            section=FEATURE_SECTION,
            base=base,
        )


def test_memory_section_validator_enforces_the_section_grammar() -> None:
    base = BASE_INDEX.encode()

    def check(section_text: str, reason: str) -> None:
        with pytest.raises(ValueError, match=reason):
            validate_memory_section(
                _index(CODE_MAP, section_text), section=FEATURE_SECTION, base=base
            )

    check(FEATURE_MAP.replace("[live]", "[exposed]"), "does not follow the grammar")
    check(
        FEATURE_MAP.replace(" · observed · https", " · seen · https"), "does not follow the grammar"
    )
    check(
        FEATURE_MAP.replace("**Reconciliation**\n", ""),
        "must contain the \\*\\*Reconciliation\\*\\* block",
    )
    swapped = (
        FEATURE_MAP.replace("**Plans and gating**", "**TMP**")
        .replace("**Integrations**", "**Plans and gating**")
        .replace("**TMP**", "**Integrations**")
    )
    check(swapped, "blocks are out of order")
    check(
        FEATURE_MAP.replace("created 14:32Z · not deleted", "created 14:32Z"), "test footprint line"
    )
    check(FEATURE_MAP.replace("- Account: ", "- User: "), "verification record is incomplete")
    check(re.sub(r"^- \[.*$", "", FEATURE_MAP, flags=re.MULTILINE), "lists no feature lines")
    # The code map uses its own status vocabulary and record line.
    validate_memory_section(_index(CODE_MAP), section=CODE_SECTION, base=base)
    with pytest.raises(ValueError, match="does not follow the grammar"):
        validate_memory_section(
            _index(CODE_MAP.replace("[exposed]", "[live]")), section=CODE_SECTION, base=base
        )
    with pytest.raises(ValueError, match="verification record is incomplete"):
        validate_memory_section(
            _index(CODE_MAP.replace("- Commit: ", "- Sha: ")), section=CODE_SECTION, base=base
        )


AUDIT = """---
kind: product_audit
verified_at: 2026-09-04T16:40:00Z
product_url: https://lite.tin.computer/
host: lite.tin.computer
identity_email: founder+tin-1a2b3c4d@example.org
scope: all
depth: standard
features_checked: 3
findings: 2
blockers: 1
blocked: false
---
# Product audit: lite.tin.computer

**Headline.** Usable end to end; the registry search loses its query on refresh.

## Identity
- Email used: founder+tin-1a2b3c4d@example.org
- Account: existing
- Status recorded: active

## Coverage
| # | Feature | Area | Map status | Result | Findings |
|---|---------|------|------------|--------|----------|
| 1 | Registry | Workflows | live | broken | F1 |
| 2 | Chat | Chat | live | works | - |
| 3 | Dead links | cross-cutting | - | degraded | F2 |
| 4 | Slack digest | Workflows | documented-not-verified | not-reached | - |

## Findings
### F1 · [blocker] [bug] Starting a run from the registry fails
- Feature: Registry
- Where: https://lite.tin.computer/#workflows
- Repro: 1. Open https://lite.tin.computer/#workflows 2. Click "Run" on Weekly brief 3. Confirm
- Observed: The card shows "Run failed"; "Run failed"
- Expected: The run starts and appears under Your workflows
- Evidence: `POST /api/workflows/00000000-0000-4000-8000-000000000007/runs 500`
- Suggestion: Return the validation error inline instead of failing the request

### F2 · [minor] [copy-ux] Footer link returns 404
- Feature: cross-cutting: Dead links
- Where: https://lite.tin.computer/help
- Repro: 1. Open https://lite.tin.computer/ 2. Click "Help"
- Observed: A blank 404 page; "Not Found"
- Expected: The help page or no link
- Evidence: `HEAD https://lite.tin.computer/help 404`
- Suggestion: Remove the link until the page exists

## Improvements
- Show the last run time on each registry card — saves a click into Activity · https://lite.tin.computer/#workflows

## Not proven
- Slack digest — no surface found from the map or navigation
- Mobile layout — the browser cannot resize the window

## Test footprint
- Workflow configuration — tin-qa weekly brief 16:20Z · https://lite.tin.computer/#workflows · created 16:20Z · not deleted

## Verification record
- Verified at: 2026-09-04T16:40:00Z
- Account: founder+tin-1a2b3c4d@example.org (existing), status active
- Feature map: verified 2026-09-04, via product.deep_dive, depth standard, lenses docs, live, code
- Executed live: 3 features across 9 screens
- Budget: depth standard, 3/30 features, 9/40 screens
- Walls: none
"""


def _audit_spec() -> PinnedCodexProcedure:
    return PinnedCodexProcedure(
        workflow_key="qa.product_audit",
        prompt="Audit the product.",
        entry_skill="product-audit",
        skill_files={"product-audit/SKILL.md": b"---\nname: product-audit\n---\n"},
        output_path="reports/qa/audit/lite.tin.computer/2026-09-04T16-00-00Z.md",
        output_media_type="text/markdown",
        output_validator=PRODUCT_AUDIT_VALIDATOR,
        output_max_bytes=300_000,
        sandbox=SandboxProfile("browser", 3600, "open"),
        identity=TestIdentityPolicy(create=True, reuse="active"),
    )


def test_product_audit_validator_checks_counts_sections_and_findings() -> None:
    spec = _audit_spec()
    validate_procedure_artifact(AUDIT.encode(), spec=spec)
    assert product_audit_summary(AUDIT) == {
        "features_checked": 3,
        "findings": 2,
        "blockers": 1,
        "blocked": False,
    }
    empty = re.sub(
        r"## Findings.*?## Improvements",
        "## Findings\n- none\n\n## Improvements",
        AUDIT,
        flags=re.S,
    )
    empty = empty.replace("findings: 2", "findings: 0").replace("blockers: 1", "blockers: 0")
    empty = empty.replace("| broken | F1 |", "| broken | - |").replace(
        "| degraded | F2 |", "| degraded | - |"
    )
    validate_procedure_artifact(empty.encode(), spec=spec)

    for bad, reason in (
        (AUDIT.replace("kind: product_audit", "kind: audit"), "kind must be product_audit"),
        (AUDIT.replace("host: lite.tin.computer", "host: tin.computer"), "host must match"),
        (AUDIT.replace("depth: standard", "depth: deep"), "depth is unsupported"),
        (AUDIT.replace("blocked: false", "blocked: no"), "blocked must be true or false"),
        (AUDIT.replace("## Improvements\n", "## Ideas\n"), "sections must be exactly"),
        (
            AUDIT.replace("features_checked: 3", "features_checked: 4"),
            "features_checked must equal",
        ),
        (AUDIT.replace("| works | - |", "| passes | - |"), "coverage result is unsupported"),
        (AUDIT.replace("| broken | F1 |", "| broken | F9 |"), "references an unknown finding"),
        (AUDIT.replace("### F2 ·", "### F3 ·"), "numbered consecutively"),
        (AUDIT.replace("findings: 2", "findings: 3"), "findings count must equal"),
        (AUDIT.replace("blockers: 1", "blockers: 2"), "blockers must equal"),
        (
            AUDIT.replace("[blocker] [bug] Starting", "[polish] [bug] Starting"),
            "sorted from blocker to polish",
        ),
        (
            AUDIT.replace("- Expected: The run starts", "- Expects: The run starts"),
            "missing a required bullet",
        ),
        (
            AUDIT.replace("- Suggestion: Remove the link until the page exists", "- Suggestion:"),
            "has an empty bullet",
        ),
        (
            AUDIT.replace("· created 16:20Z · not deleted", "· created 16:20Z · deleted"),
            "test footprint line",
        ),
        (
            AUDIT.replace("- Budget: depth standard", "- Spent: depth standard"),
            "verification record is incomplete",
        ),
        (
            AUDIT.replace(
                "| # | Feature | Area | Map status | Result | Findings |", "| # | Feature |"
            ),
            "coverage table header",
        ),
    ):
        with pytest.raises(ValueError, match=reason):
            validate_procedure_artifact(bad.encode(), spec=spec)


def _definition(**procedure_overrides) -> dict:
    procedure = {
        "prompt_path": "procedures/product.deep_dive/PROMPT.md",
        "skills_path": "procedures/product.deep_dive/skills",
        "skill_files": ["procedures/product.deep_dive/skills/product-deep-dive/SKILL.md"],
        "entry_skill": "product-deep-dive",
        "workspace": {"kind": "project.state"},
        "output": {
            "kind": "project.artifact",
            "path": MEMORY_INDEX_PATH,
            "media_type": "text/markdown",
            "validator": MEMORY_SECTION_VALIDATOR,
            "max_bytes": 100_000,
            "section": {"parent": "## Product", "heading": "### Feature map", "max_bytes": 24_000},
        },
        "verification": {"commands": []},
        "project_skills": [],
        "sandbox": {"profile": "browser", "timeout_seconds": 3600, "egress": "open"},
        "identity": {"create": True, "reuse": "active"},
    }
    procedure.update(procedure_overrides)
    return {"key": "product.deep_dive", "executor": "codex.procedure", "procedure": procedure}


def _output(**overrides) -> dict:
    output = {
        "kind": "project.artifact",
        "path": MEMORY_INDEX_PATH,
        "media_type": "text/markdown",
        "validator": MEMORY_SECTION_VALIDATOR,
        "max_bytes": 100_000,
        "section": {"parent": "## Product", "heading": "### Feature map", "max_bytes": 24_000},
    }
    output.update(overrides)
    return output


def test_section_owned_outputs_and_dynamic_paths_are_gated_by_validator() -> None:
    spec = validate_codex_procedure_definition(_definition())
    assert spec.output_section == FEATURE_SECTION
    assert spec.identity == TestIdentityPolicy(create=True, reuse="active")

    with pytest.raises(ValueError, match="exactly one owned section"):
        validate_codex_procedure_definition(_definition(output=_output(section=None)))
    with pytest.raises(ValueError, match="exactly one owned section"):
        validate_codex_procedure_definition(
            _definition(output=_output(validator=None, path="reports/X.md"))
        )
    with pytest.raises(ValueError, match="only the bounded project memory index"):
        validate_codex_procedure_definition(
            _definition(output=_output(path="reports/product/FEATURE_MAP.md"))
        )
    with pytest.raises(ValueError, match="output section is unsupported"):
        validate_codex_procedure_definition(
            _definition(
                output=_output(
                    section={"parent": "## Product", "heading": "### Roadmap", "max_bytes": 100}
                )
            )
        )
    with pytest.raises(ValueError, match="max_bytes must be 1-40000"):
        validate_codex_procedure_definition(
            _definition(
                output=_output(
                    section={"parent": "## Product", "heading": "### Feature map", "max_bytes": 0}
                )
            )
        )
    with pytest.raises(ValueError, match="reserved for validated procedures"):
        validate_codex_procedure_definition(
            _definition(output=_output(path=None, path_template="wiki/{host}/{started_at}.md"))
        )

    audit = validate_codex_procedure_definition(
        _definition(
            output={
                "kind": "project.artifact",
                "path_template": "reports/qa/audit/{host}/{started_at}.md",
                "media_type": "text/markdown",
                "validator": PRODUCT_AUDIT_VALIDATOR,
                "max_bytes": 300_000,
            }
        )
    )
    assert audit.output_path_template == "reports/qa/audit/{host}/{started_at}.md"
    with pytest.raises(ValueError, match="paths use {host} and {started_at}"):
        validate_codex_procedure_definition(
            _definition(
                output={
                    "kind": "project.artifact",
                    "path_template": "reports/qa/audit/{slug}.md",
                    "media_type": "text/markdown",
                    "validator": PRODUCT_AUDIT_VALIDATOR,
                    "max_bytes": 300_000,
                }
            )
        )

    repository = validate_codex_procedure_definition(
        _definition(
            workspace={
                "kind": "github.repository",
                "provider_key": "infra.github",
                "capabilities": ["contents.read"],
            },
            output=_output(
                section={"parent": "## Product", "heading": "### Code map", "max_bytes": 16_000}
            ),
            sandbox={"profile": "default", "timeout_seconds": 1800, "egress": "fenced"},
            identity={"create": False, "reuse": "none"},
        )
    )
    assert repository.repository_workspace and repository.workspace_capabilities == (
        "contents.read",
    )
    with pytest.raises(ValueError, match="repository workspace are read-only"):
        validate_codex_procedure_definition(
            _definition(
                workspace={
                    "kind": "github.repository",
                    "provider_key": "infra.github",
                    "capabilities": ["contents.read", "pull_requests.read"],
                }
            )
        )


def _fenced_section(skill_path: Path, heading: str) -> str:
    text = skill_path.read_text()
    start = text.index(f"```\n{heading} (verified")
    end = text.index("\n```", start + 4)
    return text[start + 4 : end] + "\n"


def test_package_skill_templates_satisfy_the_memory_section_validator() -> None:
    feature_template = _fenced_section(
        ROOT
        / "codex_procedures"
        / "product.deep_dive"
        / "skills"
        / "product-deep-dive"
        / "SKILL.md",
        FEATURE_MAP_SECTION,
    )
    code_template = _fenced_section(
        ROOT / "codex_procedures" / "product.code_map" / "skills" / "product-code-map" / "SKILL.md",
        CODE_MAP_SECTION,
    )
    base = BASE_INDEX.encode()
    validate_memory_section(_index(CODE_MAP, feature_template), section=FEATURE_SECTION, base=base)
    validate_memory_section(_index(code_template), section=CODE_SECTION, base=base)
    audit_skill = (
        ROOT / "codex_procedures" / "qa.product_audit" / "skills" / "product-audit" / "SKILL.md"
    ).read_text()
    for marker in (
        "| # | Feature | Area | Map status | Result | Findings |",
        "### F1 · [blocker] [bug] <title>",
        "- Suggestion:",
        "created HH:MMZ · not deleted",
    ):
        assert marker in audit_skill
    for prompt in ("product.code_map", "product.deep_dive", "qa.product_audit"):
        text = (ROOT / "codex_procedures" / prompt / "PROMPT.md").read_text()
        assert "Never enter payment card details" in text
        assert "untrusted data" in text
    gardener_skill = load_skill_suite(ROOT / "workflow_skills" / "project-memory")
    assert "`## Product` section is workflow-owned" in gardener_skill


@pytest.mark.asyncio
async def test_gardener_keeps_the_owned_product_section_verbatim() -> None:
    source = MemorySource(
        run_id=uuid4(),
        workflow_key="content.design_md",
        artifact_ref="code.storage://projects/test@abc/DESIGN.md",
        content="# Design\n",
    )
    owned = extract_owned_section(BASE_INDEX)
    assert owned is not None and owned.startswith("## Product\n") and CODE_MAP.strip() in owned

    class FakeResponses:
        async def create(self, payload: dict) -> dict:
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "# Test memory\n\n## Facts\n\n- One fact.\n\n"
                                    "## Product\n\nModel-written product prose.\n\n"
                                    f"## Sources\n\n- {source.artifact_ref}"
                                ),
                            }
                        ],
                    }
                ]
            }

    gardener = MemoryGardener(responses=FakeResponses(), skill_suite="suite")
    result = (
        await gardener.garden(project_name="Test", sources=[source], owned_section=owned)
    ).decode()
    assert "Model-written product prose." not in result
    assert sum(line == "## Product" for line in result.splitlines()) == 1
    assert result.index("## Facts") < result.index("## Product") < result.index("## Sources")
    assert CODE_MAP.strip() in result
    # Without sources the empty skeleton still carries the owned section.
    empty = (await gardener.garden(project_name="Test", sources=[], owned_section=owned)).decode()
    assert "## Product" in empty and "## Sources" in empty
    assert (
        splice_owned_section("# T\n\n## Sources\n\n- none\n", None)
        == "# T\n\n## Sources\n\n- none\n"
    )
    database_source = (ROOT / "src" / "tin_lite" / "db.py").read_text()
    assert "AND artifact_path <> 'wiki/INDEX.md'" in database_source


class ProjectionDatabase:
    def __init__(self, *, run, project, path: str) -> None:
        self.run = run
        self.project = project
        self.path = path
        self.receipts: dict[str, EffectReceipt] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.events: list[str] = []
        self.memory_refreshes: list[dict] = []
        self.successes = 0

    @asynccontextmanager
    async def effect_lock(
        self, execution_key: str, operation: str
    ) -> AsyncIterator[tuple[None, EffectReceipt | None]]:
        async with self._locks.setdefault(execution_key, asyncio.Lock()):
            yield None, self.receipts.get(execution_key)

    async def start_effect(self, conn, *, execution_key: str, operation: str) -> None:
        self.receipts.setdefault(
            execution_key, EffectReceipt(execution_key, operation, "started", None)
        )

    async def complete_effect(self, conn, *, execution_key: str, result: dict) -> None:
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "completed", result)

    async def fail_effect(self, conn, *, execution_key: str, error_message: str) -> None:
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "failed", None)

    async def get_effect(self, execution_key: str):
        if execution_key.endswith(":procedure_canonical_commit"):
            return EffectReceipt(
                execution_key,
                "procedure_canonical_commit",
                "completed",
                {"canonical_commit_sha": "c" * 40, "artifact_path": self.path, "summary": "Done."},
            )
        return self.receipts.get(execution_key)

    async def get_run(self, run_id: UUID):
        return self.run if run_id == self.run.id else None

    async def get_workflow(self, workflow_id):
        return SimpleNamespace(
            project_id=None, current_commit_sha=self.run.definition_commit_sha, definition={}
        )

    async def get_project(self, project_id: UUID):
        return self.project if project_id == self.project.id else None

    async def complete_procedure_projection(self, conn, *, execution_key: str, **values) -> None:
        self.successes += 1
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "completed", values)

    async def refresh_project_memory_projection(self, **values) -> None:
        self.memory_refreshes.append(values)

    async def finalize_pending_test_identity(self, **values) -> bool:
        return False

    async def add_activity(self, *, event_type: str, **values) -> None:
        self.events.append(event_type)


class ProjectionStorage:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.reads: list[str] = []

    async def read_canonical_artifact(self, *, repo_id: str, commit_sha: str, path: str) -> bytes:
        self.reads.append(path)
        return self.content


@pytest.mark.asyncio
async def test_procedure_projection_refreshes_memory_only_for_index_writes() -> None:
    project = SimpleNamespace(id=uuid4(), state_repo_id="projects/test", canonical_branch="main")
    run = SimpleNamespace(
        id=uuid4(),
        project_id=project.id,
        status=RunStatus.RUNNING,
        executor="codex.procedure",
        workflow_id=uuid4(),
        definition_commit_sha="d" * 40,
    )
    index = _index(CODE_MAP, FEATURE_MAP)
    database = ProjectionDatabase(run=run, project=project, path=MEMORY_INDEX_PATH)
    storage = ProjectionStorage(index)
    activities = TinActivities(
        database=database,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
    )
    await asyncio.gather(
        activities.project_codex_procedure_result(str(run.id)),
        activities.project_codex_procedure_result(str(run.id)),
    )
    assert database.successes == 1
    assert database.memory_refreshes == [
        {
            "project_id": project.id,
            "canonical_commit_sha": "c" * 40,
            "artifact_path": MEMORY_INDEX_PATH,
            "memory_index": index.decode(),
        }
    ]
    assert database.events == ["project_memory_updated"]

    other = ProjectionDatabase(run=run, project=project, path="reports/qa/audit/x/y.md")
    activities = TinActivities(
        database=other,  # type: ignore[arg-type]
        storage=ProjectionStorage(b"# report\n"),  # type: ignore[arg-type]
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
    )
    await activities.project_codex_procedure_result(str(run.id))
    assert other.memory_refreshes == [] and other.events == [] and other.successes == 1


def _identity(**overrides) -> ProjectTestIdentity:
    values = dict(
        id=uuid4(),
        project_id=uuid4(),
        created_by_run_id=uuid4(),
        target_host="app.example.com",
        label="walk",
        email="founder+tin-abc@example.com",
        auth_kind="password",
        username=None,
        password_ciphertext=b"sealed",
        credential_key_version="v1",
        status="active",
        status_note=None,
        verified_at=None,
        last_used_run_id=None,
        last_used_at=None,
        notes=None,
        created_at=None,
        updated_at=None,
    )
    values.update(overrides)
    return ProjectTestIdentity(**values)


class ReuseDatabase:
    def __init__(self, *, reusable: ProjectTestIdentity | None) -> None:
        self.reusable = reusable
        self.bound: dict[UUID, tuple[ProjectTestIdentity, str]] = {}
        self.events: list[tuple[str, dict]] = []
        self.minted = 0

    async def get_test_identity_for_run(self, *, run_id):
        entry = self.bound.get(run_id)
        return entry[0] if entry else None

    async def get_test_identity_use_mode(self, *, run_id):
        entry = self.bound.get(run_id)
        return entry[1] if entry else None

    async def find_reusable_test_identity(self, *, project_id, target_host):
        if self.reusable is not None and self.reusable.project_id == project_id:
            assert target_host == "app.example.com"
            return self.reusable
        return None

    async def bind_test_identity_to_run(self, *, run_id, identity_id, project_id):
        assert self.reusable is not None and identity_id == self.reusable.id
        bound = _identity(
            id=self.reusable.id,
            project_id=project_id,
            created_by_run_id=self.reusable.created_by_run_id,
            last_used_run_id=run_id,
        )
        self.bound[run_id] = (bound, "reused")
        return bound

    async def add_activity(self, *, run_id, event_type, details, dedupe_key):
        self.events.append((event_type, details))

    async def get_integration_connection(self, *, project_id, provider_key):
        return SimpleNamespace(
            id=uuid4(),
            status="connected",
            external_account_id="sub-1",
            configuration={
                "email": "founder@example.com",
                "granted_capabilities": ["gmail.messages.read"],
            },
        )

    async def create_test_identity(self, **values):
        self.minted += 1
        row = _identity(
            id=values["identity_id"],
            project_id=values["project_id"],
            created_by_run_id=values["run_id"],
            email=values["email"],
            status="pending",
        )
        self.bound[values["run_id"]] = (row, "created")
        return row


@pytest.mark.asyncio
async def test_identity_reuse_binds_the_active_identity_before_minting() -> None:
    project_id = uuid4()
    reusable = _identity(project_id=project_id)
    database = ReuseDatabase(reusable=reusable)
    integrations = SimpleNamespace(
        seal_test_identity_password=lambda **_values: (b"sealed", "v1"),
    )
    activities = TinActivities(
        database=database,  # type: ignore[arg-type]
        storage=object(),
        sandboxes=object(),
        settings=SimpleNamespace(),
        integrations=integrations,  # type: ignore[arg-type]
    )
    run = SimpleNamespace(
        id=uuid4(), project_id=project_id, input={"product_url": "https://App.Example.com/x"}
    )
    procedure = _spec(FEATURE_SECTION, identity=TestIdentityPolicy(create=True, reuse="active"))

    identity, mode = await activities._resolve_test_identity(run=run, procedure=procedure)
    again, again_mode = await activities._resolve_test_identity(run=run, procedure=procedure)
    assert (identity.id, mode) == (reusable.id, "reused")
    assert (again.id, again_mode) == (reusable.id, "reused")
    assert database.minted == 0
    assert database.events == [
        (
            "test_identity_reused",
            {
                "identity_id": str(reusable.id),
                "created_by_run_id": str(reusable.created_by_run_id),
                "target_host": "app.example.com",
            },
        )
    ]

    # No active identity on the host: a creating policy mints, a reuse-only policy fails.
    fresh = ReuseDatabase(reusable=None)
    activities = TinActivities(
        database=fresh,  # type: ignore[arg-type]
        storage=object(),
        sandboxes=object(),
        settings=SimpleNamespace(),
        integrations=integrations,  # type: ignore[arg-type]
    )
    minted, minted_mode = await activities._resolve_test_identity(run=run, procedure=procedure)
    assert minted_mode == "created" and fresh.minted == 1 and minted.status == "pending"
    with pytest.raises(RuntimeError, match="no active test identity"):
        await activities._resolve_test_identity(
            run=SimpleNamespace(id=uuid4(), project_id=project_id, input=run.input),
            procedure=_spec(
                FEATURE_SECTION, identity=TestIdentityPolicy(create=False, reuse="active")
            ),
        )
    with pytest.raises(RuntimeError, match="require a product URL"):
        await activities._resolve_test_identity(
            run=SimpleNamespace(id=uuid4(), project_id=project_id, input={}),
            procedure=procedure,
        )


def test_reused_identity_status_rules_are_persisted_in_sql() -> None:
    migration = (ROOT / "migrations" / "019_test_identity_uses.sql").read_text()
    assert "CREATE TABLE project_test_identity_uses" in migration
    assert "mode text NOT NULL CHECK (mode IN ('created', 'reused'))" in migration
    assert "SELECT created_by_run_id, id, 'created', created_at" in migration
    database_source = (ROOT / "src" / "tin_lite" / "db.py").read_text()
    reused = database_source[database_source.index('if mode == "reused":') :]
    assert "a reused test identity is never failed by a later run" in reused
    assert "identity.status IN ('active', 'blocked')" in reused
    assert (
        "status = 'active'"
        in database_source[database_source.index("find_reusable_test_identity") :]
    )
    assert "ON CONFLICT (run_id) DO NOTHING" in database_source
    assert "last_used_run_id = $1::uuid, last_used_at = now()" in database_source
