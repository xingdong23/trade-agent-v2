"""专用量化模型的可复现训练契约、基准模型与候选发布门禁。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self

EVALUATION_PROTOCOL_VERSION = "quant-evaluation.v1"
JsonScalar = str | int | float | bool | None


class TrainingAlgorithm(StrEnum):
    DETERMINISTIC_RULE = "deterministic_rule"
    STATISTICAL_BENCHMARK = "statistical_benchmark"
    LIGHTGBM = "lightgbm"
    LSTM = "lstm"


@dataclass(frozen=True, slots=True)
class TrainingWindow:
    """定义一次训练任务覆盖的时间区间。

    Attributes:
        starts_at: 训练窗口起始时间。
        ends_at: 训练窗口结束时间。

    Invariants:
        - 起止时间都必须带时区。
        - 结束时间必须晚于开始时间。
    """

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("训练时间区间必须包含时区")
        if self.ends_at <= self.starts_at:
            raise ValueError("训练结束时间必须晚于开始时间")


@dataclass(frozen=True, slots=True)
class TrainingJob:
    """足以重放一次训练的不可变输入记录。

    Attributes:
        job_id: 训练任务稳定标识。
        algorithm: 训练算法类型。
        hyperparameters: 训练超参数映射, 必须可编码为规范 JSON。
        random_seed: 控制训练可复现性的随机种子。
        code_version: 训练代码版本标识。
        data_snapshot_id: 训练数据快照标识。
        feature_set_version: 特征集版本。
        target: 预测目标标识。
        horizon_trading_days: 预测周期的交易日数量。
        training_window: 训练覆盖的时间区间。
        artifact_hash: 已产出模型工件的 SHA-256 摘要; 未落盘时可为空。

    Invariants:
        - 关键标识字段不能为空白字符串。
        - `horizon_trading_days` 必须为正。
        - `artifact_hash` 若存在则必须是 SHA-256 十六进制摘要。
    """

    job_id: str
    algorithm: TrainingAlgorithm
    hyperparameters: Mapping[str, JsonScalar]
    random_seed: int
    code_version: str
    data_snapshot_id: str
    feature_set_version: str
    target: str
    horizon_trading_days: int
    training_window: TrainingWindow
    artifact_hash: str | None = None

    def __post_init__(self) -> None:
        required = {
            "job_id": self.job_id,
            "code_version": self.code_version,
            "data_snapshot_id": self.data_snapshot_id,
            "feature_set_version": self.feature_set_version,
            "target": self.target,
        }
        empty = [name for name, value in required.items() if not value.strip()]
        if empty:
            raise ValueError(f"训练任务字段不能为空: {', '.join(empty)}")
        if self.horizon_trading_days < 1:
            raise ValueError("预测周期必须为正")
        if self.artifact_hash is not None and not _is_sha256(self.artifact_hash):
            raise ValueError("artifact_hash 必须是 SHA-256 十六进制摘要")
        _canonical_json(dict(self.hyperparameters))

    @property
    def reproducibility_key(self) -> str:
        payload = {
            "algorithm": self.algorithm.value,
            "code_version": self.code_version,
            "data_snapshot_id": self.data_snapshot_id,
            "feature_set_version": self.feature_set_version,
            "horizon_trading_days": self.horizon_trading_days,
            "hyperparameters": dict(self.hyperparameters),
            "random_seed": self.random_seed,
            "target": self.target,
            "training_window": {
                "ends_at": self.training_window.ends_at.isoformat(),
                "starts_at": self.training_window.starts_at.isoformat(),
            },
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def record_artifact(self, artifact: bytes) -> Self:
        return replace(self, artifact_hash=hashlib.sha256(artifact).hexdigest())


@dataclass(frozen=True, slots=True)
class TrainingExample:
    """表示单个监督训练样本。

    Attributes:
        sample_id: 样本稳定标识。
        features: 用于训练的数值特征映射。
        label: 由当前 `ModelTaskSpec.label_schema` 解释的有限数值标签。

    Invariants:
        - `sample_id` 不能为空。
        - 特征映射不能为空, 且不允许 NaN 或无穷值。
        - 标签必须是有限数值；具体取值范围由任务 spec 校验。
    """

    sample_id: str
    features: Mapping[str, float]
    label: float

    def __post_init__(self) -> None:
        if not self.sample_id:
            raise ValueError("训练样本必须包含 sample_id")
        if not math.isfinite(float(self.label)):
            raise ValueError("训练 label 不允许 NaN 或无穷值")
        if not self.features:
            raise ValueError("训练样本必须包含 feature")
        if any(not math.isfinite(value) for value in self.features.values()):
            raise ValueError("训练 feature 不允许 NaN 或无穷值")


@dataclass(frozen=True, slots=True)
class BenchmarkMetrics:
    """描述基准模型在二分类任务上的评测指标。

    Attributes:
        accuracy: 分类准确率。
        brier_score: Brier 分数, 越低越好。
        log_loss: 对数损失, 越低越好。
    """

    accuracy: float
    brier_score: float
    log_loss: float


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """汇总一次基准模型评测的概率输出与指标。

    Attributes:
        algorithm: 参与评测的基准算法类型。
        probabilities: 与样本顺序一致的正类概率输出。
        metrics: 根据这些概率计算出的评测指标。
    """

    algorithm: TrainingAlgorithm
    probabilities: tuple[float, ...]
    metrics: BenchmarkMetrics


class ProbabilityBenchmark(Protocol):
    """供训练评测复用的概率基线协议。

    Contract:
        - 实现方必须为每个输入样本返回一个顺序一致的正类概率。
        - 返回概率必须位于 0 到 1 之间, 由实现方保证可用于指标计算。
        - `algorithm` 必须稳定标识该基线类型, 便于审计和比较。

    Implemented by:
        `DeterministicRuleBenchmark`、`StatisticalBenchmark` 及测试中的 fake benchmark。
    """

    @property
    def algorithm(self) -> TrainingAlgorithm:
        """返回该基线实现的稳定算法标识。

        Returns:
            该基线的 `TrainingAlgorithm` 枚举值。
        """

        ...

    def predict(self, examples: Sequence[TrainingExample]) -> tuple[float, ...]:
        """为一批训练样本生成正类概率。

        Args:
            examples: 按既定顺序排列的训练样本序列。

        Returns:
            与输入顺序一致的正类概率元组。

        Raises:
            ValueError: 实现方无法基于给定样本生成有效概率时抛出。
        """

        ...


@dataclass(frozen=True, slots=True)
class DeterministicRuleBenchmark:
    """以一个显式 feature 阈值作为不学习参数的最低规则基线。

    Attributes:
        feature_name: 用于决定正负类的特征名称。
        threshold: 触发正类概率的阈值。
        positive_probability: 命中阈值时返回的正类概率。
        negative_probability: 未命中阈值时返回的正类概率。
        algorithm: 该基线的稳定算法标识。
    """

    feature_name: str
    threshold: float
    positive_probability: float
    negative_probability: float
    algorithm: TrainingAlgorithm = TrainingAlgorithm.DETERMINISTIC_RULE

    def predict(self, examples: Sequence[TrainingExample]) -> tuple[float, ...]:
        if not 0.0 < self.negative_probability < self.positive_probability < 1.0:
            raise ValueError("规则 benchmark 概率必须严格位于 0 和 1 之间")
        try:
            return tuple(
                self.positive_probability
                if example.features[self.feature_name] >= self.threshold
                else self.negative_probability
                for example in examples
            )
        except KeyError as error:
            raise ValueError(f"规则 benchmark 缘少 feature: {error.args[0]}") from error


@dataclass(frozen=True, slots=True)
class StatisticalBenchmark:
    """使用训练标签的拉普拉斯平滑先验, 避免零概率与数据顺序影响。

    Attributes:
        positive_probability: 对任意样本返回的固定正类概率。
        algorithm: 该基线的稳定算法标识。
    """

    positive_probability: float
    algorithm: TrainingAlgorithm = TrainingAlgorithm.STATISTICAL_BENCHMARK

    @classmethod
    def fit(cls, examples: Sequence[TrainingExample]) -> StatisticalBenchmark:
        if not examples:
            raise ValueError("统计 benchmark 至少需要一个训练样本")
        _binary_labels(tuple(example.label for example in examples))
        positive = sum(example.label for example in examples)
        return cls((positive + 1.0) / (len(examples) + 2.0))

    def predict(self, examples: Sequence[TrainingExample]) -> tuple[float, ...]:
        return (self.positive_probability,) * len(examples)


def evaluate_benchmark(
    benchmark: ProbabilityBenchmark, examples: Sequence[TrainingExample]
) -> BenchmarkResult:
    if not examples:
        raise ValueError("benchmark 评测至少需要一个样本")
    probabilities = benchmark.predict(examples)
    return BenchmarkResult(
        algorithm=benchmark.algorithm,
        probabilities=probabilities,
        metrics=classification_metrics(tuple(item.label for item in examples), probabilities),
    )


def classification_metrics(
    labels: Sequence[float], probabilities: Sequence[float]
) -> BenchmarkMetrics:
    if not labels or len(labels) != len(probabilities):
        raise ValueError("label 与 probability 必须非空且长度一致")
    clipped = tuple(min(max(float(value), 1e-15), 1.0 - 1e-15) for value in probabilities)
    _binary_labels(labels)
    count = len(labels)
    accuracy = sum(
        (probability >= 0.5) == bool(label)
        for label, probability in zip(labels, clipped, strict=True)
    )
    brier = sum(
        (probability - label) ** 2 for label, probability in zip(labels, clipped, strict=True)
    )
    loss = -sum(
        label * math.log(probability) + (1 - label) * math.log(1.0 - probability)
        for label, probability in zip(labels, clipped, strict=True)
    )
    return BenchmarkMetrics(accuracy / count, brier / count, loss / count)


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """LightGBM 与 LSTM 必须共同提供的样本外评测协议。

    Attributes:
        protocol_version: 统一评测协议版本。
        quality_score: 样本外综合质量分数。
        calibration_error: 概率校准误差, 越低越好。
        stability_score: 跨样本期稳定性分数。
        net_return_after_cost: 扣除交易成本后的收益指标。
        inference_latency_ms: 推理延迟, 单位为毫秒。

    Invariants:
        - 指标值不允许 NaN 或无穷值。
        - `protocol_version` 必须显式提供且不能为空。
        - 校准误差和推理延迟不能为负。
    """

    protocol_version: str
    quality_score: float
    calibration_error: float
    stability_score: float
    net_return_after_cost: float
    inference_latency_ms: float

    def __post_init__(self) -> None:
        values = (
            self.quality_score,
            self.calibration_error,
            self.stability_score,
            self.net_return_after_cost,
            self.inference_latency_ms,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("评测指标不允许 NaN 或无穷值")
        if not self.protocol_version.strip():
            raise ValueError("评测协议版本不能为空")
        if self.calibration_error < 0 or self.inference_latency_ms < 0:
            raise ValueError("校准误差和 inference 延迟不能为负")


@dataclass(frozen=True, slots=True)
class CandidateReleaseThresholds:
    """定义任意候选模型相对当前基线的发布改进门槛。

    Attributes:
        min_quality_improvement: 质量分数必须超过的最小改善量。
        min_calibration_improvement: 校准误差必须改善的最小量。
        min_stability_improvement: 稳定性必须超过的最小改善量。
        min_net_return_improvement: 成本后收益必须超过的最小改善量。
        max_latency_ms: 允许的最大推理延迟。

    Invariants:
        - 各项改善量不能为负。
        - 最大推理延迟必须为正。
    """

    min_quality_improvement: float
    min_calibration_improvement: float
    min_stability_improvement: float
    min_net_return_improvement: float
    max_latency_ms: float

    def __post_init__(self) -> None:
        if (
            min(
                self.min_quality_improvement,
                self.min_calibration_improvement,
                self.min_stability_improvement,
                self.min_net_return_improvement,
            )
            < 0
        ):
            raise ValueError("候选模型发布门槛改善量不能为负")
        if self.max_latency_ms <= 0:
            raise ValueError("候选模型最大 inference 延迟必须为正")


@dataclass(frozen=True, slots=True)
class ReleaseGateDecision:
    """给出候选模型是否可提请发布审批的门禁结论。

    Attributes:
        may_request_release: 是否满足提请发布审批的前置条件。
        reasons: 未满足门槛时的原因列表。
    """

    may_request_release: bool
    reasons: tuple[str, ...]


def assess_candidate_release(
    *,
    candidate: EvaluationMetrics,
    incumbent: EvaluationMetrics,
    thresholds: CandidateReleaseThresholds,
) -> ReleaseGateDecision:
    """所有门槛均须严格超过; 该结果只允许提请审批, 不代表已发布。"""

    if candidate.protocol_version != incumbent.protocol_version:
        raise ValueError("候选模型与基线必须使用相同评测协议")

    checks = (
        (
            candidate.quality_score > incumbent.quality_score + thresholds.min_quality_improvement,
            "样本外预测质量未严格超过当前基线门槛",
        ),
        (
            candidate.calibration_error
            < incumbent.calibration_error - thresholds.min_calibration_improvement,
            "概率校准未严格超过当前基线门槛",
        ),
        (
            candidate.stability_score
            > incumbent.stability_score + thresholds.min_stability_improvement,
            "稳定性未严格超过当前基线门槛",
        ),
        (
            candidate.net_return_after_cost
            > incumbent.net_return_after_cost + thresholds.min_net_return_improvement,
            "交易成本后指标未严格超过当前基线门槛",
        ),
        (candidate.inference_latency_ms < thresholds.max_latency_ms, "inference 延迟未满足门槛"),
    )
    reasons = tuple(message for passed, message in checks if not passed)
    return ReleaseGateDecision(not reasons, reasons)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("训练配置必须可编码为规范 JSON, 且不能包含 NaN") from error


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _binary_labels(labels: Sequence[float]) -> None:
    if any(float(label) not in (0.0, 1.0) for label in labels):
        raise ValueError("分类指标和概率 benchmark 只接受二分类 label")
