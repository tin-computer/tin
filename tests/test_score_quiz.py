"""Offline fixture tests for the score-quiz package.

No network, no model provider and no execution runtime are used: the manifest is checked
against the shared workflow.code contract, and main.py's run() is exercised directly against
a synthetic ctx.models.generate, exactly like the two shipped code examples.
"""

import json
import re
import runpy
from types import SimpleNamespace

import pytest
from test_public_workflows import PublishedSnapshots, select
from test_registry_recipe_publication import WIKI, catalog_database

from tin_lite import catalog
from tin_lite.code_models import request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import load_code_package, validate_code_definition, validate_code_result

KEY = "growth.score_quiz"


def _question(text, *point_labels):
    return {
        "text": text,
        "options": [{"label": label, "points": points} for points, label in point_labels],
    }


GOOD_QUIZ = {
    "intro": "Answer four quick questions to see how ready your API is for production traffic.",
    "questions": [
        _question(
            "How do you currently handle a burst of requests?",
            (0, "We don't do anything special"),
            (2, "We have basic rate limiting"),
            (3, "We have adaptive, per-client rate limiting"),
        ),
        _question(
            "How do you find out about a rate-limit false positive today?",
            (0, "A customer tells us"),
            (1, "We notice it in generic error logs"),
            (3, "We have dedicated visibility into bucket state"),
        ),
        _question(
            "How many services enforce their own limits independently?",
            (0, "Every service has its own ad hoc logic"),
            (2, "Most share one library"),
            (3, "All share one centrally configured policy"),
        ),
        _question(
            "How confident are you explaining a 429 to an engineer who hit one?",
            (0, "Not confident at all"),
            (1, "Somewhat, after digging through logs"),
            (3, "Very confident, it's a quick lookup"),
        ),
    ],
    "bands": [
        {
            "title": "Just getting started",
            "verdict": "You're flying blind on rate limits.",
            "min_score": 0,
            "max_score": 39,
        },
        {
            "title": "On your way",
            "verdict": "You have the basics but no real visibility.",
            "min_score": 40,
            "max_score": 74,
        },
        {
            "title": "Production ready",
            "verdict": "You'd catch most rate-limit issues quickly.",
            "min_score": 75,
            "max_score": 100,
        },
    ],
}

GOOD_RESULT = {
    "share_template": "I scored {score} on the API rate-limit readiness quiz.",
    "cta_line": "Want to see exactly why a request was rejected? Try Loopwire.",
}

GOOD_INPUTS = {
    "product_name": "Loopwire",
    "quiz_topic": "API rate-limit readiness",
    "signup_url": "https://loopwire.example/quiz",
}


def _load():
    root = REPOSITORY_ROOT / "workflow_packages" / KEY
    definition = json.loads((root / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(root / "main.py")))
    return module, definition


def _context(quiz=None, result=None):
    calls = []

    async def generate(**payload):
        calls.append(payload)
        if payload["step"] == "design_quiz":
            return {"parsed": quiz or GOOD_QUIZ, "text": "..."}
        return {"parsed": result or GOOD_RESULT, "text": "..."}

    return SimpleNamespace(models=SimpleNamespace(generate=generate)), calls


def _extract_data_blob(content):
    match = re.search(r"var DATA = (\{.*\});", content)
    assert match, "expected an embedded DATA object in the rendered widget"
    return json.loads(match.group(1))


def test_manifest_matches_the_shared_code_workflow_contract():
    _, definition = _load()
    spec = validate_code_definition(definition)
    assert spec.entrypoint == "main.py"
    assert {route.name for route in spec.model_routes} == {"design_quiz", "write_result_copy"}
    assert sum(route.max_calls for route in spec.model_routes) == 2


async def test_calls_both_steps_and_renders_a_bounded_markdown_artifact():
    module, definition = _load()
    spec = validate_code_definition(definition)
    context, calls = _context()
    result = await module.run(context, GOOD_INPUTS)
    validate_code_result(json.dumps(result).encode(), spec)
    assert [c["step"] for c in calls] == ["design_quiz", "write_result_copy"]
    assert result["path"] == "reports/SCORE_QUIZ.md"
    content = result["content"]
    assert "```html" in content
    assert GOOD_INPUTS["signup_url"] in content
    data = _extract_data_blob(content)
    assert len(data["questions"]) == 4
    assert data["max_possible"] == 3 + 3 + 3 + 3
    assert data["signup_url"] == GOOD_INPUTS["signup_url"]
    assert content.startswith("# Loopwire score quiz\n")
    assert (
        "_Scores: API rate-limit readiness. Aimed at developers evaluating this space._" in content
    )


