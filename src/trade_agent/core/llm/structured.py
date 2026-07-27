"""模型结构化输出的本地校验与有界修复。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

from trade_agent.core.tools import JsonSchemaValidator, SchemaValidationError

from .contracts import (
    JsonValue,
    LLMClient,
    LLMError,
    LLMErrorCode,
    LLMMessage,
    LLMRequest,
    LLMResponse,
)


class ValidatedLLMClient:
    """只允许一次受控修复, 不进入自由 ReAct 循环。"""

    def __init__(
        self,
        client: LLMClient,
        *,
        validator: JsonSchemaValidator | None = None,
        max_repairs: int = 1,
    ) -> None:
        if max_repairs not in {0, 1}:
            raise ValueError("结构化输出修复最多允许 1 次")
        self._client = client
        self._validator = validator or JsonSchemaValidator()
        self._max_repairs = max_repairs

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self._client.complete(request)
        if request.response_schema is None:
            return response
        for repair in range(self._max_repairs + 1):
            try:
                structured = _structured(response)
                self._validator.validate(structured, request.response_schema)
                return replace(response, structured=structured)
            except (ValueError, SchemaValidationError) as exc:
                if repair >= self._max_repairs:
                    raise LLMError(
                        LLMErrorCode.INVALID_RESPONSE,
                        "模型结构化输出未通过本地 schema",
                        route=request.route.name,
                        attempts=repair + 1,
                    ) from exc
                repair_request = replace(
                    request,
                    messages=(
                        *request.messages,
                        LLMMessage("assistant", response.content),
                        LLMMessage(
                            "user",
                            "仅修复为符合既定 JSON schema 的 JSON, 不增加新事实或调用工具。",
                        ),
                    ),
                    metadata={**request.metadata, "repair_attempt": str(repair + 1)},
                )
                response = await self._client.complete(repair_request)
        raise AssertionError("unreachable")

    def stream(self, request: LLMRequest):  # type: ignore[no-untyped-def]
        return self._client.stream(request)


def _structured(response: LLMResponse) -> Mapping[str, JsonValue]:
    if response.structured is not None:
        return response.structured
    parsed = json.loads(response.content)
    if not isinstance(parsed, dict):
        raise ValueError("结构化响应必须是 JSON object")
    return parsed


__all__ = ["ValidatedLLMClient"]
