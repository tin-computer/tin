from __future__ import annotations

import base64
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS, BuiltinWorkflow, HumanReviewPolicy
from tin_lite.domain import (
    CODEX_PROCEDURE_EXECUTOR,
    CONTENT_DIAGRAM_WORKFLOW_NAME,
    CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME,
    EMAIL_SHORTLIST_WORKFLOW_NAME,
    PRODUCT_CODE_MAP_WORKFLOW_NAME,
    PRODUCT_DEEP_DIVE_WORKFLOW_NAME,
    PUBLIC_ARTICLE_WORKFLOW_NAME,
    QA_PRODUCT_AUDIT_WORKFLOW_NAME,
    QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
    RESEARCH_DEEP_DIVE_WORKFLOW_NAME,
    SITE_HEALTH_WORKFLOW_NAME,
    EffectReceipt,
)
from tin_lite.e2b_runtime import E2BRuntime, SandboxProcedureInput
from tin_lite.integrations import GitHubOpenPullRequestEvidence, GitHubRepositoryBundle
from tin_lite.procedures import (
    GITHUB_PULL_REQUEST_RESULT,
    TIN_DIAGRAM_REVIEWED_VALIDATOR,
    CodexProcedureSource,
    GitHubPullRequestProcedure,
    PinnedCodexProcedure,
    ProjectSkillDependency,
    build_procedure_pull_request_receipt,
    load_pinned_codex_procedure,
    validate_codex_procedure_definition,
    validate_procedure_artifact,
    validate_procedure_pull_request,
)
from tin_lite.workflow_inputs import normalize_workflow_inputs

ROOT = Path(__file__).parents[1]


def test_procedure_sandbox_template_pins_repository_tooling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "tin_lite_sandbox_template", ROOT / "sandbox" / "template.py"
    )
    assert spec is not None and spec.loader is not None
    sandbox_template = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox_template)
    calls: list[tuple[str, object]] = []

    class Builder:
        def __init__(self, *, file_context_path):
            assert file_context_path == ROOT

        def from_template(self, name):
            calls.append(("from_template", name))
            return self

        def pip_install(self, packages, **values):
            calls.append(("pip_install", (packages, values)))
            return self

        def copy(self, source, destination, **values):
            calls.append(("copy", (source, destination, values)))
            return self

        def run_cmd(self, command, **values):
            calls.append(("run_cmd", (command, values)))
            return self

    monkeypatch.setattr(sandbox_template, "Template", Builder)

    assert sandbox_template.task_template().__class__ is Builder
    assert calls[:3] == [
        ("from_template", "codex"),
        ("pip_install", ("uv==0.5.21", {})),
        (
            "run_cmd",
            (
                "npm install -g @openai/codex@0.153.4 && codex --version",
                {"user": "root"},
            ),
        ),
    ]
    assert (
        "copy",
        (
            "sandbox/codex_config.toml",
            "/home/user/.codex/config.toml",
            {"user": "user", "mode": 0o644},
        ),
    ) in calls
    assert calls[3][0] == "run_cmd"
    assert "hatchling==1.32.0" in calls[3][1][0]
    assert calls[3][1][1] == {"user": "root"}
    assert (
        "copy",
        (
            "sandbox/verify_technical_metadata.py",
            "/opt/tin-lite/verify-technical-metadata.py",
            {"user": "root", "mode": 0o644},
        ),
    ) in calls
    config = (ROOT / "sandbox" / "codex_config.toml").read_text()
    assert 'model = "gpt-6-astra"' in config
    assert "OPENAI_API_KEY" not in config


def _procedure_source(tmp_path: Path) -> CodexProcedureSource:
    root = tmp_path / "research"
    skill = root / "skills" / "research-deep-dive"
    skill.mkdir(parents=True)
    (root / "PROMPT.md").write_text("Research the requested topic and cite sources.\n")
    (skill / "SKILL.md").write_text(
        "---\nname: research-deep-dive\ndescription: Run a bounded deep dive.\n---\n"
        "Write the declared Markdown artifact.\n"
    )
    (skill / "references.md").write_text("Prefer primary sources.\n")
    return CodexProcedureSource(
        root=root,
        entry_skill="research-deep-dive",
        output_path="reports/RESEARCH_DEEP_DIVE.md",
        project_skills=(
            ProjectSkillDependency(
                name="brand-context",
                path=".agents/skills/brand-context/SKILL.md",
                required=False,
            ),
        ),
    )


