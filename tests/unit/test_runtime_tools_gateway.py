"""类型化 checkpoint 与 ToolGateway 的核心契约测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from trade_agent.core.runtime import AgentState, validate_checkpoint_state
from trade_agent.core.tools import (
    DefaultToolGateway,
    ManifestToolPolicy,
    ToolErrorCode,
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
