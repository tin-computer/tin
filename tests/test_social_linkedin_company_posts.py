"""Offline contract tests for social.linkedin_company_posts; no supplier calls.

The batch is written by one model call and checked by code; posts with problems get one fix
call. Every post waits for its card, and its link goes in its own first comment.
"""

from __future__ import annotations

import json
import runpy
from datetime import datetime
from fnmatch import fnmatchcase
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.linkedin_posts import validate_draft
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = REPOSITORY_ROOT / "workflow_packages" / "social.linkedin_company_posts"
PROJECT_ID = "a0000000-0000-0000-0000-000000000001"
BRAND = (
    "# Example brand\n\nTin is an open-source marketing system that runs inside your coding "
    "agent. Founders keep their marketing in plain files in the repo.\n"
)
SITE = "# Example site\n\nAdd Tin to your agent: https://tin.computer/\n"
COMMENT = "Explore Tin: https://tin.computer/"
TOPICS = ("analytics", "answers", "people", "install", "workflows", "growth")


def text(topic):
    return (
        f"Founders who run {topic} by hand lose track of what changed this week.\n\n"
        f"Tin keeps the {topic} work in plain files inside the repo, so the coding agent reads "
        f"the same context every time it runs the {topic} workflow.\n\n"
        f"Open the {topic} view, read what the latest run wrote, and decide what goes out "
        f"next. The {topic} notes stay with the project, and each change keeps its history "
        "in git for later."
    )


def post(topic, **changes):
    return {
        "format": "Product in one picture",
        "audience": "Technical founders",
        "text": text(topic),
        "card_line": f"See the {topic} work in one place",
        "card_kind": "screen",
        "card_brief": f"The {topic} view with sample data.",
        "alt_text": f"The {topic} view with sample data.",
        "first_comment": COMMENT,
        "support": [{"source": "s1", "excerpt": "Tin is an open-source marketing system"}],
        **changes,
    }


BATCH = {
    "formats": [{"name": "Product in one picture", "what": "One screen, one claim."}],
    "posts": [post(topic) for topic in TOPICS[:5]] + [post(TOPICS[5], first_comment="")],
    "gaps": [],
}


def load_package():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
    return module, definition


class Files:
    def __init__(self, files):
        self.files = dict(files)

    def read_text(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def glob(self, pattern):
        return [path for path in self.files if fnmatchcase(path, pattern)]

    def read_section(self, heading):
        # No wiki here; the package reads its feature map when one exists.
        raise FileNotFoundError(heading)


class Context(dict):
    def __init__(self, spec, outputs, files=None):
        super().__init__(
            run_id="b0000000-0000-0000-0000-000000000099", created_at="2026-11-11T12:00:00+00:00"
        )
        self.files = Files(files or {"brand/BRAND.md": BRAND, "context/site/site.md": SITE})
        self.calls = []
        answers = list(outputs)

        async def generate(**payload):
            request_contract(spec, payload)
            self.calls.append(payload)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            jsonschema.validate(answer, payload["output_schema"])
            return {"parsed": answer, "text": json.dumps(answer)}

        self.models = SimpleNamespace(generate=generate)


def check(result, spec):
    validate_code_result(
        json.dumps(result).encode(),
        spec,
        created_at=datetime.fromisoformat("2026-11-11T12:00:00+00:00"),
    )
    # The reader opens it: the same draft schema it saves and previews.
    return validate_draft(result["content"].encode())


def test_manifest_has_one_write_route_with_room_for_one_fix():
    _, definition = load_package()
    spec = validate_code_definition(definition)
    assert definition["schedule_modes"] == ["on_demand"]
    assert spec.output_path == "social/linkedin/drafts/{date}-{slug}.json"
    assert [(route.name, route.max_calls) for route in spec.model_routes] == [("write", 2)]


async def test_a_clean_batch_waits_for_cards_with_links_in_first_comments():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    ctx = Context(spec, [BATCH])
    result = await module.run(ctx, {"project_id": PROJECT_ID})
    assert [call["step"] for call in ctx.calls] == ["write"]
    assert result["path"] == "social/linkedin/drafts/2026-11-11-company-posts-b0000000.json"
    draft = check(result, spec)
    assert [item["id"] for item in draft["posts"]] == ["p1", "p2", "p3", "p4", "p5", "p6"]
    assert all(item["readiness"] == "needs_asset" for item in draft["posts"])
    assert draft["posts"][0]["missing_assets"] == ["Card: See the analytics work in one place"]
    # The link is a field of its own, posted after the post, and out of the notes.
    assert [item["first_comment"] for item in draft["posts"]] == [COMMENT] * 5 + [""]
    assert "First comment" not in draft["posts"][0]["editor_notes"]
    assert "Check:" not in json.dumps(draft)
    assert "Each one waits for its card" in result["summary"]


async def test_an_unusable_fix_keeps_the_posts_with_their_checks():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    linked = post("analytics", text=text("analytics") + " Read more at https://tin.computer/")
    repeated = post("answers", text=text("answers") + "\n\n" + text("analytics").split("\n\n")[0])
    batch = {**BATCH, "posts": [linked, repeated, *BATCH["posts"][2:]]}
    # A plausible fix that fixes nothing: the link is still in the text.
    ctx = Context(spec, [batch, {"posts": [{"id": "p1", **linked}]}])
    result = await module.run(ctx, {"project_id": PROJECT_ID})
    assert [call["step"] for call in ctx.calls] == ["write", "fix"]
    assert [item["id"] for item in ctx.calls[1]["data"]["posts"]] == ["p1", "p2"]
    draft = check(result, spec)
    notes = [item["editor_notes"] for item in draft["posts"]]
    assert "Check: The post has a link; move it to the first comment." in notes[0]
    assert "repeats a sentence from p1" in notes[1]
    assert "Read the checks on p1, p2" in result["summary"]


async def test_a_fix_replaces_a_post_only_when_it_clears_its_problems():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    linked = post("analytics", text=text("analytics") + " Read more at https://tin.computer/")
    batch = {**BATCH, "posts": [linked, *BATCH["posts"][1:]]}
    ctx = Context(spec, [batch, {"posts": [{"id": "p1", **post("analytics")}]}])
    result = await module.run(ctx, {"project_id": PROJECT_ID})
    draft = check(result, spec)
    assert "https://" not in draft["posts"][0]["text"]
    assert draft["posts"][0]["first_comment"] == COMMENT
    assert "Check:" not in json.dumps(draft)


async def test_a_write_call_with_no_usable_answer_leaves_a_note_and_no_posts():
    module, definition = load_package()
    spec = validate_code_definition(definition)
    ctx = Context(spec, [ValueError("model_output_invalid: the answer had no posts")])
    result = await module.run(ctx, {"project_id": PROJECT_ID})
    draft = check(result, spec)
    assert draft["posts"] == []
    assert draft["gaps"][0]["angle"] == "This batch"
    assert "did not have the expected shape" in draft["gaps"][0]["reason"]


async def test_without_a_brand_or_notes_it_asks_for_them():
    module, definition = load_package()
    ctx = Context(validate_code_definition(definition), [], files={"README.md": "# Hi\n"})
    with pytest.raises(ValueError, match="brand guide"):
        await module.run(ctx, {"project_id": PROJECT_ID})