def test_procedure_definition_and_resources_are_one_registry_revision(tmp_path: Path) -> None:
    builtin = BuiltinWorkflow(
        id=uuid4(),
        key="research.deep_dive",
        title="Deep research",
        description="Create a source-backed research report.",
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.0.0",
        review_policy=HumanReviewPolicy(
            reason="Produces a reviewable report.",
            review_label="Review report",
            defer_label="Not now",
            summary="The report is ready for review.",
            queue_clause="Report ready to finish",
        ),
        procedure=_procedure_source(tmp_path),
    )

    definition, files = builtin.definition_and_resource_files()

    assert definition["executor"] == CODEX_PROCEDURE_EXECUTOR
    assert definition["procedure"]["prompt_path"] == ("procedures/research.deep_dive/PROMPT.md")
    assert definition["procedure"]["entry_skill"] == "research-deep-dive"
    assert definition["procedure"]["output"] == {
        "kind": "project.artifact",
        "path": "reports/RESEARCH_DEEP_DIVE.md",
        "media_type": "text/markdown",
        "max_bytes": 250_000,
    }
    assert set(files) == {
        "procedures/research.deep_dive/PROMPT.md",
        "procedures/research.deep_dive/skills/research-deep-dive/SKILL.md",
        "procedures/research.deep_dive/skills/research-deep-dive/references.md",
    }
    validate_codex_procedure_definition(definition)


