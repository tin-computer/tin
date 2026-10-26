"""List ordinals are structure; factual numbers still need cited support."""

import json
from copy import deepcopy

import pytest
from test_social_x_compose import POST, Context, inputs, package

from tin_lite.workflow_code import validate_code_definition

FACT = "Connect a listener, send a message, and verify the reply."


@pytest.mark.parametrize(
    "text",
    [
        "1. Connect a listener.\n2. Send a message.\n3. Verify the reply.",
        "1) Connect a listener.\n2) Send a message.\n3) Verify the reply.",
        "Steps:\n \t1. Connect a listener.\n  2) Send a message.\n\t3. Verify the reply.",
        "- Connect a listener.\n- Send a message.\n- Verify the reply.",
    ],
)
async def test_list_markers_do_not_require_numeric_evidence(text):
    module, definition = package()
    spec = validate_code_definition(definition)
    ctx = Context({}, spec, {"posts": [{**deepcopy(POST), "text": text}]})

    result = await module.run(ctx, inputs(direction=FACT))

    post = json.loads(result["content"])["posts"][0]
    assert post["text"] == text
    assert post["readiness"] == "ready"
    assert "Check the number" not in post["editor_notes"]


@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.parametrize(
    "text, label",
    [
        ("1. Send 250 messages.", "number"),
        ("1) Send 250 messages.", "number"),
        ("1. 250 messages reached the listener.", "number"),
        ("1) 250 messages reached the listener.", "number"),
        ("1. Send 25% of the messages.", "number"),
        ("1. Send messages 2x faster.", "number"),
        ("1. Launch on 2026-10-25.", "number"),
        ("1. Use the release dated 25.10.2026.", "number"),
        ("1. Use version 2.4.0.", "number"),
        ("1. Read https://example.com/250.", "URL"),
        ("1. 2.5 seconds per message.", "number"),
        ("1) 2,5 seconds per message.", "number"),
        ("1.2 seconds per message.", "number"),
        ("1)250 messages reached the listener.", "number"),
        ("250 messages reached the listener.", "number"),
        ("1.", "number"),
        ("1)", "number"),
        ("Steps:\n1.\n250 messages reached the listener.", "number"),
        ("Send 1. 250 messages to the listener.", "number"),
    ],
)
def test_numbers_and_urls_still_require_evidence(text, label, supported):
    module, _ = package()
    source = text if supported else FACT
    candidate = {**deepcopy(POST), "text": text}
    units = [{"id": "s1", "source_path": "notes", "excerpt": source}]

    post = module._validate_model({"posts": [candidate]}, 1, units, {"notes": source}, {})[0]
    assert post["text"] == text
    assert post["readiness"] == ("ready" if supported else "needs_evidence")
    assert (f"Check the {label}" in post["editor_notes"]) is not supported
