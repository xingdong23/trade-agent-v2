"""量化薄 tool、Agent 白名单与确定性 Card presenter 测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

import pytest

from trade_agent.agents.research import MANIFEST as RESEARCH_AGENT
from trade_agent.agents.strategy import MANIFEST as STRATEGY_AGENT
from trade_agent.capabilities.contracts import CapabilityResult
from trade_agent.capabilities.quantitative.cards import QuantitativeCardPresenter
from trade_agent.capabilities.quantitative.tools import (
    GetPredictionTool,
    GetQuantitativeSnapshotTool,
    GetScanStatusTool,
    ListScanResultsTool,
    SubmitScanTool,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import DEFAULT_CARD_CATALOG, CardEnvelope
from trade_agent.core.tools import ToolExecutionContext, ToolExecutionPrincipal, ToolRequest


class FakeQuantitativeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, JsonValue], str | None]] = []

    async def get_prediction(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        self.calls.append(("get_prediction", arguments, None))
        return {"status": "available", "probability": 0.72}

    async def get_quantitative_snapshot(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        self.calls.append(("get_quantitative_snapshot", arguments, None))
        return {"status": "available"}

    async def submit_scan(
        self, arguments: Mapping[str, JsonValue], *, idempotency_key: str
    ) -> Mapping[str, JsonValue]:
        self.calls.append(("submit_scan", arguments, idempotency_key))
        return {"scan_id": "scan-1", "status": "queued"}

    async def get_scan_status(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        self.calls.append(("get_scan_status", arguments, None))
        return {"scan_id": "scan-1", "status": "running"}

    async def list_scan_results(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        self.calls.append(("list_scan_results", arguments, None))
        return {"scan_id": "scan-1", "results": []}


def _security_arguments() -> dict[str, JsonValue]:
    return {
        "owner_id": "owner-a",
        "security_id": "US:NASDAQ:NVDA",
        "target": "direction",
        "horizon": "5d",
        "as_of": "2026-07-27T09:30:00Z",
    }


def _scan_arguments() -> dict[str, JsonValue]:
    return {
        "owner_id": "owner-a",
        "strategy_id": "strategy-1",
        "strategy_version": 2,
        "universe_snapshot_id": "universe-1",
        "data_snapshot_id": "data-1",
        "feature_snapshot_id": "features-1",
        "model_version_id": "model-7",
        "ranking_function_version": "rank.v1",
        "configuration": {"max_candidates": 20},
    }


def _trusted_context() -> ToolExecutionContext:
    return ToolExecutionContext(ToolExecutionPrincipal(owner_id="owner-a"))


def test_quantitative_tools_delegate_without_business_logic() -> None:
    application = FakeQuantitativeApplication()
    prediction = asyncio.run(
        GetPredictionTool(application).handle(
            ToolRequest(
                "quantitative.get_prediction",
                {key: value for key, value in _security_arguments().items() if key != "owner_id"},
                context=_trusted_context(),
            )
        )
    )
    snapshot = asyncio.run(
        GetQuantitativeSnapshotTool(application).handle(
            ToolRequest(
                "quantitative.get_quantitative_snapshot",
                {key: value for key, value in _security_arguments().items() if key != "owner_id"},
                context=_trusted_context(),
            )
        )
    )
    submitted = asyncio.run(
        SubmitScanTool(application).handle(
            ToolRequest(
                "quantitative.submit_scan",
                {key: value for key, value in _scan_arguments().items() if key != "owner_id"},
                "submit-1",
                context=_trusted_context(),
            )
        )
    )
    status = asyncio.run(
        GetScanStatusTool(application).handle(
            ToolRequest(
                "quantitative.get_scan_status",
                {"scan_id": "scan-1"},
                context=_trusted_context(),
            )
        )
    )
    results = asyncio.run(
        ListScanResultsTool(application).handle(
            ToolRequest(
                "quantitative.list_scan_results",
                {"scan_id": "scan-1"},
                context=_trusted_context(),
            )
        )
    )
    assert prediction.payload["probability"] == 0.72
    assert snapshot.payload["status"] == "available"
    assert submitted.payload["status"] == "queued"
    assert status.payload["status"] == "running"
    assert results.payload["results"] == []
    assert [call[0] for call in application.calls] == [
        "get_prediction",
        "get_quantitative_snapshot",
        "submit_scan",
        "get_scan_status",
        "list_scan_results",
    ]
    assert all(call[1]["owner_id"] == "owner-a" for call in application.calls)


def test_scan_submission_declares_and_requires_hitl_idempotency() -> None:
    tool = SubmitScanTool(FakeQuantitativeApplication())
    assert tool.manifest.requires_hitl
    assert tool.manifest.requires_idempotency_key
    assert tool.manifest.side_effect == "create_scan_job"
    with pytest.raises(ValueError, match="idempotency key"):
        asyncio.run(tool.handle(ToolRequest("quantitative.submit_scan", _scan_arguments())))


def test_agent_manifests_expose_only_intended_quantitative_tools() -> None:
    assert "quantitative.get_prediction" in RESEARCH_AGENT.allowed_tool_ids
    assert "quantitative.get_quantitative_snapshot" in RESEARCH_AGENT.allowed_tool_ids
    assert "quantitative.submit_scan" not in RESEARCH_AGENT.allowed_tool_ids
    assert "quantitative.submit_scan" in STRATEGY_AGENT.allowed_tool_ids
    assert "quantitative.get_scan_status" in STRATEGY_AGENT.allowed_tool_ids
    assert "quantitative.list_scan_results" in STRATEGY_AGENT.allowed_tool_ids


def _data(card: CardEnvelope) -> dict[str, Any]:
    return cast(dict[str, Any], card.data)


def test_quantitative_cards_preserve_lineage_gaps_and_persisted_scores() -> None:
    presenter = QuantitativeCardPresenter()
    snapshot = presenter.present(
        CapabilityResult(
            "prediction-1",
            1,
            {
                "card_type": "quantitative_snapshot",
                "security_id": "US:NASDAQ:NVDA",
                "status": "available",
                "target": "direction",
                "horizon": "5d",
                "distribution": {"up_probability": 0.72},
                "applicability": {"market": "US"},
                "model_version_id": "model-7",
                "feature_snapshot_id": "features-1",
                "gaps": [],
            },
        )
    )
    result = presenter.present(
        CapabilityResult(
            "result-1",
            3,
            {
                "card_type": "scan_result",
                "security_id": "US:NASDAQ:NVDA",
                "status": "match",
                "score": 0.81,
                "probability": 0.72,
                "matched_conditions": ["trend_20d > 0"],
                "exclusions": [],
                "risks": ["event risk"],
                "gaps": ["short interest unavailable"],
                "evidence_ids": ["evidence-1"],
                "model_version_id": "model-7",
                "feature_snapshot_id": "features-1",
            },
        )
    )
    progress = presenter.present(
        CapabilityResult(
            "scan-1",
            2,
            {
                "card_type": "scan_progress",
                "status": "running",
                "completed": 4,
                "total": 10,
                "current_step": "NVDA",
                "eta_seconds": 15,
            },
        )
    )
    assert snapshot.kind == "artifact.quantitative_snapshot"
    assert "model-7" in str(_data(snapshot)["provenance"])
    assert result.kind == "artifact.scan_result"
    assert "score=0.81" in _data(result)["summary"]
    assert "short interest unavailable" in str(_data(result)["sections"])
    assert progress.kind == "progress.scan"
    assert _data(progress)["progress"] == 40
    assert progress.actions == ("cancel",)
    for card in (snapshot, result, progress):
        assert DEFAULT_CARD_CATALOG.supports(card.kind, card.schema_version)