def test_concrete_procedure_packages_are_pinned_and_ui_renderable() -> None:
    procedures = {
        item.key: item for item in BUILTIN_WORKFLOWS if item.executor == CODEX_PROCEDURE_EXECUTOR
    }

    assert set(procedures) == {
        "content.deliver",
        "content.generate",
        "organic.technical_fix",
        RESEARCH_DEEP_DIVE_WORKFLOW_NAME,
        PUBLIC_ARTICLE_WORKFLOW_NAME,
        SITE_HEALTH_WORKFLOW_NAME,
        EMAIL_SHORTLIST_WORKFLOW_NAME,
        QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME,
        CONTENT_DIAGRAM_WORKFLOW_NAME,
        PRODUCT_CODE_MAP_WORKFLOW_NAME,
        PRODUCT_DEEP_DIVE_WORKFLOW_NAME,
        QA_PRODUCT_AUDIT_WORKFLOW_NAME,
        CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME,
    }
    diagram = procedures[CONTENT_DIAGRAM_WORKFLOW_NAME]
    diagram_definition, diagram_files = diagram.definition_and_resource_files()
    assert diagram_definition["procedure"]["output"] == {
        "kind": "project.artifact",
        "path_template": "diagrams/{slug}.mmd",
        "media_type": "text/vnd.mermaid",
        "max_bytes": 64_000,
        "validator": TIN_DIAGRAM_REVIEWED_VALIDATOR,
    }
    assert diagram_definition["human_review"]["eligible"] is True
    assert "procedures/content.diagram/skills/content-diagram/SKILL.md" in diagram_files
    research = procedures[RESEARCH_DEEP_DIVE_WORKFLOW_NAME]
    research_definition, research_files = research.definition_and_resource_files()
    assert research_definition["procedure"]["entry_skill"] == "research-deep-dive"
    assert research_definition["procedure"]["output"]["path"] == ("reports/RESEARCH_DEEP_DIVE.md")
    assert "human_review" not in research_definition
    assert any(path.endswith("research-deep-dive/SKILL.md") for path in research_files)

    article = procedures[PUBLIC_ARTICLE_WORKFLOW_NAME]
    article_definition, article_files = article.definition_and_resource_files()
    assert article_definition["procedure"]["entry_skill"] == "public-article"
    assert (
        article_definition["procedure"]["output"]["path_template"] == "content/articles/{run_id}.md"
    )
    assert article_definition["human_review"] == {
        "eligible": True,
        "reason": "Produces a public-facing article draft.",
        "review_label": "Review article",
        "defer_label": "Not now",
        "summary": (
            "Your public article draft is ready. Review the claims and copy before Tin marks the "
            "workflow complete; otherwise it stays safely on hold."
        ),
        "queue_clause": "Public article draft ready to finish",
        "revision_adapter": "content-revision.v1",
    }
    assert any(path.endswith("public-article/SKILL.md") for path in article_files)
    assert any(path.endswith("public-article-edit/SKILL.md") for path in article_files)

    site_health = procedures[SITE_HEALTH_WORKFLOW_NAME]
    site_definition, site_files = site_health.definition_and_resource_files()
    assert site_definition["procedure"]["workspace"] == {
        "kind": "github.repository",
        "provider_key": "infra.github",
        "capabilities": ["contents.read", "pull_requests.read"],
    }
    assert site_definition["procedure"]["output"]["kind"] == GITHUB_PULL_REQUEST_RESULT
    assert site_definition["procedure"]["output"]["max_files"] == 3
    # Founder repositories use any toolchain: Tin reruns only the repo-agnostic check and
    # Codex may honestly report that nothing bounded is worth changing.
    assert site_definition["procedure"]["verification"]["commands"] == ["git diff --check"]
    assert site_definition["procedure"]["output"]["allow_no_change"] is True
    assert any(path.endswith("site-health-improvement/SKILL.md") for path in site_files)

    shortlist = procedures[EMAIL_SHORTLIST_WORKFLOW_NAME]
    shortlist_definition, shortlist_files = shortlist.definition_and_resource_files()
    assert shortlist_definition["procedure"]["output"] == {
        "kind": "project.artifact",
        "path": "outreach/email/SHORTLIST.csv",
        "media_type": "text/csv",
        "validator": "email-shortlist.v1",
        "max_bytes": 250_000,
    }
    assert shortlist_definition["integration_requirements"] == [
        {
            "provider_key": "workspace.google",
            "capabilities": ["gmail.messages.read", "calendar.events.read"],
            "required": True,
        }
    ]
    assert any(path.endswith("email-shortlist/SKILL.md") for path in shortlist_files)
    assert shortlist_definition["procedure"]["sandbox"] == {
        "profile": "default",
        "timeout_seconds": 900,
        "egress": "fenced",
    }
    assert shortlist_definition["procedure"]["identity"] == {"create": False, "reuse": "none"}

    walkthrough = procedures[QA_SIGNUP_WALKTHROUGH_WORKFLOW_NAME]
    walkthrough_definition, walkthrough_files = walkthrough.definition_and_resource_files()
    assert walkthrough_definition["procedure"]["entry_skill"] == "signup-walkthrough"
    assert walkthrough_definition["procedure"]["output"] == {
        "kind": "project.artifact",
        "path_template": "reports/qa/signup/{host}/{started_at}.md",
        "media_type": "text/markdown",
        "validator": "signup-walkthrough.v1",
        "max_bytes": 200_000,
    }
    assert walkthrough_definition["procedure"]["sandbox"] == {
        "profile": "browser",
        "timeout_seconds": 1800,
        "egress": "open",
    }
    assert walkthrough_definition["procedure"]["identity"] == {"create": True, "reuse": "none"}
    assert walkthrough_definition["system"] == "product-qa"
    assert walkthrough_definition["integration_requirements"] == [
        {
            "provider_key": "workspace.google",
            "capabilities": ["gmail.messages.read"],
            "required": True,
        }
    ]
    assert walkthrough_definition["schedule_modes"] == ["on_demand"]
    assert "human_review" not in walkthrough_definition
    assert any(path.endswith("signup-walkthrough/SKILL.md") for path in walkthrough_files)
    assert normalize_workflow_inputs(
        schema=walkthrough_definition["input_schema"],
        project_id=uuid4(),
        inputs={"product_url": "https://app.example.com/"},
    ) == {"product_url": "https://app.example.com/", "notes": ""}

    code_map = procedures[PRODUCT_CODE_MAP_WORKFLOW_NAME]
    code_definition, code_files = code_map.definition_and_resource_files()
    assert code_definition["system"] == "product-qa"
    assert code_definition["procedure"]["workspace"] == {
        "kind": "github.repository",
        "provider_key": "infra.github",
        "capabilities": ["contents.read"],
    }
    assert code_definition["procedure"]["output"] == {
        "kind": "project.artifact",
        "path": "wiki/INDEX.md",
        "media_type": "text/markdown",
        "max_bytes": 100_000,
        "validator": "memory-section.v1",
        "section": {"parent": "## Product", "heading": "### Code map", "max_bytes": 16_000},
    }
    assert code_definition["procedure"]["verification"] == {"commands": []}
    assert code_definition["procedure"]["sandbox"] == {
        "profile": "default",
        "timeout_seconds": 1800,
        "egress": "fenced",
    }
    assert code_definition["procedure"]["identity"] == {"create": False, "reuse": "none"}
    assert code_definition["integration_requirements"] == [
        {"provider_key": "infra.github", "capabilities": ["contents.read"], "required": True}
    ]
    assert code_definition["schedule_modes"] == ["on_demand", "weekly"]
    assert any(path.endswith("product-code-map/SKILL.md") for path in code_files)
    assert normalize_workflow_inputs(
        schema=code_definition["input_schema"], project_id=uuid4(), inputs={}
    ) == {"focus": "", "depth": "standard"}

    deep_dive = procedures[PRODUCT_DEEP_DIVE_WORKFLOW_NAME]
    dive_definition, dive_files = deep_dive.definition_and_resource_files()
    assert dive_definition["system"] == "product-qa"
    assert dive_definition["procedure"]["workspace"] == {"kind": "project.state"}
    assert dive_definition["procedure"]["output"]["path"] == "wiki/INDEX.md"
    assert dive_definition["procedure"]["output"]["section"] == {
        "parent": "## Product",
        "heading": "### Feature map",
        "max_bytes": 24_000,
    }
    assert dive_definition["procedure"]["sandbox"] == {
        "profile": "browser",
        "timeout_seconds": 3600,
        "egress": "open",
    }
    assert dive_definition["procedure"]["identity"] == {"create": True, "reuse": "active"}
    assert dive_definition["integration_requirements"] == [
        {
            "provider_key": "workspace.google",
            "capabilities": ["gmail.messages.read"],
            "required": True,
        }
    ]
    assert "human_review" not in dive_definition
    assert any(path.endswith("product-deep-dive/SKILL.md") for path in dive_files)
    assert normalize_workflow_inputs(
        schema=dive_definition["input_schema"],
        project_id=uuid4(),
        inputs={"product_url": "https://app.example.com/"},
    ) == {
        "product_url": "https://app.example.com/",
        "docs_urls": "",
        "depth": "standard",
        "notes": "",
    }

    audit = procedures[QA_PRODUCT_AUDIT_WORKFLOW_NAME]
    audit_definition, audit_files = audit.definition_and_resource_files()
    assert audit_definition["system"] == "product-qa"
    assert audit_definition["procedure"]["output"] == {
        "kind": "project.artifact",
        "path_template": "reports/qa/audit/{host}/{started_at}.md",
        "media_type": "text/markdown",
        "max_bytes": 300_000,
        "validator": "product-audit.v1",
    }
    assert audit_definition["procedure"]["sandbox"] == {
        "profile": "browser",
        "timeout_seconds": 3600,
        "egress": "open",
    }
    assert audit_definition["procedure"]["identity"] == {"create": True, "reuse": "active"}
    assert any(path.endswith("product-audit/SKILL.md") for path in audit_files)
    assert normalize_workflow_inputs(
        schema=audit_definition["input_schema"],
        project_id=uuid4(),
        inputs={"product_url": "https://app.example.com/", "scope": "Editor"},
    ) == {
        "product_url": "https://app.example.com/",
        "scope": "Editor",
        "depth": "standard",
        "notes": "",
    }

    project_id = uuid4()
    assert normalize_workflow_inputs(
        schema=research_definition["input_schema"],
        project_id=project_id,
        inputs={"question": "Which assumption is most likely to fail?"},
    ) == {
        "question": "Which assumption is most likely to fail?",
        "depth": "standard",
        "audience": "Project founders and decision-makers",
        "known_assumptions": "",
        "constraints": "",
    }
    assert normalize_workflow_inputs(
        schema=article_definition["input_schema"],
        project_id=project_id,
        inputs={"brief": "Explain what the research changes for founders."},
    ) == {
        "brief": "Explain what the research changes for founders.",
        "audience": "An informed general audience",
        "goal": "explain",
        "length": "standard",
        "source_policy": "project_and_web",
        "voice_notes": "",
    }


