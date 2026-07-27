"""专用量化模型训练、基准与 LSTM 发布门禁测试。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trade_agent.adapters.model_runtime import (
    LightGBMArtifact,
    LightGBMPredictor,
    LightGBMTrainer,
    LSTMArtifact,
    LSTMCandidateTrainer,
    LSTMRuntimeUnavailable,
)
from trade_agent.capabilities.quantitative.application.training import (
    EVALUATION_PROTOCOL_VERSION,
    DeterministicRuleBenchmark,
    EvaluationMetrics,
    LSTMReleaseThresholds,
    StatisticalBenchmark,
    TrainingAlgorithm,
    TrainingExample,
    TrainingJob,
    TrainingWindow,
    assess_lstm_release,
    evaluate_benchmark,
)


def _job(**overrides: object) -> TrainingJob:
    values: dict[str, Any] = {
        "job_id": "train-1",
        "algorithm": TrainingAlgorithm.LIGHTGBM,
        "hyperparameters": {"learning_rate": 0.05, "num_leaves": 7},
        "random_seed": 20260727,
        "code_version": "git:fixed",
        "data_snapshot_id": "dataset-us-v1",
        "feature_set_version": "us-equity-core.v1",
        "target": "direction",
        "horizon_trading_days": 5,
        "training_window": TrainingWindow(
            datetime(2020, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)
        ),
    }
    values.update(overrides)
    return TrainingJob(**values)


def test_training_job_has_stable_reproducibility_key_and_records_artifact_hash() -> None:
    first = _job(hyperparameters={"num_leaves": 7, "learning_rate": 0.05})
    second = _job(hyperparameters={"learning_rate": 0.05, "num_leaves": 7})

    assert first.reproducibility_key == second.reproducibility_key
    completed = first.record_artifact(b"fixed-model")
    assert completed.artifact_hash == hashlib.sha256(b"fixed-model").hexdigest()
    assert first.artifact_hash is None


def test_deterministic_rule_and_statistical_benchmarks_are_repeatable() -> None:
    examples = (
        TrainingExample("a", {"trend": -0.2}, 0),
        TrainingExample("b", {"trend": 0.1}, 1),
        TrainingExample("c", {"trend": 0.3}, 1),
        TrainingExample("d", {"trend": -0.1}, 0),
    )
    rule = DeterministicRuleBenchmark("trend")
    statistical = StatisticalBenchmark.fit(examples)

    assert evaluate_benchmark(rule, examples) == evaluate_benchmark(rule, examples)
    assert evaluate_benchmark(rule, examples).metrics.accuracy == 1.0
    assert evaluate_benchmark(statistical, examples).probabilities == (0.5,) * 4


def test_lightgbm_fixed_dataset_produces_identical_calibrated_artifact() -> None:
    pytest.importorskip("lightgbm")
    features = tuple((float(index % 11), float((index * 7) % 13)) for index in range(120))
    labels = tuple(int(first + second > 10) for first, second in features)
    trainer = LightGBMTrainer()

    def train_once() -> LightGBMArtifact:
        return trainer.train(
            feature_names=("momentum", "quality"),
            training_features=features[:80],
            training_labels=labels[:80],
            calibration_features=features[80:],
            calibration_labels=labels[80:],
            random_seed=20260727,
            hyperparameters={
                "learning_rate": 0.08,
                "num_leaves": 7,
                "min_data_in_leaf": 3,
                "num_boost_round": 30,
            },
        )

    first = train_once()
    second = train_once()

    assert first.artifact_bytes == second.artifact_bytes
    assert first.artifact_hash == second.artifact_hash
    assert sum(first.feature_attribution.values()) == pytest.approx(1.0)
    probabilities = LightGBMPredictor(first.artifact_bytes).predict_probabilities(features[-4:])
    assert all(0.0 <= probability <= 1.0 for probability in probabilities)


class _FixedLSTMBackend:
    runtime_name = "fake-sequence-runtime"
    runtime_version = "1"

    def train(self, **_: object) -> bytes:
        return b"deterministic-lstm-model"


def test_lstm_is_optional_and_artifact_uses_shared_evaluation_protocol() -> None:
    with pytest.raises(LSTMRuntimeUnavailable, match="可选候选模型"):
        LSTMCandidateTrainer().train(
            feature_names=("return",),
            sequences=(((0.1,), (0.2,)),),
            labels=(1,),
            random_seed=7,
            hyperparameters={},
        )

    trainer = LSTMCandidateTrainer(_FixedLSTMBackend())

    def train_once() -> LSTMArtifact:
        return trainer.train(
            feature_names=("return",),
            sequences=(((0.1,), (0.2,)), ((-0.2,), (-0.1,))),
            labels=(1, 0),
            random_seed=7,
            hyperparameters={"hidden_size": 8},
        )

    first = train_once()
    second = train_once()
    assert first == second
    assert first.evaluation_protocol == EVALUATION_PROTOCOL_VERSION


def test_lstm_cannot_request_release_unless_every_gate_strictly_beats_lightgbm() -> None:
    baseline = EvaluationMetrics(EVALUATION_PROTOCOL_VERSION, 0.70, 0.08, 0.80, 0.05, 4.0)
    thresholds = LSTMReleaseThresholds(0.01, 0.01, 0.01, 0.01, 10.0)
    insufficient = EvaluationMetrics(EVALUATION_PROTOCOL_VERSION, 0.72, 0.06, 0.82, 0.07, 10.0)
    accepted = EvaluationMetrics(EVALUATION_PROTOCOL_VERSION, 0.72, 0.06, 0.82, 0.07, 9.9)

    rejected = assess_lstm_release(
        candidate=insufficient, lightgbm_baseline=baseline, thresholds=thresholds
    )
    approved_for_request = assess_lstm_release(
        candidate=accepted, lightgbm_baseline=baseline, thresholds=thresholds
    )
    assert rejected.may_request_release is False
    assert rejected.reasons == ("inference 延迟未满足门槛",)
    assert approved_for_request.may_request_release is True
    assert approved_for_request.reasons == ()


def test_quantitative_training_modules_do_not_reference_litellm() -> None:
    modules = (
        Path("src/trade_agent/capabilities/quantitative/application/training.py"),
        Path("src/trade_agent/adapters/model_runtime/lightgbm.py"),
        Path("src/trade_agent/adapters/model_runtime/lstm.py"),
    )
    for module in modules:
        assert "litellm" not in module.read_text(encoding="utf-8").lower()
