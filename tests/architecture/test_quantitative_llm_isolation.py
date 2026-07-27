"""量化预测、扫描、评分与排序不得依赖 LLM。"""

from pathlib import Path

from trade_agent.capabilities.quantitative.application.summary import (
    PersistedScanResultView,
    ScanResultSummaryProjector,
)
from trade_agent.core.llm.contracts import JsonValue

ROOT = Path(__file__).parents[2] / "src" / "trade_agent"


def test_quantitative_execution_modules_do_not_reference_litellm_or_llm_client() -> None:
    roots = (
        ROOT / "capabilities" / "quantitative" / "domain",
        ROOT / "capabilities" / "quantitative" / "application",
        ROOT / "adapters" / "model_runtime",
    )
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if path.name == "summary.py":
                continue
            content = path.read_text(encoding="utf-8").lower()
            if "litellm" in content or "llmclient" in content or "llm_client" in content:
                violations.append(str(path.relative_to(ROOT)))
    assert not violations


def test_summary_projection_only_copies_persisted_scan_results() -> None:
    payload: dict[str, JsonValue] = {
        "score": 0.8,
        "rank": 1,
        "model_version_id": "model-7",
    }
    projected = ScanResultSummaryProjector.project(
        (PersistedScanResultView("scan-1", "result-1", 3, payload),)
    )
    assert projected[0]["persisted_payload"] == payload
    persisted = projected[0]["persisted_payload"]
    assert isinstance(persisted, dict)
    persisted["score"] = 0.1
    assert payload["score"] == 0.8