@pytest.mark.asyncio
async def test_loader_reads_definition_prompt_and_skills_from_exact_pinned_commit(
    tmp_path: Path,
) -> None:
    source = _procedure_source(tmp_path)
    procedure, files = source.materialize("research.deep_dive")
    definition = {
        "key": "research.deep_dive",
        "executor": CODEX_PROCEDURE_EXECUTOR,
        "procedure": procedure,
    }
    definition_path = "workflows/research.deep_dive.json"
    documents = {
        definition_path: json.dumps(definition).encode(),
        **files,
    }

    class Storage:
        calls: list[tuple[str, str]] = []

        async def read_canonical_artifact(self, *, commit_sha, path, **values):
            del values
            self.calls.append((commit_sha, path))
            return documents[path]

    storage = Storage()
    loaded = await load_pinned_codex_procedure(
        storage=storage,  # type: ignore[arg-type]
        repo_id="registry/workflows",
        commit_sha="a" * 40,
        definition_path=definition_path,
    )

    assert loaded.workflow_key == "research.deep_dive"
    assert loaded.output_path == "reports/RESEARCH_DEEP_DIVE.md"
    assert set(loaded.skill_files) == {
        "research-deep-dive/SKILL.md",
        "research-deep-dive/references.md",
    }
    assert {commit for commit, _path in storage.calls} == {"a" * 40}
    validate_procedure_artifact(b"# Result\n", spec=loaded)


def test_procedure_contract_rejects_unsupported_artifact_media_type() -> None:
    with pytest.raises(ValueError, match="media type is unsupported"):
        validate_codex_procedure_definition(
            {
                "key": "research.deep_dive",
                "executor": CODEX_PROCEDURE_EXECUTOR,
                "procedure": {
                    "prompt_path": "procedures/research.deep_dive/PROMPT.md",
                    "skills_path": "procedures/research.deep_dive/skills",
                    "skill_files": [
                        "procedures/research.deep_dive/skills/research-deep-dive/SKILL.md"
                    ],
                    "entry_skill": "research-deep-dive",
                    "output": {
                        "path": "src/application.py",
                        "media_type": "application/octet-stream",
                        "max_bytes": 10_000,
                    },
                },
            }
        )


