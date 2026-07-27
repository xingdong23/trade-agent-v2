"""交易计划、生命周期与复盘领域模型, 不包含任何 broker 执行概念。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from trade_agent.core.llm.contracts import JsonValue


class PlanStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REVIEWED = "reviewed"


class ReviewOutcome(StrEnum):
    USEFUL = "useful"
    FALSE_POSITIVE = "false_positive"
    FALSE_NEGATIVE = "false_negative"
    EXECUTED = "executed"
    IGNORED = "ignored"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PlanLineage:
    """计划或复盘所引用的精确、不可变来源版本。"""

    source_type: str
    source_id: str
    source_version: int
    evidence_ids: tuple[str, ...] = ()
    strategy_id: str | None = None
    strategy_version: int | None = None
    model_version_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_type not in {"research_artifact", "scan_result", "user_request"}:
            raise ValueError("计划来源必须是 research artifact、scan result 或用户请求")
        if not self.source_id.strip() or self.source_version < 1:
            raise ValueError("计划来源必须包含有效 id 与版本")
        if self.strategy_version is not None and self.strategy_version < 1:
            raise ValueError("strategy_version 必须从 1 开始")
        if self.strategy_version is not None and not self.strategy_id:
            raise ValueError("strategy_version 必须与 strategy_id 同时提供")
        if self.strategy_id is not None and self.strategy_version is None:
            raise ValueError("strategy_id 必须与 strategy_version 同时提供")
        if any(not evidence_id.strip() for evidence_id in self.evidence_ids):
            raise ValueError("evidence id 不能为空")


@dataclass(frozen=True, slots=True)
class PlanTransition:
    from_status: PlanStatus
    to_status: PlanStatus
    actor_id: str
    occurred_at: datetime
    reason: str
    approval_interaction_id: str | None = None

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or not self.reason.strip():
            raise ValueError("状态迁移必须记录 actor 与原因")
        if self.occurred_at.tzinfo is None:
            raise ValueError("状态迁移时间必须包含时区")


_RISK_CRITICAL_FIELDS = (
    "horizon",
    "entry_condition",
    "invalidation_condition",
    "target",
    "position_notes",
    "risk_notes",
)


@dataclass(frozen=True, slots=True)
class TradingPlan:
    plan_id: str
    owner_id: str
    security_id: str
    version: int
    status: PlanStatus
    direction: str
    horizon: str | None
    entry_condition: str | None
    invalidation_condition: str | None
    target: str | None
    position_notes: str | None
    risk_notes: str | None
    source_references: tuple[PlanLineage, ...]
    created_at: datetime
    field_sources: Mapping[str, str] = field(default_factory=dict)
    transitions: tuple[PlanTransition, ...] = ()
    supersedes_version: int | None = None

    def __post_init__(self) -> None:
        if not self.plan_id.strip() or not self.owner_id.strip():
            raise ValueError("计划必须包含 plan_id 与 owner_id")
        if not _is_us_security(self.security_id):
            raise ValueError("unsupported_market: 首版只支持规范化美股证券")
        if self.version < 1:
            raise ValueError("计划版本必须从 1 开始")
        if not self.direction.strip():
            raise ValueError("计划必须包含方向或逻辑")
        if not self.source_references:
            raise ValueError("计划草稿必须关联 research、scan 或用户请求来源")
        if self.created_at.tzinfo is None:
            raise ValueError("计划创建时间必须包含时区")
        if self.supersedes_version is not None and self.supersedes_version >= self.version:
            raise ValueError("supersedes_version 必须早于当前版本")
        object.__setattr__(self, "field_sources", MappingProxyType(dict(self.field_sources)))

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name in _RISK_CRITICAL_FIELDS
            if not _has_text(getattr(self, field_name))
        )

    @property
    def approval_payload_hash(self) -> str:
        payload = {
            "plan_id": self.plan_id,
            "owner_id": self.owner_id,
            "security_id": self.security_id,
            "version": self.version,
            "direction": self.direction,
            "horizon": self.horizon,
            "entry_condition": self.entry_condition,
            "invalidation_condition": self.invalidation_condition,
            "target": self.target,
            "position_notes": self.position_notes,
            "risk_notes": self.risk_notes,
            "source_references": [
                {
                    "source_type": item.source_type,
                    "source_id": item.source_id,
                    "source_version": item.source_version,
                    "evidence_ids": list(item.evidence_ids),
                    "strategy_id": item.strategy_id,
                    "strategy_version": item.strategy_version,
                    "model_version_id": item.model_version_id,
                }
                for item in self.source_references
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    def revised(
        self,
        *,
        direction: str,
        horizon: str | None,
        entry_condition: str | None,
        invalidation_condition: str | None,
        target: str | None,
        position_notes: str | None,
        risk_notes: str | None,
        source_references: tuple[PlanLineage, ...],
        field_sources: Mapping[str, str],
        created_at: datetime,
    ) -> TradingPlan:
        return TradingPlan(
            plan_id=self.plan_id,
            owner_id=self.owner_id,
            security_id=self.security_id,
            version=self.version + 1,
            status=PlanStatus.DRAFT,
            direction=direction,
            horizon=horizon,
            entry_condition=entry_condition,
            invalidation_condition=invalidation_condition,
            target=target,
            position_notes=position_notes,
            risk_notes=risk_notes,
            source_references=source_references,
            created_at=created_at,
            field_sources=field_sources,
            transitions=self.transitions,
            supersedes_version=self.version,
        )


@dataclass(frozen=True, slots=True)
class Review:
    review_id: str
    owner_id: str
    subject_type: str
    subject_id: str
    subject_version: int
    outcome: ReviewOutcome
    created_at: datetime
    lineage: tuple[PlanLineage, ...]
    annotations: Mapping[str, JsonValue] = field(default_factory=dict)
    feedback_destinations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.subject_type not in {"plan", "scan_result"}:
            raise ValueError("复盘对象只能是 plan 或 scan_result")
        if self.subject_version < 1:
            raise ValueError("复盘对象版本必须从 1 开始")
        if self.created_at.tzinfo is None:
            raise ValueError("复盘时间必须包含时区")
        allowed_destinations = {"future_strategy_draft", "future_training_data"}
        if not set(self.feedback_destinations).issubset(allowed_destinations):
            raise ValueError("复盘反馈只能进入未来策略草稿或未来训练数据")
        object.__setattr__(self, "annotations", MappingProxyType(dict(self.annotations)))


_ALLOWED_TRANSITIONS: Mapping[PlanStatus, frozenset[PlanStatus]] = {
    PlanStatus.DRAFT: frozenset({PlanStatus.ACTIVE, PlanStatus.CANCELLED, PlanStatus.EXPIRED}),
    PlanStatus.ACTIVE: frozenset(
        {PlanStatus.TRIGGERED, PlanStatus.CANCELLED, PlanStatus.EXPIRED, PlanStatus.REVIEWED}
    ),
    PlanStatus.TRIGGERED: frozenset(
        {PlanStatus.CANCELLED, PlanStatus.EXPIRED, PlanStatus.REVIEWED}
    ),
    PlanStatus.CANCELLED: frozenset({PlanStatus.REVIEWED}),
    PlanStatus.EXPIRED: frozenset({PlanStatus.REVIEWED}),
    PlanStatus.REVIEWED: frozenset(),
}


def transition_plan(
    plan: TradingPlan,
    *,
    target_status: PlanStatus,
    actor_id: str,
    occurred_at: datetime,
    reason: str,
    approval_interaction_id: str | None = None,
) -> TradingPlan:
    if target_status not in _ALLOWED_TRANSITIONS[plan.status]:
        raise ValueError(f"不允许从 {plan.status.value} 迁移到 {target_status.value}")
    if target_status is PlanStatus.ACTIVE and plan.missing_fields:
        raise ValueError(f"计划仍缺少风险关键字段: {', '.join(plan.missing_fields)}")
    transition = PlanTransition(
        plan.status,
        target_status,
        actor_id,
        occurred_at,
        reason,
        approval_interaction_id,
    )
    return replace(
        plan,
        version=plan.version + 1,
        status=target_status,
        transitions=(*plan.transitions, transition),
        supersedes_version=plan.version,
    )


def _is_us_security(security_id: str) -> bool:
    parts = security_id.split(":")
    return len(parts) >= 3 and parts[0] == "US" and all(part.strip() for part in parts[1:])


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
