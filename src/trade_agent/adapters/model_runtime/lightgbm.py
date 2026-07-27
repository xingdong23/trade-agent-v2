"""LightGBM 专用模型训练、校准、归因与可重现 artifact adapter。"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from trade_agent.capabilities.quantitative.contracts import (
    LabelSchema,
    ModelArtifactLineage,
    ModelTaskSpec,
    SupervisedTaskType,
)

ARTIFACT_FORMAT = "trade-agent.lightgbm.v1"


class ModelRuntimeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlattCalibration:
    """把原始模型分数转换为校准概率的 Platt 参数。

    Attributes:
        slope: Logit 线性变换斜率。
        intercept: Logit 线性变换截距。
    """

    slope: float
    intercept: float

    def apply(self, probability: float) -> float:
        clipped = min(max(probability, 1e-12), 1.0 - 1e-12)
        score = self.slope * math.log(clipped / (1.0 - clipped)) + self.intercept
        return _sigmoid(score)


@dataclass(frozen=True, slots=True)
class LightGBMRuntimeSpec:
    """声明一次 LightGBM 训练的任务语义与 SDK 参数。

    Attributes:
        task: 与 target、label 和评测协议绑定的领域任务契约。
        objective: 交给 LightGBM SDK 的显式 objective。
        metric: 交给 LightGBM SDK 的显式评测 metric。

    Invariants:
        - objective 与 metric 必须由调用方显式提供，adapter 不猜测。
    """

    task: ModelTaskSpec
    objective: str
    metric: str

    def __post_init__(self) -> None:
        if not self.objective.strip() or not self.metric.strip():
            raise ValueError("LightGBM objective 与 metric 必须显式提供")


@dataclass(frozen=True, slots=True)
class LightGBMArtifact:
    """训练后可持久化、可复现的 LightGBM 模型包。

    Attributes:
        artifact_bytes: 包含模型、特征和校准信息的规范字节串。
        artifact_hash: Artifact SHA-256 完整性摘要。
        feature_names: 推理时必须保持相同顺序的特征名。
        feature_attribution: 归一化特征重要性。
        calibration: 二分类任务使用的 Platt 参数；回归任务为空。
        runtime_spec: 训练时实际采用的任务、objective 与 metric。
        effective_parameters: 包含确定性控制项在内的完整生效参数。
        lineage: 训练任务、数据、特征与代码版本来源。
    """

    artifact_bytes: bytes
    artifact_hash: str
    feature_names: tuple[str, ...]
    feature_attribution: Mapping[str, float]
    calibration: PlattCalibration | None
    runtime_spec: LightGBMRuntimeSpec
    effective_parameters: Mapping[str, int | float | str | bool]
    lineage: ModelArtifactLineage


class LightGBMTrainer:
    """只使用 LightGBM SDK 训练可复现模型。

    Contract:
        - 训练与校准数据形状必须匹配同一特征顺序。
        - 固定随机种子并使用单线程 deterministic 模式。
        - 本 adapter 不依赖 LLM，也不接受 LLM 生成的评分或排名。
    """

    def train(
        self,
        *,
        feature_names: Sequence[str],
        training_features: Sequence[Sequence[float]],
        training_labels: Sequence[float],
        calibration_features: Sequence[Sequence[float]],
        calibration_labels: Sequence[float],
        runtime_spec: LightGBMRuntimeSpec,
        lineage: ModelArtifactLineage,
        random_seed: int,
        hyperparameters: Mapping[str, int | float | str | bool],
    ) -> LightGBMArtifact:
        _validate_matrix(
            feature_names, training_features, training_labels, runtime_spec.task, "训练"
        )
        _validate_matrix(
            feature_names, calibration_features, calibration_labels, runtime_spec.task, "校准"
        )
        lightgbm = _load_lightgbm()
        numpy = _load_numpy()
        parameters: dict[str, int | float | str | bool] = {
            "objective": runtime_spec.objective,
            "metric": runtime_spec.metric,
            "verbosity": -1,
            "seed": random_seed,
            "feature_fraction_seed": random_seed,
            "bagging_seed": random_seed,
            "data_random_seed": random_seed,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": 1,
        }
        reserved = set(parameters).intersection(hyperparameters)
        if reserved:
            raise ValueError(
                "LightGBM 确定性参数必须通过 runtime spec 或 trainer 控制: "
                + ", ".join(sorted(reserved))
            )
        parameters.update(hyperparameters)
        rounds = int(parameters.pop("num_boost_round", 40))
        if rounds < 1:
            raise ValueError("num_boost_round 必须为正")
        dataset = lightgbm.Dataset(
            numpy.asarray(training_features, dtype="float64"),
            label=numpy.asarray(training_labels, dtype="float64"),
            feature_name=list(feature_names),
            free_raw_data=False,
        )
        booster = lightgbm.train(parameters, dataset, num_boost_round=rounds)
        raw_predictions = tuple(
            float(value)
            for value in booster.predict(numpy.asarray(calibration_features, dtype="float64"))
        )
        calibration = (
            fit_platt_calibration(raw_predictions, calibration_labels)
            if runtime_spec.task.task_type is SupervisedTaskType.BINARY_CLASSIFICATION
            else None
        )
        attribution = _normalised_attribution(
            tuple(feature_names),
            tuple(float(value) for value in booster.feature_importance(importance_type="gain")),
        )
        payload = {
            "calibration": (
                {"intercept": calibration.intercept, "slope": calibration.slope}
                if calibration is not None
                else None
            ),
            "effective_parameters": {**parameters, "num_boost_round": rounds},
            "feature_attribution": attribution,
            "feature_names": list(feature_names),
            "format": ARTIFACT_FORMAT,
            "lineage": {
                "code_version": lineage.code_version,
                "data_snapshot_id": lineage.data_snapshot_id,
                "feature_set_version": lineage.feature_set_version,
                "reproducibility_key": lineage.reproducibility_key,
                "training_job_id": lineage.training_job_id,
            },
            "model": booster.model_to_string(num_iteration=booster.current_iteration()),
            "runtime_spec": _runtime_spec_payload(runtime_spec),
        }
        artifact_bytes = _canonical_json(payload)
        return LightGBMArtifact(
            artifact_bytes,
            hashlib.sha256(artifact_bytes).hexdigest(),
            tuple(feature_names),
            attribution,
            calibration,
            runtime_spec,
            cast(dict[str, int | float | str | bool], payload["effective_parameters"]),
            lineage,
        )


class LightGBMPredictor:
    def __init__(self, artifact: bytes) -> None:
        payload = json.loads(artifact)
        if not isinstance(payload, dict) or payload.get("format") != ARTIFACT_FORMAT:
            raise ValueError("不是受支持的 LightGBM model artifact")
        calibration = cast(dict[str, float] | None, payload["calibration"])
        self.feature_names = tuple(cast(list[str], payload["feature_names"]))
        self.calibration = (
            PlattCalibration(float(calibration["slope"]), float(calibration["intercept"]))
            if calibration is not None
            else None
        )
        self.runtime_spec = _runtime_spec_from_payload(
            cast(dict[str, object], payload["runtime_spec"])
        )
        self.effective_parameters = cast(
            dict[str, int | float | str | bool], payload["effective_parameters"]
        )
        lineage = cast(dict[str, str], payload["lineage"])
        self.lineage = ModelArtifactLineage(
            training_job_id=lineage["training_job_id"],
            reproducibility_key=lineage["reproducibility_key"],
            data_snapshot_id=lineage["data_snapshot_id"],
            feature_set_version=lineage["feature_set_version"],
            code_version=lineage["code_version"],
        )
        self._booster = _load_lightgbm().Booster(model_str=cast(str, payload["model"]))

    def predict_values(self, features: Sequence[Sequence[float]]) -> tuple[float, ...]:
        """按 artifact 中声明的任务输出原始预测值。"""

        _validate_feature_rows(self.feature_names, features, "inference")
        values = self._booster.predict(_load_numpy().asarray(features, dtype="float64"))
        return tuple(float(value) for value in values)

    def predict_probabilities(self, features: Sequence[Sequence[float]]) -> tuple[float, ...]:
        if self.runtime_spec.task.task_type is not SupervisedTaskType.BINARY_CLASSIFICATION:
            raise ValueError("当前 LightGBM artifact 不是二分类概率模型")
        if self.calibration is None:
            raise ValueError("二分类 LightGBM artifact 缺少校准参数")
        return tuple(self.calibration.apply(value) for value in self.predict_values(features))


def fit_platt_calibration(
    probabilities: Sequence[float], labels: Sequence[float], *, iterations: int = 100
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
    labels: Sequence[float],
    task: ModelTaskSpec,
    purpose: str,
) -> None:
    _validate_feature_rows(feature_names, features, purpose)
    if len(features) != len(labels):
        raise ValueError(f"{purpose} feature 与 label 数量不一致")
    task.label_schema.validate(labels, purpose=purpose)


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


def _runtime_spec_payload(spec: LightGBMRuntimeSpec) -> dict[str, object]:
    return {
        "evaluation_protocol": spec.task.evaluation_protocol,
        "label_schema": {
            "allowed_values": list(spec.task.label_schema.allowed_values),
            "maximum": spec.task.label_schema.maximum,
            "minimum": spec.task.label_schema.minimum,
            "schema_id": spec.task.label_schema.schema_id,
        },
        "metric": spec.metric,
        "objective": spec.objective,
        "output_name": spec.task.output_name,
        "horizon": spec.task.horizon,
        "target": spec.task.target,
        "task_type": spec.task.task_type.value,
    }


def _runtime_spec_from_payload(payload: Mapping[str, object]) -> LightGBMRuntimeSpec:
    label_payload = cast(dict[str, object], payload["label_schema"])
    task = ModelTaskSpec(
        target=cast(str, payload["target"]),
        horizon=cast(str, payload["horizon"]),
        task_type=SupervisedTaskType(cast(str, payload["task_type"])),
        label_schema=LabelSchema(
            schema_id=cast(str, label_payload["schema_id"]),
            allowed_values=tuple(
                float(value) for value in cast(list[float], label_payload["allowed_values"])
            ),
            minimum=(
                float(cast(float, label_payload["minimum"]))
                if label_payload["minimum"] is not None
                else None
            ),
            maximum=(
                float(cast(float, label_payload["maximum"]))
                if label_payload["maximum"] is not None
                else None
            ),
        ),
        output_name=cast(str, payload["output_name"]),
        evaluation_protocol=cast(str, payload["evaluation_protocol"]),
    )
    return LightGBMRuntimeSpec(
        task=task,
        objective=cast(str, payload["objective"]),
        metric=cast(str, payload["metric"]),
    )


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)
