"""持久化量化预测与扫描数据的确定性 Card presenter。"""

from typing import Any

from trade_agent.capabilities.quantitative.contracts import CapabilityResult
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource


class QuantitativeCardPresenter:
    def present(self, result: CapabilityResult) -> CardEnvelope:
        card_type = _string(result.payload, "card_type")
        if card_type == "quantitative_snapshot":
            return self.snapshot(result)
        if card_type == "scan_result":
            return self.scan_result(result)
        if card_type == "scan_progress":
            return self.scan_progress(result)
        raise ValueError(f"不支持的量化 card_type: {card_type}")

    def snapshot(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        security_id = _string(payload, "security_id")
        status = _string(payload, "status")
        model_version_id = _optional_string(payload.get("model_version_id"), "unavailable")
        feature_snapshot_id = _optional_string(payload.get("feature_snapshot_id"), "unavailable")
        gaps = _strings(payload.get("gaps"))
        data: dict[str, Any] = {
            "title": f"{security_id} 量化快照",
            "summary": (
                f"status={status}; target={_string(payload, 'target')}; "
                f"horizon={_string(payload, 'horizon')}"
            ),
            "sections": [
                {
                    "title": "专用模型输出",
                    "content": _mapping_text(payload.get("distribution")),
                    "kind": "analysis",
                },
                {
                    "title": "适用性与缺口",
                    "content": (
                        f"applicability={_mapping_text(payload.get('applicability'))}; "
                        f"gaps={_join(gaps)}"
                    ),
                    "kind": "risk",
                },
            ],
            "provenance": [
                {
                    "label": "model_version",
                    "value": model_version_id,
                    "source_id": model_version_id,
                    "source_type": "quant_model",
                },
                {
                    "label": "feature_snapshot",
                    "value": feature_snapshot_id,
                    "source_id": feature_snapshot_id,
                    "source_type": "feature_snapshot",
                },
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"quantitative:{result.reference_id}",
            "artifact.quantitative_snapshot",
            1,
            result.version,
            CardSource("quantitative_snapshot", result.reference_id, result.version),
            "resolved",
            data,
            text_fallback=f"{security_id} 量化快照: {status}",
        )

    def scan_result(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        security_id = _string(payload, "security_id")
        status = _string(payload, "status")
        model_version_id = _optional_string(payload.get("model_version_id"), "unavailable")
        feature_snapshot_id = _optional_string(payload.get("feature_snapshot_id"), "unavailable")
        evidence_ids = _strings(payload.get("evidence_ids"))
        gaps = _strings(payload.get("gaps"))
        data: dict[str, Any] = {
            "title": f"{security_id} 扫描结果",
            "summary": (
                f"status={status}; score={payload.get('score')}; "
                f"probability={payload.get('probability')}"
            ),
            "sections": [
                {
                    "title": "条件与排除项",
                    "content": (
                        f"matched={_join(_strings(payload.get('matched_conditions')))}; "
                        f"exclusions={_join(_strings(payload.get('exclusions')))}"
                    ),
                    "kind": "analysis",
                },
                {
                    "title": "风险与数据缺口",
                    "content": (
                        f"risks={_join(_strings(payload.get('risks')))}; gaps={_join(gaps)}"
                    ),
                    "kind": "risk",
                },
            ],
            "provenance": [
                {
                    "label": "model_version",
                    "value": model_version_id,
                    "source_id": model_version_id,
                    "source_type": "quant_model",
                },
                {
                    "label": "feature_snapshot",
                    "value": feature_snapshot_id,
                    "source_id": feature_snapshot_id,
                    "source_type": "feature_snapshot",
                },
                *[
                    {
                        "label": "evidence",
                        "value": evidence_id,
                        "source_id": evidence_id,
                        "source_type": "evidence_snapshot",
                    }
                    for evidence_id in evidence_ids
                ],
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"scan-result:{result.reference_id}",
            "artifact.scan_result",
            1,
            result.version,
            CardSource("scan_result", result.reference_id, result.version),
            "resolved",
            data,
            text_fallback=f"{security_id} 扫描结果: {status}",
        )

    def scan_progress(self, result: CapabilityResult) -> CardEnvelope:
        payload = result.payload
        completed = _integer(payload, "completed")
        total = _integer(payload, "total")
        progress = 0 if total == 0 else int(completed * 100 / total)
        data: dict[str, Any] = {
            "title": "量化扫描进度",
            "message": f"{_string(payload, 'status')}: {completed}/{total}",
            "progress": progress,
            "current_step": _optional_string(payload.get("current_step"), "等待 worker"),
            "eta_seconds": payload.get("eta_seconds"),
        }
        actions = ("cancel",) if _string(payload, "status") in {"queued", "running"} else ()
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"scan-progress:{result.reference_id}",
            "progress.scan",
            1,
            result.version,
            CardSource("scan_job", result.reference_id, result.version),
            "pending" if actions else "resolved",
            data,
            actions,
            text_fallback=f"量化扫描进度 {completed}/{total}",
        )


def _string(payload: Any, key: str) -> str:
    if not isinstance(payload, dict):
        payload = dict(payload)
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def _integer(payload: Any, key: str) -> int:
    if not isinstance(payload, dict):
        payload = dict(payload)
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    return int(value)


def _optional_string(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("期望字符串数组")
    return value


def _join(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _mapping_text(value: Any) -> str:
    if not isinstance(value, dict):
        return "unavailable"
    return "; ".join(f"{key}={value[key]}" for key in sorted(value))


__all__ = ["QuantitativeCardPresenter"]
