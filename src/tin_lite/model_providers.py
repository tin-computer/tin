from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
import jsonschema
from anthropic import AnthropicError, AsyncAnthropic
from anthropic import APIConnectionError as AnthropicConnectionError
from anthropic import APIStatusError as AnthropicStatusError
from anthropic import APITimeoutError as AnthropicTimeoutError
from google import genai
from google.genai import errors as google_errors
from google.genai import types as google_types
from openai import APIConnectionError as OpenAIConnectionError
from openai import APIError as OpenAIError
from openai import APIStatusError as OpenAIStatusError
from openai import APITimeoutError as OpenAITimeoutError
from openai import AsyncOpenAI

from tin_lite.settings import Settings


class ProviderName(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"


class ModelCapability(StrEnum):
    TEXT = "text"
    JSON_SCHEMA = "json_schema"
    REASONING_EFFORT = "reasoning_effort"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ModelProviderError(RuntimeError):
    """A provider call failed without exposing provider or credential details."""

    def __init__(self, message: str, *, observation: ModelObservation | None = None) -> None:
        super().__init__(message)
        self.observation = observation


class ModelOutputTruncated(ModelProviderError):
    """The response stopped at its output-token cap, so its structured output is incomplete.

    Raised when the provider says so (an OpenAI response `incomplete` for
    `max_output_tokens`) or when structured output does not parse and the response used its
    whole output allowance (or did not report its usage).
    """


class ModelCapabilityError(ValueError):
    """A route cannot satisfy the workflow's declared model contract."""


@dataclass(frozen=True)
class ModelMessage:
    role: MessageRole
    content: str


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    system: str | None = None
    max_output_tokens: int = 4096
    temperature: float | None = None
    reasoning_effort: ReasoningEffort | None = None
    output_schema: dict[str, Any] | None = None
    output_schema_name: str = "workflow_output"

    def required_capabilities(self) -> frozenset[ModelCapability]:
        required = {ModelCapability.TEXT}
        if self.output_schema is not None:
            required.add(ModelCapability.JSON_SCHEMA)
        if self.reasoning_effort is not None:
            required.add(ModelCapability.REASONING_EFFORT)
        return frozenset(required)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_write_input_tokens: int | None = None


@dataclass(frozen=True)
class ModelObservation:
    """Provider-reported metadata, available even when output validation fails."""

    provider: ProviderName
    model: str
    request_id: str | None
    usage: ModelUsage
    service_tier: str | None = None


@dataclass(frozen=True)
class ModelResult:
    provider: ProviderName
    model: str
    text: str
    parsed: Any | None
    request_id: str | None
    usage: ModelUsage
    service_tier: str | None = None


@dataclass(frozen=True)
class ModelRoute:
    key: str
    provider: ProviderName
    model: str
    capabilities: frozenset[ModelCapability]


class ModelProvider(Protocol):
    name: ProviderName
    capabilities: frozenset[ModelCapability]

    async def generate(
        self, *, model: str, request: ModelRequest, timeout_seconds: float | None = None
    ) -> ModelResult: ...

    async def close(self) -> None: ...


class ModelCallRecorder(Protocol):
    async def generate(
        self,
        route: ModelRoute,
        request: ModelRequest,
        call: Callable[[], Awaitable[ModelResult]],
    ) -> ModelResult: ...


class ModelRouter:
    """Selects explicit routes; provider or model names are never inferred from strings."""

    def __init__(
        self,
        *,
        providers: dict[ProviderName, ModelProvider],
        routes: tuple[ModelRoute, ...] = (),
        recorder: ModelCallRecorder | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._recorder = recorder
        self._routes: dict[str, ModelRoute] = {}
        for route in routes:
            if not route.key or route.key in self._routes:
                raise ValueError("model route keys must be non-empty and unique")
            provider = self._providers.get(route.provider)
            if provider is None:
                raise ValueError(f"model route {route.key!r} has no configured provider")
            if not route.capabilities.issubset(provider.capabilities):
                raise ValueError(f"model route {route.key!r} overstates provider capabilities")
            self._routes[route.key] = route

    @property
    def configured_providers(self) -> frozenset[ProviderName]:
        return frozenset(self._providers)

    async def generate(
        self, route_key: str, request: ModelRequest, *, timeout_seconds: float | None = None
    ) -> ModelResult:
        """`timeout_seconds` overrides the provider's default wait for this call only. It is
        not part of the request, so it never changes a request fingerprint."""
        route = self._routes.get(route_key)
        if route is None:
            raise LookupError(f"model route {route_key!r} is not registered")
        missing = request.required_capabilities() - route.capabilities
        if missing:
            labels = ", ".join(sorted(item.value for item in missing))
            raise ModelCapabilityError(
                f"model route {route.key!r} cannot satisfy required capabilities: {labels}"
            )
        _validate_request(request)

        async def call() -> ModelResult:
            provider = self._providers[route.provider]
            if timeout_seconds is None:
                return await provider.generate(model=route.model, request=request)
            return await provider.generate(
                model=route.model, request=request, timeout_seconds=timeout_seconds
            )

        if self._recorder is not None:
            return await self._recorder.generate(route, request, call)
        return await call()

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()


_COMMON_CAPABILITIES = frozenset(
    {
        ModelCapability.TEXT,
        ModelCapability.JSON_SCHEMA,
        ModelCapability.REASONING_EFFORT,
    }
)


class OpenAIModelProvider:
    name = ProviderName.OPENAI
    capabilities = _COMMON_CAPABILITIES

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 90,
        client: Any | None = None,
    ) -> None:
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def generate(
        self, *, model: str, request: ModelRequest, timeout_seconds: float | None = None
    ) -> ModelResult:
        _validate_request(request)
        parameters: dict[str, Any] = {
            "model": model,
            "service_tier": "default",
            "input": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        if request.system is not None:
            parameters["instructions"] = request.system
        if request.temperature is not None:
            parameters["temperature"] = request.temperature
        if request.reasoning_effort is not None:
            parameters["reasoning"] = {"effort": request.reasoning_effort.value}
        if request.output_schema is not None:
            parameters["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.output_schema_name,
                    "schema": request.output_schema,
                    "strict": True,
                }
            }
        try:
            client = (
                self._client
                if timeout_seconds is None
                else self._client.with_options(timeout=timeout_seconds)
            )
            response = await client.responses.create(**parameters)
        except OpenAIError as exc:
            raise ModelProviderError("OpenAI model request failed") from exc
        text = str(response.output_text or "").strip()
        usage = getattr(response, "usage", None)
        output_details = getattr(usage, "output_tokens_details", None)
        input_details = getattr(usage, "input_tokens_details", None)
        incomplete = getattr(getattr(response, "incomplete_details", None), "reason", None)
        return _result(
            provider=self.name,
            model=str(getattr(response, "model", model)),
            text=text,
            request_id=_optional_string(getattr(response, "id", None)),
            service_tier=_optional_string(getattr(response, "service_tier", None)),
            request=request,
            truncated=getattr(response, "status", None) == "incomplete"
            and incomplete == "max_output_tokens",
            usage=ModelUsage(
                input_tokens=_optional_int(getattr(usage, "input_tokens", None)),
                output_tokens=_optional_int(getattr(usage, "output_tokens", None)),
                total_tokens=_optional_int(getattr(usage, "total_tokens", None)),
                cached_input_tokens=_optional_int(getattr(input_details, "cached_tokens", None)),
                cache_write_input_tokens=_optional_int(
                    getattr(input_details, "cache_write_tokens", None)
                ),
                reasoning_tokens=_optional_int(getattr(output_details, "reasoning_tokens", None)),
            ),
        )

    async def close(self) -> None:
        await self._client.close()


class AnthropicModelProvider:
    name = ProviderName.ANTHROPIC
    capabilities = _COMMON_CAPABILITIES

    def __init__(
        self,
        *,
        api_key: str,
        workspace_id: str | None = None,
        timeout_seconds: float = 90,
        client: Any | None = None,
    ) -> None:
        client_options: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_seconds,
            "max_retries": 0,
        }
        if workspace_id is not None:
            client_options["default_headers"] = {"anthropic-workspace-id": workspace_id}
        self._client = client or AsyncAnthropic(**client_options)

    async def generate(
        self, *, model: str, request: ModelRequest, timeout_seconds: float | None = None
    ) -> ModelResult:
        _validate_request(request)
        parameters: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
        }
        if request.system is not None:
            parameters["system"] = request.system
        if request.temperature is not None:
            parameters["temperature"] = request.temperature
        output_config: dict[str, Any] = {}
        if request.reasoning_effort is not None:
            output_config["effort"] = request.reasoning_effort.value
        if request.output_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": request.output_schema,
            }
        if output_config:
            parameters["output_config"] = output_config
        try:
            client = (
                self._client
                if timeout_seconds is None
                else self._client.with_options(timeout=timeout_seconds)
            )
            response = await client.messages.create(**parameters)
        except AnthropicError as exc:
            raise ModelProviderError("Anthropic model request failed") from exc
        text = "".join(
            str(block.text) for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        usage = getattr(response, "usage", None)
        cache_write_tokens = _optional_int(getattr(usage, "cache_creation_input_tokens", None))
        cache_read_tokens = _optional_int(getattr(usage, "cache_read_input_tokens", None))
        # Anthropic reports uncached input separately; the shared input total includes cache.
        input_tokens = _sum_optional_ints(
            getattr(usage, "input_tokens", None), cache_write_tokens, cache_read_tokens
        )
        output_tokens = _optional_int(getattr(usage, "output_tokens", None))
        return _result(
            provider=self.name,
            model=str(getattr(response, "model", model)),
            text=text,
            request_id=_optional_string(getattr(response, "_request_id", None)),
            request=request,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=_sum_optional_ints(input_tokens, output_tokens),
                cached_input_tokens=cache_read_tokens,
                cache_write_input_tokens=cache_write_tokens,
            ),
        )

    async def close(self) -> None:
        await self._client.close()


