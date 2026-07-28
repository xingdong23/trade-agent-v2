"""生产量化模型监控指标、阈值与确定性路由动作。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MonitoringAction(StrEnum):
    """生产监控命中阈值后的稳定路由动作枚举。

    Attributes:
        CONTINUE: 继续使用当前生产模型版本。
        FALLBACK_TO_BASELINE: 回退到已批准的基线模型版本。
        STOP: 停止继续使用当前模型，等待人工处理。

    Invariants:
        - 枚举值直接驱动运行时路由决策，属于稳定控制流字段。
    """

    CONTINUE = "continue"
    FALLBACK_TO_BASELINE = "fallback_to_approved_baseline"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class MonitoringThresholds:
    """定义生产监控触发路由动作的告警门槛。

    Attributes:
        min_data_quality: 最低可接受数据质量分数。
        max_feature_drift: 最大可接受特征漂移。
        max_prediction_drift: 最大可接受预测分布漂移。
        max_calibration_error: 最大可接受概率校准误差。
        min_coverage: 最低可接受预测覆盖率。
        max_latency_ms: 最大可接受推理延迟, 单位为毫秒。
        min_labeled_performance: 最低可接受已标注表现分数。
    """

    min_data_quality: float
    max_feature_drift: float
    max_prediction_drift: float
    max_calibration_error: float
    min_coverage: float
    max_latency_ms: float
    min_labeled_performance: float


@dataclass(frozen=True, slots=True)
class ProductionObservation:
    """记录一次生产观测窗口内收集到的监控指标。

    Attributes:
        model_version_id: 被观测模型版本标识。
        observed_at: 观测时间。
        data_quality: 数据质量分数。
        feature_drift: 特征漂移指标。
        prediction_drift: 预测漂移指标。
        calibration_error: 概率校准误差。
        coverage: 有效预测覆盖率。
        latency_ms: 推理延迟, 单位为毫秒。
        labeled_performance: 已回填标签后的表现指标; 标签未到齐时可为空。

    Invariants:
        - `observed_at` 必须带时区。
    """

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
    """记录监控策略对一次生产观测得出的动作结论。

    Attributes:
        model_version_id: 被评估模型版本标识。
        observed_at: 对应观测时间。
        action: 应执行的路由动作。
        breaches: 触发动作的门槛名称列表。
        route_model_version_id: 继续或回退时应路由到的模型版本; 停止时可为空。
    """

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
