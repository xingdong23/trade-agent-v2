"""与 capability 和 provider 无关的 tool 调用契约。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class ToolManifest:
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
    tool_id: str
    arguments: Mapping[str, JsonValue]
    idempotency_key: str | None = None
    agent_id: str | None = None
    approval_interaction_id: str | None = None


class ToolErrorCode(StrEnum):
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


@dataclass(frozen=True, slots=True)
class ToolError:
    code: ToolErrorCode
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    error: ToolError | None = None


@runtime_checkable
class ToolProtocol(Protocol):
    manifest: ToolManifest

    async def handle(self, request: ToolRequest) -> ToolResult: ...


class ToolGateway(Protocol):
    async def invoke(self, request: ToolRequest) -> ToolResult: ...
