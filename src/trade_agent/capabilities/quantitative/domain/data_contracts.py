"""美股量化数据、目标与 feature 版本契约。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class PredictionTarget(StrEnum):
    RETURN = "return"
    DIRECTION = "direction"
    VOLATILITY = "volatility"


class AdjustmentPolicy(StrEnum):
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

    market: str = "US"
    exchanges: tuple[str, ...] = ("NASDAQ", "NYSE", "AMEX")

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
class FeatureRow:
    """承载单个交易日原始输入字段及质量元数据。

    Attributes:
        trading_date: 该行数据对应的交易日。
        values: 原始字段到数值的映射。
        suspended: 该证券在当日是否停牌。
        corporate_action_adjusted: 是否已按要求完成公司行动复权。
        adjustment_version: 该行采用的复权版本标识。
    """

    trading_date: date
    values: Mapping[str, float | None]
    suspended: bool = False
    corporate_action_adjusted: bool = True
    adjustment_version: str = "split.v1"


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
            if adjustment_version is not None and row.adjustment_version != adjustment_version:
                raise FeatureQualityError("feature row 的复权版本不一致")
            if not row.corporate_action_adjusted:
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


def default_feature_set() -> FeatureSet:
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
