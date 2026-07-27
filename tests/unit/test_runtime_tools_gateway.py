"""类型化 checkpoint 与 ToolGateway 的核心契约测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.runtime import AgentState, validate_checkpoint_state
from trade_agent.core.tools import (
    DefaultToolGateway,
    ManifestToolPolicy,
    ToolErrorCode,
    ToolExecutionContext,
    ToolExecutionError,
    ToolExecutionPrincipal,
    ToolManifest,
    ToolRegistry,
    ToolRequest,
    ToolResult,
)


@dataclass(frozen=True, slots=True)
class EchoTool:
    manifest = ToolManifest(
        "test.echo",
        "测试回显",
        True,
        False,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string", "minLength": 1}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        return ToolResult("ok", {"value": request.arguments["value"]})


@dataclass(frozen=True, slots=True)
class FailingTool:
    manifest = ToolManifest(
        "test.failure",
        "测试结构化错误",
        True,
        False,
        {"type": "object", "additionalProperties": False},
        {"type": "object", "additionalProperties": False},
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        del request
        raise ToolExecutionError(
            ToolErrorCode.UNAVAILABLE,
            "任意展示文案，不包含供程序识别的关键词",
            retryable=True,
        )


@dataclass(slots=True)
class OwnerScopedEchoTool:
    seen_arguments: list[dict[str, JsonValue]] = field(default_factory=list)

    manifest = ToolManifest(
        "test.owner_scoped",
        "测试 owner 作用域工具的受信身份绑定",
        False,
        False,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["owner_id", "actor_owner_id", "actor_id", "value"],
            "properties": {
                "owner_id": {"type": "string", "minLength": 1},
                "actor_owner_id": {"type": "string", "minLength": 1},
                "actor_id": {"type": "string", "minLength": 1},
                "value": {"type": "string", "minLength": 1},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["owner_id", "actor_owner_id", "actor_id", "value"],
            "properties": {
                "owner_id": {"type": "string"},
                "actor_owner_id": {"type": "string"},
                "actor_id": {"type": "string"},
                "value": {"type": "string"},
            },
        },
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        payload: dict[str, JsonValue] = dict(request.arguments)
        self.seen_arguments.append(payload)
        return ToolResult("ok", payload)


def _trusted_context(
    owner_id: str = "owner-a", actor_id: str | None = None
) -> ToolExecutionContext:
    return ToolExecutionContext(ToolExecutionPrincipal(owner_id=owner_id, actor_id=actor_id))


def _gateway(*allowed_tools: str) -> DefaultToolGateway:
    return DefaultToolGateway(
        ToolRegistry((EchoTool(),)),
        ManifestToolPolicy({"research": frozenset(allowed_tools)}),
    )


def test_gateway_allows_only_manifest_allowlisted_tool() -> None:
    denied = asyncio.run(
        _gateway().invoke(ToolRequest("test.echo", {"value": "NVDA"}, agent_id="research"))
    )
    allowed = asyncio.run(
        _gateway("test.echo").invoke(
            ToolRequest("test.echo", {"value": "NVDA"}, agent_id="research")
        )
    )
    assert denied.error is not None
    assert denied.error.code == ToolErrorCode.FORBIDDEN
    assert allowed.payload == {"value": "NVDA"}


def test_gateway_validates_input_and_unknown_tool() -> None:
    invalid = asyncio.run(
        _gateway("test.echo").invoke(
            ToolRequest("test.echo", {"value": "", "extra": True}, agent_id="research")
        )
    )
    unknown = asyncio.run(
        _gateway("test.echo").invoke(ToolRequest("test.unknown", {}, agent_id="research"))
    )
    assert invalid.error is not None
    assert invalid.error.code == ToolErrorCode.INVALID_INPUT
    assert unknown.error is not None
    assert unknown.error.code == ToolErrorCode.UNKNOWN_TOOL


def test_registry_rejects_duplicate_tool_id() -> None:
    with pytest.raises(ValueError, match="重复注册"):
        ToolRegistry((EchoTool(), EchoTool()))


def test_gateway_uses_typed_error_instead_of_parsing_message() -> None:
    gateway = DefaultToolGateway(
        ToolRegistry((FailingTool(),)),
        ManifestToolPolicy({"research": frozenset({"test.failure"})}),
    )

    result = asyncio.run(gateway.invoke(ToolRequest("test.failure", {}, agent_id="research")))

    assert result.error is not None
    assert result.error.code is ToolErrorCode.UNAVAILABLE
    assert result.error.retryable is True
    assert result.error.message == "任意展示文案，不包含供程序识别的关键词"


def test_gateway_rejects_owner_scoped_tool_without_trusted_context() -> None:
    gateway = DefaultToolGateway(
        ToolRegistry((OwnerScopedEchoTool(),)),
        ManifestToolPolicy({"research": frozenset({"test.owner_scoped"})}),
    )

    result = asyncio.run(
        gateway.invoke(
            ToolRequest(
                "test.owner_scoped",
                {
                    "owner_id": "owner-a",
                    "actor_owner_id": "owner-a",
                    "actor_id": "owner-a",
                    "value": "NVDA",
                },
                agent_id="research",
            )
        )
    )

    assert result.error is not None
    assert result.error.code is ToolErrorCode.FORBIDDEN
    assert "受信执行上下文" in result.error.message


def test_gateway_rejects_mismatched_owner_or_actor_identity() -> None:
    gateway = DefaultToolGateway(
        ToolRegistry((OwnerScopedEchoTool(),)),
        ManifestToolPolicy({"research": frozenset({"test.owner_scoped"})}),
    )

    result = asyncio.run(
        gateway.invoke(
            ToolRequest(
                "test.owner_scoped",
                {
                    "owner_id": "owner-b",
                    "actor_owner_id": "owner-b",
                    "actor_id": "intruder",
                    "value": "NVDA",
                },
                agent_id="research",
                context=_trusted_context(owner_id="owner-a", actor_id="owner-a"),
            )
        )
    )

    assert result.error is not None
    assert result.error.code is ToolErrorCode.FORBIDDEN
    assert result.error.message in {
        "owner_id 必须与受信执行主体一致",
        "actor_owner_id 必须与受信执行主体一致",
        "actor_id 必须与受信执行主体一致",
    }


def test_gateway_injects_trusted_identity_before_schema_and_handler() -> None:
    tool = OwnerScopedEchoTool()
    gateway = DefaultToolGateway(
        ToolRegistry((tool,)),
        ManifestToolPolicy({"research": frozenset({"test.owner_scoped"})}),
    )

    result = asyncio.run(
        gateway.invoke(
            ToolRequest(
                "test.owner_scoped",
                {"value": "NVDA"},
                agent_id="research",
                context=_trusted_context(owner_id="owner-a", actor_id="session-actor"),
            )
        )
    )

    assert result.error is None
    assert result.payload == {
        "owner_id": "owner-a",
        "actor_owner_id": "owner-a",
        "actor_id": "session-actor",
        "value": "NVDA",
    }
    assert tool.seen_arguments == [
        {
            "owner_id": "owner-a",
            "actor_owner_id": "owner-a",
            "actor_id": "session-actor",
            "value": "NVDA",
        }
    ]


def test_checkpoint_state_rejects_large_or_domain_payload() -> None:
    state: AgentState = {
        "user_id": "owner-a",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "message": "研究 NVDA",
    }
    validate_checkpoint_state(state)
    with pytest.raises(ValueError, match="未声明字段"):
        validate_checkpoint_state({**state, "evidence_payload": {"raw": "forbidden"}})  # type: ignore[typeddict-unknown-key]
    with pytest.raises(ValueError, match="16 KiB"):
        validate_checkpoint_state({**state, "message": "x" * 16_385})
