"""Offline checks for outreach.newsletter_placements: package, inputs, qualification, report."""

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from tin_lite.community import CheckoutStorage, ContributedPackage, discover, validate
from tin_lite.procedures import load_pinned_codex_procedure
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs
from tin_lite.workflow_qualification import Qualification
from tin_lite.workflow_qualification_cli import check_checkout

KEY = "outreach.newsletter_placements"
ROOT = Path(__file__).parents[1]
PACKAGE_DIR = ROOT / "workflow_packages" / KEY
MANIFEST_PATH = PACKAGE_DIR / "workflow.json"
QUALIFICATION_PATH = ROOT / "workflow_evals" / KEY / "qualification.json"

KINDS = {
    "newsletter slot",
    "newsletter submission",
    "community showcase",
    "community sponsorship",
}
HANDED_OFF = re.compile(
    r"\b(conference|call for (papers|speakers)|cfp|meetup|podcast|hackathon|campus|student club)\b",
    re.I,
)
BROAD_PLATFORMS = {"reddit", "linkedin", "discord", "twitter", "x", "facebook", "product hunt"}
SECTIONS = {"Handed off", "Drafted in earlier runs", "Excluded candidates"}


def parse_placements(markdown: str, *, max_picks: int, budget_usd: int) -> list[dict]:
    """Parse one placements report and enforce what makes a pick usable."""
    if not markdown.startswith("# Newsletter and community placements"):
        raise ValueError("Missing report title")
    state = re.findall(r"```tin-placement-state\n(.*?)\n```", markdown, re.S)
    if len(state) != 1:
        raise ValueError("Missing tin-placement-state block")
    json.loads(state[0])
    picks = []
    for block in re.split(r"^## ", markdown, flags=re.M)[1:]:
        name, _, body = block.partition("\n")
        if name.strip() in SECTIONS:
            continue
        fields = dict(
            re.findall(r"^- (Kind|URL|Rules|Price|Audience|Last active): (.*)$", body, re.M)
        )
        pick = {"name": name.strip(), **fields}
        if name.strip().lower() in BROAD_PLATFORMS:
            raise ValueError(f"'{name.strip()}' is a broad platform, not a placement")
        if fields.get("Kind") not in KINDS:
            raise ValueError(f"'{name.strip()}' is not a newsletter or community placement")
        if HANDED_OFF.search(name) or HANDED_OFF.search(fields.get("Rules", "")):
            raise ValueError(f"'{name.strip()}' belongs to another workflow")
        if not fields.get("URL", "").startswith("https://"):
            raise ValueError(f"'{name.strip()}' has no verified URL")
        if not fields.get("Rules", "").startswith("https://"):
            raise ValueError(f"'{name.strip()}' has no rules or intake URL")
        if "https://" not in fields.get("Audience", ""):
            raise ValueError(f"'{name.strip()}' has no audience source URL")
        price = fields.get("Price", "")
        if price != "free":
            amount = re.search(r"\$([\d,]+)", price)
            if not amount or int(amount.group(1).replace(",", "")) > budget_usd:
                raise ValueError(f"'{name.strip()}' is over the sponsorship budget")
        draft = body.split("### Draft", 1)
        if len(draft) != 2 or not draft[1].strip():
            raise ValueError(f"'{name.strip()}' has no drafted submission")
        picks.append(pick)
    if len(picks) > max_picks:
        raise ValueError(f"More than {max_picks} placements")
    return picks


def report(*placements: str) -> str:
    state = {"version": 1, "placements": [{"name": "Kube Weekly", "rules_url": "https://k.dev/r"}]}
    return (
        "# Newsletter and community placements\n\n"
        "Context: growth plan, Feature map · Buyers: platform engineers · Geography: EU · "
        "Budget: free only\nStatus: ready\n\n"
        + "\n".join(placements)
        + "\n## Handed off\n\n- KubeCon EU CFP: Find the talks and podcasts worth pitching\n\n"
        "## Drafted in earlier runs\n\n- none\n\n"
        "## Excluded candidates\n\n- r/devops: no showcase rules\n\n"
        f"```tin-placement-state\n{json.dumps(state)}\n```\n"
    )


