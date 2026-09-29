"""The unregistered source consumer works as a public package and a private copy."""

import hashlib
import json
import runpy
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from tin_lite.community import ContributedPackage, validate, validate_private_copy
from tin_lite.private_workflows import authoring_guide
from tin_lite.public_workflows import PUBLIC_WORKFLOWS
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/example.approved_evidence"


async def test_approved_source_example_accepts_both_ownership_forms():
    package = ContributedPackage(key="example.approved_evidence", path=PACKAGE)
    await validate(package, root=ROOT)
    await validate_private_copy(package, root=ROOT)
    assert all(item.key != package.key for item in PUBLIC_WORKFLOWS)


def source():
    content = "# Post batch\n\nA fact backed by the approved article.\n"
    return {
        "present": True,
        "content": content,
        "run_id": str(uuid4()),
        "path": "reports/SOCIAL_POST_BATCH.md",
        "revision": "a" * 40,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def test_consumer_uses_the_selected_text_and_keeps_its_exact_reference():
    posts = source()
    run = runpy.run_path(str(PACKAGE / "main.py"))["run"]
    result = run({"evidence": {"posts": posts}}, {"posts_run_id": posts["run_id"]})
    spec = validate_code_definition(
        json.loads((PACKAGE / "workflow.json").read_text())["definition"]
    )
    text = validate_code_result(json.dumps(result).encode(), spec).decode()
    assert text.endswith(posts["content"])
    assert posts["revision"] in text and posts["sha256"] in text


@pytest.mark.parametrize("change", ["missing", "changed", "wrong_run"])
def test_consumer_refuses_an_unusable_source(change):
    posts = source()
    inputs = {"posts_run_id": posts["run_id"]}
    if change == "missing":
        posts = {"present": False}
    elif change == "changed":
        posts["content"] = "A plausible replacement that was not selected."
    else:
        inputs["posts_run_id"] = str(uuid4())
    run = runpy.run_path(str(PACKAGE / "main.py"))["run"]
    with pytest.raises(ValueError):
        run({"evidence": {"posts": posts}}, inputs)


def test_authoring_guide_declares_a_contract_that_validates():
    from types import SimpleNamespace

    guide = authoring_guide(settings=SimpleNamespace(), project_id=uuid4())
    definition = deepcopy(json.loads((PACKAGE / "workflow.json").read_text())["definition"])
    definition["code"]["evidence"] = guide["code_contract"]["evidence"]["declaration"]
    validate_code_definition(definition)
