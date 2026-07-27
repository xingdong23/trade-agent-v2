"""提醒规则、观测与触发事件。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from math import isfinite

from trade_agent.core.llm.contracts import JsonValue


class ReminderRuleType(StrEnum):
    PRICE_THRESHOLD = "price_threshold"
    SCHEDULED_REVIEW = "scheduled_review"
    INVALIDATION = "invalidation"


class ReminderStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class ThresholdDirection(StrEnum):
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReminderRule:
    reminder_id: str
    owner_id: str
    plan_id: str
    version: int
    status: ReminderStatus
    rule_type: ReminderRuleType
    condition: Mapping[str, JsonValue]
    notification_channel: str = "in_app"
    cooldown: timedelta = timedelta()
    approved_by: str | None = None
    approved_payload_hash: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.reminder_id.strip(), self.owner_id.strip(), self.plan_id.strip())):
            raise ValueError("reminder、owner 与 plan 标识不能为空")
        if self.version < 1:
            raise ValueError("reminder version 必须从 1 开始")
        if not self.notification_channel.strip():
            raise ValueError("notification channel 不能为空")
        if self.cooldown < timedelta():
            raise ValueError("cooldown 不能为负数")
        validate_condition(self.rule_type, self.condition)
        if self.status is ReminderStatus.ACTIVE and (
            not self.approved_by or not self.approved_payload_hash
        ):
            raise ValueError("active reminder 必须保留批准者与 payload hash")

    def transition(
        self,
        target: ReminderStatus,
        *,
        approved: bool,
        actor_id: str,
        payload_hash: str,
    ) -> ReminderRule:
        if target not in {ReminderStatus.ACTIVE, ReminderStatus.DISABLED}:
            raise ValueError("提醒只能经审批启用或停用")
        if not approved or not actor_id.strip() or not payload_hash.strip():
            raise PermissionError("启用或停用提醒必须经过明确审批")
        if target is self.status:
            return self
        if self.status not in {
            ReminderStatus.DRAFT,
            ReminderStatus.ACTIVE,
            ReminderStatus.DISABLED,
        }:
            raise ValueError(f"不允许从 {self.status} 迁移提醒状态")
        return replace(
            self,
            version=self.version + 1,
            status=target,
            approved_by=actor_id,
            approved_payload_hash=payload_hash,
        )


@dataclass(frozen=True, slots=True)
class ReminderObservation:
    reminder_id: str
    observed_at: datetime
    observation_reference: str
    value: float | bool | None
    fresh: bool = True

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("reminder observation 时间必须包含时区")
        if not self.observation_reference.strip():
            raise ValueError("observation reference 不能为空")
        if isinstance(self.value, float) and not isfinite(self.value):
            raise ValueError("observation value 必须是有限数值")


@dataclass(frozen=True, slots=True)
class ReminderTrigger:
    trigger_id: str
    reminder_id: str
    rule_version: int
    observed_at: datetime
    observation_reference: str
    message: str
    indicates_execution: bool = False
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING
    delivery_attempts: int = 0
    delivery_reference: str | None = None
    delivery_error: str | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("reminder trigger 时间必须包含时区")
        if self.indicates_execution:
            raise ValueError("提醒触发不得表示下单或成交")
        if self.rule_version < 1 or self.delivery_attempts < 0:
            raise ValueError("trigger version/attempts 非法")


def validate_condition(rule_type: ReminderRuleType, condition: Mapping[str, JsonValue]) -> None:
    if rule_type is ReminderRuleType.PRICE_THRESHOLD:
        threshold = condition.get("threshold")
        direction = condition.get("direction")
        security_id = condition.get("security_id")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int | float)
            or not isfinite(float(threshold))
            or not isinstance(security_id, str)
            or not security_id.strip()
        ):
            raise ValueError("price threshold 必须包含证券与有限阈值")
        try:
            ThresholdDirection(str(direction))
        except ValueError as exc:
            raise ValueError("price threshold direction 非法") from exc
        return
    if rule_type is ReminderRuleType.SCHEDULED_REVIEW:
        _condition_datetime(condition, "scheduled_at")
        return
    if rule_type is ReminderRuleType.INVALIDATION:
        key = condition.get("condition_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("invalidation rule 必须包含 condition_key")
        return
    raise ValueError(f"不支持的 reminder rule type: {rule_type}")


def should_trigger(
    rule: ReminderRule,
    observation: ReminderObservation,
    *,
    previous: ReminderObservation | None,
    latest_trigger: ReminderTrigger | None,
) -> bool:
    if rule.status is not ReminderStatus.ACTIVE or not observation.fresh:
        return False
    if latest_trigger is not None and observation.observed_at < (
        latest_trigger.observed_at + rule.cooldown
    ):
        return False
    if rule.rule_type is ReminderRuleType.PRICE_THRESHOLD:
        if previous is None:
            return False
        current = _numeric_observation(observation)
        prior = _numeric_observation(previous)
        threshold = float(_number(rule.condition, "threshold"))
        direction = ThresholdDirection(str(rule.condition["direction"]))
        if direction is ThresholdDirection.CROSSES_ABOVE:
            return prior < threshold <= current
        return prior > threshold >= current
    if rule.rule_type is ReminderRuleType.SCHEDULED_REVIEW:
        return latest_trigger is None and observation.observed_at >= _condition_datetime(
            rule.condition, "scheduled_at"
        )
    if rule.rule_type is ReminderRuleType.INVALIDATION:
        current = _boolean_observation(observation)
        prior = False if previous is None else _boolean_observation(previous)
        return not prior and current
    return False


def build_trigger(rule: ReminderRule, observation: ReminderObservation) -> ReminderTrigger:
    raw_key = (
        f"{rule.reminder_id}:{rule.version}:{observation.observation_reference}:"
        f"{observation.observed_at.isoformat()}"
    )
    trigger_id = f"trigger-{sha256(raw_key.encode('utf-8')).hexdigest()[:24]}"
    return ReminderTrigger(
        trigger_id=trigger_id,
        reminder_id=rule.reminder_id,
        rule_version=rule.version,
        observed_at=observation.observed_at,
        observation_reference=observation.observation_reference,
        message="提醒条件已满足: 这只是条件观察 / 不表示已下单或成交。",
    )


def _condition_datetime(condition: Mapping[str, JsonValue], key: str) -> datetime:
    raw = condition.get(key)
    if not isinstance(raw, str):
        raise ValueError(f"{key} 必须是 ISO datetime")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是 ISO datetime") from exc
    if value.tzinfo is None:
        raise ValueError(f"{key} 必须包含时区")
    return value


def _number(condition: Mapping[str, JsonValue], key: str) -> int | float:
    value = condition.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key} 必须是数值")
    return value


def _numeric_observation(observation: ReminderObservation) -> float:
    if isinstance(observation.value, bool) or not isinstance(observation.value, int | float):
        raise ValueError("price observation 必须是数值")
    return float(observation.value)


def _boolean_observation(observation: ReminderObservation) -> bool:
    if not isinstance(observation.value, bool):
        raise ValueError("invalidation observation 必须是 boolean")
    return observation.value


__all__ = [
    "DeliveryStatus",
    "ReminderObservation",
    "ReminderRule",
    "ReminderRuleType",
    "ReminderStatus",
    "ReminderTrigger",
    "ThresholdDirection",
    "build_trigger",
    "should_trigger",
    "validate_condition",
]
