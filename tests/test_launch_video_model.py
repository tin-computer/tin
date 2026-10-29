from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tin_lite.launch_video import (
    MAX_REQUEST_BYTES,
    MODEL_ROUTE,
    ROUTE_KEY,
    LaunchVideoError,
    LaunchVideoGenerator,
)

HTML = "<!doctype html><html><body><script>" + (" " * 220) + "</script></body></html>"
INPUTS = {"sound_mode": "music", "feature_moment": "Translate one existing product button"}


def fixture(**changes):
    draft = {"html": HTML, "story": "One command translates the same button.", "narration": ""}
    draft.update(changes)
    router = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(parsed=draft)))
    return LaunchVideoGenerator(router=router), router


async def test_direct_anthropic_route_preserves_brief_and_measured_cues():
    generator, router = fixture()
    draft = await generator.generate(
        inputs=INPUTS, context={"memory": "Real feature"}, cues={"onset_times": [0.5, 1.2]}
    )
    assert draft["html"] == HTML
    route, request = router.generate.await_args.args
    assert route == ROUTE_KEY and MODEL_ROUTE.provider == "anthropic"
    assert MODEL_ROUTE.model == "claude-opus-5-5"
    assert request.max_output_tokens == 24_000
    assert "Real feature" in request.messages[0].content
    assert "onset_times" in request.messages[0].content
    assert "untrusted source data" in request.system
    assert "ANTHROPIC_API_KEY" not in request.messages[0].content


@pytest.mark.parametrize(
    "change",
    [
        {"html": ""},
        {"html": "x" * 80100},
        {"html": " " * 100 + HTML},
        {"html": "<!doctype html>too short"},
        {"html": 7},
        {"story": ""},
        {"narration": "Invented voice in a music film"},
        {"story": None},
    ],
)
async def test_plausible_but_unusable_outputs_are_rejected(change):
    generator, router = fixture(**change)
    with pytest.raises(LaunchVideoError, match="usable local film|selected sound"):
        await generator.generate(inputs=INPUTS, context={}, cues={})
    assert router.generate.await_count == 1


async def test_narrated_film_must_have_real_text_and_preserves_repair_findings():
    generator, router = fixture(narration="Translate this button with one command.")
    await generator.generate(
        inputs={**INPUTS, "sound_mode": "voice_and_music"},
        context={},
        cues={},
        previous={"html": HTML},
        problems=["overflow at 1.0s"],
    )
    assert "overflow at 1.0s" in router.generate.await_args.args[1].messages[0].content
    generator, _ = fixture()
    with pytest.raises(LaunchVideoError, match="selected sound"):
        await generator.generate(inputs={"sound_mode": "voice_and_music"}, context={}, cues={})


async def test_oversized_request_is_refused_before_purchasing_a_call():
    generator, router = fixture()
    with pytest.raises(LaunchVideoError, match="request limit"):
        await generator.generate(
            inputs=INPUTS, context={"memory": "x" * MAX_REQUEST_BYTES}, cues={}
        )
    router.generate.assert_not_awaited()


async def test_confirmed_bad_response_can_repair_but_unknown_call_cannot():
    from tin_lite.launch_video import LaunchVideoOutputError
    from tin_lite.model_providers import (
        ModelObservation,
        ModelProviderError,
        ModelUsage,
        ProviderName,
    )

    generator, router = fixture()
    observed = ModelObservation(
        provider=ProviderName.ANTHROPIC,
        model=MODEL_ROUTE.model,
        request_id="observed-response",
        usage=ModelUsage(input_tokens=10, output_tokens=24_000, total_tokens=24_010),
        stop_reason="max_tokens",
    )
    router.generate.side_effect = ModelProviderError("Invalid output", observation=observed)
    with pytest.raises(LaunchVideoOutputError, match="completed model response"):
        await generator.generate(inputs=INPUTS, context={}, cues={})
    router.generate.side_effect = ModelProviderError("Request unconfirmed")
    with pytest.raises(ModelProviderError, match="unconfirmed"):
        await generator.generate(inputs=INPUTS, context={}, cues={})
