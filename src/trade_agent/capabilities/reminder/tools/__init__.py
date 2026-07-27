"""只调用 reminder application service 的薄 tool adapters。"""

from dataclasses import dataclass
from typing import Protocol

from trade_agent.capabilities.reminder.contracts import (
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolManifest, ToolRequest, ToolResult
from trade_agent.core.tools.identity import bind_trusted_identity, identity_fields_for_manifest


class ReminderToolApplication(Protocol):
    async def execute(self, command: CapabilityCommand) -> CapabilityResult: ...

    async def query(self, query: CapabilityQuery) -> CapabilityResult: ...


_CONDITION_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "description": "由 rule_type 对应的 capability validator 校验",
}
_REMINDER_OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reminder_id",
        "plan_id",
        "status",
        "rule_type",
        "condition",
        "notification_channel",
        "cooldown_seconds",
        "execution_disclaimer",
    ],
    "properties": {
        "card_type": {"const": "reminder"},
        "reminder_id": {"type": "string"},
        "owner_id": {"type": "string"},
        "plan_id": {"type": "string"},
        "status": {"enum": ["draft", "active", "disabled"]},
        "rule_type": {"enum": ["price_threshold", "scheduled_review", "invalidation"]},
        "condition": _CONDITION_SCHEMA,
        "notification_channel": {"type": "string"},
        "cooldown_seconds": {"type": "integer", "minimum": 0},
        "approved_by": {"type": ["string", "null"]},
        "approved_payload_hash": {"type": ["string", "null"]},
        "execution_disclaimer": {"type": "string"},
    },
}


@dataclass(frozen=True, slots=True)
class CreateReminderTool:
    application: ReminderToolApplication

    manifest = ToolManifest(
        tool_id="reminder.create",
        description="创建与交易计划关联但尚未启用的提醒草稿",
        read_only=False,
        requires_hitl=False,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "reminder_id",
                "owner_id",
                "plan_id",
                "rule_type",
                "condition",
                "notification_channel",
                "cooldown_seconds",
            ],
            "properties": {
                "reminder_id": {"type": "string", "minLength": 1},
                "owner_id": {"type": "string", "minLength": 1},
                "plan_id": {"type": "string", "minLength": 1},
                "rule_type": {"enum": ["price_threshold", "scheduled_review", "invalidation"]},
                "condition": _CONDITION_SCHEMA,
                "notification_channel": {"type": "string", "minLength": 1},
                "cooldown_seconds": {"type": "integer", "minimum": 0},
            },
        },
        output_schema=_REMINDER_OUTPUT_SCHEMA,
        side_effect="create_reminder_draft",
        risk="low_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _require_tool(request, self.manifest)
        key = _require_idempotency_key(request)
        result = await self.application.execute(
            CapabilityCommand("reminder.create", {**request.arguments, "idempotency_key": key})
        )
        return ToolResult("drafted", result.payload)


@dataclass(frozen=True, slots=True)
class SetReminderStatusTool:
    application: ReminderToolApplication

    manifest = ToolManifest(
        tool_id="reminder.set_status",
        description="按完全一致的人工审批 payload 启用或停用提醒",
        read_only=False,
        requires_hitl=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "reminder_id",
                "owner_id",
                "target_status",
                "approved",
                "actor_id",
                "payload_hash",
            ],
            "properties": {
                "reminder_id": {"type": "string", "minLength": 1},
                "owner_id": {"type": "string", "minLength": 1},
                "target_status": {"enum": ["active", "disabled"]},
                "approved": {"const": True},
                "actor_id": {"type": "string", "minLength": 1},
                "payload_hash": {"type": "string", "minLength": 1},
            },
        },
        output_schema=_REMINDER_OUTPUT_SCHEMA,
        side_effect="transition_reminder_status",
        risk="controlled_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _require_tool(request, self.manifest)
        key = _require_idempotency_key(request)
        if request.approval_interaction_id is None or not request.approval_interaction_id.strip():
            raise PermissionError("启用或停用 reminder 必须引用已解决的 HITL approval")
        result = await self.application.execute(
            CapabilityCommand("reminder.set_status", {**request.arguments, "idempotency_key": key})
        )
        return ToolResult("transitioned", result.payload)


@dataclass(frozen=True, slots=True)
class GetReminderTool:
    application: ReminderToolApplication

    manifest = ToolManifest(
        tool_id="reminder.get",
        description="读取 owner 拥有的提醒规则与当前状态",
        read_only=True,
        requires_hitl=False,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["reminder_id", "owner_id"],
            "properties": {
                "reminder_id": {"type": "string", "minLength": 1},
                "owner_id": {"type": "string", "minLength": 1},
            },
        },
        output_schema=_REMINDER_OUTPUT_SCHEMA,
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _require_tool(request, self.manifest)
        result = await self.application.query(CapabilityQuery("reminder.get", request.arguments))
        return ToolResult("found", result.payload)


def _require_tool(request: ToolRequest, manifest: ToolManifest) -> None:
    if request.tool_id != manifest.tool_id:
        raise ValueError("tool id 与 handler 不匹配")


def _require_idempotency_key(request: ToolRequest) -> str:
    if request.idempotency_key is None or not request.idempotency_key.strip():
        raise ValueError("reminder 写操作必须提供 idempotency key")
    return request.idempotency_key


__all__ = ["CreateReminderTool", "GetReminderTool", "SetReminderStatusTool"]
