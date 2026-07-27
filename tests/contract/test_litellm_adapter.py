import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from litellm import ModelResponse

from trade_agent.adapters.llm.litellm import LiteLLMClient, LiteLLMRouteConfig
from trade_agent.core.llm import (
    LLMError,
    LLMErrorCode,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ModelEndpoint,
    ModelRoute,
)
from trade_agent.core.llm.structured import ValidatedLLMClient
from trade_agent.core.testing import FakeLLMClient


def _route(**overrides: Any) -> LiteLLMRouteConfig:
    values: dict[str, Any] = {
        "logical_route": "research_summarizer",
        "endpoint": ModelEndpoint(provider="openai", model="test-model"),
        "allowed_providers": frozenset({"openai"}),
        "timeout_seconds": 2,
        "max_tokens": 100,
        "concurrency_limit": 4,
        "max_attempts": 2,
    }
    values.update(overrides)
    return LiteLLMRouteConfig(**values)


def _request(*, structured: bool = False) -> LLMRequest:
    return LLMRequest(
        route=ModelRoute("research_summarizer"),
        messages=(LLMMessage("user", "总结已持久化的研究结果"),),
        response_schema=(
            {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            }
            if structured
            else None
        ),
        prompt_version="research-summary.v1",
        metadata={"correlation_id": "corr-1"},
    )


def _response(content: str = "完成") -> ModelResponse:
    return ModelResponse(
        id="provider-request-1",
        model="test-model",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )


def test_complete_maps_route_schema_usage_and_request_metadata() -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> ModelResponse:
        calls.append(kwargs)
        return _response('{"summary":"NVDA 风险与催化剂已整理"}')

    response = asyncio.run(
        LiteLLMClient({"research_summarizer": _route()}, completion=completion).complete(
            _request(structured=True)
        )
    )

    assert response.structured == {"summary": "NVDA 风险与催化剂已整理"}
    assert response.usage.input_tokens == 3
    assert response.provider_request_id == "provider-request-1"
    assert calls[0]["model"] == "test-model"
    assert calls[0]["metadata"]["prompt_version"] == "research-summary.v1"
    assert calls[0]["response_format"]["type"] == "json_schema"


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "research-summary-alias"),
        ("azure", "gpt-4o-mini-deployment"),
        ("vertex_ai", "publishers/google/models/gemini-1.5-pro"),
    ],
)
def test_explicit_endpoint_provider_preserves_model_identifier(provider: str, model: str) -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> ModelResponse:
        calls.append(kwargs)
        return _response()

    route = _route(
        endpoint=ModelEndpoint(provider=provider, model=model),
        allowed_providers=frozenset({provider}),
    )

    response = asyncio.run(
        LiteLLMClient({"research_summarizer": route}, completion=completion).complete(_request())
    )

    assert response.content == "完成"
    assert len(calls) == 1
    assert calls[0]["model"] == model


def test_retry_and_explicit_fallback_never_use_unapproved_provider() -> None:
    calls: list[str] = []

    async def completion(**kwargs: Any) -> ModelResponse:
        calls.append(kwargs["model"])
        if len(calls) < 3:
            raise TimeoutError("temporary")
        return _response()

    route = _route(
        max_attempts=2,
        fallback_endpoints=(ModelEndpoint(provider="openai", model="fallback"),),
    )
    response = asyncio.run(
        LiteLLMClient({"research_summarizer": route}, completion=completion).complete(_request())
    )
    assert response.content == "完成"
    assert calls == ["test-model", "test-model", "fallback"]


def test_route_config_rejects_unapproved_fallback_provider() -> None:
    with pytest.raises(ValueError, match="fallback provider 未在 route allowlist"):
        _route(
            max_attempts=1,
            fallback_endpoints=(ModelEndpoint(provider="anthropic", model="fallback"),),
        )


def test_invalid_request_is_not_retried_and_error_is_sanitized() -> None:
    calls = 0

    async def completion(**_: Any) -> ModelResponse:
        nonlocal calls
        calls += 1
        raise ValueError("secret-key-123")

    with pytest.raises(LLMError) as failure:
        asyncio.run(
            LiteLLMClient(
                {"research_summarizer": _route(max_attempts=3)}, completion=completion
            ).complete(_request())
        )
    assert failure.value.code is LLMErrorCode.UNAVAILABLE
    assert "secret-key-123" not in str(failure.value)
    assert calls == 1


def test_stream_merges_chunks_and_preserves_cancellation() -> None:
    async def chunks() -> AsyncIterator[dict[str, Any]]:
        for text in ("研究", "完成"):
            yield {"choices": [{"delta": {"content": text}}]}

    async def completion(**_: Any) -> AsyncIterator[dict[str, Any]]:
        return chunks()

    async def collect() -> list[str]:
        client = LiteLLMClient({"research_summarizer": _route()}, completion=completion)
        return [chunk async for chunk in client.stream(_request())]

    assert asyncio.run(collect()) == ["研究", "完成"]


def test_structured_output_gets_one_bounded_repair() -> None:
    fake = FakeLLMClient(
        [
            LLMResponse(content="不是 JSON"),
            LLMResponse(content='{ "summary": "已修复" }'),
        ]
    )
    response = asyncio.run(ValidatedLLMClient(fake).complete(_request(structured=True)))
    assert response.structured == {"summary": "已修复"}
    assert len(fake.requests) == 2
    assert fake.requests[1].metadata["repair_attempt"] == "1"
