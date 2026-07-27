"""进程内异步 LiteLLM SDK adapter。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import litellm
from litellm.exceptions import (
    AuthenticationError,
    BadRequestError,
    ContentPolicyViolationError,
    ContextWindowExceededError,
    RateLimitError,
    Timeout,
)

from trade_agent.core.llm import (
    JsonValue,
    LLMError,
    LLMErrorCode,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)


@dataclass(frozen=True, slots=True)
class LiteLLMRouteConfig:
    logical_route: str
    model: str
    timeout_seconds: float
    max_tokens: int
    provider: str
    allowed_providers: frozenset[str]
    concurrency_limit: int = 4
    max_attempts: int = 2
    budget_usd: float | None = None
    fallback_models: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider not in self.allowed_providers:
            raise ValueError("主模型 provider 未在 route allowlist")
        if self.max_attempts < 1 or self.concurrency_limit < 1:
            raise ValueError("LiteLLM retry 与 concurrency 必须大于 0")


class LiteLLMCompletion(Protocol):
    def __call__(self, **kwargs: Any) -> Awaitable[Any]: ...


class LiteLLMClient:
    def __init__(
        self,
        routes: Mapping[str, LiteLLMRouteConfig],
        *,
        completion: LiteLLMCompletion | None = None,
    ) -> None:
        self._routes = dict(routes)
        self._completion = completion or litellm.acompletion
        self._semaphores = {
            name: asyncio.Semaphore(route.concurrency_limit) for name, route in routes.items()
        }
        self._spent_usd: dict[str, float] = {name: 0.0 for name in routes}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        route = self._require_route(request)
        async with self._semaphores[request.route.name]:
            return await self._complete_with_policy(request, route)

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        route = self._require_route(request)
        async with self._semaphores[request.route.name]:
            try:
                raw = await self._completion(
                    **self._kwargs(request, route.model, route, stream=True)
                )
                async for chunk in raw:
                    text = _chunk_text(chunk)
                    if text:
                        yield text
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise self._map_error(exc, request.route.name, attempts=1) from exc

    def _require_route(self, request: LLMRequest) -> LiteLLMRouteConfig:
        try:
            return self._routes[request.route.name]
        except KeyError as error:
            raise ValueError(f"未配置逻辑模型路由: {request.route.name}") from error

    async def _complete_with_policy(
        self, request: LLMRequest, route: LiteLLMRouteConfig
    ) -> LLMResponse:
        models = (route.model, *route.fallback_models)
        attempts = 0
        last_error: LLMError | None = None
        for model in models:
            provider = _provider(model)
            if provider not in route.allowed_providers:
                raise LLMError(
                    LLMErrorCode.PROVIDER_NOT_ALLOWED,
                    "fallback provider 未获批准",
                    route=request.route.name,
                    attempts=max(attempts, 1),
                )
            for _ in range(route.max_attempts):
                attempts += 1
                self._check_budget(request.route.name, route)
                try:
                    raw = await self._completion(
                        **self._kwargs(request, model, route, stream=False)
                    )
                    response = _normalize_response(raw)
                    self._record_cost(request.route.name, route, response.usage)
                    return response
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = self._map_error(exc, request.route.name, attempts=attempts)
                    if not last_error.retryable:
                        raise last_error from exc
            # 当前 model 的重试耗尽后才进入配置中显式列出的 fallback。
        if last_error is not None:
            raise LLMError(
                LLMErrorCode.UNAVAILABLE,
                "所有已批准 LiteLLM model 均不可用",
                route=request.route.name,
                attempts=attempts,
            ) from last_error
        raise AssertionError("route 至少包含一个 model")

    @staticmethod
    def _kwargs(
        request: LLMRequest,
        model: str,
        route: LiteLLMRouteConfig,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "timeout": route.timeout_seconds,
            "max_tokens": route.max_tokens,
            "stream": stream,
            "metadata": {
                **route.metadata,
                **request.metadata,
                "logical_route": request.route.name,
                "prompt_version": request.prompt_version,
            },
        }
        if request.response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"{request.route.name}_response",
                    "strict": True,
                    "schema": dict(request.response_schema),
                },
            }
        return kwargs

    def _check_budget(self, route_name: str, route: LiteLLMRouteConfig) -> None:
        if route.budget_usd is not None and self._spent_usd[route_name] >= route.budget_usd:
            raise LLMError(
                LLMErrorCode.BUDGET_EXCEEDED,
                "逻辑模型路由已达到成本预算",
                route=route_name,
            )

    def _record_cost(self, route_name: str, route: LiteLLMRouteConfig, usage: LLMUsage) -> None:
        cost = usage.estimated_cost_usd
        if cost is not None:
            self._spent_usd[route_name] += cost
        if route.budget_usd is not None and self._spent_usd[route_name] > route.budget_usd:
            raise LLMError(
                LLMErrorCode.BUDGET_EXCEEDED,
                "本次调用超过逻辑模型路由成本预算",
                route=route_name,
            )

    @staticmethod
    def _map_error(error: Exception, route: str, *, attempts: int) -> LLMError:
        if isinstance(error, (Timeout, TimeoutError)):
            return LLMError(LLMErrorCode.TIMEOUT, "模型调用超时", True, route, attempts)
        if isinstance(error, RateLimitError):
            return LLMError(LLMErrorCode.RATE_LIMITED, "模型调用限流", True, route, attempts)
        if isinstance(error, AuthenticationError):
            return LLMError(LLMErrorCode.AUTHENTICATION, "模型认证失败", False, route, attempts)
        if isinstance(error, ContextWindowExceededError):
            return LLMError(LLMErrorCode.CONTEXT_LIMIT, "模型上下文超限", False, route, attempts)
        if isinstance(error, ContentPolicyViolationError):
            return LLMError(LLMErrorCode.CONTENT_POLICY, "模型内容策略拒绝", False, route, attempts)
        if isinstance(error, BadRequestError):
            return LLMError(LLMErrorCode.INVALID_REQUEST, "模型请求无效", False, route, attempts)
        return LLMError(LLMErrorCode.UNAVAILABLE, "模型服务不可用", False, route, attempts)


LiteLLMClientScaffold = LiteLLMClient


def _provider(model: str) -> str:
    return model.split("/", maxsplit=1)[0] if "/" in model else "unknown"


def _normalize_response(raw: Any) -> LLMResponse:
    payload = raw.model_dump() if hasattr(raw, "model_dump") else raw
    if not isinstance(payload, Mapping):
        raise ValueError("LiteLLM 响应必须是 mapping")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LiteLLM 响应缺少 choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise ValueError("LiteLLM choice 无效")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("LiteLLM message 无效")
    content_value = message.get("content")
    content = content_value if isinstance(content_value, str) else ""
    structured: Mapping[str, JsonValue] | None = None
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            structured = parsed
    except json.JSONDecodeError:
        pass
    usage_value = payload.get("usage")
    usage = usage_value if isinstance(usage_value, Mapping) else {}
    cost = usage.get("cost")
    return LLMResponse(
        content=content,
        structured=structured,
        usage=LLMUsage(
            input_tokens=_integer(usage.get("prompt_tokens")),
            output_tokens=_integer(usage.get("completion_tokens")),
            estimated_cost_usd=float(cost) if isinstance(cost, int | float) else None,
        ),
        provider_request_id=_optional_string(payload.get("id")),
        finish_reason=_optional_string(choice.get("finish_reason")),
    )


def _chunk_text(chunk: Any) -> str:
    payload = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
    if not isinstance(payload, Mapping):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return ""
    delta = choices[0].get("delta")
    if not isinstance(delta, Mapping):
        return ""
    return _optional_string(delta.get("content")) or ""


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
