"""Reminder artifact 与 unsupported notice 的确定性 presenter。"""

import json
from collections.abc import Mapping
from typing import Any

from trade_agent.capabilities.reminder.contracts import CapabilityResult
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource


class ReminderCardPresenter:
    def present(self, result: CapabilityResult) -> CardEnvelope:
        card_type = _string(result.payload, "card_type")
        if card_type == "reminder":
            return self.artifact(result)
        if card_type == "unsupported":
            return self.unsupported(result)
        raise ValueError(f"不支持的 reminder card_type: {card_type}")

    def artifact(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        reminder_id = _string(payload, "reminder_id")
        plan_id = _string(payload, "plan_id")
        status = _string(payload, "status")
        rule_type = _string(payload, "rule_type")
        channel = _string(payload, "notification_channel")
        disclaimer = _string(payload, "execution_disclaimer")
        condition = _mapping(payload.get("condition"), "condition")
        data: dict[str, Any] = {
            "title": f"交易计划提醒 {reminder_id}",
            "summary": f"{rule_type} / {status} / channel={channel}",
            "sections": [
                {
                    "title": "触发规则",
                    "content": json.dumps(
                        condition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                    "kind": "plan",
                },
                {
                    "title": "安全边界",
                    "content": disclaimer,
                    "kind": "risk",
                },
            ],
            "provenance": [
                {
                    "label": "交易计划",
                    "value": plan_id,
                    "source_id": plan_id,
                    "source_type": "trading_plan",
                }
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"reminder:{result.reference_id}",
            "artifact.reminder",
            1,
            result.version,
            CardSource("reminder_rule", result.reference_id, result.version),
            "resolved",
            data,
            text_fallback=f"提醒 {reminder_id}: {status}。{disclaimer}",
        )

    def unsupported(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        message = _string(payload, "message")
        data: dict[str, Any] = {
            "title": _string(payload, "title"),
            "message": message,
            "unsupported_kind": _string(payload, "unsupported_kind"),
            "unsupported_schema_version": _integer(payload, "unsupported_schema_version"),
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"reminder-unsupported:{result.reference_id}",
            "notice.unsupported",
            1,
            result.version,
            CardSource("reminder_request", result.reference_id, result.version),
            "failed",
            data,
            ("refresh",),
            text_fallback=message,
        )


def _string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def _integer(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    return value


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} 必须是 object")
    return {str(item_key): item_value for item_key, item_value in value.items()}


__all__ = ["ReminderCardPresenter"]
