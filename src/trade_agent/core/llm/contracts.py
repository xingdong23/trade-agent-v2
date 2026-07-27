"""由 LiteLLM 等模型 adapter 实现的供应商无关协议。"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """模型逻辑路由。

    Attributes:
        name: 配置中注册的逻辑路由名，而不是供应商模型名称。
    """

    name: str


class LLMErrorCode(StrEnum):
    """业务层可以稳定处理的模型调用错误类别。"""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication_error"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LIMIT = "context_limit"
    CONTENT_POLICY = "content_policy"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROVIDER_NOT_ALLOWED = "provider_not_allowed"
    INVALID_RESPONSE = "invalid_response"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LLMError(RuntimeError):
    """跨模型供应商统一的调用错误。

    Attributes:
        code: 调用方可以稳定处理的错误类别。
        message: 已脱敏错误说明。
        retryable: 是否允许在预算内重试。
        route: 失败的逻辑模型路由。
        attempts: 包含首次调用在内的尝试次数。
    """

    code: LLMErrorCode
    message: str
    retryable: bool = False
    route: str | None = None
    attempts: int = 1

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """发送给 LLM 的单条对话消息。

    Attributes:
        role: 模型协议角色，例如 system、user 或 assistant。
        content: 已完成必要脱敏的文本内容。
    """

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """模型 adapter 接收的完整、版本化请求。

    Attributes:
        route: 逻辑模型路由。
        messages: 按顺序发送的消息。
        response_schema: 可选 JSON Schema；存在时必须本地校验模型输出。
        prompt_version: Prompt 的稳定版本，禁止使用默认未版本化值。
        metadata: 追踪和预算所需元数据，不应包含业务秘密。

    Invariants:
        - Route 和 messages 不得为空。
        - Prompt 必须显式版本化，便于回放和评估。
    """

    route: ModelRoute
    messages: Sequence[LLMMessage]
    response_schema: Mapping[str, JsonValue] | None = None
    prompt_version: str = "unversioned"
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.route.name.strip() or not self.messages:
            raise ValueError("LLMRequest 必须包含 route 与 message")
        if self.prompt_version == "unversioned":
            raise ValueError("LLMRequest 必须声明 prompt_version")


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """一次模型调用的统一消耗统计。

    Attributes:
        input_tokens: 输入 token 数。
        output_tokens: 输出 token 数。
        estimated_cost_usd: 可获得时记录的美元估算成本。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """供应商响应归一化后的结果。

    Attributes:
        content: 原始文本输出。
        structured: 通过本地 schema 校验后的结构化输出。
        usage: 统一 token 与成本统计。
        provider_request_id: 供应商请求追踪标识。
        finish_reason: 供应商归一化结束原因。
    """

    content: str
    structured: Mapping[str, JsonValue] | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    provider_request_id: str | None = None
    finish_reason: str | None = None


class LLMClient(Protocol):
    """LLM 供应商 adapter 必须实现的异步调用协议。

    Contract:
        - 实现方按 ``ModelRoute`` 读取配置，业务层不能指定供应商模型名。
        - Provider 异常必须映射为 ``LLMError``，不得泄漏 SDK 异常和凭据。
        - 本协议只生成文本或结构化草稿，不能执行 Tool 或量化预测。

    Implemented by:
        ``adapters.llm.litellm`` 和 ``core.testing.FakeLLMClient``。
    """

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """完成一次非流式模型调用并返回归一化响应。"""

        ...

    def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        """流式返回文本片段；错误语义与 ``complete`` 一致。"""

        ...


class StructuredResponseValidator(Protocol):
    """结构化模型输出的本地 JSON Schema 校验协议。

    Contract:
        - 校验必须在结果进入业务层前完成。
        - 失败必须抛出可归一化异常，不能自动补充新事实。

    Implemented by:
        Core JSON Schema validator 或测试 fake。
    """

    def validate(
        self, value: JsonValue | Mapping[str, JsonValue], schema: Mapping[str, JsonValue]
    ) -> None: ...