class GeminiModelProvider:
    name = ProviderName.GEMINI
    capabilities = _COMMON_CAPABILITIES

    def __init__(self, *, api_key: str, client: Any | None = None) -> None:
        self._client = client or genai.Client(
            api_key=api_key,
            http_options=google_types.HttpOptions(
                api_version="v1", retry_options=google_types.HttpRetryOptions(attempts=1)
            ),
        )

    async def generate(
        self, *, model: str, request: ModelRequest, timeout_seconds: float | None = None
    ) -> ModelResult:
        _validate_request(request)
        config: dict[str, Any] = {"max_output_tokens": request.max_output_tokens}
        if timeout_seconds is not None:
            config["http_options"] = google_types.HttpOptions(timeout=int(timeout_seconds * 1000))
        if request.system is not None:
            config["system_instruction"] = request.system
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if request.reasoning_effort is not None:
            config["thinking_config"] = {"thinking_level": request.reasoning_effort.value.upper()}
        if request.output_schema is not None:
            config.update(
                {
                    "response_mime_type": "application/json",
                    "response_json_schema": request.output_schema,
                }
            )
        contents = [
            google_types.Content(
                role="model" if message.role is MessageRole.ASSISTANT else "user",
                parts=[google_types.Part.from_text(text=message.content)],
            )
            for message in request.messages
        ]
        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=google_types.GenerateContentConfig(**config),
            )
        except google_errors.APIError as exc:
            raise ModelProviderError("Gemini model request failed") from exc
        text = str(response.text or "").strip()
        usage = getattr(response, "usage_metadata", None)
        return _result(
            provider=self.name,
            model=str(getattr(response, "model_version", None) or model),
            text=text,
            request_id=_optional_string(getattr(response, "response_id", None)),
            request=request,
            usage=ModelUsage(
                input_tokens=_optional_int(getattr(usage, "prompt_token_count", None)),
                output_tokens=_optional_int(getattr(usage, "candidates_token_count", None)),
                total_tokens=_optional_int(getattr(usage, "total_token_count", None)),
                cached_input_tokens=_optional_int(
                    getattr(usage, "cached_content_token_count", None)
                ),
                reasoning_tokens=_optional_int(getattr(usage, "thoughts_token_count", None)),
            ),
        )

    async def close(self) -> None:
        await self._client.aio.aclose()


