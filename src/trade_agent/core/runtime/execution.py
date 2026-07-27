"""LangGraph node 的有界执行、稳定错误与幂等 command 协调。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue


class NodeErrorCode(StrEnum):
    TIMEOUT = "timeout"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_UNAUTHORIZED = "provider_unauthorized"
    INVALID_RESPONSE = "invalid_response"
    RETRY_EXHAUSTED = "retry_exhausted"
    INTERNAL = "internal_error"


@dataclass(frozen=True, slots=True)
class NodeExecutionError(RuntimeError):
    code: NodeErrorCode
    message: str
    retryable: bool
    attempts: int

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class NodeExecutionPolicy:
    timeout_seconds: float = 30.0
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_attempts < 1:
            raise ValueError("node timeout 与 max_attempts 必须大于 0")


class CommandStore(Protocol):
    def begin(
        self, *, owner_id: str, idempotency_key: str, payload_hash: str
    ) -> CommandReceipt: ...

    def complete(
        self, *, owner_id: str, command_id: str, result: Mapping[str, JsonValue]
    ) -> CommandReceipt: ...


class CommandReceipt(Protocol):
    @property
    def command_id(self) -> str: ...

    @property
    def result(self) -> Mapping[str, JsonValue] | None: ...

    @property
    def reused(self) -> bool: ...


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


def map_node_error(error: Exception, *, attempts: int) -> NodeExecutionError:
    code_value = str(getattr(error, "code", "")).casefold()
    retryable = bool(getattr(error, "retryable", False))
    if "rate" in code_value:
        return NodeExecutionError(
            NodeErrorCode.PROVIDER_RATE_LIMITED, "provider 请求受限", retryable, attempts
        )
    if "timeout" in code_value:
        return NodeExecutionError(NodeErrorCode.TIMEOUT, "provider 调用超时", retryable, attempts)
    if "unavailable" in code_value:
        return NodeExecutionError(
            NodeErrorCode.PROVIDER_UNAVAILABLE, "provider 暂不可用", retryable, attempts
        )
    if "unauthorized" in code_value:
        return NodeExecutionError(
            NodeErrorCode.PROVIDER_UNAUTHORIZED, "provider 认证失败", False, attempts
        )
    if "invalid" in code_value:
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
