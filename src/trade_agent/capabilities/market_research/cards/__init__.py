"""研究 artifact、progress、data gap 与 failure 的确定性 Card presenter。"""

from typing import Any

from trade_agent.capabilities.market_research.contracts import (
    CapabilityCardPresenter,
    CapabilityResult,
)
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource


class MarketResearchCardPresenter(CapabilityCardPresenter):
    def present(self, result: CapabilityResult) -> CardEnvelope:
        card_type = _string(result.payload, "card_type")
        if card_type == "research_artifact":
            return self.artifact(result)
        if card_type == "research_progress":
            return self.progress(result)
        if card_type == "data_gap":
            return self.data_gap(result)
        if card_type == "failure":
            return self.failure(result)
        raise ValueError(f"不支持的 research card_type: {card_type}")

    def artifact(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        title = _string(payload, "title")
        evidence = _object_list(payload.get("evidence"))
        sections = _object_list(payload.get("sections"))
        data: dict[str, Any] = {
            "title": title,
            "summary": _string(payload, "summary"),
            "sections": [
                {
                    "title": _string(item, "title"),
                    "content": _string(item, "content"),
                    "kind": _string(item, "kind"),
                }
                for item in sections
            ],
            "provenance": [
                {
                    "label": _string(item, "provider"),
                    "value": (
                        f"{_string(item, 'source_reference')} | "
                        f"freshness={_string(item, 'freshness')} | "
                        f"retrieved_at={_string(item, 'retrieved_at')}"
                    ),
                    "source_id": _string(item, "evidence_id"),
                    "source_type": "evidence_snapshot",
                }
                for item in evidence
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"research:{result.reference_id}",
            "artifact.research",
            1,
            result.version,
            CardSource("research_artifact", result.reference_id, result.version),
            "resolved",
            data,
            text_fallback=title,
        )

    def progress(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        data: dict[str, Any] = {
            "title": _string(payload, "title"),
            "message": _string(payload, "message"),
            "progress": payload.get("progress"),
            "current_step": payload.get("current_step"),
            "eta_seconds": payload.get("eta_seconds"),
        }
        status = _string(payload, "status")
        actions = ("cancel", "retry") if status in {"running", "failed"} else ()
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"research-progress:{result.reference_id}",
            "progress.research",
            1,
            result.version,
            CardSource("research_job", result.reference_id, result.version),
            "pending" if actions else "resolved",
            data,
            actions,
            text_fallback=_string(payload, "message"),
        )

    def data_gap(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        missing = _strings(payload.get("missing_fields"))
        data: dict[str, Any] = {
            "title": _string(payload, "title"),
            "message": _string(payload, "message"),
            "missing_fields": missing,
            "provenance": _provenance(payload.get("evidence")),
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"research-gap:{result.reference_id}",
            "notice.data_gap",
            1,
            result.version,
            CardSource("research_artifact", result.reference_id, result.version),
            "failed",
            data,
            ("retry", "cancel"),
            text_fallback=f"研究数据缺口: {', '.join(missing)}",
        )

    def failure(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        data: dict[str, Any] = {
            "title": _string(payload, "title"),
            "message": _string(payload, "message"),
            "error_code": _string(payload, "error_code"),
            "retryable": _boolean(payload, "retryable"),
        }
        actions = ("retry", "cancel") if data["retryable"] else ("cancel",)
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"research-failure:{result.reference_id}",
            "notice.failure",
            1,
            result.version,
            CardSource("research_job", result.reference_id, result.version),
            "failed",
            data,
            actions,
            text_fallback=_string(payload, "message"),
        )


def _string(payload: Any, key: str) -> str:
    if not isinstance(payload, dict):
        payload = dict(payload)
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def _boolean(payload: Any, key: str) -> bool:
    if not isinstance(payload, dict):
        payload = dict(payload)
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是 boolean")
    return bool(value)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("期望字符串数组")
    return value


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("期望 object 数组")
    return value


def _provenance(value: Any) -> list[dict[str, str]] | None:
    if value is None:
        return None
    return [
        {
            "label": _string(item, "provider"),
            "value": _string(item, "source_reference"),
            "source_id": _string(item, "evidence_id"),
            "source_type": "evidence_snapshot",
        }
        for item in _object_list(value)
    ]


__all__ = ["MarketResearchCardPresenter"]
