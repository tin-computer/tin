"""Reviewed decision resources for growth.framework_starter; no provider or model calls in CI."""

import json
import re
from pathlib import Path

import pytest

from tin_lite.workflow_prerequisites import parse_workflow_prerequisites
from tin_lite.workflow_qualification import Qualification

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/growth.framework_starter"
RESOURCES = PACKAGE / "skills/framework-starter"
DEFINITION = json.loads((PACKAGE / "workflow.json").read_text(encoding="utf-8"))["definition"]


def resource(name):
    path = RESOURCES / name
    blocks = re.findall(r"```python\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only the fixed repository resource executes here, never repository or model text.
    exec(compile(blocks[0], str(path), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture
def choose():
    return resource("FRAMEWORKS.md")["choose_starter"]


@pytest.fixture
def check():
    return resource("CHECK.md")["check_starter"]


def facts(**overrides):
    return {
        "surface": "sdk",
        "auth": "secret_key",
        "languages": ["typescript"],
        "examples_dir": "",
        **overrides,
    }


def test_a_typescript_sdk_gets_a_nextjs_starter_under_examples(choose):
    decided = choose(facts())
    assert decided["outcome"] == "patch"
    assert decided["framework"] == "nextjs"
    assert decided["path"] == "examples/nextjs-starter"
    assert "app/api/demo/route.ts" in decided["files"]
    assert decided["run"] == "npm run dev"


def test_the_repository_examples_convention_is_kept(choose):
    assert choose(facts(examples_dir="packages/examples/"))["path"] == (
        "packages/examples/nextjs-starter"
    )


@pytest.mark.parametrize(
    ("overrides", "framework"),
    [
        ({"languages": ["python"]}, "fastapi"),
        ({"languages": ["python", "typescript"]}, "nextjs"),
        ({"surface": "http_api", "languages": []}, "nextjs"),
        ({"surface": "http_api", "languages": ["python"]}, "fastapi"),
        ({"surface": "webhooks", "languages": []}, "express"),
        ({"surface": "webhooks", "languages": ["python"]}, "fastapi"),
        ({"surface": "embed", "auth": "publishable_key", "languages": []}, "vite-react"),
        ({"surface": "embed", "auth": "secret_key", "languages": []}, "nextjs"),
    ],
)
def test_the_framework_follows_what_the_product_publishes(choose, overrides, framework):
    assert choose(facts(**overrides))["framework"] == framework


def test_nothing_a_developer_integrates_is_an_honest_no(choose):
    decided = choose(facts(surface="none", languages=[]))
    assert decided["outcome"] == "no_change"
    assert decided["framework"] is None and decided["files"] == ()
    assert "nothing a developer integrates" in decided["reason"]


def test_an_earlier_starter_moves_on_to_the_next_fit_instead_of_repeating(choose):
    earlier = [{"framework": "nextjs", "path": "examples/nextjs-starter"}]
    decided = choose(facts(languages=["typescript", "python"]), earlier)
    assert decided["framework"] == "express"
    decided = choose(
        facts(languages=["python", "typescript"]),
        earlier + [{"framework": "express", "path": "examples/with-express"}],
    )
    assert decided["framework"] == "fastapi"


def test_when_every_fitting_framework_has_a_starter_nothing_changes(choose):
    earlier = [
        {"framework": "nextjs", "path": "examples/nextjs-starter"},
        {"framework": "express", "path": "examples/express-starter"},
    ]
    decided = choose(facts(), earlier)
    assert decided["outcome"] == "no_change"
    assert "already has a starter" in decided["reason"]
    assert "examples/express-starter" in decided["reason"]


def test_a_requested_framework_is_honoured_once(choose):
    assert choose(facts(), requested="express")["framework"] == "express"
    decided = choose(facts(), [{"framework": "express", "path": "demo/express"}], "express")
    assert decided["outcome"] == "no_change"
    assert "already has a express starter at demo/express" in decided["reason"]


@pytest.mark.parametrize("auth", ["secret_key", "oauth"])
def test_a_browser_only_framework_never_holds_a_secret(choose, auth):
    decided = choose(facts(surface="embed", auth=auth, languages=[]), requested="vite-react")
    assert decided["outcome"] == "no_change"
    assert "vite-react cannot demonstrate" in decided["reason"]


def test_a_framework_that_does_not_fit_the_file_budget_is_skipped(choose):
    assert choose(facts(), max_files=7)["framework"] == "express"
    assert choose(facts(), max_files=5)["outcome"] == "no_change"


@pytest.mark.parametrize(
    "broken",
    [
        None,
        facts(surface="cli"),
        facts(auth="basic"),
        facts(languages="typescript"),
        facts(languages=["ruby"]),
        facts(surface="sdk", languages=[]),
        facts(examples_dir="../elsewhere"),
        facts(examples_dir=".github"),
        {k: v for k, v in facts().items() if k != "examples_dir"},
    ],
)
def test_unusable_facts_are_rejected_rather_than_guessed(choose, broken):
    with pytest.raises(ValueError):
        choose(broken)


@pytest.mark.parametrize(
    ("earlier", "requested", "budget"),
    [
        ([{"framework": "rails", "path": "examples/rails"}], "", 10),
        ([{"framework": "nextjs", "path": " "}], "", 10),
        ([], "ruby-on-rails-hotwire", 10),
        ([], "", 11),
        ([], "", True),
    ],
)
def test_earlier_starters_requests_and_budgets_are_validated(choose, earlier, requested, budget):
    with pytest.raises(ValueError):
        choose(facts(), earlier, requested, budget)


ROUTE = """import { Acme } from "@acme/sdk";

const client = new Acme({ apiKey: process.env.ACME_API_KEY });

export async function POST(request: Request) {
  const { query } = await request.json();
  try {
    return Response.json(await client.search.query({ query }));
  } catch {
    return Response.json({ error: "Search failed" }, { status: 502 });
  }
}
"""


def nextjs_starter(**overrides):
    root = "examples/nextjs-starter"
    files = {
        "package.json": '{"name": "acme-nextjs-starter", "dependencies": {"@acme/sdk": "^2.1.0"}}',
        "tsconfig.json": "{}",
        ".env.example": "# Server-only key from https://acme.dev/keys\nACME_API_KEY=\n",
        ".gitignore": ".env.local\nnode_modules\n",
        "README.md": "Copy .env.example to .env.local, then npm install and npm run dev.",
        "app/layout.tsx": "export default function Layout({ children }) { return children }",
        "app/page.tsx": '"use client";\nexport default function Page() { fetch("/api/demo") }',
        "app/api/demo/route.ts": ROUTE,
        **overrides,
    }
    return {f"{root}/{name}": text for name, text in files.items() if text is not None}


CALLS = [{"call": "client.search.query", "evidence": "sdk/src/search.ts:42"}]


def test_a_grounded_server_side_starter_passes(choose, check):
    assert check(choose(facts()), "secret_key", nextjs_starter(), CALLS) == []


@pytest.mark.parametrize(
    ("files", "calls", "problem"),
    [
        (
            {".env.example": "NEXT_PUBLIC_ACME_API_KEY=\n"},
            CALLS,
            "NEXT_PUBLIC_ACME_API_KEY would ship a secret to the browser",
        ),
        (
            {"app/page.tsx": '"use client";\nconst key = process.env.ACME_API_KEY;'},
            CALLS,
            "app/page.tsx runs in the browser and reads ACME_API_KEY",
        ),
        (
            {".env.example": "ACME_API_KEY=sk_live_51HxQ2aBcDeFgHiJkLmN\n"},
            CALLS,
            "contains what looks like a real credential",
        ),
        ({".env.example": "ACME_API_KEY=my-key\n"}, CALLS, "has a value in .env.example"),
        ({".env.example": "ACME_BASE_URL=\n"}, CALLS, "declares no server-side secret"),
        ({"app/api/demo/route.ts": None}, CALLS, "missing examples/nextjs-starter/app/api"),
        ({"README.md": "Run it."}, CALLS, "README.md does not mention npm run dev"),
        ({}, [], "no product call is listed"),
        ({}, [{"call": "client.search.query", "evidence": "the SDK docs"}], "without repository"),
        (
            {},
            [{"call": "client.vectors.upsert", "evidence": "sdk/src/vectors.ts:9"}],
            "client.vectors.upsert is listed but no starter file uses it",
        ),
    ],
)
def test_a_plausible_but_unusable_starter_is_caught(choose, check, files, calls, problem):
    problems = check(choose(facts()), "secret_key", nextjs_starter(**files), calls)
    assert any(problem in p for p in problems), problems


def test_files_outside_the_starter_and_over_budget_are_caught(choose, check):
    choice = choose(facts())
    files = nextjs_starter()
    files["src/app/signup.tsx"] = "changed product code"
    assert any(
        "outside examples/nextjs-starter/" in p for p in check(choice, "secret_key", files, CALLS)
    )
    files = nextjs_starter(**{f"extra{i}.ts": "" for i in range(3)})
    assert any("at most 10" in p for p in check(choice, "secret_key", files, CALLS))


def test_a_publishable_key_may_ship_in_a_browser_starter(choose, check):
    choice = choose(facts(surface="embed", auth="publishable_key", languages=[]))
    root = choice["path"]
    files = {f"{root}/{name}": "" for name in choice["files"]}
    files[f"{root}/.env.example"] = "VITE_ACME_PUBLISHABLE_KEY=\n"
    files[f"{root}/README.md"] = "cp .env.example .env.local && npm run dev"
    files[f"{root}/src/App.tsx"] = "mountWidget(import.meta.env.VITE_ACME_PUBLISHABLE_KEY)"
    calls = [{"call": "mountWidget", "evidence": "embed/src/index.ts:3"}]
    assert check(choice, "publishable_key", files, calls) == []


def test_check_refuses_a_no_change_choice(choose, check):
    with pytest.raises(ValueError):
        check(choose(facts(surface="none", languages=[])), "none", {}, [])


def test_manifest_declares_every_shipped_resource_and_a_pull_request_output():
    procedure = DEFINITION["procedure"]
    shipped = sorted(
        str(path.relative_to(PACKAGE)) for path in (PACKAGE / "skills").rglob("*") if path.is_file()
    )
    assert sorted(procedure["skill_files"]) == shipped
    assert DEFINITION["key"] == PACKAGE.name
    assert "system" not in DEFINITION
    assert procedure["workspace"]["kind"] == "github.repository"
    assert procedure["output"]["kind"] == "github.pull_request"
    assert procedure["output"]["allow_no_change"] is True
    assert procedure["output"]["max_files"] == resource("CHECK.md")["MAX_FILES"] == 10
    assert procedure["output"]["receipt_path_template"] == (
        "reports/framework-starter/{run_id}/RESULT.md"
    )


def test_the_framework_input_is_exactly_the_supported_list():
    schema = DEFINITION["input_schema"]
    assert schema["required"] == ["project_id"]
    assert schema["additionalProperties"] is False
    frameworks = resource("FRAMEWORKS.md")["FRAMEWORKS"]
    assert schema["properties"]["framework"]["enum"] == ["", *frameworks]
    assert set(resource("CHECK.md")["PUBLIC_PREFIX"]) == set(frameworks)
    assert schema["properties"]["use_case"]["maxLength"] == 300


def test_prerequisites_are_recommended_reads_of_what_tin_already_holds():
    prerequisites = parse_workflow_prerequisites(
        DEFINITION["prerequisites"], input_schema=DEFINITION["input_schema"]
    )
    assert [(p.producer, p.level) for p in prerequisites] == [
        ("product.code_map", "recommended"),
        ("product.deep_dive", "recommended"),
        ("growth.onboarding_plan", "recommended"),
        ("style.capture", "recommended"),
    ]


def test_the_skill_uses_the_resources_unchanged_and_never_publishes():
    skill = " ".join((RESOURCES / "SKILL.md").read_text().split())
    prompt = " ".join((PACKAGE / "PROMPT.md").read_text().split())
    assert "Run `choose_starter` unchanged" in skill
    assert "Run `check_starter` from CHECK.md unchanged" in skill
    assert "reports/framework-starter/*/RESULT.md" in skill
    for promise in (
        "secret in browser code",
        "merge, deploy, publish a package",
        "create a repository",
    ):
        assert promise in prompt


def test_the_receipt_hands_off_and_can_be_read_back():
    text = (RESOURCES / "RESULT.md").read_text()
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings == [
        "Outcome",
        "What a developer gets",
        "Grounded in",
        "Secrets",
        "Verification",
        "Next steps",
        "Not changed",
    ]
    for handoff in ("`content.plan`", "`outreach.awesome_lists`"):
        assert handoff in text
    block = re.search(r"```json framework-starter\n(.*?)\n```", text, re.S).group(1)
    state = json.loads(block)
    assert state["framework"] in resource("FRAMEWORKS.md")["FRAMEWORKS"]


def test_qualification_cases_cover_the_honest_no_and_the_unusable_request():
    cases = Qualification.model_validate_json(
        (ROOT / "workflow_evals/growth.framework_starter/qualification.json").read_text()
    ).cases
    assert [case.id for case in cases] == [
        "ordinary",
        "saas_without_developer_surface",
        "repeat_after_nextjs",
        "browser_framework_for_secret_key",
    ]
