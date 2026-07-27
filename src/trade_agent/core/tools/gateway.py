"""所有 Agent tool 调用的唯一受控入口。"""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import (
    ToolError,
    ToolErrorCode,
    ToolManifest,
    ToolProtocol,
    ToolRequest,
    ToolResult,
)
from .policy import ToolPolicy
from .schema import JsonSchemaValidator, SchemaValidationError


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolProtocol] = ()) -> None:
        self._tools: dict[str, ToolProtocol] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolProtocol) -> None:
        tool_id = tool.manifest.tool_id
        if tool_id in self._tools:
            raise ValueError(f"重复注册 tool: {tool_id}")
        self._tools[tool_id] = tool

    def get(self, tool_id: str) -> ToolProtocol | None:
        return self._tools.get(tool_id)

    def manifests(self) -> tuple[ToolManifest, ...]:
        return tuple(tool.manifest for tool in self._tools.values())


class DefaultToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolPolicy,
        validator: JsonSchemaValidator | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._validator = validator or JsonSchemaValidator()

    async def invoke(self, request: ToolRequest) -> ToolResult:
        tool = self._registry.get(request.tool_id)
        if tool is None:
            return self._failure(ToolErrorCode.UNKNOWN_TOOL, "未知 tool id")

        denied = self._policy.evaluate(request, tool.manifest)
        if denied is not None:
            return ToolResult("error", error=denied)
        try:
            self._validator.validate(request.arguments, tool.manifest.input_schema)
        except SchemaValidationError as error:
            return self._failure(ToolErrorCode.INVALID_INPUT, str(error))

        try:
            result = await tool.handle(request)
        except TimeoutError:
            return self._failure(ToolErrorCode.TIMEOUT, "tool 调用超时", retryable=True)
        except (KeyError, TypeError, ValueError) as error:
            return self._failure(ToolErrorCode.INVALID_INPUT, str(error))
        except RuntimeError as error:
            return self._map_runtime_error(error)

        try:
            self._validator.validate(result.payload, tool.manifest.output_schema)
        except SchemaValidationError as error:
            return self._failure(ToolErrorCode.INVALID_OUTPUT, str(error))
        return result

    @staticmethod
    def _map_runtime_error(error: RuntimeError) -> ToolResult:
        message = str(error)
        normalized = message.casefold()
        if "conflict" in normalized or "冲突" in message:
            return DefaultToolGateway._failure(ToolErrorCode.CONFLICT, message)
        if "unavailable" in normalized or "不可用" in message:
            return DefaultToolGateway._failure(ToolErrorCode.UNAVAILABLE, message, retryable=True)
        return DefaultToolGateway._failure(ToolErrorCode.INTERNAL, "tool 执行失败")

    @staticmethod
    def _failure(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
        return ToolResult("error", error=ToolError(code, message, retryable))


__all__ = ["DefaultToolGateway", "ToolRegistry"]
