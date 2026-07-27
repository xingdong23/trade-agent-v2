"""提醒 capability 的持久化、观测与通知端口。"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from trade_agent.capabilities.reminder.contracts import (
    ReminderObservation,
    ReminderRule,
    ReminderTrigger,
)
from trade_agent.core.llm.contracts import JsonValue


class NotificationDeliveryError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ReminderRepository(Protocol):
    def save_rule(
        self,
        rule: ReminderRule,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderRule: ...

    def get_rule(self, owner_id: str, reminder_id: str) -> ReminderRule | None: ...

    def list_active_rules(self) -> Sequence[ReminderRule]: ...

    def previous_observation(
        self, reminder_id: str, rule_version: int
    ) -> ReminderObservation | None: ...

    def save_observation(
        self, reminder_id: str, rule_version: int, observation: ReminderObservation
    ) -> None: ...

    def latest_trigger(self, reminder_id: str, rule_version: int) -> ReminderTrigger | None: ...

    def record_trigger(self, trigger: ReminderTrigger) -> bool: ...

    def update_trigger(self, trigger: ReminderTrigger) -> ReminderTrigger: ...


class ReminderObservationProvider(Protocol):
    async def observe(self, rule: ReminderRule, *, now: datetime) -> ReminderObservation: ...


class NotificationProvider(Protocol):
    async def deliver(
        self,
        *,
        recipient_id: str,
        channel: str,
        template_id: str,
        payload: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> str: ...


__all__ = [
    "NotificationDeliveryError",
    "NotificationProvider",
    "ReminderObservationProvider",
    "ReminderRepository",
]
