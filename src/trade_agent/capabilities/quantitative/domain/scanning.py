"""量化扫描提交快照、确定性条件与结构化结果契约。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from trade_agent.core.llm.contracts import JsonValue


class ComparisonOperator(StrEnum):
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    EQUAL = "eq"
    NOT_EQUAL = "ne"


class ScanDisposition(StrEnum):
    MATCHED = "matched"
    NON_MATCH = "non_match"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HardRule:
    """定义一个必须命中的确定性硬规则。

    Attributes:
        rule_id: 规则稳定标识。
        feature_name: 被比较的特征名称。
        operator: 比较运算符。
        expected: 与特征值比较的目标数值。

    Invariants:
        - `rule_id` 与 `feature_name` 不能为空白字符串。
        - `expected` 必须是有限数值。
    """

    rule_id: str
    feature_name: str
    operator: ComparisonOperator
    expected: float

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or not self.feature_name.strip():
            raise ValueError("hard rule 必须包含 rule_id 与 feature_name")
        if not math.isfinite(self.expected):
            raise ValueError("hard rule expected 必须是有限数值")

    def matches(self, actual: float) -> bool:
        operators = {
            ComparisonOperator.GREATER_THAN: actual > self.expected,
            ComparisonOperator.GREATER_THAN_OR_EQUAL: actual >= self.expected,
            ComparisonOperator.LESS_THAN: actual < self.expected,
            ComparisonOperator.LESS_THAN_OR_EQUAL: actual <= self.expected,
            ComparisonOperator.EQUAL: actual == self.expected,
            ComparisonOperator.NOT_EQUAL: actual != self.expected,
        }
        return operators[self.operator]


@dataclass(frozen=True, slots=True)
class ConditionOutcome:
    """记录某个条件或规则在单只证券上的判定结果。

    Attributes:
        condition_id: 条件稳定标识。
        matched: 条件是否命中。
        actual: 实际观测值。
        expected: 期望值或门槛。
        message: 面向上层的结构化解释文本。
    """

    condition_id: str
    matched: bool
    actual: JsonValue
    expected: JsonValue
    message: str


@dataclass(frozen=True, slots=True)
class StrategyVersionSnapshot:
    """冻结扫描所依赖的策略版本信息。

    Attributes:
        strategy_version_id: 策略版本稳定标识。
        owner_id: 该策略所属 owner。
        published: 该版本是否已发布且允许扫描使用。
        target: 策略绑定的预测目标。
        horizon: 策略绑定的预测周期。
        required_features: 策略要求必须提供的特征集合。
        hard_rules: 扫描前必须满足的确定性硬规则。
    """

    strategy_version_id: str
    owner_id: str
    published: bool
    target: str
    horizon: str
    required_features: tuple[str, ...]
    hard_rules: tuple[HardRule, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_features", tuple(self.required_features))
        object.__setattr__(self, "hard_rules", tuple(self.hard_rules))


@dataclass(frozen=True, slots=True)
class ScanUniverseSnapshot:
    """冻结一次扫描所覆盖的证券集合。

    Attributes:
        universe_snapshot_id: 证券集合快照标识。
        owner_id: 该集合所属 owner。
        security_ids: 扫描需要覆盖的全部证券稳定标识。
    """

    universe_snapshot_id: str
    owner_id: str
    security_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "security_ids", tuple(self.security_ids))


@dataclass(frozen=True, slots=True)
class ApprovedModelSnapshot:
    """冻结扫描时指定的生产模型版本信息。

    Attributes:
        model_version_id: 模型版本稳定标识。
        market: 模型适用市场。
        target: 模型预测目标。
        horizon: 模型预测周期。
        approved: 该模型是否已通过审批并允许生产使用。
    """

    model_version_id: str
    market: str
    target: str
    horizon: str
    approved: bool


@dataclass(frozen=True, slots=True)
class ScanSecurityInput:
    """封装单只证券进入扫描时的全部冻结输入。

    Attributes:
        security_id: 证券稳定标识。
        market: 证券所属市场。
        exchange: 证券上市交易所。
        average_dollar_volume: 平均美元成交额, 用于流动性门禁。
        feature_snapshot_id: 该证券特征快照标识。
        features: 特征名到数值的映射, 缺失值可为 `None`。
        missing_ratio: 特征缺失占比, 范围为 0 到 1。
        out_of_distribution: 输入是否被判定为超出模型适用范围。
        data_available: 决策时点数据是否完整可用。
        evidence_refs: 支撑该输入的证据引用标识。
        risks: 已知风险标签。
        gaps: 已知数据缺口标签。

    Invariants:
        - `missing_ratio` 必须位于 0 到 1。
        - `average_dollar_volume` 必须是有限数值。
        - 可变映射与序列在实例化后会被冻结为只读快照。
    """

    security_id: str
    market: str
    exchange: str
    average_dollar_volume: float
    feature_snapshot_id: str
    features: Mapping[str, float | None]
    missing_ratio: float
    out_of_distribution: bool
    data_available: bool
    evidence_refs: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.missing_ratio <= 1.0:
            raise ValueError("missing_ratio 必须位于 0 到 1")
        if not math.isfinite(self.average_dollar_volume):
            raise ValueError("average_dollar_volume 必须是有限数值")
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "risks", tuple(self.risks))
        object.__setattr__(self, "gaps", tuple(self.gaps))


@dataclass(frozen=True, slots=True)
class DataFeatureSnapshot:
    """冻结某一时点的扫描输入数据与特征集合。

    Attributes:
        data_snapshot_id: 数据快照标识。
        feature_set_version: 该快照对应的特征集版本。
        as_of: 数据与特征信息截止时点。
        securities: 全部证券输入快照。

    Invariants:
        - `as_of` 必须带时区。
    """

    data_snapshot_id: str
    feature_set_version: str
    as_of: datetime
    securities: tuple[ScanSecurityInput, ...]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("data/feature snapshot as_of 必须包含时区")
        object.__setattr__(self, "securities", tuple(self.securities))


@dataclass(frozen=True, slots=True)
class RankingDefinition:
    """定义扫描匹配结果的确定性打分函数。

    Attributes:
        version: 排名函数版本标识。
        probability_key: 从模型输出中读取概率的键名。
        probability_weight: 模型概率在总分中的权重。
        feature_weights: 额外特征对总分的线性权重映射。

    Invariants:
        - `version` 与 `probability_key` 不能为空白字符串。
        - 所有权重必须是有限数值。
    """

    version: str
    probability_key: str
    probability_weight: float
    feature_weights: Mapping[str, float]

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.probability_key.strip():
            raise ValueError("ranking 必须包含 version 与 probability_key")
        weights = dict(self.feature_weights)
        if not math.isfinite(self.probability_weight) or any(
            not math.isfinite(value) for value in weights.values()
        ):
            raise ValueError("ranking weight 必须是有限数值")
        object.__setattr__(self, "feature_weights", MappingProxyType(weights))

    def score(self, *, probability: float, features: Mapping[str, float | None]) -> float:
        score = probability * self.probability_weight
        for name, weight in self.feature_weights.items():
            value = features.get(name)
            if value is not None:
                score += value * weight
        return score


@dataclass(frozen=True, slots=True)
class ScanConfiguration:
    """定义一次扫描运行时的确定性筛选参数。

    Attributes:
        version: 配置版本标识。
        market: 扫描允许的市场代码。
        allowed_exchanges: 允许进入扫描的交易所集合。
        minimum_dollar_volume: 最低平均美元成交额门槛。
        maximum_missing_ratio: 最大特征缺失占比。
        minimum_probability: 进入匹配结果所需的最低模型概率。
        parameters: 其他版本化配置参数。

    Invariants:
        - `version` 不能为空, 且 `allowed_exchanges` 不能为空集合。
        - 成交额门槛必须是非负有限数值。
        - 缺失占比和概率门槛必须位于 0 到 1。
    """

    version: str
    market: str
    allowed_exchanges: tuple[str, ...]
    minimum_dollar_volume: float
    maximum_missing_ratio: float
    minimum_probability: float
    parameters: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.market.strip() or not self.allowed_exchanges:
            raise ValueError("scan config 必须包含 version、market 与交易所范围")
        if self.minimum_dollar_volume < 0 or not math.isfinite(self.minimum_dollar_volume):
            raise ValueError("minimum_dollar_volume 必须是非负有限数值")
        if not 0.0 <= self.maximum_missing_ratio <= 1.0:
            raise ValueError("maximum_missing_ratio 必须位于 0 到 1")
        if not 0.0 <= self.minimum_probability <= 1.0:
            raise ValueError("minimum_probability 必须位于 0 到 1")
        object.__setattr__(self, "allowed_exchanges", tuple(self.allowed_exchanges))
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(deepcopy(dict(self.parameters))),
        )


@dataclass(frozen=True, slots=True)
class ScanSubmission:
    """表示一次已通过校验、可执行的扫描提交快照。

    Attributes:
        scan_id: 扫描稳定标识。
        owner_id: 发起该扫描的 owner。
        strategy: 冻结的策略版本快照。
        universe: 冻结的证券集合快照。
        data_features: 冻结的数据与特征快照。
        model: 冻结的已批准模型快照。
        ranking: 冻结的排名函数定义。
        configuration: 冻结的扫描配置。
        submitted_at: 提交时间。
    """

    scan_id: str
    owner_id: str
    strategy: StrategyVersionSnapshot
    universe: ScanUniverseSnapshot
    data_features: DataFeatureSnapshot
    model: ApprovedModelSnapshot
    ranking: RankingDefinition
    configuration: ScanConfiguration
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class ScanResult:
    """记录单只证券在一次扫描中的最终结果与 lineage。

    Attributes:
        scan_id: 所属扫描标识。
        security_id: 证券稳定标识。
        disposition: 结果类型, 如命中、未命中或不可用。
        rank: 命中结果的排序名次; 未命中或不可用时可为空。
        probability: 用于筛选的模型概率。
        score: 排名分数。
        matched_conditions: 已命中的条件列表。
        excluded_conditions: 导致排除或未命中的条件列表。
        evidence_refs: 支撑该结果的证据引用标识。
        data_snapshot_id: 数据快照标识。
        feature_snapshot_id: 特征快照标识。
        feature_set_version: 特征集版本。
        model_version_id: 产生该结果的模型版本; 不可用时可为空。
        ranking_version: 排名函数版本。
        risks: 结果携带的风险标签。
        gaps: 结果携带的数据缺口标签。
        reason: 非命中或不可用时的人类可读原因。
    """

    scan_id: str
    security_id: str
    disposition: ScanDisposition
    rank: int | None
    probability: float | None
    score: float | None
    matched_conditions: tuple[ConditionOutcome, ...]
    excluded_conditions: tuple[ConditionOutcome, ...]
    evidence_refs: tuple[str, ...]
    data_snapshot_id: str
    feature_snapshot_id: str
    feature_set_version: str
    model_version_id: str | None
    ranking_version: str
    risks: tuple[str, ...]
    gaps: tuple[str, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScanEvaluation:
    """汇总一次扫描对全部证券的评估输出。

    Attributes:
        scan_id: 扫描稳定标识。
        results: 按扫描 universe 顺序排列的证券结果集合。
    """

    scan_id: str
    results: tuple[ScanResult, ...]

    @property
    def matched(self) -> tuple[ScanResult, ...]:
        return tuple(item for item in self.results if item.disposition is ScanDisposition.MATCHED)
