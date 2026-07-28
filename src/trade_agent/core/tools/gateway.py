"""所有 Agent Tool 调用的唯一受控入口。

这个模块是 Agent 与业务能力之间的服务端策略边界：

- ``ToolRegistry`` 负责回答“系统里有哪些 Tool”；
- ``ToolPolicy`` 负责回答“当前 Agent 能不能调用它”；
- ``DefaultToolGateway`` 负责回答“输入是否合法、执行失败怎么归一化、输出能否对外发布”。
"""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import (
    ToolError,
    ToolErrorCode,
    ToolExecutionError,
    ToolGateway,
    ToolManifest,
    ToolProtocol,
    ToolRequest,
    ToolResult,
)
from .identity import bind_trusted_identity, identity_fields_for_manifest
from .policy import ToolPolicy
from .schema import JsonSchemaValidator, SchemaValidationError


class ToolRegistry:
    """保存可调用 Tool 的注册表。

    这里不处理权限和参数校验，只维护 ``tool_id -> tool 实现`` 的唯一映射。
    这样测试和生产都可以使用同一套查找逻辑。
    """

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


class DefaultToolGateway(ToolGateway):
    """把权限、校验和执行串成一次受控 Tool 调用。

    调用顺序固定为：

    1. 查找 Tool；
    2. 检查 manifest policy；
    3. 校验输入 schema；
    4. 调用 Tool；
    5. 校验输出 schema；
    6. 把异常统一映射为 ``ToolResult``。

    这样 Agent 永远拿到结构化结果，而不是未约束的 Python 异常。
    """

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
        """执行一次完整的 Tool 调用防线。"""

        tool = self._registry.get(request.tool_id)
        if tool is None:
            return self._failure(ToolErrorCode.UNKNOWN_TOOL, "未知 tool id")

        denied = self._policy.evaluate(request, tool.manifest)
        if denied is not None:
            return ToolResult("error", error=denied)
        try:
            trusted_request = bind_trusted_identity(
                request,
                identity_fields=identity_fields_for_manifest(tool.manifest),
                require_context_for_owner_scope=True,
            )
            # 输入先过 schema，避免 Tool 实现里充斥重复的字段形状判断。
            self._validator.validate(trusted_request.arguments, tool.manifest.input_schema)
        except SchemaValidationError as error:
            return self._failure(ToolErrorCode.INVALID_INPUT, str(error))
        except ToolExecutionError as error:
            return self._failure(error.code, error.message, retryable=error.retryable)

        try:
            result = await tool.handle(trusted_request)
        except TimeoutError:
            return self._failure(ToolErrorCode.TIMEOUT, "tool 调用超时", retryable=True)
        except ToolExecutionError as error:
            return self._failure(error.code, error.message, retryable=error.retryable)
        except (KeyError, TypeError, ValueError) as error:
            return self._failure(ToolErrorCode.INVALID_INPUT, str(error))
        except RuntimeError:
            return self._failure(ToolErrorCode.INTERNAL, "tool 执行失败")

        try:
            # 输出也必须过 schema，防止 Tool 静默返回前端无法消费的数据。
            self._validator.validate(result.payload, tool.manifest.output_schema)
        except SchemaValidationError as error:
            return self._failure(ToolErrorCode.INVALID_OUTPUT, str(error))
        return result

    @staticmethod
    def _failure(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
        return ToolResult("error", error=ToolError(code, message, retryable))


__all__ = ["DefaultToolGateway", "ToolRegistry"]
