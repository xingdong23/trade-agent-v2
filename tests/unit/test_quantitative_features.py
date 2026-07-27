"""量化市场契约和训练/inference feature parity。"""

from collections.abc import Mapping
from datetime import UTC, date, datetime

import pytest

from trade_agent.capabilities.quantitative.contracts import (
    AdjustmentMetadata,
    AdjustmentPolicy,
    FeatureCalculator,
    FeatureDefinition,
    FeatureQualityError,
    FeatureRegistry,
    FeatureRow,
    FeatureSet,
    MarketScope,
    PredictionTarget,
    TargetDefinition,
    TradingSession,
    USTradingCalendar,
    us_equity_core_feature_set,
)

ADJUSTMENT = AdjustmentMetadata(AdjustmentPolicy.SPLIT_ADJUSTED, "split.v1", True)


def _return_1d(row: Mapping[str, float | None]) -> float | None:
    current = row.get("close")
    previous = row.get("previous_close")
    if current is None or previous is None or previous == 0:
        return None
    return current / previous - 1


def test_market_scope_and_target_reject_non_us_or_invalid_horizon() -> None:
    assert MarketScope("US", ("NASDAQ", "NYSE")).market == "US"
    with pytest.raises(ValueError, match="只支持美股"):
        MarketScope("HK", ("SEHK",))
    with pytest.raises(ValueError, match="预测周期"):
        TargetDefinition(PredictionTarget.RETURN, 0, 0)


def test_training_and_inference_use_identical_versioned_feature_path() -> None:
    registry = FeatureRegistry()
    registry.register(
        FeatureSet(
            "price-volume.v1",
            (
                FeatureDefinition(
                    name="return_1d",
                    version="1",
                    category="return",
                    input_fields=("close", "previous_close"),
                    lineage="adjusted_close",
                    compute=_return_1d,
                ),
            ),
        )
    )
    calculator = FeatureCalculator(registry)
    rows = (
        FeatureRow(date(2026, 7, 24), {"close": 110.0, "previous_close": 100.0}, ADJUSTMENT),
        FeatureRow(date(2026, 7, 25), {"close": None}, ADJUSTMENT),
    )

    training = calculator.calculate("price-volume.v1", rows)
    inference = calculator.calculate("price-volume.v1", rows)

    assert training == inference
    assert training[0].values["return_1d"] == pytest.approx(0.1)
    assert training[1].values["return_1d"] is None


def test_feature_row_requires_explicit_adjustment_lineage() -> None:
    with pytest.raises(TypeError, match="adjustment"):
        FeatureRow(date(2026, 7, 24), {"close": 100.0})  # type: ignore[call-arg]


def test_feature_registry_rejects_version_and_name_collisions() -> None:
    definition = FeatureDefinition("x", "1", "price", ("x",), "raw", lambda row: row["x"])
    registry = FeatureRegistry()
    registry.register(FeatureSet("v1", (definition,)))
    with pytest.raises(ValueError, match="版本已注册"):
        registry.register(FeatureSet("v1", (definition,)))
    with pytest.raises(ValueError, match="重复 feature"):
        FeatureRegistry().register(FeatureSet("v2", (definition, definition)))


def test_us_equity_core_profile_covers_required_feature_categories() -> None:
    categories = {item.category for item in us_equity_core_feature_set().definitions}
    assert categories == {
        "price",
        "volume",
        "return",
        "volatility",
        "trend",
        "liquidity",
        "fundamental",
    }


def test_feature_path_handles_suspension_and_rejects_calendar_or_adjustment_errors() -> None:
    registry = FeatureRegistry()
    registry.register(us_equity_core_feature_set())
    calculator = FeatureCalculator(registry)
    trading_date = date(2026, 7, 24)
    calendar = USTradingCalendar(
        "nyse-2026.v1",
        "America/New_York",
        {
            trading_date: TradingSession(
                trading_date,
                datetime(2026, 7, 24, 13, 30, tzinfo=UTC),
                datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
            )
        },
    )
    suspended = FeatureRow(trading_date, {"close": 120.0}, ADJUSTMENT, suspended=True)
    result = calculator.calculate(
        "us-equity-core.v1",
        (suspended,),
        calendar=calendar,
        adjustment_version="split.v1",
    )
    assert set(result[0].values.values()) == {None}

    with pytest.raises(ValueError, match="不是已配置"):
        calculator.calculate(
            "us-equity-core.v1",
            (FeatureRow(date(2026, 7, 25), {}, ADJUSTMENT),),
            calendar=calendar,
        )
    with pytest.raises(FeatureQualityError, match="公司行动"):
        calculator.calculate(
            "us-equity-core.v1",
            (
                FeatureRow(
                    trading_date,
                    {},
                    AdjustmentMetadata(AdjustmentPolicy.SPLIT_ADJUSTED, "split.v1", False),
                ),
            ),
        )
    with pytest.raises(FeatureQualityError, match="复权版本"):
        calculator.calculate(
            "us-equity-core.v1",
            (
                FeatureRow(
                    trading_date,
                    {},
                    AdjustmentMetadata(AdjustmentPolicy.RAW, "raw.v1", True),
                ),
            ),
            adjustment_version="split.v1",
        )
