"""只调用 PlanningService 的交易计划薄 tool adapters。"""

from dataclasses import dataclass

from trade_agent.capabilities.planning.application import PlanningService
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolManifest, ToolRequest, ToolResult

_OBJECT_OUTPUT: dict[str, JsonValue] = {"type": "object", "additionalProperties": True}
_LINEAGE_SCHEMA: dict[str, JsonValue] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["source_type", "source_id", "source_version"],
        "properties": {
            "source_type": {
                "type": "string",
                "enum": ["research_artifact", "scan_result", "user_request"],
            },
            "source_id": {"type": "string", "minLength": 1},
            "source_version": {"type": "integer", "minimum": 1},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "strategy_id": {"type": ["string", "null"]},
            "strategy_version": {"type": ["integer", "null"], "minimum": 1},
            "model_version_id": {"type": ["string", "null"]},
        },
    },
}


@dataclass(frozen=True, slots=True)
class CreatePlanDraftTool:
    application: PlanningService

    manifest = ToolManifest(
        "planning.create_plan_draft",
        "保存关联 research/scan 来源的美股交易计划草稿; 缺失字段保持为空",
        False,
        False,
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "plan_id",
                "owner_id",
                "security_id",
                "direction",
                "created_at",
                "source_references",
            ],
            "properties": {
                "plan_id": {"type": "string", "minLength": 1},
                "owner_id": {"type": "string", "minLength": 1},
                "security_id": {"type": "string", "pattern": "^US:"},
                "direction": {"type": "string", "minLength": 1},
                "created_at": {"type": "string", "format": "date-time"},
                "source_references": _LINEAGE_SCHEMA,
                "expected_version": {"type": ["integer", "null"], "minimum": 1},
                "horizon": {"type": ["string", "null"]},
                "entry_condition": {"type": ["string", "null"]},
                "invalidation_condition": {"type": ["string", "null"]},
                "target": {"type": ["string", "null"]},
                "position_notes": {"type": ["string", "null"]},
                "risk_notes": {"type": ["string", "null"]},
                "field_sources": {"type": "object", "additionalProperties": {"type": "string"}},
            },
        },
        _OBJECT_OUTPUT,
        side_effect="create_plan_draft_version",
        risk="low_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        _validate(request, self.manifest)
        idempotency_key = _idempotency_key(request)
        payload = self.application.create_draft_from_mapping(
            request.arguments, idempotency_key=idempotency_key
        )
        return ToolResult("drafted", payload)


@dataclass(frozen=True, slots=True)
class TransitionPlanTool:
    application: PlanningService

    manifest = ToolManifest(
        "planning.transition_plan",
        "执行经过 HITL 确认且有幂等保护的计划状态迁移",
        False,
        True,
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "owner_id",
                "plan_id",
                "expected_version",
                "target_status",
                "actor_id",
                "occurred_at",
            ],
            "properties": {
                "owner_id": {"type": "string", "minLength": 1},
                "plan_id": {"type": "string", "minLength": 1},
                "expected_version": {"type": "integer", "minimum": 1},
                "target_status": {
                    "type": "string",
                    "enum": ["active", "triggered", "cancelled", "expired", "reviewed"],
                },
                "actor_id": {"type": "string", "minLength": 1},
                "occurred_at": {"type": "string", "format": "date-time"},
                "reason": {"type": "string", "minLength": 1},
                "approved": {"type": "boolean"},
                "approved_payload_hash": {"type": "string", "minLength": 1},
            },
        },
        _OBJECT_OUTPUT,
        side_effect="transition_plan_state",
        risk="controlled_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        _validate(request, self.manifest)
        idempotency_key = _idempotency_key(request)
        approval_id = _approval_interaction_id(request)
        payload = self.application.transition_from_mapping(
            request.arguments,
            approval_interaction_id=approval_id,
            idempotency_key=idempotency_key,
        )
        return ToolResult("transitioned", payload)


@dataclass(frozen=True, slots=True)
class RecordPlanningReviewTool:
    application: PlanningService

    manifest = ToolManifest(
        "planning.record_review",
        "记录计划或扫描结果复盘, 并冻结 strategy/model/evidence lineage",
        False,
        True,
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "owner_id",
                "review_id",
                "subject_type",
                "subject_id",
                "subject_version",
                "outcome",
                "actor_id",
                "created_at",
                "feedback_destinations",
                "lineage",
            ],
            "properties": {
                "owner_id": {"type": "string", "minLength": 1},
                "review_id": {"type": "string", "minLength": 1},
                "subject_type": {"type": "string", "enum": ["plan", "scan_result"]},
                "subject_id": {"type": "string", "minLength": 1},
                "subject_version": {"type": "integer", "minimum": 1},
                "outcome": {
                    "type": "string",
                    "enum": [
                        "useful",
                        "false_positive",
                        "false_negative",
                        "executed",
                        "ignored",
                        "other",
                    ],
                },
                "actor_id": {"type": "string", "minLength": 1},
                "created_at": {"type": "string", "format": "date-time"},
                "annotations": {"type": "object"},
                "feedback_destinations": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["future_strategy_draft", "future_training_data"],
                    },
                },
                "lineage": _LINEAGE_SCHEMA,
            },
        },
        _OBJECT_OUTPUT,
        side_effect="append_planning_review",
        risk="controlled_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        _validate(request, self.manifest)
        payload = self.application.record_review_from_mapping(
            request.arguments,
            approval_interaction_id=_approval_interaction_id(request),
            idempotency_key=_idempotency_key(request),
        )
        return ToolResult("reviewed", payload)


def _validate(request: ToolRequest, manifest: ToolManifest) -> None:
    if request.tool_id != manifest.tool_id:
        raise ValueError("tool id 与 handler 不匹配")
    required = manifest.input_schema.get("required")
    if isinstance(required, list):
        missing = [key for key in required if isinstance(key, str) and key not in request.arguments]
        if missing:
            raise ValueError(f"tool 参数缺少字段: {', '.join(missing)}")


def _idempotency_key(request: ToolRequest) -> str:
    value = request.idempotency_key
    if value is None or not value.strip():
        raise ValueError("planning 写操作必须提供 idempotency key")
    return value


def _approval_interaction_id(request: ToolRequest) -> str:
    value = request.approval_interaction_id
    if value is None or not value.strip():
        raise ValueError("受控 planning 写操作必须提供 HITL interaction id")
    return value


__all__ = ["CreatePlanDraftTool", "RecordPlanningReviewTool", "TransitionPlanTool"]
