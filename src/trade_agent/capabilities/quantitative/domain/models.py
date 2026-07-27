"""量化模型版本、预测结果和扫描 lineage 的基础模型。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """一个可审计、不可变的专用量化模型版本。

    Attributes:
        model_version_id: 模型版本稳定标识。
        algorithm: LightGBM、LSTM 等算法类别。
        status: candidate、approved 或 retired。
        target: 收益、方向或波动率等预测目标。
        horizon: 预测周期。
        data_snapshot_id: 训练数据快照标识。
        feature_set_version: 特征定义版本。
        artifact_hash: 模型 artifact 完整性摘要。
        created_at: 模型版本创建时间。
    """

    model_version_id: str
    algorithm: str
    status: str
    target: str
    horizon: str
    data_snapshot_id: str
    feature_set_version: str
    artifact_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Prediction:
    """专用模型对一个证券产生的版本化预测。

    Attributes:
        prediction_id: 预测稳定标识。
        owner_id: 资源所有者。
        security_id: 规范证券标识。
        model_version_id: 实际执行的已批准模型版本。
        feature_snapshot_id: 推理输入特征快照。
        target: 预测目标。
        horizon: 预测周期。
        as_of: 信息截止时点。
        output: 概率分布或数值预测。
        uncertainty: 校准、适用性和不确定性说明。
    """

    prediction_id: str
    owner_id: str
    security_id: str
    model_version_id: str
    feature_snapshot_id: str
    target: str
    horizon: str
    as_of: datetime
    output: Mapping[str, JsonValue]
    uncertainty: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Scan:
    """冻结策略、universe 和模型版本的一次量化扫描引用。

    Attributes:
        scan_id: 扫描稳定标识。
        owner_id: 资源所有者。
        strategy_version_id: 冻结策略版本。
        universe_snapshot_id: 冻结证券集合。
        model_version_id: 冻结的已批准模型版本。
        status: 扫描生命周期状态。
        version: 乐观并发版本。
    """

    scan_id: str
    owner_id: str
    strategy_version_id: str
    universe_snapshot_id: str
    model_version_id: str
    status: str
    version: int
