"""可选 LSTM 候选训练 adapter; 具体深度学习 runtime 通过后端注入。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

EVALUATION_PROTOCOL_VERSION = "quant-evaluation.v1"


class LSTMRuntimeUnavailable(RuntimeError):
    pass


class LSTMBackend(Protocol):
    """PyTorch、Keras 等可选 runtime 应实现的最小专用模型接口。

    Contract:
        - 训练输入必须按固定 sequence length 与 feature 维度解释。
        - 返回值必须是非空二进制 artifact，且可被上层完整持久化。

    Implemented by:
        生产环境的 PyTorch/Keras runtime adapter
        测试中注入的 backend fake
    """

    runtime_name: str
    runtime_version: str

    def train(
        self,
        *,
        sequences: Sequence[Sequence[Sequence[float]]],
        labels: Sequence[int],
        random_seed: int,
        hyperparameters: Mapping[str, int | float | str | bool],
    ) -> bytes:
        """训练一个候选 LSTM 模型并返回原始 artifact。

        Args:
            sequences: ``样本 -> 时间步 -> 特征`` 的三层数值序列。
            labels: 与样本一一对应的二分类标签。
            random_seed: 训练随机种子，保证可复现。
            hyperparameters: runtime 自身识别的超参数集合。

        Returns:
            可持久化的模型二进制内容。

        Raises:
            ValueError: 输入 shape、标签或超参数不满足 runtime 要求。
        """
        ...


@dataclass(frozen=True, slots=True)
class LSTMArtifact:
    """LSTM 候选模型的可持久化打包结果。

    Attributes:
        artifact_bytes: 包含头信息与模型二进制内容的最终 artifact。
        artifact_hash: 对 artifact_bytes 计算得到的稳定摘要。
        feature_names: 训练时使用的特征名顺序。
        sequence_length: 每个样本包含的固定时间步数量。
        evaluation_protocol: 上层评估/发布流程使用的协议版本。
    """

    artifact_bytes: bytes
    artifact_hash: str
    feature_names: tuple[str, ...]
    sequence_length: int
    evaluation_protocol: str = EVALUATION_PROTOCOL_VERSION


class LSTMCandidateTrainer:
    """仅产生候选 artifact; 发布资格由 capability 的统一严格门禁决定。"""

    def __init__(self, backend: LSTMBackend | None = None) -> None:
        self._backend = backend

    def train(
        self,
        *,
        feature_names: Sequence[str],
        sequences: Sequence[Sequence[Sequence[float]]],
        labels: Sequence[int],
        random_seed: int,
        hyperparameters: Mapping[str, int | float | str | bool],
    ) -> LSTMArtifact:
        if self._backend is None:
            raise LSTMRuntimeUnavailable("LSTM 是可选候选模型, 当前未配置专用训练 runtime")
        sequence_length = _validate_sequences(feature_names, sequences, labels)
        model_bytes = self._backend.train(
            sequences=sequences,
            labels=labels,
            random_seed=random_seed,
            hyperparameters=hyperparameters,
        )
        if not model_bytes:
            raise ValueError("LSTM runtime 返回了空 model artifact")
        header = {
            "evaluation_protocol": EVALUATION_PROTOCOL_VERSION,
            "feature_names": list(feature_names),
            "model_hash": hashlib.sha256(model_bytes).hexdigest(),
            "random_seed": random_seed,
            "runtime": self._backend.runtime_name,
            "runtime_version": self._backend.runtime_version,
            "sequence_length": sequence_length,
        }
        encoded_header = json.dumps(
            header,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        artifact_bytes = len(encoded_header).to_bytes(8, "big") + encoded_header + model_bytes
        return LSTMArtifact(
            artifact_bytes,
            hashlib.sha256(artifact_bytes).hexdigest(),
            tuple(feature_names),
            sequence_length,
        )


def _validate_sequences(
    feature_names: Sequence[str],
    sequences: Sequence[Sequence[Sequence[float]]],
    labels: Sequence[int],
) -> int:
    if not feature_names or len(set(feature_names)) != len(feature_names):
        raise ValueError("LSTM feature name 必须非空且唯一")
    if not sequences or len(sequences) != len(labels):
        raise ValueError("LSTM sequence 与 label 必须非空且数量一致")
    if any(label not in (0, 1) for label in labels):
        raise ValueError("LSTM label 只能是 0 或 1")
    sequence_length = len(sequences[0])
    if sequence_length < 1 or any(len(sequence) != sequence_length for sequence in sequences):
        raise ValueError("LSTM sequence 长度必须固定且为正")
    if any(len(step) != len(feature_names) for sequence in sequences for step in sequence):
        raise ValueError("LSTM feature 维度与 feature name 不一致")
    if any(
        not math.isfinite(float(value))
        for sequence in sequences
        for step in sequence
        for value in step
    ):
        raise ValueError("LSTM feature 不允许 NaN 或无穷值")
    return sequence_length
