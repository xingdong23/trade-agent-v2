"""美股量化数据、目标与 feature 版本契约。"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class PredictionTarget(StrEnum):
    """量化预测目标的稳定枚举。

    Attributes:
        RETURN: 预测未来收益或回报幅度。
        DIRECTION: 预测未来方向，例如涨跌或正负类。
        VOLATILITY: 预测未来波动率或风险水平。

    Invariants:
        - 枚举值用于任务定义、模型 lineage 与推理协议，属于稳定字段。
    """

    RETURN = "return"
    DIRECTION = "direction"
    VOLATILITY = "volatility"


class SupervisedTaskType(StrEnum):
    """声明专用模型训练任务的统计类型。

    Attributes:
        BINARY_CLASSIFICATION: 二分类任务，标签空间固定为两个离散值。
        REGRESSION: 回归任务，标签位于连续数值区间。

    Invariants:
        - 枚举值决定标签 schema 与评测方式，属于稳定训练协议字段。
    """

    BINARY_CLASSIFICATION = "binary_classification"
    REGRESSION = "regression"


@dataclass(frozen=True, slots=True)
class LabelSchema:
    """定义训练标签允许的数值空间。

    Attributes:
        schema_id: 可写入模型 lineage 的稳定 schema 标识。
        allowed_values: 离散任务允许的完整标签集合；连续任务应留空。
        minimum: 连续标签允许的最小值；不限制时为空。
        maximum: 连续标签允许的最大值；不限制时为空。

    Invariants:
        - `schema_id` 不能为空。
        - 离散值、最小值和最大值都必须是有限数。
        - 同一 schema 不能同时声明离散集合与连续区间。
    """

    schema_id: str
    allowed_values: tuple[float, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if not self.schema_id.strip():
            raise ValueError("label schema_id 不能为空")
        declared = (*self.allowed_values, self.minimum, self.maximum)
        if any(value is not None and not math.isfinite(value) for value in declared):
            raise ValueError("label schema 只允许有限数值")
        if self.allowed_values and (self.minimum is not None or self.maximum is not None):
            raise ValueError("label schema 不能同时声明离散集合与连续区间")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("label schema 最小值不能大于最大值")

    def validate(self, values: Sequence[float], *, purpose: str) -> None:
        """校验一组标签是否满足当前 schema。"""

        if not values:
            raise ValueError(f"{purpose} label 不能为空")
        normalized = tuple(float(value) for value in values)
        if any(not math.isfinite(value) for value in normalized):
            raise ValueError(f"{purpose} label 不允许 NaN 或无穷值")
        if self.allowed_values and any(value not in self.allowed_values for value in normalized):
            raise ValueError(f"{purpose} label 不符合 {self.schema_id}")
        if self.minimum is not None and any(value < self.minimum for value in normalized):
            raise ValueError(f"{purpose} label 小于 {self.schema_id} 下限")
        if self.maximum is not None and any(value > self.maximum for value in normalized):
            raise ValueError(f"{purpose} label 大于 {self.schema_id} 上限")


@dataclass(frozen=True, slots=True)
class ModelTaskSpec:
    """把预测目标、任务类型、标签和评测协议绑定为不可变训练契约。

    Attributes:
        target: 目标稳定标识，例如 direction、return 或 volatility。
        horizon: 预测周期稳定标识。
        task_type: 二分类或回归等统计任务类型。
        label_schema: 训练标签的显式校验规则。
        output_name: 模型标准输出字段名。
        evaluation_protocol: 训练产物必须采用的评测协议版本。

    Invariants:
        - 所有稳定标识不能为空。
        - 二分类任务必须声明且只能声明两个离散标签。
        - 回归任务不能声明离散标签集合。
    """

    target: str
    horizon: str
    task_type: SupervisedTaskType
    label_schema: LabelSchema
    output_name: str
    evaluation_protocol: str

    def __post_init__(self) -> None:
        required = (self.target, self.horizon, self.output_name, self.evaluation_protocol)
        if any(not value.strip() for value in required):
            raise ValueError("model task spec 的稳定标识不能为空")
        if self.task_type is SupervisedTaskType.BINARY_CLASSIFICATION:
            if len(self.label_schema.allowed_values) != 2:
                raise ValueError("二分类任务必须声明两个离散 label")
        elif self.label_schema.allowed_values:
            raise ValueError("回归任务不能声明离散 label 集合")


@dataclass(frozen=True, slots=True)
class ModelArtifactLineage:
    """记录模型工件可追溯到的训练任务与数据版本。

    Attributes:
        training_job_id: 产生该工件的训练任务标识。
        reproducibility_key: 训练任务规范输入的稳定摘要。
        data_snapshot_id: point-in-time 训练数据快照标识。
        feature_set_version: 训练使用的特征集合版本。
        code_version: 训练代码版本。

    Invariants:
        - 每个 lineage 字段都必须由训练编排层显式提供。
    """

    training_job_id: str
    reproducibility_key: str
    data_snapshot_id: str
    feature_set_version: str
    code_version: str

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.training_job_id,
                self.reproducibility_key,
                self.data_snapshot_id,
                self.feature_set_version,
                self.code_version,
            )
        ):
            raise ValueError("model artifact lineage 字段不能为空")


class AdjustmentPolicy(StrEnum):
    """量化数据公司行动处理策略的稳定枚举。

    Attributes:
        RAW: 使用未复权原始价格与成交量。
        SPLIT_ADJUSTED: 仅做拆股等股本变动复权。
        TOTAL_RETURN: 同时考虑拆股与分红的总回报复权。

    Invariants:
        - 枚举值用于特征 lineage 与数据快照声明，属于稳定协议字段。
    """

    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN = "total_return"


@dataclass(frozen=True, slots=True)
class MarketScope:
    """定义量化能力允许覆盖的市场与交易所范围。

    Attributes:
        market: 稳定市场标识。首版固定为 `US`。
        exchanges: 允许进入训练和推理流程的美国交易所列表。

    Invariants:
        - 首版量化能力只支持美股市场。
        - 交易所范围至少包含一个交易所。
    """

    market: str
    exchanges: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.market != "US":
            raise ValueError("首版量化能力只支持美股")
        if not self.exchanges:
            raise ValueError("必须配置至少一个美国交易所")


@dataclass(frozen=True, slots=True)
class TargetDefinition:
    """定义一个量化预测目标的标签语义与可用延迟。

    Attributes:
        target: 预测目标类型, 如收益率、方向或波动率。
        horizon_trading_days: 预测覆盖的未来交易日数量。
        label_available_delay_days: 标签在决策时点后需要等待的额外交易日数量。

    Invariants:
        - 预测周期必须为正整数。
        - 标签可用延迟不能为负。
    """

    target: PredictionTarget
    horizon_trading_days: int
    label_available_delay_days: int

    def __post_init__(self) -> None:
        if self.horizon_trading_days < 1 or self.label_available_delay_days < 0:
            raise ValueError("预测周期必须为正, label 延迟不能为负")


@dataclass(frozen=True, slots=True)
class DataAvailability:
    """描述单个数据点从事件发生到可被模型使用的时间边界。

    Attributes:
        event_time: 数据代表的业务事件发生时间。
        available_at: 数据对训练或推理流程可见的最早时间。

    Invariants:
        - 两个时间都必须带时区。
        - `available_at` 不得早于 `event_time`。
    """

    event_time: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        if self.event_time.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("数据时间必须包含时区")
        if self.available_at < self.event_time:
            raise ValueError("available_at 不能早于 event_time")


@dataclass(frozen=True, slots=True)
class TradingSession:
    """描述一个美股交易日的开闭市时间窗口。

    Attributes:
        trading_date: 交易日日期。
        opens_at: 当日开市时间。
        closes_at: 当日收市时间。

    Invariants:
        - 开闭市时间必须带时区。
        - 收市时间必须晚于开市时间。
    """

    trading_date: date
    opens_at: datetime
    closes_at: datetime

    def __post_init__(self) -> None:
        if self.opens_at.tzinfo is None or self.closes_at.tzinfo is None:
            raise ValueError("交易时段必须包含时区")
        if self.closes_at <= self.opens_at:
            raise ValueError("收盘时间必须晚于开盘时间")


@dataclass(frozen=True, slots=True)
class USTradingCalendar:
    """提供版本化的美股交易日日历查询。

    Attributes:
        version: 交易日日历版本标识。
        timezone: 日历采用的时区名称。
        sessions: 以交易日为键的交易时段映射。

    Invariants:
        - `sessions` 中只应包含已配置的美国交易日。
    """

    version: str
    timezone: str
    sessions: Mapping[date, TradingSession]

    def session_for(self, trading_date: date) -> TradingSession:
        try:
            return self.sessions[trading_date]
        except KeyError as error:
            raise ValueError(f"不是已配置的美股交易日: {trading_date}") from error


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """描述一个可复用特征的定义、来源与计算入口。

    Attributes:
        name: 特征稳定名称。
        version: 特征定义版本。
        category: 特征类别, 例如价格、流动性或基本面。
        input_fields: 计算该特征所依赖的原始字段集合。
        lineage: 特征来源或加工链路标识。
        compute: 从原始字段映射计算该特征值的纯函数。
    """

    name: str
    version: str
    category: str
    input_fields: tuple[str, ...]
    lineage: str
    compute: Callable[[Mapping[str, float | None]], float | None]


@dataclass(frozen=True, slots=True)
class FeatureSet:
    """将一组特征定义冻结为可复用的版本快照。

    Attributes:
        version: 特征集合版本标识。
        definitions: 属于该版本的全部特征定义。
    """

    version: str
    definitions: tuple[FeatureDefinition, ...]


class FeatureRegistry:
    def __init__(self) -> None:
        self._sets: dict[str, FeatureSet] = {}

    def register(self, feature_set: FeatureSet) -> None:
        if feature_set.version in self._sets:
            raise ValueError(f"feature set 版本已注册: {feature_set.version}")
        names = [item.name for item in feature_set.definitions]
        if len(names) != len(set(names)):
            raise ValueError("同一 feature set 不允许重复 feature name")
        self._sets[feature_set.version] = feature_set

    def get(self, version: str) -> FeatureSet:
        try:
            return self._sets[version]
        except KeyError as error:
            raise KeyError(f"未知 feature set: {version}") from error


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """表示一次计算结果中与特征集版本对齐的特征向量。

    Attributes:
        feature_set_version: 生成该向量所用的特征集版本。
        values: 特征名到数值的映射, 缺失值可显式为 `None`。
    """

    feature_set_version: str
    values: Mapping[str, float | None]


@dataclass(frozen=True, slots=True)
class AdjustmentMetadata:
    """一行特征输入使用的公司行动复权声明。

    Attributes:
        policy: 原始、拆股复权或总回报复权策略。
        version: 生成该复权结果的规则或数据版本。
        adjusted: 调用方是否确认已按声明策略完成处理。

    Invariants:
        - Version 必须显式提供，不能用默认值伪造 lineage。
    """

    policy: AdjustmentPolicy
    version: str
    adjusted: bool

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("复权 metadata 必须包含显式版本")


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """承载单个交易日原始输入字段及质量元数据。

    Attributes:
        trading_date: 该行数据对应的交易日。
        values: 原始字段到数值的映射。
        adjustment: 调用方显式提供的复权策略、版本与完成状态。
        suspended: 该证券在当日是否停牌。
    """

    trading_date: date
    values: Mapping[str, float | None]
    adjustment: AdjustmentMetadata
    suspended: bool = False


class FeatureQualityError(ValueError):
    pass


class FeatureCalculator:
    """训练和 inference 必须复用的唯一 feature 计算入口。"""

    def __init__(self, registry: FeatureRegistry) -> None:
        self._registry = registry

    def calculate(
        self,
        feature_set_version: str,
        rows: Sequence[FeatureRow],
        *,
        calendar: USTradingCalendar | None = None,
        adjustment_version: str | None = None,
    ) -> tuple[FeatureVector, ...]:
        feature_set = self._registry.get(feature_set_version)
        vectors: list[FeatureVector] = []
        for row in rows:
            if calendar is not None:
                calendar.session_for(row.trading_date)
            if adjustment_version is not None and row.adjustment.version != adjustment_version:
                raise FeatureQualityError("feature row 的复权版本不一致")
            if not row.adjustment.adjusted:
                raise FeatureQualityError("公司行动尚未按配置规则复权")
            values = (
                {definition.name: None for definition in feature_set.definitions}
                if row.suspended
                else {
                    definition.name: definition.compute(row.values)
                    for definition in feature_set.definitions
                }
            )
            vectors.append(FeatureVector(feature_set_version, values))
        return tuple(vectors)


def us_equity_core_feature_set() -> FeatureSet:
    """返回显式命名的首版美股核心特征集快照。"""

    def passthrough(name: str) -> Callable[[Mapping[str, float | None]], float | None]:
        return lambda row: row.get(name)

    definitions = (
        ("close", "price", "close", "split_adjusted_ohlcv"),
        ("volume", "volume", "volume", "raw_exchange_volume"),
        ("return_1d", "return", "return_1d", "split_adjusted_close"),
        ("volatility_20d", "volatility", "volatility_20d", "daily_returns"),
        ("trend_20d", "trend", "trend_20d", "split_adjusted_close"),
        ("dollar_volume_20d", "liquidity", "dollar_volume_20d", "price_volume"),
        ("revenue_growth", "fundamental", "revenue_growth", "point_in_time_filing"),
    )
    return FeatureSet(
        version="us-equity-core.v1",
        definitions=tuple(
            FeatureDefinition(name, "1", category, (source,), lineage, passthrough(source))
            for name, category, source, lineage in definitions
        ),
    )
