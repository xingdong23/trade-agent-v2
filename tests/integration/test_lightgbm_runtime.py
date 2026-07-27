"""LightGBM 专用模型 adapter 的可复现与 artifact 测试。"""

import hashlib

import pytest

from trade_agent.adapters.model_runtime import (
    LightGBMArtifact,
    LightGBMPredictor,
    LightGBMRuntimeSpec,
    LightGBMTrainer,
)
from trade_agent.capabilities.quantitative.contracts import (
    LabelSchema,
    ModelArtifactLineage,
    ModelTaskSpec,
    SupervisedTaskType,
)

FEATURES = (
    (-0.10, 0.30),
    (-0.05, 0.20),
    (0.01, 0.10),
    (0.05, 0.15),
    (0.08, 0.12),
    (0.12, 0.18),
)
LABELS = (0, 0, 0, 1, 1, 1)


def _task(
    *,
    target: str = "direction",
    task_type: SupervisedTaskType = SupervisedTaskType.BINARY_CLASSIFICATION,
) -> ModelTaskSpec:
    label_schema = (
        LabelSchema("direction-binary.v1", (0.0, 1.0))
        if task_type is SupervisedTaskType.BINARY_CLASSIFICATION
        else LabelSchema("continuous-return.v1")
    )
    return ModelTaskSpec(
        target,
        "5d",
        task_type,
        label_schema,
        "up_probability" if task_type is SupervisedTaskType.BINARY_CLASSIFICATION else "return",
        "quant-evaluation.v1",
    )


def _lineage() -> ModelArtifactLineage:
    return ModelArtifactLineage("train-1", "repro-1", "data-1", "features-1", "git:test")


def _train() -> LightGBMArtifact:
    return LightGBMTrainer().train(
        feature_names=("return_1d", "volatility_20d"),
        training_features=FEATURES,
        training_labels=LABELS,
        calibration_features=FEATURES,
        calibration_labels=LABELS,
        runtime_spec=LightGBMRuntimeSpec(_task(), "binary", "binary_logloss"),
        lineage=_lineage(),
        random_seed=42,
        hyperparameters={"num_leaves": 3, "min_data_in_leaf": 1, "num_boost_round": 8},
    )


def test_lightgbm_training_is_reproducible_calibrated_and_loadable() -> None:
    first = _train()
    second = _train()

    assert first.artifact_hash == second.artifact_hash
    assert first.artifact_bytes == second.artifact_bytes
    assert first.artifact_hash == hashlib.sha256(first.artifact_bytes).hexdigest()
    assert set(first.feature_attribution) == {"return_1d", "volatility_20d"}

    predictor = LightGBMPredictor(first.artifact_bytes)
    predictions = predictor.predict_probabilities(((0.1, 0.15),))
    assert 0 <= predictions[0] <= 1
    assert predictor.feature_names == first.feature_names
    assert predictor.runtime_spec == first.runtime_spec
    assert predictor.lineage == _lineage()
    assert predictor.effective_parameters["objective"] == "binary"


def test_lightgbm_rejects_invalid_training_shape() -> None:
    with pytest.raises(ValueError, match="数量不一致"):
        LightGBMTrainer().train(
            feature_names=("return_1d",),
            training_features=((0.1,),),
            training_labels=(),
            calibration_features=((0.1,),),
            calibration_labels=(1,),
            runtime_spec=LightGBMRuntimeSpec(_task(), "binary", "binary_logloss"),
            lineage=_lineage(),
            random_seed=42,
            hyperparameters={},
        )


def test_lightgbm_regression_task_does_not_use_binary_calibration() -> None:
    artifact = LightGBMTrainer().train(
        feature_names=("return_1d", "volatility_20d"),
        training_features=FEATURES,
        training_labels=(-0.1, -0.05, 0.01, 0.05, 0.08, 0.12),
        calibration_features=FEATURES,
        calibration_labels=(-0.1, -0.05, 0.01, 0.05, 0.08, 0.12),
        runtime_spec=LightGBMRuntimeSpec(
            _task(target="return", task_type=SupervisedTaskType.REGRESSION), "regression", "l2"
        ),
        lineage=_lineage(),
        random_seed=42,
        hyperparameters={"num_leaves": 3, "min_data_in_leaf": 1, "num_boost_round": 8},
    )

    predictor = LightGBMPredictor(artifact.artifact_bytes)
    assert artifact.calibration is None
    assert len(predictor.predict_values(((0.1, 0.15),))) == 1
    with pytest.raises(ValueError, match="不是二分类概率模型"):
        predictor.predict_probabilities(((0.1, 0.15),))
