"""生产量化监控和显式降级策略测试。"""

from datetime import UTC, datetime

from trade_agent.capabilities.quantitative.application.monitoring import (
    QuantitativeMonitoringService,
)
from trade_agent.capabilities.quantitative.domain.monitoring import (
    MonitoringAction,
    MonitoringPolicy,
    MonitoringThresholds,
    ProductionObservation,
)


def _policy() -> MonitoringPolicy:
    return MonitoringPolicy(
        MonitoringThresholds(
            min_data_quality=0.95,
            max_feature_drift=0.2,
            max_prediction_drift=0.2,
            max_calibration_error=0.08,
            min_coverage=0.9,
            max_latency_ms=100,
            min_labeled_performance=0.55,
        )
    )


def _observation(**overrides: float | None) -> ProductionObservation:
    values: dict[str, float | None] = {
        "data_quality": 0.99,
        "feature_drift": 0.05,
        "prediction_drift": 0.04,
        "calibration_error": 0.03,
        "coverage": 0.98,
        "latency_ms": 12,
        "labeled_performance": 0.7,
    }
    values.update(overrides)
    return ProductionObservation(
        "model-7",
        datetime(2026, 7, 27, tzinfo=UTC),
        data_quality=float(values["data_quality"] or 0),
        feature_drift=float(values["feature_drift"] or 0),
        prediction_drift=float(values["prediction_drift"] or 0),
        calibration_error=float(values["calibration_error"] or 0),
        coverage=float(values["coverage"] or 0),
        latency_ms=float(values["latency_ms"] or 0),
        labeled_performance=values["labeled_performance"],
    )


def test_healthy_observation_keeps_current_model() -> None:
    decision = _policy().evaluate(_observation(), approved_baseline_model_version_id="baseline-1")
    assert decision.action is MonitoringAction.CONTINUE
    assert decision.route_model_version_id == "model-7"
    assert decision.breaches == ()


def test_threshold_breach_falls_back_only_to_explicit_approved_baseline() -> None:
    service = QuantitativeMonitoringService(_policy())
    decision = service.observe(
        _observation(feature_drift=0.6, coverage=0.5, labeled_performance=0.4),
        approved_baseline_model_version_id="baseline-1",
    )
    assert decision.action is MonitoringAction.FALLBACK_TO_BASELINE
    assert decision.route_model_version_id == "baseline-1"
    assert decision.breaches == ("feature_drift", "coverage", "labeled_performance")
    assert tuple(service.events) == (decision,)


def test_threshold_breach_stops_when_no_approved_baseline_exists() -> None:
    decision = _policy().evaluate(
        _observation(data_quality=0.2), approved_baseline_model_version_id=None
    )
    assert decision.action is MonitoringAction.STOP
    assert decision.route_model_version_id is None