class OpenRouterModelProvider:
    """OpenRouter is an explicit service, not a fallback for the direct OpenAI adapter."""

    name = ProviderName.OPENROUTER
    capabilities = _COMMON_CAPABILITIES

    def __init__(
        self, *, api_key: str, timeout_seconds: float = 90, client: Any | None = None
    ) -> None:
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def generate(
        self, *, model: str, request: ModelRequest, timeout_seconds: float | None = None
    ) -> ModelResult:
        _validate_request(request)
        messages = [{"role": m.role.value, "content": m.content} for m in request.messages]
        if request.system is not None:
            messages.insert(0, {"role": "system", "content": request.system})
        extra: dict[str, Any] = {"provider": {"require_parameters": True, "allow_fallbacks": False}}
        parameters: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "stream": False,
            "extra_body": extra,
        }
        if request.temperature is not None:
            parameters["temperature"] = request.temperature
        if request.reasoning_effort is not None:
            extra["reasoning"] = {"effort": request.reasoning_effort.value}
        if request.output_schema is not None:
            parameters["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.output_schema_name,
                    "schema": request.output_schema,
                    "strict": True,
                },
            }
        try:
            client = (
                self._client
                if timeout_seconds is None
                else self._client.with_options(timeout=timeout_seconds)
            )
            response = await client.chat.completions.create(**parameters)
        except OpenAIError as exc:
            raise ModelProviderError("OpenRouter model request failed") from exc
        choices = getattr(response, "choices", None) or []
        content = getattr(choices[0].message, "content", None) if choices else None
        usage = getattr(response, "usage", None)
        input_details = getattr(usage, "prompt_tokens_details", None)
        output_details = getattr(usage, "completion_tokens_details", None)
        return _result(
            provider=self.name,
            model=str(getattr(response, "model", None) or model),
            text=content.strip() if isinstance(content, str) else "",
            request_id=_optional_string(getattr(response, "id", None)),
            request=request,
            usage=ModelUsage(
                input_tokens=_optional_int(getattr(usage, "prompt_tokens", None)),
                output_tokens=_optional_int(getattr(usage, "completion_tokens", None)),
                total_tokens=_optional_int(getattr(usage, "total_tokens", None)),
                cached_input_tokens=_optional_int(getattr(input_details, "cached_tokens", None)),
                cache_write_input_tokens=_optional_int(
                    getattr(input_details, "cache_write_tokens", None)
                ),
                reasoning_tokens=_optional_int(getattr(output_details, "reasoning_tokens", None)),
            ),
        )

    async def close(self) -> None:
        await self._client.close()