def test_pull_request_contract_validates_workspace_diff_and_checks(tmp_path: Path) -> None:
    source = _procedure_source(tmp_path)
    pull_source = CodexProcedureSource(
        root=source.root,
        entry_skill=source.entry_skill,
        github_pull_request=GitHubPullRequestProcedure(
            receipt_path_template="reports/site-health/{run_id}.md",
            verification_commands=("uv run pytest",),
            max_files=2,
        ),
    )
    procedure, _files = pull_source.materialize("site.health_improve")
    definition = {
        "key": "site.health_improve",
        "executor": CODEX_PROCEDURE_EXECUTOR,
        "procedure": procedure,
    }
    spec = validate_codex_procedure_definition(definition)
    pinned = PinnedCodexProcedure(
        workflow_key="site.health_improve",
        prompt="Improve the site.",
        entry_skill="research-deep-dive",
        skill_files={},
        result_kind=spec.result_kind,
        workspace_kind=spec.workspace_kind,
        provider_key=spec.provider_key,
        output_max_bytes=spec.output_max_bytes,
        output_max_files=spec.output_max_files,
        receipt_path_template=spec.receipt_path_template,
        verification_commands=spec.verification_commands,
    )
    checkpoint = json.dumps(
        {
            "repository": "example/site",
            "default_branch": "main",
            "head_sha": "a" * 40,
            "title": "Add a page description",
            "body": "Evidence, change, and verification.",
            "files": [{"path": "src/index.html", "content": "<meta name='description'>"}],
            "verification": ["uv run pytest"],
        }
    ).encode()

    assert validate_procedure_pull_request(checkpoint, spec=pinned)["title"] == (
        "Add a page description"
    )
    changed = json.loads(checkpoint)
    changed["files"][0]["path"] = ".github/workflows/ci.yml"
    with pytest.raises(ValueError, match="writable path"):
        validate_procedure_pull_request(json.dumps(changed).encode(), spec=pinned)

    no_change = {**json.loads(checkpoint), "files": [], "outcome": "no_change"}
    no_change["title"] = "No change: descriptions are already present"
    with pytest.raises(ValueError, match="result contract"):
        validate_procedure_pull_request(json.dumps(no_change).encode(), spec=pinned)
    lenient = replace(pinned, allow_no_change=True)
    assert (
        validate_procedure_pull_request(json.dumps(no_change).encode(), spec=lenient)["outcome"]
        == "no_change"
    )
    # Under the opt-in, a patch must still change files and an outcome must be recognised.
    with pytest.raises(ValueError, match="result contract"):
        validate_procedure_pull_request(
            json.dumps({**no_change, "outcome": "patch"}).encode(), spec=lenient
        )
    with pytest.raises(ValueError, match="result contract"):
        validate_procedure_pull_request(
            json.dumps({**json.loads(checkpoint), "outcome": "no_change"}).encode(), spec=lenient
        )
    with pytest.raises(ValueError, match="outcome"):
        validate_procedure_pull_request(
            json.dumps({**json.loads(checkpoint), "outcome": "bogus"}).encode(), spec=lenient
        )
    # Older sandboxes never wrote an outcome; that still reads as a patch.
    assert validate_procedure_pull_request(checkpoint, spec=lenient)["files"]


