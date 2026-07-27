"""可重复、幂等的通知 adapter; 真实 provider 可按同一 port 替换。"""

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from trade_agent.capabilities.reminder.ports import (
    NotificationDeliveryError,
    NotificationProvider,
)
from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class NotificationAttempt:
    """一次通知投递尝试的留痕记录。

    Attributes:
        recipient_id: 接收方稳定标识。
        channel: 投递渠道，例如 email 或 sms。
        template_id: 所使用的模板标识。
        payload: 模板渲染所需的 JSON 负载。
        idempotency_key: 幂等投递键，同一次通知重放必须复用。
    """

    recipient_id: str
    channel: str
    template_id: str
    payload: Mapping[str, JsonValue]
    idempotency_key: str


class InMemoryNotificationAdapter(NotificationProvider):
    """用于本地 composition/test 的幂等 notification provider。"""

    def __init__(self, *, failures_before_success: int = 0) -> None:
        if failures_before_success < 0:
            raise ValueError("failures_before_success 不能为负数")
        self._remaining_failures = failures_before_success
        self._deliveries: dict[str, str] = {}
        self._attempts: list[NotificationAttempt] = []

    @property
    def attempts(self) -> tuple[NotificationAttempt, ...]:
        return tuple(self._attempts)

    async def deliver(
        self,
        *,
        recipient_id: str,
        channel: str,
        template_id: str,
        payload: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> str:
        if not all(
            value.strip() for value in (recipient_id, channel, template_id, idempotency_key)
        ):
            raise ValueError("通知 recipient/channel/template/idempotency key 不能为空")
        existing = self._deliveries.get(idempotency_key)
        if existing is not None:
            return existing
        self._attempts.append(
            NotificationAttempt(
                recipient_id,
                channel,
                template_id,
                dict(payload),
                idempotency_key,
            )
        )
        if self._remaining_failures:
            self._remaining_failures -= 1
            raise NotificationDeliveryError("notification provider 暂时不可用", retryable=True)
        digest = sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]
        reference = f"notification-{digest}"
        self._deliveries[idempotency_key] = reference
        return reference


__all__ = [
    "InMemoryNotificationAdapter",
    "NotificationAttempt",
    "NotificationDeliveryError",
]