def configured_model_router(
    settings: Settings,
    *,
    routes: tuple[ModelRoute, ...] = (),
    recorder: ModelCallRecorder | None = None,
) -> ModelRouter:
    """Build trusted-switchboard adapters without inventing model routes."""
    providers: dict[ProviderName, ModelProvider] = {}
    if settings.luna_api_key is not None:
        providers[ProviderName.OPENAI] = OpenAIModelProvider(
            api_key=settings.luna_api_key.get_secret_value(),
            base_url=settings.luna_base_url,
            timeout_seconds=settings.luna_timeout_seconds,
        )
    if settings.anthropic_api_key is not None:
        providers[ProviderName.ANTHROPIC] = AnthropicModelProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            workspace_id=settings.anthropic_workspace_id,
            timeout_seconds=settings.luna_timeout_seconds,
        )
    if settings.gemini_api_key is not None:
        providers[ProviderName.GEMINI] = GeminiModelProvider(
            api_key=settings.gemini_api_key.get_secret_value()
        )
    if settings.openrouter_api_key is not None:
        providers[ProviderName.OPENROUTER] = OpenRouterModelProvider(
            api_key=settings.openrouter_api_key.get_secret_value(),
            timeout_seconds=settings.luna_timeout_seconds,
        )
    return ModelRouter(providers=providers, routes=routes, recorder=recorder)


