"""与 capability 和 provider 无关的 tool 调用契约。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class ToolExecutionPrincipal:
    """由平台认证链路提供的受信执行主体。

    Attributes:
        owner_id: 当前租户或资源所有者标识。
        actor_id: 实际触发本次工具调用的主体标识；未显式区分时退化为 ``owner_id``。

    Invariants:
        - 所有值都来自认证或会话绑定上下文，而不是 LLM 生成参数。
        - ``resolved_actor_id`` 始终返回非空字符串，供需要 ``actor_id`` 的工具统一使用。
    """

    owner_id: str
    actor_id: str | None = None

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("owner_id 不能为空")
        if self.actor_id is not None and not self.actor_id.strip():
            raise ValueError("actor_id 不能为空字符串")

    @property
    def resolved_actor_id(self) -> str:
        """返回用于工具执行的稳定 actor 标识。"""

        return self.actor_id if self.actor_id is not None else self.owner_id


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """一次工具调用的受信上下文。

    Attributes:
        principal: 经过认证和会话绑定的执行主体。

    Notes:
        该对象保留为独立上下文层，便于后续附加 trace、审计或会话约束而不修改
        ``ToolRequest`` 的调用面。
    """

    principal: ToolExecutionPrincipal


@dataclass(frozen=True, slots=True)
class ToolManifest:
    """一个 Tool 的静态能力与安全声明。

    Attributes:
        tool_id: 全局唯一、版本稳定的 Tool 协议标识。
        description: 提供给 Agent 的用途说明，不是执行提示词。
        read_only: 是否保证不改变业务状态。
        requires_hitl: 调用前是否必须附带有效人工批准。
        input_schema: 输入参数 JSON Schema。
        output_schema: 成功结果 JSON Schema。
        side_effect: 副作用类别，用于策略与审计。
        risk: 风险等级。
        idempotent: 相同输入是否可以安全重放。
        requires_idempotency_key: 调用时是否强制携带幂等键。
    """

    tool_id: str
    description: str
    read_only: bool
    requires_hitl: bool
    input_schema: Mapping[str, JsonValue] = field(default_factory=dict)
    output_schema: Mapping[str, JsonValue] = field(default_factory=dict)
    side_effect: str = "none"
    risk: str = "low"
    idempotent: bool = True
    requires_idempotency_key: bool = False


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """Agent 通过 ToolGateway 发起的标准调用请求。

    Attributes:
        tool_id: 目标 Tool 协议标识。
        arguments: 等待 input schema 校验的结构化参数。
        idempotency_key: 写操作的稳定幂等键。
        agent_id: 发起调用的 Agent manifest ID。
        approval_interaction_id: 满足 HITL 门禁的交互标识。
        context: 由运行时注入的受信执行主体上下文。
    """

    tool_id: str
    arguments: Mapping[str, JsonValue]
    idempotency_key: str | None = None
    agent_id: str | None = None
    approval_interaction_id: str | None = None
    context: ToolExecutionContext | None = None


class ToolErrorCode(StrEnum):
    """Tool 实现和 Gateway 共享的稳定错误类别。"""

    UNKNOWN_TOOL = "unknown_tool"
    FORBIDDEN = "forbidden"
    INVALID_INPUT = "invalid_input"
    INVALID_OUTPUT = "invalid_output"
    HITL_REQUIRED = "hitl_required"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    CONFLICT = "conflict"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal_error"


class ToolExecutionError(RuntimeError):
    """Tool 实现主动返回给 Gateway 的类型化执行错误。

    Attributes:
        code: 不依赖展示文案的稳定错误类别。
        message: 已脱敏、可记录或展示的错误说明。
        retryable: Gateway 是否可以建议调度器重试。

    Invariants:
        - 控制流只能读取 ``code`` 和 ``retryable``，不得解析 ``message`` 关键词。
    """

    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ToolError:
    """Tool 调用失败的结构化说明。

    Attributes:
        code: 跨实现稳定的错误码。
        message: 已脱敏、可审计的错误说明。
        retryable: 调度器是否允许在预算内重试。
    """

    code: ToolErrorCode
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ToolResult:
    """ToolGateway 返回给 Agent 的唯一结果形状。

    Attributes:
        status: 成功、失败或应用自定义状态。
        payload: 通过 output schema 校验的结构化载荷。
        error: 失败时的结构化错误。

    Invariants:
        - Tool 实现不得把 SDK 对象或未校验异常作为 payload 返回。
    """

    status: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    error: ToolError | None = None


@runtime_checkable
class ToolProtocol(Protocol):
    """Capability Tool adapter 必须实现的协议。

    Contract:
        - ``manifest`` 必须静态声明输入、输出和安全属性。
        - ``handle`` 只做协议转换并委托 application service，不复制领域规则。

    Implemented by:
        各 capability 的 ``tools`` 子包。
    """

    manifest: ToolManifest

    async def handle(self, request: ToolRequest) -> ToolResult: ...


class ToolGateway(Protocol):
    """Agent 调用所有 Tool 时必须经过的统一入口协议。

    Contract:
        - 实现方必须完成注册查找、Agent 权限、HITL 和 schema 门禁。
        - 任何 owner/actor 相关身份都必须来自 ``ToolRequest.context`` 等受信上下文，
          不能直接信任 LLM 生成参数。
        - 所有异常必须归一化为 ``ToolResult``。

    Implemented by:
        ``DefaultToolGateway`` 和测试 ``FakeToolGateway``。
    """

    async def invoke(self, request: ToolRequest) -> ToolResult:
        """执行一次受控 Tool 调用。"""

        ...
