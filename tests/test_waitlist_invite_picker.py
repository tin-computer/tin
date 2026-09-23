import json
import runpy
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.workflow_code import validate_code_definition, validate_code_result


def package():
    root = "workflow_packages/growth.waitlist_invite_picker"
    module = SimpleNamespace(**runpy.run_path(f"{root}/main.py"))
    with open(f"{root}/workflow.json", encoding="utf-8") as handle:
        definition = json.load(handle)["definition"]
    return module, definition


async def test_waitlist_invite_picker_ranks_invites():
    module, definition = package()
    spec = validate_code_definition(definition)
    seen = {}

    async def generate(**payload):
        seen.update(payload)
        output = {
            "leads": [
                {
                    "id": 0,
                    "fit": 5,
                    "urgency": 5,
                    "authority": "decision_maker",
                    "stage": "ready_now",
                    "segment": "B2B SaaS founder",
                    "invite_reason": "High-fit founder with a named launch workflow need.",
                    "risk": "",
                },
                {
                    "id": 1,
                    "fit": 2,
                    "urgency": 1,
                    "authority": "user",
                    "stage": "curious",
                    "segment": "Student researcher",
                    "invite_reason": "Curious but not an urgent buyer.",
                    "risk": "Likely learning, not buying.",
                },
            ]
        }
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    ctx = SimpleNamespace(models=SimpleNamespace(generate=generate))
    result = await module.run(
        ctx,
        {
            "csv_text": (
                "name,email,company,role,notes\n"
                "Ava,a@example.com,Acme,Founder,Need to launch onboarding emails this week\n"
                "Ben,b@example.com,Campus,Student,Researching tools for a class\n"
            ),
            "ideal_customer_profile": "Small B2B SaaS founders with an urgent growth workflow.",
            "invite_count": 1,
            "constraints": "Prefer founders with a near-term project.",
        },
    )
    validate_code_result(json.dumps(result).encode(), spec)
    assert seen["step"] == "score_waitlist_leads"
    assert "Ava" in result["content"]
    assert "Ben" in result["content"]
    assert result["content"].find("Ava") < result["content"].find("Ben")


async def test_waitlist_invite_picker_rejects_model_that_drops_ids():
    module, _definition = package()

    async def generate(**payload):
        output = {
            "leads": [
                {
                    "id": 99,
                    "fit": 5,
                    "urgency": 5,
                    "authority": "decision_maker",
                    "stage": "ready_now",
                    "segment": "Founder",
                    "invite_reason": "Looks urgent.",
                    "risk": "",
                }
            ]
        }
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    ctx = SimpleNamespace(models=SimpleNamespace(generate=generate))
    with pytest.raises(ValueError, match="preserve every input row ID"):
        await module.run(
            ctx,
            {
                "csv_text": "name,email,notes\nAva,a@example.com,Need this now\n",
                "ideal_customer_profile": "B2B founders",
            },
        )


def test_waitlist_invite_picker_rejects_bad_csv():
    module, _definition = package()
    with pytest.raises(ValueError, match="unique column"):
        module._read_csv("email,email\na@example.com,b@example.com\n")
