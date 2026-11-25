"""The intel judge contract: only Antigravity, isolated, one Markdown file, no other run tools."""

from __future__ import annotations

import copy

import pytest

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.procedures import (
    INTEL_NOTES_VALIDATOR,
    validate_codex_procedure_definition,
    validate_intel_markdown,
)


def world_judge():
    found = next(b for b in BUILTIN_WORKFLOWS if b.key == "intel.world_judge")
    definition, _files = found.definition_and_resource_files()
    return definition


def test_the_world_judge_is_an_isolated_antigravity_procedure():
    definition = world_judge()
    spec = validate_codex_procedure_definition(definition)
    assert spec.intel_scope == "world"
    assert spec.agent.harness == "antigravity" and spec.agent.model == "gemini-4-neon"
    assert spec.sandbox.isolated and spec.sandbox.timeout_seconds == 3600
    assert spec.output_path_template == "intel/world/runs/{run_folder}/NOTES.md"
    assert spec.output_validator == INTEL_NOTES_VALIDATOR and spec.output_max_bytes == 20_000
    assert definition["public_discovery"] is False
    assert definition["input_schema"]["required"] == ["project_id", "parent_run_id", "scope", "day"]


# E2B answers "400: Timeout cannot be greater than 1 hours" when a sandbox asks for more
# (staging, 2026-11-25: the world judge at 5,400 s never started).
E2B_MAX_SANDBOX_SECONDS = 3600


def test_no_builtin_procedure_asks_e2b_for_more_than_an_hour():
    for builtin in BUILTIN_WORKFLOWS:
        if builtin.procedure is None:
            continue
        definition, _files = builtin.definition_and_resource_files()
        spec = validate_codex_procedure_definition(definition)
        assert spec.sandbox.timeout_seconds <= E2B_MAX_SANDBOX_SECONDS, builtin.key


def changed(edit):
    definition = copy.deepcopy(world_judge())
    edit(definition, definition["procedure"])
    return definition


@pytest.mark.parametrize(
    "edit, said",
    [
        (lambda d, p: p.pop("agent"), "Antigravity"),
        (lambda d, p: p.update(intel={"scope": "client"}), "intel.client_judge"),
        (lambda d, p: p.update(intel={"scope": "elsewhere"}), "scope"),
        (lambda d, p: p.update(intel={"scope": "world", "extra": 1}), "scope"),
        (lambda d, p: d.update(key="intel.other"), "prompt path"),
        (lambda d, p: p.pop("intel"), "belong to an intel judge"),
        (lambda d, p: p["output"].update(max_bytes=200_000), "bounded Markdown"),
        (lambda d, p: p["output"].update(path_template="reports/{run_id}.md"), "run-owned"),
        (
            lambda d, p: p.update(identity={"create": True}),
            "Codex only",
        ),
        (
            lambda d, p: p.update(
                sandbox={"profile": "studio", "timeout_seconds": 5400, "egress": "fenced"}
            ),
            "fenced default or isolated",
        ),
    ],
)
def test_anything_else_is_refused(edit, said):
    with pytest.raises(ValueError, match=said):
        validate_codex_procedure_definition(changed(edit))


def test_services_are_refused_on_a_judge():
    definition = changed(
        lambda d, p: (
            p.update(services=[{"alias": "x", "provider_key": "workspace.google"}]),
            d.update(
                integration_requirements=[
                    {"provider_key": "workspace.google", "capabilities": ["gmail.messages.read"]}
                ]
            ),
        )
    )
    with pytest.raises(ValueError):
        validate_codex_procedure_definition(definition)


def test_the_judges_markdown_check_is_lenient():
    validate_intel_markdown(
        b"- Wires dominated by the Nvidia story.\n- FT was empty.", limit=20_000
    )
    validate_intel_markdown("Türkçe kaynaklar boştu.".encode(), limit=20_000)
    for content in (b"", b"   \n", b"x" * 20_001):
        with pytest.raises(ValueError):
            validate_intel_markdown(content, limit=20_000)
    with pytest.raises(ValueError, match="credential"):
        validate_intel_markdown(
            b"The grant was Bearer abcdefghijklmnopqrstuvwxyz0123", limit=20_000
        )


def test_the_client_judges_brief_example_reports_and_never_suggests():
    """Staging 2026-11-25: the skill's own example modelled a page suggestion, and Databasin's
    brief copied it ("Worth refreshing our ... comparison page"). The example passes Tin's
    brief check with no warning, and the rules no longer hold a suggestion up as good style."""
    import json
    import re
    from pathlib import Path

    from tin_lite import intel_brief

    root = Path(__file__).resolve().parents[1] / "codex_procedures" / "intel.client_judge"
    skill = (root / "skills" / "client-intel" / "SKILL.md").read_text()
    example = json.loads(re.search(r"Example, for[^\n]*\n\n```json\n(.*?)\n```", skill, re.S)[1])
    links = intel_brief.LinkIndex(
        re.findall(r"\]\((https://[^)]+)\)", json.dumps(example).replace("\\/", "/"))
    )
    _kept, warnings = intel_brief.validate(intel_brief.CLIENT_SCHEMA, example, links=links)
    assert warnings == []
    # The old rule held the suggestion up as the right way to write.
    assert '"Worth refreshing our pricing page", never' not in skill