def test_allow_no_change_is_an_explicit_pull_request_opt_in(tmp_path: Path) -> None:
    source = _procedure_source(tmp_path)
    plain = CodexProcedureSource(
        root=source.root,
        entry_skill=source.entry_skill,
        github_pull_request=GitHubPullRequestProcedure(
            receipt_path_template="reports/site-health/{run_id}.md",
            verification_commands=("git diff --check",),
        ),
    )
    procedure, _files = plain.materialize("site.health_improve")
    assert "allow_no_change" not in procedure["output"]
    definition = {"key": "site.health_improve", "executor": CODEX_PROCEDURE_EXECUTOR}
    assert (
        validate_codex_procedure_definition({**definition, "procedure": procedure}).allow_no_change
        is False
    )

    opted = CodexProcedureSource(
        root=source.root,
        entry_skill=source.entry_skill,
        github_pull_request=GitHubPullRequestProcedure(
            receipt_path_template="reports/site-health/{run_id}.md",
            verification_commands=("git diff --check",),
            allow_no_change=True,
        ),
    )
    procedure, _files = opted.materialize("site.health_improve")
    assert procedure["output"]["allow_no_change"] is True
    spec = validate_codex_procedure_definition({**definition, "procedure": procedure})
    assert spec.allow_no_change is True
    pinned = PinnedCodexProcedure(
        workflow_key="site.health_improve",
        prompt="Improve the site.",
        entry_skill="research-deep-dive",
        skill_files={},
        result_kind=spec.result_kind,
        workspace_kind=spec.workspace_kind,
        allow_no_change=spec.allow_no_change,
    )
    assert pinned.sandbox_context(inputs={})["output"]["allow_no_change"] is True
    assert (
        "allow_no_change"
        not in replace(pinned, allow_no_change=False).sandbox_context(inputs={})["output"]
    )

    with pytest.raises(ValueError, match="boolean"):
        validate_codex_procedure_definition(
            {
                **definition,
                "procedure": {
                    **procedure,
                    "output": {**procedure["output"], "allow_no_change": "yes"},
                },
            }
        )
    artifact, _files = source.materialize("research.deep_dive")
    artifact_output = {**artifact["output"], "allow_no_change": True}
    with pytest.raises(ValueError, match="allow_no_change"):
        validate_codex_procedure_definition(
            {
                "key": "research.deep_dive",
                "executor": CODEX_PROCEDURE_EXECUTOR,
                "procedure": {**artifact, "output": artifact_output},
            }
        )
    technical = next(item for item in BUILTIN_WORKFLOWS if item.key == "organic.technical_fix")
    technical_definition = json.loads(json.dumps(technical.definition))
    technical_definition["procedure"]["output"]["allow_no_change"] = True
    with pytest.raises(ValueError, match="allow_no_change"):
        validate_codex_procedure_definition(technical_definition)


def test_pull_request_receipt_covers_no_change_and_requires_the_opened_pr() -> None:
    manifest = {
        "repository": "example/site",
        "default_branch": "main",
        "head_sha": "a" * 40,
        "title": "Add a page description",
        "body": "Evidence, change, and verification.",
        "files": [{"path": "src/index.html", "content": "<meta name='description'>"}],
        "verification": ["git diff --check"],
    }
    delivered = build_procedure_pull_request_receipt(
        workflow_title="Improve site health",
        manifest=manifest,
        pull_request_url="https://github.com/example/site/pull/7",
        pull_request_number=7,
        pull_request_branch="tin/site-health",
    ).decode()
    assert "- Pull request: [example/site#7](https://github.com/example/site/pull/7)" in delivered
    assert "- Branch: `tin/site-health`" in delivered
    assert "- `src/index.html`" in delivered
    assert "merging remains a human decision in GitHub." in delivered
    with pytest.raises(ValueError, match="opened pull request"):
        build_procedure_pull_request_receipt(
            workflow_title="Improve site health", manifest=manifest
        )

    skipped = build_procedure_pull_request_receipt(
        workflow_title="Improve site health",
        manifest={
            **manifest,
            "files": [],
            "outcome": "no_change",
            "title": "No change: descriptions are already present",
            "body": "Every public page already carries a description.",
        },
    ).decode()
    assert "No change proposed; no pull request was opened." in skipped
    assert "**No change: descriptions are already present**" in skipped
    assert "Every public page already carries a description." in skipped
    assert "- none" in skipped
    assert "- `git diff --check` — passed before delivery" in skipped
    assert "Pull request:" not in skipped


@pytest.mark.asyncio
async def test_github_procedure_workspace_fetch_is_heartbeat_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeats: list[dict[str, str]] = []
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", heartbeats.append)
    expected = GitHubRepositoryBundle(
        repository="example/site",
        default_branch="main",
        head_sha="a" * 40,
        archive=b"archive",
        file_count=1,
    )
    expected_evidence = GitHubOpenPullRequestEvidence(
        document=b'{"pull_requests":[],"version":1}',
        pull_request_count=0,
        file_count=0,
        changed_paths=(),
        truncated=False,
    )

    class Integrations:
        async def github_repository_bundle(self, **values):
            calls.append(("repository", values))
            return expected

        async def github_open_pull_requests(self, **values):
            calls.append(("pull_requests", values))
            return expected_evidence

    activities = TinActivities(
        database=SimpleNamespace(),
        storage=SimpleNamespace(),
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
        integrations=Integrations(),  # type: ignore[arg-type]
    )
    project_id = uuid4()
    run_id = uuid4()

    actual = await activities._github_procedure_workspace(
        project_id=project_id,
        run_id=run_id,
        sandbox_id="sandbox-1",
    )

    assert actual == (expected, expected_evidence)
    assert calls == [
        (
            "repository",
            {
                "project_id": project_id,
                "execution_key": f"{run_id}:procedure_repository_workspace",
                "run_id": run_id,
            },
        ),
        (
            "pull_requests",
            {
                "project_id": project_id,
                "execution_key": f"{run_id}:procedure_open_pull_requests",
                "base_branch": "main",
                "run_id": run_id,
            },
        ),
    ]
    assert heartbeats == [
        {"sandbox_id": "sandbox-1", "stage": "procedure_repository_workspace"},
        {"sandbox_id": "sandbox-1", "stage": "procedure_open_pull_requests"},
    ]


