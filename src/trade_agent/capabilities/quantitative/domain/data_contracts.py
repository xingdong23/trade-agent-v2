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
    market: str = "US"
    exchanges: tuple[str, ...] = ("NASDAQ", "NYSE", "AMEX")

    def __post_init__(self) -> None:
        if self.market != "US":
            raise ValueError("首版量化能力只支持美股")
        if not self.exchanges:
            raise ValueError("必须配置至少一个美国交易所")


@dataclass(frozen=True, slots=True)
class TargetDefinition:
    target: PredictionTarget
    horizon_trading_days: int
    label_available_delay_days: int

    def __post_init__(self) -> None:
        if self.horizon_trading_days < 1 or self.label_available_delay_days < 0:
            raise ValueError("预测周期必须为正, label 延迟不能为负")


@dataclass(frozen=True, slots=True)
class DataAvailability:
    event_time: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        if self.event_time.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("数据时间必须包含时区")
        if self.available_at < self.event_time:
            raise ValueError("available_at 不能早于 event_time")


@dataclass(frozen=True, slots=True)
class TradingSession:
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
    name: str
    version: str
    category: str
    input_fields: tuple[str, ...]
    lineage: str
    compute: Callable[[Mapping[str, float | None]], float | None]


@dataclass(frozen=True, slots=True)
class FeatureSet:
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
    feature_set_version: str
    values: Mapping[str, float | None]


@dataclass(frozen=True, slots=True)
class FeatureRow:
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
