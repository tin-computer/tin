from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from pydantic import SecretStr

from tin_lite.model_providers import (
    AnthropicModelProvider,
    GeminiModelProvider,
    MessageRole,
    ModelCapability,
    ModelCapabilityError,
    ModelMessage,
    ModelObservation,
    ModelProviderError,
    ModelRequest,
    ModelRoute,
    ModelRouter,
    ModelUsage,
    OpenAIModelProvider,
    ProviderName,
    ReasoningEffort,
    configured_model_router,
    model_failure_reason,
)


def _request(*, structured: bool = False) -> ModelRequest:
    return ModelRequest(
        system="Be accurate.",
        messages=(ModelMessage(MessageRole.USER, "Return the answer."),),
        max_output_tokens=200,
        reasoning_effort=ReasoningEffort.HIGH,
        output_schema=(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            }
            if structured
            else None
        ),
    )


class _Closable:
    closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_openai_adapter_uses_responses_and_validates_json_schema() -> None:
    class Responses:
        parameters = None

        async def create(self, **parameters):
            self.parameters = parameters
            return SimpleNamespace(
                output_text='{"answer":"openai"}',
                model="gpt-test",
                id="resp_test",
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=4,
                    total_tokens=14,
                    input_tokens_details=SimpleNamespace(cached_tokens=2),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=1),
                ),
            )

    client = _Closable()
    client.responses = Responses()
    provider = OpenAIModelProvider(api_key="test", client=client)  # noqa: S106

    result = await provider.generate(model="gpt-test", request=_request(structured=True))

    assert result.provider is ProviderName.OPENAI
    assert result.parsed == {"answer": "openai"}
    assert result.usage.cached_input_tokens == 2
    assert client.responses.parameters["reasoning"] == {"effort": "high"}
    assert client.responses.parameters["text"]["format"]["strict"] is True


@pytest.mark.asyncio
async def test_anthropic_adapter_preserves_native_messages_contract() -> None:
    class Messages:
        parameters = None

        async def create(self, **parameters):
            self.parameters = parameters
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"answer":"anthropic"}')],
                model="claude-test",
                _request_id="req_test",
                usage=SimpleNamespace(
                    input_tokens=8,
                    output_tokens=5,
                    cache_creation_input_tokens=1,
                    cache_read_input_tokens=2,
                ),
            )

    client = _Closable()
    client.messages = Messages()
    provider = AnthropicModelProvider(api_key="test", client=client)  # noqa: S106

    result = await provider.generate(model="claude-test", request=_request(structured=True))

    assert result.provider is ProviderName.ANTHROPIC
    assert result.parsed == {"answer": "anthropic"}
    assert result.usage.cached_input_tokens == 2
    assert result.usage.cache_write_input_tokens == 1
    assert result.usage.input_tokens == 11
    assert result.usage.total_tokens == 16
    assert client.messages.parameters["output_config"] == {
        "effort": "high",
        "format": {
            "type": "json_schema",
            "schema": _request(structured=True).output_schema,
        },
    }


@pytest.mark.asyncio
async def test_gemini_adapter_uses_async_official_client() -> None:
    class Models:
        parameters = None

        async def generate_content(self, **parameters):
            self.parameters = parameters
            return SimpleNamespace(
                text='{"answer":"gemini"}',
                model_version="gemini-test",
                response_id="gemini-request",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=7,
                    candidates_token_count=3,
                    total_token_count=11,
                    cached_content_token_count=1,
                    thoughts_token_count=1,
                ),
            )

    class AsyncClient:
        models = Models()
        closed = False

        async def aclose(self):
            self.closed = True

    client = SimpleNamespace(aio=AsyncClient())
    provider = GeminiModelProvider(api_key="test", client=client)  # noqa: S106

    result = await provider.generate(model="gemini-test", request=_request(structured=True))

    assert result.provider is ProviderName.GEMINI
    assert result.parsed == {"answer": "gemini"}
    assert result.usage.reasoning_tokens == 1
    config = client.aio.models.parameters["config"]
    assert config.thinking_config.thinking_level.value == "HIGH"
    assert config.response_mime_type == "application/json"


