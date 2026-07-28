"""LangGraph node 的有界执行、稳定错误与幂等 command 协调。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue


class NodeErrorCode(StrEnum):
    """节点执行层共享的稳定错误码。

    Attributes:
        TIMEOUT: 节点或底层 provider 调用超时。
        PROVIDER_RATE_LIMITED: provider 因限流拒绝请求。
        PROVIDER_UNAVAILABLE: provider 暂不可用或服务故障。
        PROVIDER_UNAUTHORIZED: provider 认证、授权或凭据校验失败。
        INVALID_RESPONSE: provider 返回结构或语义无效。
        RETRY_EXHAUSTED: 在重试预算内仍未成功完成。
        INTERNAL: 未注册或不可归类的内部错误。

    Invariants:
        - 枚举值是调度层决定重试、降级与用户提示的稳定协议字段。
    """

    TIMEOUT = "timeout"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_UNAUTHORIZED = "provider_unauthorized"
    INVALID_RESPONSE = "invalid_response"
    RETRY_EXHAUSTED = "retry_exhausted"
    INTERNAL = "internal_error"


_DEFAULT_ERROR_CODE_MAP: Mapping[str, NodeErrorCode] = {
    "timeout": NodeErrorCode.TIMEOUT,
    "rate_limited": NodeErrorCode.PROVIDER_RATE_LIMITED,
    "provider_rate_limited": NodeErrorCode.PROVIDER_RATE_LIMITED,
    "unavailable": NodeErrorCode.PROVIDER_UNAVAILABLE,
    "provider_unavailable": NodeErrorCode.PROVIDER_UNAVAILABLE,
    "unauthorized": NodeErrorCode.PROVIDER_UNAUTHORIZED,
    "authentication_error": NodeErrorCode.PROVIDER_UNAUTHORIZED,
    "invalid_input": NodeErrorCode.INVALID_RESPONSE,
    "invalid_output": NodeErrorCode.INVALID_RESPONSE,
    "invalid_request": NodeErrorCode.INVALID_RESPONSE,
    "invalid_response": NodeErrorCode.INVALID_RESPONSE,
}


@dataclass(frozen=True, slots=True)
class NodeExecutionError(RuntimeError):
    """统一表达节点执行失败的稳定错误值对象。

    Attributes:
        code: 失败类型枚举，供上层决定重试与用户提示。
        message: 面向调用方的稳定错误文本。
        retryable: 当前错误是否允许在策略预算内重试。
        attempts: 触发该错误时已经发生的尝试次数。
    """

    code: NodeErrorCode
    message: str
    retryable: bool
    attempts: int

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class NodeExecutionPolicy:
    """节点执行的超时与重试预算。

    Attributes:
        timeout_seconds: 单次操作最大执行时长，单位秒。
        max_attempts: 允许的总尝试次数，至少为 1。
    """

    timeout_seconds: float
    max_attempts: int

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_attempts < 1:
            raise ValueError("node timeout 与 max_attempts 必须大于 0")


class CommandStore(Protocol):
    """幂等命令生命周期存储协议。

    Contract:
        - 同一 ``owner_id + idempotency_key`` 必须稳定映射到同一命令记录。
        - ``begin`` 与 ``complete`` 必须可安全重放，且不得跨 owner 泄露结果。

    Implemented by:
        trade_agent.adapters.sqlite.repositories.SQLiteCommandStore
    """

    def begin(self, *, owner_id: str, idempotency_key: str, payload_hash: str) -> CommandReceipt:
        """开始一条幂等命令记录。

        Args:
            owner_id: 命令所属租户或用户标识。
            idempotency_key: 调用方生成的稳定幂等键。
            payload_hash: 本次业务输入的规范化摘要。

        Returns:
            当前命令的 receipt；若命令已存在，可能返回复用记录。
        """
        ...

    def complete(
        self, *, owner_id: str, command_id: str, result: Mapping[str, JsonValue]
    ) -> CommandReceipt:
        """把一条命令标记为完成。

        Args:
            owner_id: 命令所属租户或用户标识。
            command_id: ``begin`` 返回的稳定命令 ID。
            result: 需要持久化并可重复返回的 JSON 结果。

        Returns:
            更新后的 receipt。
        """
        ...


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """幂等命令存储返回给执行层的不可变快照。

    Attributes:
        command_id: 命令的稳定标识。
        status: 当前持久化状态，例如 pending 或 completed。
        result: 已持久化结果；命令未完成时为空。
        reused: 本次操作是否复用了既有命令记录。

    Invariants:
        - ``reused`` 为真时，本对象描述既有记录而不是新建命令。
    """

    command_id: str
    status: str
    result: Mapping[str, JsonValue] | None
    reused: bool


class NodeExecutor:
    def __init__(self, policy: NodeExecutionPolicy) -> None:
        self._policy = policy

    async def run(
        self, operation: Callable[[], Awaitable[Mapping[str, JsonValue]]]
    ) -> Mapping[str, JsonValue]:
        last: NodeExecutionError | None = None
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return await asyncio.wait_for(operation(), timeout=self._policy.timeout_seconds)
            except TimeoutError:
                last = NodeExecutionError(NodeErrorCode.TIMEOUT, "node 执行超时", True, attempt)
            except Exception as exc:
                last = map_node_error(exc, attempts=attempt)
            if last is not None and not last.retryable:
                raise last
        assert last is not None
        raise NodeExecutionError(
            NodeErrorCode.RETRY_EXHAUSTED,
            f"重试预算已耗尽: {last.message}",
            False,
            self._policy.max_attempts,
        )


async def execute_idempotent_command(
    *,
    store: CommandStore,
    owner_id: str,
    idempotency_key: str,
    payload_hash: str,
    operation: Callable[[], Awaitable[Mapping[str, JsonValue]]],
) -> Mapping[str, JsonValue]:
    receipt = store.begin(
        owner_id=owner_id, idempotency_key=idempotency_key, payload_hash=payload_hash
    )
    reused = receipt.reused
    result = receipt.result
    if reused and isinstance(result, Mapping):
        return result
    value = await operation()
    completed = store.complete(
        owner_id=owner_id,
        command_id=receipt.command_id,
        result=value,
    )
    completed_result = completed.result
    return completed_result if isinstance(completed_result, Mapping) else value


def map_node_error(
    error: Exception,
    *,
    attempts: int,
    code_map: Mapping[str, NodeErrorCode] = _DEFAULT_ERROR_CODE_MAP,
) -> NodeExecutionError:
    """仅按注册的稳定错误码映射节点失败。

    Args:
        error: Provider、Tool 或 LLM adapter 抛出的类型化异常。
        attempts: 当前已经执行的尝试次数。
        code_map: 稳定外部错误码到节点错误码的精确映射表。

    Returns:
        已脱敏且可由调度器处理的节点执行错误。

    Notes:
        未注册错误码一律视为内部错误。本函数绝不解析异常消息，也不使用子串
        匹配决定重试或控制流。
    """

    code_value = str(getattr(error, "code", "")).casefold()
    retryable = bool(getattr(error, "retryable", False))
    mapped = code_map.get(code_value)
    if mapped is NodeErrorCode.PROVIDER_RATE_LIMITED:
        return NodeExecutionError(
            NodeErrorCode.PROVIDER_RATE_LIMITED, "provider 请求受限", retryable, attempts
        )
    if mapped is NodeErrorCode.TIMEOUT:
        return NodeExecutionError(NodeErrorCode.TIMEOUT, "provider 调用超时", retryable, attempts)
    if mapped is NodeErrorCode.PROVIDER_UNAVAILABLE:
        return NodeExecutionError(
            NodeErrorCode.PROVIDER_UNAVAILABLE, "provider 暂不可用", retryable, attempts
        )
    if mapped is NodeErrorCode.PROVIDER_UNAUTHORIZED:
        return NodeExecutionError(
            NodeErrorCode.PROVIDER_UNAUTHORIZED, "provider 认证失败", False, attempts
        )
    if mapped is NodeErrorCode.INVALID_RESPONSE:
        return NodeExecutionError(
            NodeErrorCode.INVALID_RESPONSE, "provider 返回无效响应", False, attempts
        )
    return NodeExecutionError(NodeErrorCode.INTERNAL, "node 执行失败", False, attempts)


__all__ = [
    "NodeErrorCode",
    "NodeExecutionError",
    "NodeExecutionPolicy",
    "NodeExecutor",
    "execute_idempotent_command",
    "map_node_error",
]
