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
    """提醒规则类型的稳定枚举。

    Attributes:
        PRICE_THRESHOLD: 价格穿越阈值时触发提醒。
        SCHEDULED_REVIEW: 到达预定时间点时触发复核提醒。
        INVALIDATION: 失效条件由假变真时触发提醒。

    Invariants:
        - 枚举值决定条件校验与触发逻辑，属于稳定规则字段。
    """

    PRICE_THRESHOLD = "price_threshold"
    SCHEDULED_REVIEW = "scheduled_review"
    INVALIDATION = "invalidation"


class ReminderStatus(StrEnum):
    """提醒规则生命周期状态的稳定枚举。

    Attributes:
        DRAFT: 草稿态，尚未启用。
        ACTIVE: 已批准并处于可评估状态。
        DISABLED: 已停用，不再继续评估。

    Invariants:
        - 枚举值驱动审批迁移与 worker 评估范围。
    """

    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class ThresholdDirection(StrEnum):
    """价格阈值提醒的稳定穿越方向枚举。

    Attributes:
        CROSSES_ABOVE: 从阈值下方向上穿越。
        CROSSES_BELOW: 从阈值上方向下穿越。

    Invariants:
        - 枚举值只描述穿越方向，不承载价格或市场语义。
    """

    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"


class DeliveryStatus(StrEnum):
    """提醒投递状态的稳定枚举。

    Attributes:
        PENDING: 触发已创建，尚未完成投递。
        DELIVERED: 已成功投递并保存回执。
        FAILED: 在预算内投递失败。

    Invariants:
        - 枚举值驱动重试、审计与前端展示语义。
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReminderRule:
    """表示一个与交易计划关联的提醒规则版本。

    Attributes:
        reminder_id: 提醒稳定标识。
        owner_id: 资源所有者。
        plan_id: 关联计划标识。
        version: 规则版本。
        status: 当前提醒状态。
        rule_type: 规则类型，例如价格阈值或定时复核。
        condition: 该规则对应的结构化条件载荷。
        notification_channel: 通知投递渠道。
        cooldown: 相邻两次触发之间的冷却时间。
        approved_by: 当前激活或停用状态对应的批准者；未审批时为空。
        approved_payload_hash: 当前审批卡的载荷哈希；未审批时为空。
        metadata: 与规则相关的附加元数据。
    """

    reminder_id: str
    owner_id: str
    plan_id: str
    version: int
    status: ReminderStatus
    rule_type: ReminderRuleType
    condition: Mapping[str, JsonValue]
    notification_channel: str
    cooldown: timedelta
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
    """表示提醒评估时读取到的一次观测值。

    Attributes:
        reminder_id: 对应提醒标识。
        observed_at: 观测时间。
        observation_reference: 观测来源引用。
        value: 当前观测值，可为数值、布尔值或空。
        fresh: 当前观测是否满足时效要求。
    """

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
    """表示一次已生成但尚未必然投递成功的提醒触发。

    Attributes:
        trigger_id: 触发稳定标识。
        reminder_id: 触发来源提醒标识。
        rule_version: 触发时使用的规则版本。
        observed_at: 触发所基于的观测时间。
        observation_reference: 触发所基于的观测来源引用。
        message: 面向用户的提醒文案。
        indicates_execution: 是否暗示已执行交易；该值必须始终为 ``False``。
        delivery_status: 当前投递状态。
        delivery_attempts: 已尝试投递次数。
        delivery_reference: 外部通知系统回执标识；未投递时为空。
        delivery_error: 最近一次投递失败原因；未失败时为空。
    """

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


def build_trigger(
    rule: ReminderRule,
    observation: ReminderObservation,
    *,
    message: str,
) -> ReminderTrigger:
    """基于规则与观测创建稳定触发记录。

    Args:
        rule: 已满足触发条件的提醒规则版本。
        observation: 触发所依据的来源观测。
        message: 由 application 层策略注入的用户可见文案。

    Returns:
        尚待通知投递的不可变提醒触发。

    Raises:
        ValueError: 用户可见文案为空。
    """

    if not message.strip():
        raise ValueError("reminder trigger message 不能为空")
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
        message=message,
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
