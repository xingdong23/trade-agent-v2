"""提醒 application service 与定时 worker。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from trade_agent.capabilities.reminder.contracts import (
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
    DeliveryStatus,
    ReminderRule,
    ReminderRuleType,
    ReminderStatus,
    ReminderTrigger,
)
from trade_agent.capabilities.reminder.domain import build_trigger, should_trigger
from trade_agent.capabilities.reminder.ports import (
    NotificationDeliveryError,
    NotificationProvider,
    ReminderObservationProvider,
    ReminderRepository,
)
from trade_agent.core.llm.contracts import JsonValue


class ReminderApplication:
    def __init__(self, repository: ReminderRepository, *, execution_disclaimer: str) -> None:
        if not execution_disclaimer.strip():
            raise ValueError("reminder execution_disclaimer 不能为空")
        self._repository = repository
        self._execution_disclaimer = execution_disclaimer

    async def execute(self, command: CapabilityCommand) -> CapabilityResult:
        if command.command_id == "reminder.create":
            return self._create(command.payload)
        if command.command_id == "reminder.set_status":
            return self._set_status(command.payload)
        raise ValueError(f"不支持的 reminder command: {command.command_id}")

    async def query(self, query: CapabilityQuery) -> CapabilityResult:
        if query.query_id != "reminder.get":
            raise ValueError(f"不支持的 reminder query: {query.query_id}")
        owner_id = _string(query.parameters, "owner_id")
        reminder_id = _string(query.parameters, "reminder_id")
        rule = self._repository.get_rule(owner_id, reminder_id)
        if rule is None:
            raise LookupError("reminder 不存在或不属于当前 owner")
        return _result(rule, execution_disclaimer=self._execution_disclaimer)

    def _create(self, payload: Mapping[str, JsonValue]) -> CapabilityResult:
        idempotency_key = _string(payload, "idempotency_key")
        reminder_id = _string(payload, "reminder_id")
        owner_id = _string(payload, "owner_id")
        raw_condition = payload.get("condition")
        if not isinstance(raw_condition, Mapping):
            raise ValueError("condition 必须是 JSON object")
        condition = {str(key): value for key, value in raw_condition.items()}
        rule = ReminderRule(
            reminder_id=reminder_id,
            owner_id=owner_id,
            plan_id=_string(payload, "plan_id"),
            version=1,
            status=ReminderStatus.DRAFT,
            rule_type=ReminderRuleType(_string(payload, "rule_type")),
            condition=condition,
            notification_channel=_string(payload, "notification_channel"),
            cooldown=timedelta(seconds=_non_negative_integer(payload, "cooldown_seconds")),
        )
        saved = self._repository.save_rule(
            rule, expected_version=0, idempotency_key=idempotency_key
        )
        return _result(saved, execution_disclaimer=self._execution_disclaimer)

    def _set_status(self, payload: Mapping[str, JsonValue]) -> CapabilityResult:
        owner_id = _string(payload, "owner_id")
        reminder_id = _string(payload, "reminder_id")
        current = self._repository.get_rule(owner_id, reminder_id)
        if current is None:
            raise LookupError("reminder 不存在或不属于当前 owner")
        target = ReminderStatus(_string(payload, "target_status"))
        updated = current.transition(
            target,
            approved=_boolean(payload, "approved"),
            actor_id=_string(payload, "actor_id"),
            payload_hash=_string(payload, "payload_hash"),
        )
        if updated is current:
            return _result(current, execution_disclaimer=self._execution_disclaimer)
        saved = self._repository.save_rule(
            updated,
            expected_version=current.version,
            idempotency_key=_string(payload, "idempotency_key"),
        )
        return _result(saved, execution_disclaimer=self._execution_disclaimer)


@dataclass(frozen=True, slots=True)
class ReminderDeliveryPolicy:
    """定义提醒通知模板、重试预算和退避计划。

    Attributes:
        policy_version: 可写入通知 payload 的稳定策略版本。
        template_id: Notification provider 使用的模板标识。
        max_attempts: 单个 trigger 的最大投递尝试次数。
        retry_delays: 每次可重试失败后的等待秒数。
        unavailable_error: provider 未返回错误文本时保存的稳定错误说明。
        trigger_message: 创建提醒触发时使用的用户可见文案。
    """

    policy_version: str
    template_id: str
    max_attempts: int
    retry_delays: tuple[float, ...]
    unavailable_error: str
    trigger_message: str

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.policy_version,
                self.template_id,
                self.unavailable_error,
                self.trigger_message,
            )
        ):
            raise ValueError("reminder delivery policy 标识与错误说明不能为空")
        if self.max_attempts < 1:
            raise ValueError("notification delivery 至少尝试一次")
        if len(self.retry_delays) != self.max_attempts - 1:
            raise ValueError("retry delay 数量必须与重试预算一致")
        if any(delay < 0 for delay in self.retry_delays):
            raise ValueError("retry delay 不能为负数")


class ReminderWorker:
    """在 request graph 外评估活跃提醒并投递去重通知。"""

    def __init__(
        self,
        repository: ReminderRepository,
        observations: ReminderObservationProvider,
        notifications: NotificationProvider,
        *,
        delivery_policy: ReminderDeliveryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._repository = repository
        self._observations = observations
        self._notifications = notifications
        self._delivery_policy = delivery_policy
        self._sleep = sleep

    async def run_once(self, *, now: datetime) -> tuple[ReminderTrigger, ...]:
        if now.tzinfo is None:
            raise ValueError("worker now 必须包含时区")
        completed: list[ReminderTrigger] = []
        for rule in self._repository.list_active_rules():
            observation = await self._observations.observe(rule, now=now)
            previous = self._repository.previous_observation(rule.reminder_id, rule.version)
            latest = self._repository.latest_trigger(rule.reminder_id, rule.version)
            if not observation.fresh:
                continue
            matched = should_trigger(
                rule,
                observation,
                previous=previous,
                latest_trigger=latest,
            )
            self._repository.save_observation(rule.reminder_id, rule.version, observation)
            if not matched:
                continue
            trigger = build_trigger(
                rule,
                observation,
                message=self._delivery_policy.trigger_message,
            )
            if not self._repository.record_trigger(trigger):
                continue
            delivered = await self._deliver(rule, trigger)
            completed.append(self._repository.update_trigger(delivered))
        return tuple(completed)

    async def _deliver(self, rule: ReminderRule, trigger: ReminderTrigger) -> ReminderTrigger:
        last_error: str | None = None
        attempts = 0
        for attempt in range(1, self._delivery_policy.max_attempts + 1):
            attempts = attempt
            try:
                delivery_reference = await self._notifications.deliver(
                    recipient_id=rule.owner_id,
                    channel=rule.notification_channel,
                    template_id=self._delivery_policy.template_id,
                    payload={
                        "reminder_id": rule.reminder_id,
                        "plan_id": rule.plan_id,
                        "trigger_id": trigger.trigger_id,
                        "message": trigger.message,
                        "indicates_execution": False,
                        "delivery_policy_version": self._delivery_policy.policy_version,
                    },
                    idempotency_key=trigger.trigger_id,
                )
            except NotificationDeliveryError as exc:
                last_error = str(exc) or type(exc).__name__
                if not exc.retryable:
                    break
                if attempt < self._delivery_policy.max_attempts:
                    await self._sleep(self._delivery_policy.retry_delays[attempt - 1])
                continue
            return replace(
                trigger,
                delivery_status=DeliveryStatus.DELIVERED,
                delivery_attempts=attempt,
                delivery_reference=delivery_reference,
            )
        return replace(
            trigger,
            delivery_status=DeliveryStatus.FAILED,
            delivery_attempts=attempts,
            delivery_error=last_error or self._delivery_policy.unavailable_error,
        )


def _result(rule: ReminderRule, *, execution_disclaimer: str) -> CapabilityResult:
    return CapabilityResult(
        rule.reminder_id,
        rule.version,
        {
            "card_type": "reminder",
            "reminder_id": rule.reminder_id,
            "owner_id": rule.owner_id,
            "plan_id": rule.plan_id,
            "status": rule.status.value,
            "rule_type": rule.rule_type.value,
            "condition": dict(rule.condition),
            "notification_channel": rule.notification_channel,
            "cooldown_seconds": int(rule.cooldown.total_seconds()),
            "approved_by": rule.approved_by,
            "approved_payload_hash": rule.approved_payload_hash,
            "execution_disclaimer": execution_disclaimer,
        },
    )


def _string(payload: Mapping[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def _boolean(payload: Mapping[str, JsonValue], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是 boolean")
    return value


def _non_negative_integer(payload: Mapping[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} 必须是非负整数")
    return value


__all__ = ["ReminderApplication", "ReminderDeliveryPolicy", "ReminderWorker"]
