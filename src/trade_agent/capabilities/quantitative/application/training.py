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
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("训练时间区间必须包含时区")
        if self.ends_at <= self.starts_at:
            raise ValueError("训练结束时间必须晚于开始时间")


@dataclass(frozen=True, slots=True)
class TrainingJob:
    """足以重放一次训练的不可变输入记录。"""

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
    sample_id: str
    features: Mapping[str, float]
    label: int

    def __post_init__(self) -> None:
        if not self.sample_id:
            raise ValueError("训练样本必须包含 sample_id")
        if self.label not in (0, 1):
            raise ValueError("首版方向预测 label 只能是 0 或 1")
        if not self.features:
            raise ValueError("训练样本必须包含 feature")
        if any(not math.isfinite(value) for value in self.features.values()):
            raise ValueError("训练 feature 不允许 NaN 或无穷值")


@dataclass(frozen=True, slots=True)
class BenchmarkMetrics:
    accuracy: float
    brier_score: float
    log_loss: float


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    algorithm: TrainingAlgorithm
    probabilities: tuple[float, ...]
    metrics: BenchmarkMetrics


class ProbabilityBenchmark(Protocol):
    @property
    def algorithm(self) -> TrainingAlgorithm: ...

    def predict(self, examples: Sequence[TrainingExample]) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class DeterministicRuleBenchmark:
    """以一个显式 feature 阈值作为不学习参数的最低规则基线。"""

    feature_name: str
    threshold: float = 0.0
    positive_probability: float = 0.75
    negative_probability: float = 0.25
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
    """使用训练标签的拉普拉斯平滑先验, 避免零概率与数据顺序影响。"""

    positive_probability: float
    algorithm: TrainingAlgorithm = TrainingAlgorithm.STATISTICAL_BENCHMARK

    @classmethod
    def fit(cls, examples: Sequence[TrainingExample]) -> StatisticalBenchmark:
        if not examples:
            raise ValueError("统计 benchmark 至少需要一个训练样本")
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
    labels: Sequence[int], probabilities: Sequence[float]
) -> BenchmarkMetrics:
    if not labels or len(labels) != len(probabilities):
        raise ValueError("label 与 probability 必须非空且长度一致")
    clipped = tuple(min(max(float(value), 1e-15), 1.0 - 1e-15) for value in probabilities)
    if any(label not in (0, 1) for label in labels):
        raise ValueError("分类指标只接受二分类 label")
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
    """LightGBM 与 LSTM 必须共同提供的样本外评测协议。"""

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
        if self.protocol_version != EVALUATION_PROTOCOL_VERSION:
            raise ValueError("候选模型必须使用当前统一评测协议")
        if self.calibration_error < 0 or self.inference_latency_ms < 0:
            raise ValueError("校准误差和 inference 延迟不能为负")


@dataclass(frozen=True, slots=True)
class LSTMReleaseThresholds:
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
            raise ValueError("LSTM 发布门槛改善量不能为负")
        if self.max_latency_ms <= 0:
            raise ValueError("LSTM 最大 inference 延迟必须为正")


@dataclass(frozen=True, slots=True)
class ReleaseGateDecision:
    may_request_release: bool
    reasons: tuple[str, ...]


def assess_lstm_release(
    *,
    candidate: EvaluationMetrics,
    lightgbm_baseline: EvaluationMetrics,
    thresholds: LSTMReleaseThresholds,
) -> ReleaseGateDecision:
    """所有门槛均须严格超过; 该结果只允许提请审批, 不代表已发布。"""

    checks = (
        (
            candidate.quality_score
            > lightgbm_baseline.quality_score + thresholds.min_quality_improvement,
            "样本外预测质量未严格超过 LightGBM 门槛",
        ),
        (
            candidate.calibration_error
            < lightgbm_baseline.calibration_error - thresholds.min_calibration_improvement,
            "概率校准未严格超过 LightGBM 门槛",
        ),
        (
            candidate.stability_score
            > lightgbm_baseline.stability_score + thresholds.min_stability_improvement,
            "稳定性未严格超过 LightGBM 门槛",
        ),
        (
            candidate.net_return_after_cost
            > lightgbm_baseline.net_return_after_cost + thresholds.min_net_return_improvement,
            "交易成本后指标未严格超过 LightGBM 门槛",
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
