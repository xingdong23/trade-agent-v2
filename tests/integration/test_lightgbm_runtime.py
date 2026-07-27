"""LightGBM 专用模型 adapter 的可复现与 artifact 测试。"""

import hashlib

import pytest

from trade_agent.adapters.model_runtime import LightGBMArtifact, LightGBMPredictor, LightGBMTrainer

FEATURES = (
    (-0.10, 0.30),
    (-0.05, 0.20),
    (0.01, 0.10),
    (0.05, 0.15),
    (0.08, 0.12),
    (0.12, 0.18),
)
LABELS = (0, 0, 0, 1, 1, 1)


def _train() -> LightGBMArtifact:
    return LightGBMTrainer().train(
        feature_names=("return_1d", "volatility_20d"),
        training_features=FEATURES,
        training_labels=LABELS,
        calibration_features=FEATURES,
        calibration_labels=LABELS,
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


def test_lightgbm_rejects_invalid_training_shape() -> None:
    with pytest.raises(ValueError, match="数量不一致"):
        LightGBMTrainer().train(
            feature_names=("return_1d",),
            training_features=((0.1,),),
            training_labels=(),
            calibration_features=((0.1,),),
            calibration_labels=(1,),
            random_seed=42,
            hyperparameters={},
        )
