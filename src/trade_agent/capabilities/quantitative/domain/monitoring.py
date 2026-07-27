"""生产量化模型监控指标、阈值与确定性路由动作。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MonitoringAction(StrEnum):
    CONTINUE = "continue"
    FALLBACK_TO_BASELINE = "fallback_to_approved_baseline"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class MonitoringThresholds:
    min_data_quality: float
    max_feature_drift: float
    max_prediction_drift: float
    max_calibration_error: float
    min_coverage: float
    max_latency_ms: float
    min_labeled_performance: float


@dataclass(frozen=True, slots=True)
class ProductionObservation:
    model_version_id: str
    observed_at: datetime
    data_quality: float
    feature_drift: float
    prediction_drift: float
    calibration_error: float
    coverage: float
    latency_ms: float
    labeled_performance: float | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("监控时间必须包含时区")


@dataclass(frozen=True, slots=True)
class MonitoringDecision:
    model_version_id: str
    observed_at: datetime
    action: MonitoringAction
    breaches: tuple[str, ...]
    route_model_version_id: str | None


class MonitoringPolicy:
    def __init__(self, thresholds: MonitoringThresholds) -> None:
        self._thresholds = thresholds

    def evaluate(
        self,
        observation: ProductionObservation,
        *,
        approved_baseline_model_version_id: str | None,
    ) -> MonitoringDecision:
        breaches: list[str] = []
        thresholds = self._thresholds
        if observation.data_quality < thresholds.min_data_quality:
            breaches.append("data_quality")
        if observation.feature_drift > thresholds.max_feature_drift:
            breaches.append("feature_drift")
        if observation.prediction_drift > thresholds.max_prediction_drift:
            breaches.append("prediction_drift")
        if observation.calibration_error > thresholds.max_calibration_error:
            breaches.append("calibration")
        if observation.coverage < thresholds.min_coverage:
            breaches.append("coverage")
        if observation.latency_ms > thresholds.max_latency_ms:
            breaches.append("latency")
        if (
            observation.labeled_performance is not None
            and observation.labeled_performance < thresholds.min_labeled_performance
        ):
            breaches.append("labeled_performance")
        if not breaches:
            return MonitoringDecision(
                observation.model_version_id,
                observation.observed_at,
                MonitoringAction.CONTINUE,
                (),
                observation.model_version_id,
            )
        if (
            approved_baseline_model_version_id is not None
            and approved_baseline_model_version_id != observation.model_version_id
        ):
            return MonitoringDecision(
                observation.model_version_id,
                observation.observed_at,
                MonitoringAction.FALLBACK_TO_BASELINE,
                tuple(breaches),
                approved_baseline_model_version_id,
            )
        return MonitoringDecision(
            observation.model_version_id,
            observation.observed_at,
            MonitoringAction.STOP,
            tuple(breaches),
            None,
        )