def test_catalog_rejects_procedure_executor_without_source_package() -> None:
    builtin = BuiltinWorkflow(
        id=uuid4(),
        key="research.deep_dive",
        title="Deep research",
        description="Create a source-backed research report.",
        executor=CODEX_PROCEDURE_EXECUTOR,
        version_label="1.0.0",
    )

    with pytest.raises(ValueError, match="must declare exactly one"):
        _ = builtin.definition


@pytest.mark.asyncio
@pytest.mark.parametrize("project_revision", [None, "a" * 40])
async def test_procedure_runtime_returns_bounded_result_without_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
    project_revision,
) -> None:
    payload = base64.b64encode(
        json.dumps({"summary": "Report ready.", "message": "Open the report."}).encode()
    ).decode()

    class Handle:
        async def wait(self):
            return SimpleNamespace(
                stdout=(
                    "TIN_PROCEDURE_HEARTBEAT=codex\n"
                    f"TIN_PROCEDURE_COMMIT_SHA={'c' * 40}\n"
                    f"TIN_PROCEDURE_RESULT={payload}\n"
                )
            )

    class Commands:
        async def run(self, *args, **kwargs):
            if args[0].endswith("isolated-procedure check"):
                return SimpleNamespace(stdout="TIN_ISOLATION_READY_V1")
            if args[0].endswith("codex_api_config.py --check"):
                return SimpleNamespace(stdout="TIN_CODEX_API_READY_V1")
            assert args == ("/opt/tin-lite/run-procedure",)
            assert kwargs["background"] is True
            assert callable(kwargs["on_stdout"])
            assert kwargs["envs"]["TIN_PROCEDURE_OUTPUT_PATH"] == "reports/RESEARCH.md"
            assert kwargs["envs"]["TIN_PROCEDURE_OUTPUT_MAX_BYTES"] == "250000"
            assert kwargs["envs"]["TIN_PROCEDURE_RESULT_KIND"] == "github.pull_request"
            assert kwargs["envs"]["TIN_PROCEDURE_STATE_DIR"] == "/home/user/state"
            assert kwargs["envs"].get("TIN_PROCEDURE_PROJECT_REVISION") == project_revision
            assert "TIN_PROCEDURE_CONTEXT_B64" in kwargs["envs"]
            for key in (
                "OPENAI_API_KEY",
                "CODEX_API_KEY",
                "TIN_LITE_LUNA_API_KEY",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
            ):
                assert key not in kwargs["envs"]
            return Handle()

    class Sandbox:
        commands = Commands()
        killed = False

        class Files:
            writes = []

            async def write(self, path, data):
                self.writes.append((path, data))

        files = Files()

        async def kill(self):
            self.killed = True

    sandbox = Sandbox()

    async def connect(*args, **kwargs):
        return sandbox

    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.connect", connect)
    runtime = E2BRuntime(
        api_key="e2b-test",  # noqa: S106
        template="tin-lite-codex",
        timeout_seconds=60,
        egress_allow_hosts=(),
    )
    result = await runtime.run_procedure_and_kill(
        sandbox_id="sandbox-1",
        run_input=SandboxProcedureInput(
            execution_key="run:procedure_artifact_persist",
            isolated=True,
            usage_sink=AsyncMock(),
            api_url="https://tin.test/relay",
            api_grant="synthetic-relay-grant",
            canonical_url="https://storage.test/canonical.git",
            canonical_auth_header="Authorization: Basic canonical",
            canonical_branch="main",
            ephemeral_url="https://storage.test/ephemeral.git",
            ephemeral_auth_header="Authorization: Basic ephemeral",
            ephemeral_branch="procedures/run/1",
            proxy_url="http://proxy.test:3128",
            no_proxy="tin.test",
            context={"workflow_key": "research.deep_dive"},
            output_path="reports/RESEARCH.md",
            output_max_bytes=250_000,
            project_revision=project_revision,
            result_kind="github.pull_request",
            workspace_archive=b"repository-archive",
            workspace_evidence=b'{"pull_requests":[],"version":1}',
        ),
    )

    assert result.ephemeral_commit_sha == "c" * 40
    assert result.summary == "Report ready."
    context_path, context_bytes = sandbox.files.writes[0]
    assert context_path == "/home/user/.tin-lite/procedure-context.json"
    assert json.loads(context_bytes) == {"workflow_key": "research.deep_dive"}
    assert sandbox.files.writes[1:] == [
        ("/home/user/.tin-lite/procedure-workspace.tar.gz", b"repository-archive"),
        (
            "/home/user/.tin-lite/open-pull-requests.json",
            b'{"pull_requests":[],"version":1}',
        ),
    ]
    assert sandbox.killed is True


