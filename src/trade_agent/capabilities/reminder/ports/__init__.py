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
    """提醒规则、观测与触发记录的持久化协议。

    Contract:
        - 规则写入必须校验版本和幂等键，并按 owner 隔离查询。
        - 触发记录必须可去重，历史观测不能被静默覆盖。

    Implemented by:
        部署注册的提醒 repository 与 ``FakeReminderRepository`` 测试实现。
    """

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
    """按提醒规则获取当前结构化观测的 provider 协议。

    Contract:
        - 只返回调用时点可获得的数据，不执行提醒状态迁移。
        - provider 不可用时必须显式失败，不能伪造观测值。

    Implemented by:
        部署注册的市场观测 adapter 与 ``ConstantObservationProvider`` 测试实现。
    """

    async def observe(self, rule: ReminderRule, *, now: datetime) -> ReminderObservation: ...


class NotificationProvider(Protocol):
    """幂等投递提醒通知的 provider 协议。

    Contract:
        - 同一幂等键重试必须复用投递结果，不产生重复通知。
        - 投递只表示通知，不得表达下单或成交语义。

    Implemented by:
        ``InMemoryNotificationAdapter`` 与后续注册的真实通知 adapter。
    """

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
