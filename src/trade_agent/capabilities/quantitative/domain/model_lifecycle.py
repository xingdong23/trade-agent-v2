"""量化模型评测、发布门禁和预测输出契约。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from trade_agent.core.llm.contracts import JsonValue


class ModelStatus(StrEnum):
    """量化模型版本生命周期状态的稳定枚举。

    Attributes:
        CANDIDATE: 候选模型，尚未获得生产批准。
        APPROVED: 已批准并可进入生产推理路径。
        RETIRED: 已退役，不再作为生产候选使用。

    Invariants:
        - 枚举值是模型注册表、审批与运行时选择共享的稳定协议字段。
    """

    CANDIDATE = "candidate"
    APPROVED = "approved"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """描述候选或基线模型在统一评测协议下的关键指标。

    Attributes:
        quality: 综合质量分数, 用于跨模型比较样本外表现。
        calibration_error: 概率校准误差, 越低越好。
        stability: 跨样本期或截面的稳定性分数。
        turnover: 策略换手相关指标。
        cost_adjusted_return: 扣除交易成本后的收益表现。
        latency_ms: 单次推理延迟, 单位为毫秒。
        coverage: 可生成有效预测的样本覆盖率。
    """

    quality: float
    calibration_error: float
    stability: float
    turnover: float
    cost_adjusted_return: float
    latency_ms: float
    coverage: float


@dataclass(frozen=True, slots=True)
class EvaluationThresholds:
    """定义候选模型晋级评测时必须满足的最小/最大门槛。

    Attributes:
        min_quality: 允许通过的最小质量分数。
        max_calibration_error: 允许的最大概率校准误差。
        min_stability: 允许通过的最小稳定性分数。
        min_cost_adjusted_return: 允许通过的最小成本后收益。
        max_latency_ms: 允许的最大推理延迟, 单位为毫秒。
        min_coverage: 允许通过的最小预测覆盖率。
    """

    min_quality: float
    max_calibration_error: float
    min_stability: float
    min_cost_adjusted_return: float
    max_latency_ms: float
    min_coverage: float


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """汇总单个候选模型的评测输入、结论与失败原因。

    Attributes:
        model_version_id: 被评测模型版本标识。
        metrics: 候选模型自身指标。
        benchmark_metrics: 对照基线模型指标。
        passed: 是否通过全部发布门禁。
        failures: 未通过的门禁原因列表。
    """

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
    """表示一个闭区间语义之外的时间窗口边界。

    Attributes:
        start: 窗口开始时间。
        end: 窗口结束时间。
    """

    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """描述一次 walk-forward 切分中的训练、验证与测试窗口。

    Attributes:
        train: 训练窗口。
        validation: 验证窗口。
        test: 测试窗口。
    """

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
    """注册表中一个量化模型版本的不可变记录。

    Attributes:
        model_version_id: 模型版本稳定标识。
        market: 适用市场, 首版应为美股。
        target: 预测目标。
        horizon: 预测周期标识。
        status: 当前生命周期状态。
        evaluation: 最近一次门禁评测结果。
        approved_by: 批准人标识; 候选模型可为空。
    """

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
    """量化预测可用性的稳定枚举。

    Attributes:
        AVAILABLE: 当前请求成功返回了可消费的预测结果。
        UNAVAILABLE: 当前请求因门禁或依赖缺失未能返回预测。

    Invariants:
        - 枚举值驱动上层展示与降级逻辑，属于稳定输出字段。
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class QuantitativePrediction:
    """表示批量推理后返回给调用方的标准化预测结果。

    Attributes:
        status: 预测是否可用。
        security_id: 证券稳定标识。
        target: 预测目标。
        horizon: 预测周期。
        as_of: 信息截止时点。
        model_version_id: 实际使用的模型版本; 不可用时为空。
        feature_snapshot_id: 推理输入特征快照标识; 不可用时仍保留请求值。
        distribution: 预测输出分布或关键概率。
        calibration: 与校准相关的说明指标。
        applicability: 适用性、市场等上下文说明。
        reason: 预测不可用时的原因说明。
    """

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
    """封装单只证券进入批量推理前的冻结输入。

    Attributes:
        security_id: 证券稳定标识。
        market: 证券所属市场。
        target: 预测目标。
        horizon: 预测周期。
        as_of: 输入信息截止时点。
        feature_snapshot_id: 特征快照标识。
        features: 推理所需特征值映射。
        missing_ratio: 缺失特征占比, 范围为 0 到 1。
        out_of_distribution: 输入是否已被判断为超出模型适用范围。
    """

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
    """供批量推理服务调用具体模型 runtime 的执行协议。

    Contract:
        - 实现方必须按输入顺序返回每一行的预测映射。
        - 输出键名必须与上层 ranking 或展示逻辑约定一致。
        - 不得在 runtime 内部自行选择模型版本, 调用方负责传入已批准版本。

    Implemented by:
        生产模型 runtime adapter 与测试 fake runtime。
    """

    def predict_batch(
        self, model_version_id: str, rows: Sequence[Mapping[str, float]]
    ) -> Sequence[Mapping[str, float]]:
        """对一批已数值化特征执行模型推理。

        Args:
            model_version_id: 需要执行的已批准模型版本标识。
            rows: 逐行排列的数值特征映射。

        Returns:
            与输入顺序一致的预测输出映射序列。

        Raises:
            NotImplementedError: 基类未提供具体 runtime 实现。
        """

        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class InferencePolicy:
    """定义并版本化专用模型推理前的数据适用性门禁。

    Attributes:
        policy_version: 可写入预测 lineage 的稳定策略版本。
        max_missing_ratio: 允许的最大缺失特征占比。

    Invariants:
        - 策略版本不能为空。
        - 缺失率阈值必须位于 0 到 1 之间。
    """

    policy_version: str
    max_missing_ratio: float

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("inference policy_version 不能为空")
        if not 0.0 <= self.max_missing_ratio <= 1.0:
            raise ValueError("max_missing_ratio 必须位于 0 到 1 之间")


class BatchInferenceService:
    def __init__(
        self, registry: ModelRegistry, runtime: ModelRuntime, *, policy: InferencePolicy
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._policy = policy

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
            if request.missing_ratio > self._policy.max_missing_ratio or any(
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
                    {
                        "inference_policy_version": self._policy.policy_version,
                        "market": request.market,
                        "out_of_distribution": False,
                    },
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
