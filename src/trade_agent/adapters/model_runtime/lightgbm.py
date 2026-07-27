"""LightGBM 专用模型训练、校准、归因与可重现 artifact adapter。"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

ARTIFACT_FORMAT = "trade-agent.lightgbm.v1"


class ModelRuntimeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlattCalibration:
    slope: float
    intercept: float

    def apply(self, probability: float) -> float:
        clipped = min(max(probability, 1e-12), 1.0 - 1e-12)
        score = self.slope * math.log(clipped / (1.0 - clipped)) + self.intercept
        return _sigmoid(score)


@dataclass(frozen=True, slots=True)
class LightGBMArtifact:
    artifact_bytes: bytes
    artifact_hash: str
    feature_names: tuple[str, ...]
    feature_attribution: Mapping[str, float]
    calibration: PlattCalibration


class LightGBMTrainer:
    """只使用 LightGBM SDK, 不接触任何生成式模型。"""

    def train(
        self,
        *,
        feature_names: Sequence[str],
        training_features: Sequence[Sequence[float]],
        training_labels: Sequence[int],
        calibration_features: Sequence[Sequence[float]],
        calibration_labels: Sequence[int],
        random_seed: int,
        hyperparameters: Mapping[str, int | float | str | bool],
    ) -> LightGBMArtifact:
        _validate_matrix(feature_names, training_features, training_labels, "训练")
        _validate_matrix(feature_names, calibration_features, calibration_labels, "校准")
        lightgbm = _load_lightgbm()
        numpy = _load_numpy()
        parameters: dict[str, int | float | str | bool] = {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "seed": random_seed,
            "feature_fraction_seed": random_seed,
            "bagging_seed": random_seed,
            "data_random_seed": random_seed,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": 1,
        }
        parameters.update(hyperparameters)
        rounds = int(parameters.pop("num_boost_round", 40))
        if rounds < 1:
            raise ValueError("num_boost_round 必须为正")
        dataset = lightgbm.Dataset(
            numpy.asarray(training_features, dtype="float64"),
            label=numpy.asarray(training_labels, dtype="int8"),
            feature_name=list(feature_names),
            free_raw_data=False,
        )
        booster = lightgbm.train(parameters, dataset, num_boost_round=rounds)
        raw_probabilities = tuple(
            float(value)
            for value in booster.predict(numpy.asarray(calibration_features, dtype="float64"))
        )
        calibration = fit_platt_calibration(raw_probabilities, calibration_labels)
        attribution = _normalised_attribution(
            tuple(feature_names),
            tuple(float(value) for value in booster.feature_importance(importance_type="gain")),
        )
        payload = {
            "calibration": {"intercept": calibration.intercept, "slope": calibration.slope},
            "feature_attribution": attribution,
            "feature_names": list(feature_names),
            "format": ARTIFACT_FORMAT,
            "model": booster.model_to_string(num_iteration=booster.current_iteration()),
        }
        artifact_bytes = _canonical_json(payload)
        return LightGBMArtifact(
            artifact_bytes,
            hashlib.sha256(artifact_bytes).hexdigest(),
            tuple(feature_names),
            attribution,
            calibration,
        )


class LightGBMPredictor:
    def __init__(self, artifact: bytes) -> None:
        payload = json.loads(artifact)
        if not isinstance(payload, dict) or payload.get("format") != ARTIFACT_FORMAT:
            raise ValueError("不是受支持的 LightGBM model artifact")
        calibration = cast(dict[str, float], payload["calibration"])
        self.feature_names = tuple(cast(list[str], payload["feature_names"]))
        self.calibration = PlattCalibration(
            float(calibration["slope"]), float(calibration["intercept"])
        )
        self._booster = _load_lightgbm().Booster(model_str=cast(str, payload["model"]))

    def predict_probabilities(self, features: Sequence[Sequence[float]]) -> tuple[float, ...]:
        _validate_feature_rows(self.feature_names, features, "inference")
        values = self._booster.predict(_load_numpy().asarray(features, dtype="float64"))
        return tuple(self.calibration.apply(float(value)) for value in values)


def fit_platt_calibration(
    probabilities: Sequence[float], labels: Sequence[int], *, iterations: int = 100
) -> PlattCalibration:
    """用固定迭代次数的牛顿法拟合一维 Platt scaling。"""

    if not probabilities or len(probabilities) != len(labels):
        raise ValueError("校准 probability 与 label 必须非空且长度一致")
    if any(label not in (0, 1) for label in labels):
        raise ValueError("校准只支持二分类 label")
    scores = []
    for probability in probabilities:
        clipped = min(max(float(probability), 1e-12), 1.0 - 1e-12)
        scores.append(math.log(clipped / (1.0 - clipped)))
    slope = 1.0
    intercept = 0.0
    regularisation = 1e-6
    for _ in range(iterations):
        predictions = [_sigmoid(slope * score + intercept) for score in scores]
        triples = tuple(zip(predictions, labels, scores, strict=True))
        gradient_slope = sum((prediction - label) * score for prediction, label, score in triples)
        gradient_intercept = sum(
            prediction - label for prediction, label in zip(predictions, labels, strict=True)
        )
        hessian_ss = (
            sum(prediction * (1.0 - prediction) * score * score for prediction, _, score in triples)
            + regularisation
        )
        hessian_si = sum(
            prediction * (1.0 - prediction) * score for prediction, _, score in triples
        )
        hessian_ii = (
            sum(prediction * (1.0 - prediction) for prediction in predictions) + regularisation
        )
        determinant = hessian_ss * hessian_ii - hessian_si * hessian_si
        if abs(determinant) < 1e-18:
            break
        delta_slope = (hessian_ii * gradient_slope - hessian_si * gradient_intercept) / determinant
        delta_intercept = (
            -hessian_si * gradient_slope + hessian_ss * gradient_intercept
        ) / determinant
        slope -= delta_slope
        intercept -= delta_intercept
        if max(abs(delta_slope), abs(delta_intercept)) < 1e-10:
            break
    return PlattCalibration(slope, intercept)


def _load_lightgbm() -> Any:
    try:
        return importlib.import_module("lightgbm")
    except ModuleNotFoundError as error:
        raise ModelRuntimeUnavailable(
            "LightGBM runtime 未安装, 请安装 quantitative 可选依赖"
        ) from error


def _load_numpy() -> Any:
    try:
        return importlib.import_module("numpy")
    except ModuleNotFoundError as error:
        raise ModelRuntimeUnavailable(
            "NumPy runtime 未安装, 请安装 quantitative 可选依赖"
        ) from error


def _validate_matrix(
    feature_names: Sequence[str],
    features: Sequence[Sequence[float]],
    labels: Sequence[int],
    purpose: str,
) -> None:
    _validate_feature_rows(feature_names, features, purpose)
    if len(features) != len(labels):
        raise ValueError(f"{purpose} feature 与 label 数量不一致")
    if any(label not in (0, 1) for label in labels):
        raise ValueError(f"{purpose} label 只能是 0 或 1")


def _validate_feature_rows(
    feature_names: Sequence[str], features: Sequence[Sequence[float]], purpose: str
) -> None:
    if not feature_names or len(set(feature_names)) != len(feature_names):
        raise ValueError("feature name 必须非空且唯一")
    if not features:
        raise ValueError(f"{purpose} feature 不能为空")
    if any(len(row) != len(feature_names) for row in features):
        raise ValueError(f"{purpose} feature 列数与 feature name 不一致")
    if any(not math.isfinite(float(value)) for row in features for value in row):
        raise ValueError(f"{purpose} feature 不允许 NaN 或无穷值")


def _normalised_attribution(
    feature_names: tuple[str, ...], gains: tuple[float, ...]
) -> dict[str, float]:
    total = sum(gains)
    if total <= 0:
        return {name: 0.0 for name in feature_names}
    return {name: gain / total for name, gain in zip(feature_names, gains, strict=True)}


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)
