"""量化模型评测、发布门禁和预测输出契约。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from trade_agent.core.llm.contracts import JsonValue


class ModelStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    quality: float
    calibration_error: float
    stability: float
    turnover: float
    cost_adjusted_return: float
    latency_ms: float
    coverage: float


@dataclass(frozen=True, slots=True)
class EvaluationThresholds:
    min_quality: float
    max_calibration_error: float
    min_stability: float
    min_cost_adjusted_return: float
    max_latency_ms: float
    min_coverage: float


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    model_version_id: str
    metrics: EvaluationMetrics
    benchmark_metrics: EvaluationMetrics
    passed: bool
    failures: tuple[str, ...]


def evaluate_candidate(
    *,
    model_version_id: str,
    metrics: EvaluationMetrics,
    benchmark_metrics: EvaluationMetrics,
    thresholds: EvaluationThresholds,
) -> EvaluationResult:
    failures: list[str] = []
    if metrics.quality < thresholds.min_quality:
        failures.append("quality 未达门槛")
    if metrics.calibration_error > thresholds.max_calibration_error:
        failures.append("概率校准误差超限")
    if metrics.stability < thresholds.min_stability:
        failures.append("稳定性未达门槛")
    if metrics.cost_adjusted_return < thresholds.min_cost_adjusted_return:
        failures.append("交易成本后表现未达门槛")
    if metrics.latency_ms > thresholds.max_latency_ms:
        failures.append("inference 延迟超限")
    if metrics.coverage < thresholds.min_coverage:
        failures.append("预测覆盖率未达门槛")
    if metrics.quality <= benchmark_metrics.quality:
        failures.append("未超过已批准基线质量")
    if metrics.cost_adjusted_return <= benchmark_metrics.cost_adjusted_return:
        failures.append("未超过已批准基线的成本后表现")
    return EvaluationResult(
        model_version_id, metrics, benchmark_metrics, not failures, tuple(failures)
    )


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    train: TimeWindow
    validation: TimeWindow
    test: TimeWindow


def build_walk_forward_folds(
    *,
    start: datetime,
    train_days: int,
    validation_days: int,
    test_days: int,
    fold_count: int,
    embargo_days: int,
) -> tuple[WalkForwardFold, ...]:
    if min(train_days, validation_days, test_days, fold_count) < 1 or embargo_days < 0:
        raise ValueError("walk-forward 参数无效")
    folds: list[WalkForwardFold] = []
    cursor = start
    embargo = timedelta(days=embargo_days)
    for _ in range(fold_count):
        train = TimeWindow(cursor, cursor + timedelta(days=train_days))
        validation_start = train.end + embargo
        validation = TimeWindow(
            validation_start, validation_start + timedelta(days=validation_days)
        )
        test_start = validation.end + embargo
        test = TimeWindow(test_start, test_start + timedelta(days=test_days))
        folds.append(WalkForwardFold(train, validation, test))
        cursor += timedelta(days=test_days)
    return tuple(folds)


@dataclass(frozen=True, slots=True)
class ModelRegistryEntry:
    model_version_id: str
    market: str
    target: str
    horizon: str
    status: ModelStatus
    evaluation: EvaluationResult
    approved_by: str | None = None


class ModelRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ModelRegistryEntry] = {}

    def register(self, entry: ModelRegistryEntry) -> None:
        if entry.model_version_id in self._entries:
            raise ValueError("model version 已注册且不可修改")
        if entry.status is not ModelStatus.CANDIDATE:
            raise ValueError("新 model version 必须以 candidate 状态注册")
        self._entries[entry.model_version_id] = entry

    def approve(self, model_version_id: str, *, actor_id: str) -> ModelRegistryEntry:
        candidate = self._entries[model_version_id]
        if not candidate.evaluation.passed:
            raise PermissionError("未通过评测门禁的 model 不得批准")
        approved = ModelRegistryEntry(
            candidate.model_version_id,
            candidate.market,
            candidate.target,
            candidate.horizon,
            ModelStatus.APPROVED,
            candidate.evaluation,
            actor_id,
        )
        self._entries[model_version_id] = approved
        return approved

    def production_model(self, *, market: str, target: str, horizon: str) -> ModelRegistryEntry:
        matches = tuple(
            item
            for item in self._entries.values()
            if item.market == market
            and item.target == target
            and item.horizon == horizon
            and item.status is ModelStatus.APPROVED
        )
        if len(matches) != 1:
            raise LookupError("没有唯一的已批准生产模型")
        return matches[0]


class PredictionStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class QuantitativePrediction:
    status: PredictionStatus
    security_id: str
    target: str
    horizon: str
    as_of: datetime
    model_version_id: str | None
    feature_snapshot_id: str | None
    distribution: Mapping[str, float]
    calibration: Mapping[str, float]
    applicability: Mapping[str, JsonValue]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    security_id: str
    market: str
    target: str
    horizon: str
    as_of: datetime
    feature_snapshot_id: str
    features: Mapping[str, float | None]
    missing_ratio: float
    out_of_distribution: bool


class ModelRuntime:
    def predict_batch(
        self, model_version_id: str, rows: Sequence[Mapping[str, float]]
    ) -> Sequence[Mapping[str, float]]:
        raise NotImplementedError


class BatchInferenceService:
    def __init__(
        self, registry: ModelRegistry, runtime: ModelRuntime, *, max_missing_ratio: float = 0.1
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._max_missing_ratio = max_missing_ratio

    def predict(self, requests: Sequence[InferenceRequest]) -> tuple[QuantitativePrediction, ...]:
        results: list[QuantitativePrediction] = []
        eligible: list[tuple[InferenceRequest, ModelRegistryEntry, dict[str, float]]] = []
        for request in requests:
            try:
                model = self._registry.production_model(
                    market=request.market, target=request.target, horizon=request.horizon
                )
            except LookupError:
                results.append(self._unavailable(request, "没有已批准的专用量化模型"))
                continue
            if request.out_of_distribution:
                results.append(self._unavailable(request, "输入超出模型适用范围"))
                continue
            if request.missing_ratio > self._max_missing_ratio or any(
                value is None for value in request.features.values()
            ):
                results.append(self._unavailable(request, "feature 缺失超过门槛"))
                continue
            numeric_features: dict[str, float] = {}
            for key, value in request.features.items():
                if value is None:
                    raise AssertionError("缺失 feature 已在前置门禁处理")
                numeric_features[key] = float(value)
            eligible.append((request, model, numeric_features))

        for request, model, row in eligible:
            output = self._runtime.predict_batch(model.model_version_id, (row,))[0]
            results.append(
                QuantitativePrediction(
                    PredictionStatus.AVAILABLE,
                    request.security_id,
                    request.target,
                    request.horizon,
                    request.as_of,
                    model.model_version_id,
                    request.feature_snapshot_id,
                    dict(output),
                    {"evaluation_calibration_error": model.evaluation.metrics.calibration_error},
                    {"market": request.market, "out_of_distribution": False},
                )
            )
        return tuple(results)

    @staticmethod
    def _unavailable(request: InferenceRequest, reason: str) -> QuantitativePrediction:
        return QuantitativePrediction(
            PredictionStatus.UNAVAILABLE,
            request.security_id,
            request.target,
            request.horizon,
            request.as_of,
            None,
            request.feature_snapshot_id,
            {},
            {},
            {"market": request.market},
            reason,
        )
