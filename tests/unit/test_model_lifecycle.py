"""模型评测、审批、生产路由和预测降级测试。"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from trade_agent.capabilities.quantitative.contracts import (
    BatchInferenceService,
    EvaluationMetrics,
    EvaluationResult,
    EvaluationThresholds,
    InferencePolicy,
    InferenceRequest,
    ModelRegistry,
    ModelRegistryEntry,
    ModelRuntime,
    ModelStatus,
    PredictionStatus,
    build_walk_forward_folds,
    evaluate_candidate,
)


class FakeModelRuntime(ModelRuntime):
    def __init__(self) -> None:
        self.calls = 0

    def predict_batch(
        self, model_version_id: str, rows: Sequence[Mapping[str, float]]
    ) -> Sequence[Mapping[str, float]]:
        del model_version_id
        self.calls += 1
        return tuple({"up_probability": 0.7, "down_probability": 0.3} for _ in rows)


def _metrics(*, quality: float = 0.8, cost_return: float = 0.12) -> EvaluationMetrics:
    return EvaluationMetrics(quality, 0.03, 0.8, 0.2, cost_return, 5.0, 0.95)


def _evaluation(model_id: str = "model-1") -> EvaluationResult:
    return evaluate_candidate(
        model_version_id=model_id,
        metrics=_metrics(),
        benchmark_metrics=_metrics(quality=0.6, cost_return=0.05),
        thresholds=EvaluationThresholds(0.7, 0.05, 0.7, 0.08, 20, 0.9),
    )


def _request(**overrides: object) -> InferenceRequest:
    values: dict[str, object] = {
        "security_id": "US:NASDAQ:NVDA",
        "market": "US",
        "target": "direction",
        "horizon": "5d",
        "as_of": datetime.now(UTC),
        "feature_snapshot_id": "features-1",
        "features": {"return_1d": 0.02},
        "missing_ratio": 0.0,
        "out_of_distribution": False,
    }
    values.update(overrides)
    return InferenceRequest(**values)  # type: ignore[arg-type]


def test_walk_forward_folds_keep_embargoed_time_order() -> None:
    folds = build_walk_forward_folds(
        start=datetime(2020, 1, 1, tzinfo=UTC),
        train_days=100,
        validation_days=20,
        test_days=20,
        fold_count=2,
        embargo_days=5,
    )
    assert all(
        fold.train.end < fold.validation.start < fold.validation.end < fold.test.start
        for fold in folds
    )


def test_registry_requires_passing_evaluation_and_explicit_approval() -> None:
    registry = ModelRegistry()
    passed = _evaluation()
    registry.register(
        ModelRegistryEntry("model-1", "US", "direction", "5d", ModelStatus.CANDIDATE, passed)
    )
    with pytest.raises(LookupError):
        registry.production_model(market="US", target="direction", horizon="5d")
    approved = registry.approve("model-1", actor_id="risk-owner")
    assert approved.status is ModelStatus.APPROVED
    assert approved.approved_by == "risk-owner"

    failed = evaluate_candidate(
        model_version_id="bad",
        metrics=_metrics(quality=0.5),
        benchmark_metrics=_metrics(quality=0.6, cost_return=0.05),
        thresholds=EvaluationThresholds(0.7, 0.05, 0.7, 0.08, 20, 0.9),
    )
    registry.register(
        ModelRegistryEntry("bad", "US", "direction", "5d", ModelStatus.CANDIDATE, failed)
    )
    with pytest.raises(PermissionError):
        registry.approve("bad", actor_id="risk-owner")


def test_inference_returns_lineage_and_never_calls_runtime_for_ood_or_missing_features() -> None:
    registry = ModelRegistry()
    registry.register(
        ModelRegistryEntry("model-1", "US", "direction", "5d", ModelStatus.CANDIDATE, _evaluation())
    )
    registry.approve("model-1", actor_id="risk-owner")
    runtime = FakeModelRuntime()
    service = BatchInferenceService(
        registry,
        runtime,
        policy=InferencePolicy("direction-inference.v1", 0.1),
    )

    results = service.predict(
        (
            _request(),
            _request(security_id="US:NASDAQ:MSFT", out_of_distribution=True),
            _request(security_id="US:NASDAQ:AAPL", features={"return_1d": None}),
        )
    )

    available = next(item for item in results if item.status is PredictionStatus.AVAILABLE)
    assert available.model_version_id == "model-1"
    assert available.feature_snapshot_id == "features-1"
    assert available.distribution["up_probability"] == 0.7
    assert available.applicability["inference_policy_version"] == "direction-inference.v1"
    assert sum(item.status is PredictionStatus.UNAVAILABLE for item in results) == 2
    assert runtime.calls == 1