@pytest.mark.asyncio
async def test_router_uses_only_explicit_route_and_capabilities() -> None:
    class Provider:
        name = ProviderName.OPENAI
        capabilities = frozenset({ModelCapability.TEXT})
        calls = []

        async def generate(self, *, model, request):
            self.calls.append((model, request))
            return SimpleNamespace(provider=self.name, model=model)

        async def close(self):
            pass

    provider = Provider()
    router = ModelRouter(
        providers={ProviderName.OPENAI: provider},
        routes=(
            ModelRoute(
                key="weekly-summary",
                provider=ProviderName.OPENAI,
                model="not-a-prefixed-model-name",
                capabilities=frozenset({ModelCapability.TEXT}),
            ),
        ),
    )

    await router.generate(
        "weekly-summary",
        ModelRequest(messages=(ModelMessage(MessageRole.USER, "Summarize."),)),
    )
    assert provider.calls[0][0] == "not-a-prefixed-model-name"
    with pytest.raises(LookupError, match="not registered"):
        await router.generate("gpt-guessed", _request())
    with pytest.raises(ModelCapabilityError, match="json_schema"):
        await router.generate("weekly-summary", _request(structured=True))


@pytest.mark.asyncio
async def test_switchboard_configuration_reuses_luna_key_for_openai_adapter() -> None:
    settings = SimpleNamespace(
        luna_api_key=SecretStr("openai-test"),
        luna_base_url="https://api.openai.test/v1",
        luna_timeout_seconds=30,
        anthropic_api_key=SecretStr("anthropic-test"),
        anthropic_workspace_id="wrkspc_test",
        gemini_api_key=SecretStr("gemini-test"),
        openrouter_api_key=SecretStr("openrouter-test"),
    )

    router = configured_model_router(settings)  # type: ignore[arg-type]

    assert router.configured_providers == frozenset(
        {ProviderName.OPENAI, ProviderName.ANTHROPIC, ProviderName.GEMINI, ProviderName.OPENROUTER}
    )
    with pytest.raises(LookupError, match="not registered"):
        await router.generate("vendor/model", _request())
    await router.close()


@pytest.mark.parametrize("provider_name", ["openai", "anthropic", "gemini"])
async def test_direct_provider_rejected_output_keeps_usage(provider_name):
    async def create(**kwargs):
        return SimpleNamespace(
            output_text="not JSON",
            text="not JSON",
            content=[SimpleNamespace(type="text", text="not JSON")],
            model="test-model",
            id="response-test",
            _request_id="request-test",
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=3,
                total_tokens=13,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            usage_metadata=SimpleNamespace(total_token_count=13),
        )

    api = SimpleNamespace(create=create)
    client = SimpleNamespace(
        responses=api,
        messages=api,
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=create)),
    )
    provider_class = {
        "openai": OpenAIModelProvider,
        "anthropic": AnthropicModelProvider,
        "gemini": GeminiModelProvider,
    }[provider_name]
    provider = provider_class(api_key="fake", client=client)  # noqa: S106
    with pytest.raises(ModelProviderError) as error:
        await provider.generate(model="test-model", request=_request(structured=True))
    assert error.value.observation.usage.total_tokens == 13
    assert error.value.observation.provider.value == provider_name


@pytest.mark.asyncio
async def test_router_passes_a_per_call_timeout_without_changing_the_request() -> None:
    class Provider:
        name = ProviderName.OPENAI
        capabilities = frozenset({ModelCapability.TEXT})
        calls = []

        async def generate(self, *, model, request, **options):
            self.calls.append((request, options))
            return SimpleNamespace(provider=self.name, model=model)

        async def close(self):
            pass

    class Recorder:
        requests = []

        async def generate(self, route, request, call):
            self.requests.append(asdict(request))
            return await call()

    provider, recorder = Provider(), Recorder()
    router = ModelRouter(
        providers={ProviderName.OPENAI: provider},
        routes=(
            ModelRoute(
                key="review",
                provider=ProviderName.OPENAI,
                model="test-model",
                capabilities=frozenset({ModelCapability.TEXT}),
            ),
        ),
        recorder=recorder,
    )
    request = ModelRequest(messages=(ModelMessage(MessageRole.USER, "Review."),))

    await router.generate("review", request)
    await router.generate("review", request, timeout_seconds=420)

    assert [options for _, options in provider.calls] == [{}, {"timeout_seconds": 420}]
    # The wait is not part of the request, so request fingerprints stay what they were.
    assert recorder.requests[0] == recorder.requests[1]
    assert set(recorder.requests[0]) == {
        "messages",
        "system",
        "max_output_tokens",
        "temperature",
        "reasoning_effort",
        "output_schema",
        "output_schema_name",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [None, 420])
