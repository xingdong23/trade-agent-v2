"""提醒 application service 与定时 worker。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
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
    def __init__(self, repository: ReminderRepository) -> None:
        self._repository = repository

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
        return _result(rule)

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
        return _result(saved)

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
            return _result(current)
        saved = self._repository.save_rule(
            updated,
            expected_version=current.version,
            idempotency_key=_string(payload, "idempotency_key"),
        )
        return _result(saved)


class ReminderWorker:
    """在 request graph 外评估活跃提醒并投递去重通知。"""

    def __init__(
        self,
        repository: ReminderRepository,
        observations: ReminderObservationProvider,
        notifications: NotificationProvider,
        *,
        max_delivery_attempts: int = 3,
        retry_delays: tuple[float, ...] = (0.0, 0.0),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_delivery_attempts < 1:
            raise ValueError("notification delivery 至少尝试一次")
        if len(retry_delays) < max_delivery_attempts - 1:
            raise ValueError("retry delay 数量不足")
        self._repository = repository
        self._observations = observations
        self._notifications = notifications
        self._max_delivery_attempts = max_delivery_attempts
        self._retry_delays = retry_delays
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
            trigger = build_trigger(rule, observation)
            if not self._repository.record_trigger(trigger):
                continue
            delivered = await self._deliver(rule, trigger)
            completed.append(self._repository.update_trigger(delivered))
        return tuple(completed)

    async def _deliver(self, rule: ReminderRule, trigger: ReminderTrigger) -> ReminderTrigger:
        last_error: str | None = None
        attempts = 0
        for attempt in range(1, self._max_delivery_attempts + 1):
            attempts = attempt
            try:
                delivery_reference = await self._notifications.deliver(
                    recipient_id=rule.owner_id,
                    channel=rule.notification_channel,
                    template_id="reminder.triggered.v1",
                    payload={
                        "reminder_id": rule.reminder_id,
                        "plan_id": rule.plan_id,
                        "trigger_id": trigger.trigger_id,
                        "message": trigger.message,
                        "indicates_execution": False,
                    },
                    idempotency_key=trigger.trigger_id,
                )
            except NotificationDeliveryError as exc:
                last_error = str(exc) or type(exc).__name__
                if not exc.retryable:
                    break
                if attempt < self._max_delivery_attempts:
                    await self._sleep(self._retry_delays[attempt - 1])
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
            delivery_error=last_error or "notification unavailable",
        )


def _result(rule: ReminderRule) -> CapabilityResult:
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
            "execution_disclaimer": "提醒仅表示条件观察与通知 / 不表示下单或成交。",
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


__all__ = ["ReminderApplication", "ReminderWorker"]
