"""仅从已持久化扫描结果生成 LLM 可消费的只读事实投影。"""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class PersistedScanResultView:
    """表示已持久化扫描结果的只读投影视图。

    Attributes:
        scan_id: 所属扫描标识。
        result_id: 结果稳定标识。
        result_version: 持久化结果版本号。
        payload: 供后续摘要或展示使用的原始结构化载荷。
    """

    scan_id: str
    result_id: str
    result_version: int
    payload: Mapping[str, JsonValue]


class ScanResultSummaryProjector:
    """不产生 score/ranking; 只复制已持久化结果供后续 LLM 总结。"""

    @staticmethod
    def project(results: Sequence[PersistedScanResultView]) -> tuple[dict[str, JsonValue], ...]:
        return tuple(
            {
                "scan_id": result.scan_id,
                "result_id": result.result_id,
                "result_version": result.result_version,
                "persisted_payload": deepcopy(dict(result.payload)),
            }
            for result in results
        )
