"""Core lookup recall, concise review instructions and exact older contracts."""

import json

import jsonschema
import pytest
from test_keyword_plan import finish, fixture

from tin_lite import keyword_plan as v1
from tin_lite import keyword_plan_v2 as v2
from tin_lite import keyword_plan_v3 as v3
from tin_lite import keyword_plan_v4 as v4
from tin_lite import keyword_plan_v5 as v5
from tin_lite import keyword_plan_v6 as v6


@pytest.mark.parametrize("core_count", [3, 4])
@pytest.mark.parametrize("specific_count", [1, 2, 3, 4])
def test_v5_advertised_seed_counts_fit_the_provider_limit(core_count, specific_count):
    value = {
        "core": ["invoice api", "billing software", "payment webhook", "receipt software"][
            :core_count
        ],
        "specific": [
            "invoice api migration",
            "billing integration tutorial",
            "automated payment receipts",
            "invoice delivery debugging",
        ][:specific_count],
    }
    jsonschema.validate(value, v5.SCHEMAS["seeds"])
    assert v5.seed_values(value) == value["core"] + value["specific"]
    assert len(v5.seed_values(value)) <= v1.POLICY["max_seeds"]


@pytest.mark.parametrize("core_count,specific_count", [(3, 6), (4, 5), (4, 6)])
def test_v5_schema_does_not_invite_seeds_that_exceed_the_provider_limit(core_count, specific_count):
    value = {
        "core": ["invoice api", "billing software", "payment webhook", "receipt software"][
            :core_count
        ],
        "specific": [f"invoice integration task {index}" for index in range(specific_count)],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, v5.SCHEMAS["seeds"])


@pytest.mark.parametrize(
    "core",
    [[], ["booking api"], ["booking api"] * 3, ["an overly specific booking api"] * 3],
)
def test_core_lookups_require_three_distinct_short_phrases(core):
    with pytest.raises(ValueError):
        v3.seed_values({"core": core, "specific": ["consulting scheduling software"]})


def test_core_and_specific_seeds_are_deduplicated_and_use_only_explicit_context():
    assert v3.seed_values(
        {
            "core": ["scheduling software", "booking api", "calendar integration"],
            "specific": ["booking api", "consulting scheduling software"],
        }
    ) == [
        "scheduling software",
        "booking api",
        "calendar integration",
        "consulting scheduling software",
    ]
    scope = {
        "url": "https://example.com/",
        "host": "example.com",
        "market": "US",
        "buyer_context": "Booking API for consultants",
        "audit": {"findings": "An unverified capability hypothesis"},
    }
    assert v3.seed_input(scope) == {key: value for key, value in scope.items() if key != "audit"}
    assert v3.SCHEMAS["review"] == v2.SCHEMAS["review"]
    assert "Writing contract" not in v2.INSTRUCTIONS["review"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contract, version", [(v3, "v3"), (v4, "v4"), (v5, "v5"), (v6, "current")]
)
async def test_core_seed_versions_reuse_calls_and_publish_one_complete_inventory(contract, version):
    activities, db, storage, provider, model = await fixture(
        modern=version, inputs={"seed_phrases": []}
    )
    run_id = str(db.run.id)
    for _ in range(2):
        await finish(activities, run_id)
    assert storage.repo.writes == 1 and model.generate.await_count == 3
    assert [
        call.kwargs["value"]
        for call in provider.query.await_args_list
        if call.args[0] == "suggestions"
    ] == [
        "scheduling software",
        "booking api",
        "calendar integration",
        "consulting scheduling software",
    ]
    seed_request = model.generate.await_args_list[0].args[1]
    assert set(seed_request.output_schema["properties"]) == {"core", "specific"}
    assert set(json.loads(seed_request.messages[0].content)) == {
        "url",
        "host",
        "market",
        "buyer_context",
    }
    assert model.generate.await_args_list[1].args[1].system == contract.INSTRUCTIONS["triage"]
    assert model.generate.await_args_list[-1].args[1].system == contract.INSTRUCTIONS["review"]
    artifacts = await activities._result(run_id, "artifacts")
    inventory = json.loads(artifacts[v1.paths(run_id)["keywords.json"]])
    assert inventory["schema_version"] == contract.POLICY["version"]
    assert inventory["coverage"]["buyer_fit"]["direct"] > 0