def model_failure_reason(exc: BaseException) -> str:
    """A fixed label for why a model call produced no usable result.

    Only exception classes and an integer status code are read, never a message, header or
    body, so the label is safe to keep as evidence. Provider errors wrap the SDK error that
    caused them, so the cause chain is read too.
    """
    seen: BaseException | None = exc
    for _ in range(4):
        if seen is None:
            break
        if isinstance(
            seen, TimeoutError | OpenAITimeoutError | AnthropicTimeoutError | httpx.TimeoutException
        ):
            return "provider_timeout"
        status = (
            seen.status_code
            if isinstance(seen, OpenAIStatusError | AnthropicStatusError)
            else seen.code
            if isinstance(seen, google_errors.APIError)
            else None
        )
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            return f"provider_status_{status}"
        if isinstance(
            seen, OpenAIConnectionError | AnthropicConnectionError | httpx.TransportError
        ):
            return "provider_connection"
        if isinstance(seen, LookupError | ModelCapabilityError):
            return "route_unavailable"
        if isinstance(seen, ModelOutputTruncated):
            return "output_truncated"
        if isinstance(seen, ModelProviderError) and seen.observation is not None:
            return "invalid_result"
        if isinstance(seen, ValueError):
            return "invalid_result"
        seen = seen.__cause__
    return "provider_result_unavailable"


def _validate_request(request: ModelRequest) -> None:
    if not request.messages or any(not message.content.strip() for message in request.messages):
        raise ValueError("model request requires non-empty messages")
    if request.max_output_tokens < 1:
        raise ValueError("model request max_output_tokens must be positive")
    if request.temperature is not None and not 0 <= request.temperature <= 2:
        raise ValueError("model request temperature must be between 0 and 2")
    if request.output_schema is not None:
        jsonschema.Draft202012Validator.check_schema(request.output_schema)


def _result(
    *,
    provider: ProviderName,
    model: str,
    text: str,
    request_id: str | None,
    request: ModelRequest,
    usage: ModelUsage,
    service_tier: str | None = None,
    truncated: bool = False,
) -> ModelResult:
    observation = ModelObservation(
        provider=provider,
        model=model,
        request_id=request_id,
        usage=usage,
        service_tier=service_tier,
    )
    if truncated:
        raise ModelOutputTruncated(
            f"{provider.value} model stopped at its output-token cap", observation=observation
        )
    if not text:
        raise ModelProviderError(
            f"{provider.value} model returned no text", observation=observation
        )
    parsed: Any | None = None
    if request.output_schema is not None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            # Strict structured output only fails to parse when it was cut off.
            if usage.output_tokens is None or usage.output_tokens >= request.max_output_tokens:
                raise ModelOutputTruncated(
                    f"{provider.value} model output stopped before its JSON was complete",
                    observation=observation,
                ) from exc
            raise ModelProviderError(
                f"{provider.value} model returned invalid structured output",
                observation=observation,
            ) from exc
        try:
            jsonschema.validate(parsed, request.output_schema)
        except jsonschema.ValidationError as exc:
            raise ModelProviderError(
                f"{provider.value} model returned invalid structured output",
                observation=observation,
            ) from exc
    return ModelResult(
        provider=provider,
        model=model,
        text=text,
        parsed=parsed,
        request_id=request_id,
        usage=usage,
        service_tier=service_tier,
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _sum_optional_ints(*values: object) -> int | None:
    parsed = [_optional_int(value) for value in values]
    return sum(parsed) if all(value is not None for value in parsed) else None