async def test_sdk_adapters_apply_a_per_call_timeout_only_when_given(wait) -> None:
    class Endpoint:
        def __init__(self, response):
            self.response, self.parameters = response, None

        async def create(self, **parameters):
            self.parameters = parameters
            return self.response

    class Client(_Closable):
        def __init__(self, **endpoints):
            self.options = []
            for name, endpoint in endpoints.items():
                setattr(self, name, endpoint)

        def with_options(self, **options):
            self.options.append(options)
            return self

    usage = SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)
    openai = Client(responses=Endpoint(SimpleNamespace(output_text="ok", usage=usage)))
    anthropic = Client(
        messages=Endpoint(SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")]))
    )
    request = ModelRequest(messages=(ModelMessage(MessageRole.USER, "Answer."),))

    await OpenAIModelProvider(api_key="test", client=openai).generate(  # noqa: S106
        model="m", request=request, timeout_seconds=wait
    )
    await AnthropicModelProvider(api_key="test", client=anthropic).generate(  # noqa: S106
        model="m", request=request, timeout_seconds=wait
    )

    expected = [] if wait is None else [{"timeout": wait}]
    assert openai.options == expected and anthropic.options == expected


@pytest.mark.asyncio
async def test_gemini_adapter_sets_a_per_call_timeout_in_milliseconds() -> None:
    class Models:
        parameters = None

        async def generate_content(self, **parameters):
            self.parameters = parameters
            return SimpleNamespace(text="ok", usage_metadata=None)

    client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    provider = GeminiModelProvider(api_key="test", client=client)  # noqa: S106

    await provider.generate(
        model="gemini-test",
        request=ModelRequest(messages=(ModelMessage(MessageRole.USER, "Answer."),)),
        timeout_seconds=420,
    )

    assert client.aio.models.parameters["config"].http_options.timeout == 420_000


def _wrapped(error: BaseException) -> ModelProviderError:
    try:
        raise ModelProviderError("OpenAI model request failed") from error
    except ModelProviderError as exc:
        return exc


_HTTP = httpx.Request("POST", "https://models.invalid/v1/responses")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("secret"), "provider_timeout"),
        (_wrapped(openai.APITimeoutError(request=_HTTP)), "provider_timeout"),
        (_wrapped(anthropic.APITimeoutError(request=_HTTP)), "provider_timeout"),
        (_wrapped(httpx.ReadTimeout("secret", request=_HTTP)), "provider_timeout"),
        (
            _wrapped(
                openai.RateLimitError(
                    "secret body", response=httpx.Response(429, request=_HTTP), body=None
                )
            ),
            "provider_status_429",
        ),
        (
            _wrapped(
                anthropic.BadRequestError(
                    "secret body", response=httpx.Response(400, request=_HTTP), body=None
                )
            ),
            "provider_status_400",
        ),
        (_wrapped(openai.APIConnectionError(request=_HTTP)), "provider_connection"),
        (
            ModelProviderError(
                "invalid structured output",
                observation=ModelObservation(ProviderName.OPENAI, "m", None, ModelUsage()),
            ),
            "invalid_result",
        ),
        (ValueError("Keyword model result must be a JSON object."), "invalid_result"),
        (ModelCapabilityError("secret"), "route_unavailable"),
        (LookupError("secret"), "route_unavailable"),
        (_wrapped(RuntimeError("secret")), "provider_result_unavailable"),
        (RuntimeError("secret"), "provider_result_unavailable"),
    ],
)
def test_failure_reason_is_a_fixed_label_never_provider_text(error, reason) -> None:
    assert model_failure_reason(error) == reason


def test_defaults_are_runaway_guards_not_expected_lengths() -> None:
    from tin_lite.model_providers import DEFAULT_TIMEOUT_SECONDS, OpenRouterModelProvider
    from tin_lite.settings import Settings

    request = ModelRequest(messages=(ModelMessage(MessageRole.USER, "hi"),))
    assert request.max_output_tokens == 32_000
    assert DEFAULT_TIMEOUT_SECONDS == 600
    assert Settings.model_fields["luna_timeout_seconds"].default == 600
    for provider_class in (OpenAIModelProvider, AnthropicModelProvider, OpenRouterModelProvider):
        provider = provider_class(api_key="test")  # noqa: S106
        assert provider._client.timeout == 600