@pytest.mark.asyncio
async def test_procedure_failure_projects_sanitized_effect_error() -> None:
    run_id = uuid4()

    class Database:
        projected_error = None
        released = False

        async def get_run(self, requested_id):
            assert requested_id == run_id
            return SimpleNamespace(sandbox_id="sandbox-1")

        async def get_effect(self, execution_key):
            if execution_key == f"{run_id}:procedure_artifact_persist":
                return EffectReceipt(
                    execution_key,
                    "procedure_artifact_persist",
                    "failed",
                    None,
                    "ReadError: operation failed",
                )
            return None

        async def project_failure(self, **values):
            assert values["run_id"] == run_id
            self.projected_error = values["error_message"]

        async def release_lease(self, requested_id):
            assert requested_id == run_id
            self.released = True

        async def finalize_pending_test_identity(self, **values):
            assert values["run_id"] == run_id
            assert values["status"] == "failed"
            return False

    class Sandboxes:
        killed = None

        async def kill(self, sandbox_id):
            self.killed = sandbox_id

    database = Database()
    sandboxes = Sandboxes()
    activities = TinActivities(
        database=database,
        storage=object(),
        sandboxes=sandboxes,
        settings=SimpleNamespace(),
    )

    await activities.project_codex_procedure_failure(
        {"run_id": str(run_id), "reason": "ActivityError: workflow failed"}
    )

    assert database.projected_error == "ReadError: operation failed"
    assert database.released is True
    assert sandboxes.killed == "sandbox-1"


@pytest.mark.asyncio
async def test_runtime_recovery_ignores_destroyed_sandbox_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Paginator:
        async def next_items(self, **_kwargs):
            return [
                SimpleNamespace(sandbox_id="destroyed"),
                SimpleNamespace(sandbox_id="running"),
            ]

    class Sandbox:
        def __init__(self, running):
            self.running = running

        async def is_running(self):
            return self.running

    monkeypatch.setattr(
        "tin_lite.e2b_runtime.AsyncSandbox.list", lambda *_args, **_kwargs: Paginator()
    )

    async def connect(sandbox_id, **_kwargs):
        return Sandbox(sandbox_id == "running")

    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.connect", connect)
    runtime = E2BRuntime(
        api_key="e2b-test",  # noqa: S106
        template="tin-lite-codex",
        timeout_seconds=60,
        egress_allow_hosts=(),
    )

    assert await runtime._find("run:procedure_sandbox_create") == "running"


def test_procedure_bridge_uses_explicit_skills_web_search_and_one_output() -> None:
    bridge = (ROOT / "sandbox" / "procedure_app_server.py").read_text()
    runner = (ROOT / "sandbox" / "run_procedure.sh").read_text()

    assert '"--search", "app-server"' in bridge
    assert '"networkAccess": "enabled"' in bridge
    assert "TURN_IDLE_TIMEOUT_SECONDS = 15 * 60" in bridge
    assert "PROCEDURE_HEARTBEAT_SECONDS = 30" in bridge
    assert "TIN_PROCEDURE_HEARTBEAT=codex" in bridge
    assert "${entry_skill}" in bridge
    assert "Execute the registered Tin workflow" in bridge
    assert "/home/user/.agents/skills" in bridge
    assert "/home/user/.tin-lite/open-pull-requests.json" in bridge
    assert "Do not duplicate or materially overlap" in bridge
    # The checkout is a snapshot with a synthetic baseline commit; the prompt must say so
    # instead of inviting Codex to verify a commit id that never exists locally.
    assert "Tin's local baseline commit for that snapshot" in bridge
    assert "materialized read-only at" not in bridge
    assert "NO_CHANGE_OUTPUT_SCHEMA" in bridge
    assert 'output.get("allow_no_change")' in runner
    assert "changed files outside the procedure output contract" in runner
    assert "ANTHROPIC_API_KEY" in runner
    assert "GEMINI_API_KEY" in runner
    # A read-only repository snapshot keeps the artifact in the project-state checkout.
    assert 'if [[ "${result_kind}" == "github.pull_request" ]]; then' in runner
    assert "status --porcelain --untracked-files=all" in runner
    assert "read-only repository snapshot; discarded" in runner
    assert 'STATE_DIR = Path(os.environ.get("TIN_PROCEDURE_STATE_DIR"' in bridge
    assert "is a read-only snapshot of the connected" in bridge
    assert "replace that section in full" in bridge
