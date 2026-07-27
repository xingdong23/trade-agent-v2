"""量化扫描冻结、前置门禁、专用模型调用与排名 lineage 测试。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from trade_agent.capabilities.quantitative.application.scanning import (
    ScanEvaluator,
    ScanSubmissionError,
    ScanSubmissionValidator,
)
from trade_agent.capabilities.quantitative.contracts import (
    ApprovedModelSnapshot,
    BatchInferenceService,
    ComparisonOperator,
    DataFeatureSnapshot,
    EvaluationMetrics,
    EvaluationResult,
    HardRule,
    InferencePolicy,
    ModelRegistry,
    ModelRegistryEntry,
    ModelRuntime,
    ModelStatus,
    RankingDefinition,
    ScanConfiguration,
    ScanDisposition,
    ScanSecurityInput,
    ScanUniverseSnapshot,
    StrategyVersionSnapshot,
)


class RecordingRuntime(ModelRuntime):
    def __init__(self) -> None:
        self.security_rows: list[Mapping[str, float]] = []

    def predict_batch(
        self, model_version_id: str, rows: Sequence[Mapping[str, float]]
    ) -> Sequence[Mapping[str, float]]:
        assert model_version_id == "model-approved"
        self.security_rows.extend(rows)
        return tuple({"up_probability": 0.80 if row["trend"] > 0.3 else 0.55} for row in rows)


def _evaluation() -> EvaluationResult:
    metrics = EvaluationMetrics(0.8, 0.03, 0.8, 0.2, 0.12, 5.0, 0.95)
    baseline = EvaluationMetrics(0.6, 0.07, 0.6, 0.3, 0.05, 3.0, 0.9)
    return EvaluationResult("model-approved", metrics, baseline, True, ())


def _inference(runtime: RecordingRuntime) -> BatchInferenceService:
    registry = ModelRegistry()
    registry.register(
        ModelRegistryEntry(
            "model-approved",
            "US",
            "direction",
            "5d",
            ModelStatus.CANDIDATE,
            _evaluation(),
        )
    )
    registry.approve("model-approved", actor_id="risk-owner")
    return BatchInferenceService(
        registry,
        runtime,
        policy=InferencePolicy("scan-inference.v1", 0.1),
    )


def _security(
    security_id: str,
    *,
    trend: float | None = 0.4,
    liquidity: float = 20_000_000,
    exchange: str = "NASDAQ",
    data_available: bool = True,
) -> ScanSecurityInput:
    return ScanSecurityInput(
        security_id,
        "US",
        exchange,
        liquidity,
        f"features:{security_id}",
        {"trend": trend, "quality": 0.2},
        0.0 if trend is not None else 0.5,
        False,
        data_available,
        (f"evidence:{security_id}",),
        ("market risk",),
        (),
    )


def _submission_inputs() -> dict[str, object]:
    securities = (
        _security("US:NASDAQ:NVDA", trend=0.5),
        _security("US:NASDAQ:MSFT", trend=0.2),
        _security("US:NYSE:LOWLIQ", trend=0.6, liquidity=100_000, exchange="NYSE"),
        _security("US:NASDAQ:MISSING", trend=None),
    )
    return {
        "scan_id": "scan-1",
        "owner_id": "owner-a",
        "strategy": StrategyVersionSnapshot(
            "strategy-1:v3",
            "owner-a",
            True,
            "direction",
            "5d",
            ("trend", "quality"),
            (HardRule("positive-trend", "trend", ComparisonOperator.GREATER_THAN, 0.1),),
        ),
        "universe": ScanUniverseSnapshot(
            "universe-1", "owner-a", tuple(item.security_id for item in securities)
        ),
        "data_features": DataFeatureSnapshot(
            "data-2026-07-27",
            "features-v4",
            datetime(2026, 7, 27, 20, 0, tzinfo=UTC),
            securities,
        ),
        "model": ApprovedModelSnapshot("model-approved", "US", "direction", "5d", True),
        "ranking": RankingDefinition("ranking-v2", "up_probability", 1.0, {"quality": 0.1}),
        "configuration": ScanConfiguration(
            "scan-config-v1",
            "US",
            ("NASDAQ", "NYSE"),
            1_000_000,
            0.1,
            0.6,
            {"cost_bps": 5},
        ),
        "submitted_at": datetime(2026, 7, 27, 20, 1, tzinfo=UTC),
    }


def test_submission_freezes_all_versions_and_rejects_scope_mismatches() -> None:
    validator = ScanSubmissionValidator()
    values = _submission_inputs()
    submission = validator.create(**values)  # type: ignore[arg-type]

    assert submission.strategy.strategy_version_id == "strategy-1:v3"
    assert submission.universe.universe_snapshot_id == "universe-1"
    assert submission.data_features.data_snapshot_id == "data-2026-07-27"
    assert submission.data_features.feature_set_version == "features-v4"
    assert submission.model.model_version_id == "model-approved"
    assert submission.ranking.version == "ranking-v2"
    assert submission.configuration.version == "scan-config-v1"

    wrong_owner = {**values, "owner_id": "owner-b"}
    with pytest.raises(ScanSubmissionError, match="当前 owner"):
        validator.create(**wrong_owner)  # type: ignore[arg-type]
    unapproved = {
        **values,
        "model": ApprovedModelSnapshot("model-approved", "US", "direction", "5d", False),
    }
    with pytest.raises(ScanSubmissionError, match="已批准"):
        validator.create(**unapproved)  # type: ignore[arg-type]


def test_scan_applies_deterministic_gates_before_model_and_persists_lineage() -> None:
    runtime = RecordingRuntime()
    submission = ScanSubmissionValidator().create(
        **_submission_inputs()  # type: ignore[arg-type]
    )

    evaluation = ScanEvaluator(_inference(runtime)).evaluate(submission)

    assert len(runtime.security_rows) == 2
    by_security = {item.security_id: item for item in evaluation.results}
    nvda = by_security["US:NASDAQ:NVDA"]
    msft = by_security["US:NASDAQ:MSFT"]
    low_liquidity = by_security["US:NYSE:LOWLIQ"]
    missing = by_security["US:NASDAQ:MISSING"]

    assert nvda.disposition is ScanDisposition.MATCHED
    assert nvda.rank == 1
    assert nvda.probability == 0.8
    assert nvda.model_version_id == "model-approved"
    assert nvda.data_snapshot_id == "data-2026-07-27"
    assert nvda.feature_snapshot_id == "features:US:NASDAQ:NVDA"
    assert nvda.feature_set_version == "features-v4"
    assert nvda.ranking_version == "ranking-v2"
    assert nvda.evidence_refs == ("evidence:US:NASDAQ:NVDA",)
    assert nvda.risks == ("market risk",)
    assert len(nvda.matched_conditions) == 2

    assert msft.disposition is ScanDisposition.NON_MATCH
    assert msft.probability == 0.55
    assert msft.reason == "专用模型 probability 未达门槛"
    assert low_liquidity.disposition is ScanDisposition.NON_MATCH
    assert low_liquidity.model_version_id is None
    assert missing.disposition is ScanDisposition.UNAVAILABLE
    assert "feature 缺失" in " ".join(missing.gaps)