def _openai_reply(*, text, output_tokens, status=None, reason=None):
    async def create(**parameters):
        return SimpleNamespace(
            output_text=text,
            status=status,
            incomplete_details=SimpleNamespace(reason=reason) if reason else None,
            model="gpt-test",
            id="resp_test",
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=output_tokens,
                total_tokens=10 + output_tokens,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
                output_tokens_details=SimpleNamespace(reasoning_tokens=output_tokens),
            ),
        )

    client = _Closable()
    client.responses = SimpleNamespace(create=create)
    return OpenAIModelProvider(api_key="test", client=client)  # noqa: S106


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply, reason, stop, truncated",
    [
        # Reasoning used the whole allowance before any text; OpenAI says so.
        (
            dict(text="", output_tokens=200, status="incomplete", reason="max_output_tokens"),
            "output_truncated",
            "max_output_tokens",
            True,
        ),
        # No status, but an empty answer at the cap was cut off too.
        (dict(text="", output_tokens=200), "output_truncated", None, None),
        # An empty answer well below the cap is a bad answer, not a cut-off one.
        (dict(text="", output_tokens=20, status="completed"), "invalid_result", "completed", False),
    ],
)
async def test_an_empty_answer_at_its_cap_is_cut_off(reply, reason, stop, truncated) -> None:
    provider = _openai_reply(**reply)
    with pytest.raises(ModelProviderError) as failed:
        await provider.generate(model="gpt-test", request=_request(structured=True))
    assert model_failure_reason(failed.value) == reason
    observation = failed.value.observation
    assert observation.usage.output_tokens == reply["output_tokens"]
    assert observation.stop_reason == stop and observation.output_truncated is truncated


@pytest.mark.asyncio
async def test_openai_result_keeps_its_stop_signal() -> None:
    provider = _openai_reply(text='{"answer":"ok"}', output_tokens=4, status="completed")
    result = await provider.generate(model="gpt-test", request=_request(structured=True))
    assert result.stop_reason == "completed" and result.output_truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop, text, expected",
    [
        ("end_turn", '{"answer":"ok"}', None),
        ("max_tokens", "", "output_truncated"),
        ("max_tokens", '{"answer":"o', "output_truncated"),
    ],
)
async def test_anthropic_stop_reason_is_kept_and_its_cap_names_a_cut_off(
    stop, text, expected
) -> None:
    async def create(**parameters):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)] if text else [],
            stop_reason=stop,
            model="claude-test",
            _request_id="req_test",
            usage=SimpleNamespace(
                input_tokens=8,
                output_tokens=150,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )

    client = _Closable()
    client.messages = SimpleNamespace(create=create)
    provider = AnthropicModelProvider(api_key="test", client=client)  # noqa: S106
    if expected is None:
        result = await provider.generate(model="claude-test", request=_request(structured=True))
        assert result.stop_reason == "end_turn" and result.output_truncated is False
        return
    with pytest.raises(ModelProviderError) as failed:
        await provider.generate(model="claude-test", request=_request(structured=True))
    assert model_failure_reason(failed.value) == expected
    assert failed.value.observation.stop_reason == "max_tokens"
    assert failed.value.observation.output_truncated is True


@pytest.mark.asyncio
async def test_anthropic_plain_text_at_its_cap_is_still_returned() -> None:
    """Text without a schema keeps its partial answer, as before; the result says it was cut."""

    async def create(**parameters):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="A partial answer")],
            stop_reason="max_tokens",
            model="claude-test",
            _request_id="req_test",
            usage=SimpleNamespace(
                input_tokens=8,
                output_tokens=200,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )

    client = _Closable()
    client.messages = SimpleNamespace(create=create)
    provider = AnthropicModelProvider(api_key="test", client=client)  # noqa: S106
    result = await provider.generate(model="claude-test", request=_request())
    assert result.text == "A partial answer" and result.output_truncated is True


@pytest.mark.asyncio
async def test_gemini_finish_reason_is_kept() -> None:
    async def generate_content(**parameters):
        return SimpleNamespace(
            text="",
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="MAX_TOKENS"))],
            model_version="gemini-test",
            response_id="gemini-response",
            usage_metadata=SimpleNamespace(
                prompt_token_count=5, candidates_token_count=0, total_token_count=205
            ),
        )

    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    provider = GeminiModelProvider(api_key="test", client=client)  # noqa: S106
    with pytest.raises(ModelProviderError) as failed:
        await provider.generate(model="gemini-test", request=_request(structured=True))
    assert model_failure_reason(failed.value) == "output_truncated"
    assert failed.value.observation.stop_reason == "MAX_TOKENS"
    assert failed.value.observation.output_truncated is True