def placement(
    name="Kube Weekly", kind="newsletter submission", rules="https://k.dev/submit", price="free"
):
    return (
        f"## {name}\n\n- Kind: {kind}\n- URL: https://k.dev\n"
        f"- Rules: {rules} — one link, 50 words\n"
        f"- Price: {price}\n- Audience: platform engineers (https://k.dev/about)\n"
        "- Last active: 2026-09-20 (https://k.dev/issues/301)\n\n### Draft\n\n"
        "Acme turns cluster logs into alerts. Disclosure: I make it.\n"
    )


# --- package


async def test_package_discovers_and_validates():
    assert any(p.key == KEY for p in discover(ROOT / "workflow_packages"))
    await validate(ContributedPackage(key=KEY, path=PACKAGE_DIR), root=ROOT)


async def test_procedure_loader_contract():
    procedure = await load_pinned_codex_procedure(
        storage=CheckoutStorage(ROOT),
        repo_id="workflow_packages",
        commit_sha="checkout",
        definition_path=f"workflow_packages/{KEY}/workflow.json",
    )
    assert procedure.workflow_key == KEY
    assert procedure.entry_skill == "newsletter-placements"
    assert "newsletter-placements/SKILL.md" in procedure.skill_files
    assert procedure.output_path_template == "reports/outreach/newsletters/{run_id}.md"
    assert procedure.sandbox.profile == "isolated" and procedure.sandbox.egress == "fenced"
    assert "speaking_shortlist" not in procedure.prompt
    assert "Find the talks and podcasts worth" in procedure.prompt


def test_inputs_need_nothing_but_the_project():
    schema = json.loads(MANIFEST_PATH.read_bytes())["definition"]["input_schema"]
    project_id = uuid4()
    normalized = normalize_workflow_inputs(schema=schema, project_id=project_id, inputs={})
    assert normalized["budget_usd"] == 0 and normalized["max_picks"] == 5
    assert normalized["focus"] == "" and normalized["geography"] == ""
    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(schema=schema, project_id=project_id, inputs={"max_picks": 11})
    with pytest.raises(WorkflowInputError):
        normalize_workflow_inputs(
            schema=schema, project_id=project_id, inputs={"target_audience": "old input"}
        )


def test_prerequisites_read_what_tin_already_holds():
    prerequisites = json.loads(MANIFEST_PATH.read_bytes())["definition"]["prerequisites"]
    assert {p["path"] for p in prerequisites} == {
        "reports/GROWTH_ONBOARDING_PLAN.md",
        "wiki/INDEX.md",
        ".agents/skills/writing-style/SKILL.md",
    }
    assert all(p["level"] == "recommended" for p in prerequisites)


async def test_qualification_contract():
    qualification = Qualification.model_validate_json(QUALIFICATION_PATH.read_bytes())
    assert len(qualification.cases) >= 2 and len(qualification.rubric) >= 4
    checked = await check_checkout(ROOT, f"workflow_packages/{KEY}/workflow.json")
    assert checked["shape"]["status"] == "passed" and checked["workflow"] == KEY


# --- report


def test_a_usable_report_passes():
    picks = parse_placements(
        report(placement(), placement("r/kubernetes showcase", "community showcase")),
        max_picks=5,
        budget_usd=0,
    )
    assert [p["name"] for p in picks] == ["Kube Weekly", "r/kubernetes showcase"]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        # Plausible but unusable: a real-looking newsletter with no rules page opened.
        (placement(rules="their editor probably takes tips"), "no rules or intake URL"),
        (placement("KubeCon EU call for speakers"), "belongs to another workflow"),
        (placement("Reddit", "community showcase"), "broad platform"),
        (placement(kind="podcast guest"), "not a newsletter or community placement"),
        (placement(price="$1,200 per issue"), "over the sponsorship budget"),
        (
            placement().replace(
                "Acme turns cluster logs into alerts. Disclosure: I make it.\n", ""
            ),
            "no drafted",
        ),
    ],
)
def test_unusable_picks_are_refused(bad, message):
    with pytest.raises(ValueError, match=message):
        parse_placements(report(bad), max_picks=5, budget_usd=0)


def test_padding_and_missing_memory_are_refused():
    with pytest.raises(ValueError, match="More than 2"):
        parse_placements(
            report(*(placement(f"Newsletter {i}") for i in range(3))), max_picks=2, budget_usd=0
        )
    with pytest.raises(ValueError, match="tin-placement-state"):
        parse_placements(report(placement()).split("```tin")[0], max_picks=5, budget_usd=0)