async def test_rejects_an_answer_label_cut_off_at_its_length_limit():
    module, _ = _load()
    # Strict structured output stops at maxLength mid-sentence instead of failing.
    clipped = ("We monitor delivery outcomes and backlog " * 5)[:160]
    question = _question("How would you notice failures?", (0, "We wouldn't"), (3, clipped))
    context, _ = _context(quiz={**GOOD_QUIZ, "questions": [question, *GOOD_QUIZ["questions"][1:]]})
    with pytest.raises(ValueError, match="cut off"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_result_copy_cut_off_at_its_length_limit():
    module, _ = _load()
    context, _ = _context(result={**GOOD_RESULT, "cta_line": "x" * 200})
    with pytest.raises(ValueError, match="cut off"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_a_non_https_signup_url():
    module, _ = _load()
    context, _ = _context()
    bad_inputs = {**GOOD_INPUTS, "signup_url": "http://loopwire.example/quiz"}
    with pytest.raises(ValueError, match="https://"):
        await module.run(context, bad_inputs)


async def test_rejects_a_question_whose_answers_cannot_discriminate():
    module, _ = _load()
    flat_question = _question("Flat question", (1, "Option A"), (1, "Option B"))
    context, _ = _context(
        quiz={**GOOD_QUIZ, "questions": [flat_question, *GOOD_QUIZ["questions"][1:]]}
    )
    with pytest.raises(ValueError, match="different points"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_score_bands_with_a_gap():
    module, _ = _load()
    bands = [
        {"title": "Low", "verdict": "...", "min_score": 0, "max_score": 39},
        {"title": "Mid", "verdict": "...", "min_score": 45, "max_score": 74},
        {"title": "High", "verdict": "...", "min_score": 75, "max_score": 100},
    ]
    context, _ = _context(quiz={**GOOD_QUIZ, "bands": bands})
    with pytest.raises(ValueError, match="contiguous"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_score_bands_that_do_not_cover_the_full_range():
    module, _ = _load()
    bands = [
        {"title": "Low", "verdict": "...", "min_score": 5, "max_score": 39},
        {"title": "Mid", "verdict": "...", "min_score": 40, "max_score": 74},
        {"title": "High", "verdict": "...", "min_score": 75, "max_score": 100},
    ]
    context, _ = _context(quiz={**GOOD_QUIZ, "bands": bands})
    with pytest.raises(ValueError, match="full 0-100 range"):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_a_share_template_missing_the_score_placeholder():
    module, _ = _load()
    context, _ = _context(result={**GOOD_RESULT, "share_template": "I aced the quiz!"})
    with pytest.raises(ValueError, match=re.escape("{score}")):
        await module.run(context, GOOD_INPUTS)


async def test_rejects_result_copy_that_invents_its_own_link():
    module, _ = _load()
    context, _ = _context(result={**GOOD_RESULT, "cta_line": "Sign up at https://evil.example"})
    with pytest.raises(ValueError, match="invent its own link"):
        await module.run(context, GOOD_INPUTS)


async def test_escapes_a_script_breakout_attempt_in_model_supplied_text():
    module, _ = _load()
    hostile_question = _question(
        "</script><script>alert(document.cookie)</script>",
        (0, "safe option one"),
        (3, "safe option two"),
    )
    context, _ = _context(
        quiz={**GOOD_QUIZ, "questions": [hostile_question, *GOOD_QUIZ["questions"][1:]]}
    )
    result = await module.run(context, GOOD_INPUTS)
    content = result["content"]
    # The real closing </script> tag from our own template must still be present once...
    assert content.count("</script>") == 1
    # ...but the payload's attempt to close the tag early must have been neutralized.
    assert "</script><script>alert" not in content
    data = _extract_data_blob(content)
    assert data["questions"][0]["text"] == "</script><script>alert(document.cookie)</script>"


async def test_publishes_through_the_real_catalog_sync_like_the_shipped_examples(monkeypatch):
    (entry,) = select(monkeypatch, KEY)
    db, storage = catalog_database(), PublishedSnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    row = db.rows[entry.id]
    assert row.project_id is None
    assert row.executor == "workflow.code"
    assert row.definition_repo_id == "registry/workflows"
    assert row.definition_path == f"workflow_packages/{KEY}/workflow.json"
    definition, spec, files = await load_code_package(
        storage=storage,
        repo_id=row.definition_repo_id,
        commit_sha=row.current_commit_sha,
        definition_path=row.definition_path,
    )
    assert definition["key"] == KEY
    assert spec.output_path == "reports/SCORE_QUIZ.md"
    assert (
        files["main.py"] == (REPOSITORY_ROOT / "workflow_packages" / KEY / "main.py").read_bytes()
    )


@pytest.mark.parametrize(
    "url",
    [
        'https://loopwire.example/quiz" onmouseover="alert(1)',
        "https://loopwire.example/quiz'><script>alert(1)</script>",
        "https://loopwire.example/a b",
        "javascript:alert(1)//https://",
        "https:///no-host",
    ],
)
async def test_rejects_a_signup_url_that_could_break_out_of_the_link(url):
    module, _ = _load()
    context, calls = _context()
    with pytest.raises(ValueError, match="https://"):
        await module.run(context, {**GOOD_INPUTS, "signup_url": url})
    assert calls == []


async def test_widget_sets_the_link_through_the_dom_not_markup():
    module, _ = _load()
    context, _ = _context()
    content = (await module.run(context, GOOD_INPUTS))["content"]
    assert "link.href = DATA.signup_url;" in content
    assert "href=\"' + DATA.signup_url" not in content
    assert '.replace(/"/g, "&quot;")' in content


async def test_neutralizes_comment_and_fence_breakouts_in_model_text():
    module, _ = _load()
    hostile_question = _question(
        "Do you use ``` fences or <!-- comments --> & entities?",
        (0, "No `ticks` here"),
        (3, "Yes </SCRIPT > sometimes"),
    )
    context, _ = _context(
        quiz={**GOOD_QUIZ, "questions": [hostile_question, *GOOD_QUIZ["questions"][1:]]}
    )
    content = (await module.run(context, GOOD_INPUTS))["content"]
    blob = re.search(r"var DATA = (\{.*\});", content).group(1)
    for raw in ("<", ">", "&", "`"):
        assert raw not in blob
    # The only code fence is the one the report opens and closes around the widget.
    assert content.count("```") == 2
    data = json.loads(blob)
    assert data["questions"][0]["text"] == hostile_question["text"]
    assert data["questions"][0]["options"][1]["label"] == "Yes </SCRIPT > sometimes"


async def test_rejects_a_plausible_quiz_that_slips_a_link_into_a_verdict():
    module, _ = _load()
    bands = [dict(band) for band in GOOD_QUIZ["bands"]]
    bands[0]["verdict"] = "Start with our free guide at www.loopwire-guides.example."
    context, calls = _context(quiz={**GOOD_QUIZ, "bands": bands})
    with pytest.raises(ValueError, match="invent its own link"):
        await module.run(context, GOOD_INPUTS)
    assert [c["step"] for c in calls] == ["design_quiz"]


async def test_passes_product_context_and_voice_as_data_not_instructions():
    module, definition = _load()
    spec = validate_code_definition(definition)
    context, calls = _context()
    inputs = {
        **GOOD_INPUTS,
        "product_summary": "Loopwire shows why each API request was rate limited.",
        "voice_notes": "Plain words, no exclamation marks.",
    }
    await module.run(context, inputs)
    for call in calls:
        request_contract(spec, call)
        assert call["data"]["voice_notes"] == "Plain words, no exclamation marks."
        assert "Loopwire" not in call["instructions"]
    assert calls[1]["data"]["product_summary"] == inputs["product_summary"]


BRAND_GUIDE = (
    "# Loopwire brand\n\n## Brand direction\n\nPlain, exact, a little dry. Say the number.\n\n"
    "## Visual style\n\nInk on paper.\n"
)


class _Files:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.reads = text, error, []

    def read_text(self, path):
        self.reads.append(path)
        if self.error:
            raise self.error
        if self.text is None:
            raise FileNotFoundError(path)
        return self.text


async def test_the_approved_brand_direction_sets_the_quiz_voice():
    module, definition = _load()
    spec = validate_code_definition(definition)
    context, calls = _context()
    context.files = _Files(BRAND_GUIDE)
    result = await module.run(context, GOOD_INPUTS)
    validate_code_result(json.dumps(result).encode(), spec)
    assert context.files.reads == ["brand/BRAND.md"]
    for call in calls:
        assert call["data"]["brand_direction"] == "Plain, exact, a little dry. Say the number."
        assert "brand_direction" in call["instructions"]
        request_contract(spec, call)
    # The voice guides the model; it never reaches the founder's artifact as text.
    assert "a little dry" not in result["content"]


@pytest.mark.parametrize(
    "files", [_Files(), _Files("# No direction section\n"), _Files(error=ValueError("binary"))]
)
async def test_a_missing_or_unreadable_brand_guide_leaves_the_voice_to_voice_notes(files):
    module, _ = _load()
    context, calls = _context()
    context.files = files
    await module.run(context, GOOD_INPUTS)
    assert all(call["data"]["brand_direction"] == "" for call in calls)
