"""时间点一致数据集和质量门禁测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from trade_agent.capabilities.quantitative.application import (
    DatasetQualityError,
    DatasetRecord,
    PointInTimeDatasetBuilder,
    QualityViolationCode,
    validate_split_order,
)
from trade_agent.capabilities.quantitative.contracts import DataAvailability


def _record(
    *,
    record_id: str = "row-1",
    available_offset: timedelta = timedelta(0),
    adjustment_version: str = "split.v1",
) -> DatasetRecord:
    decision_time = datetime(2026, 7, 24, 20, tzinfo=UTC)
    return DatasetRecord(
        record_id=record_id,
        security_id="US:NASDAQ:NVDA",
        decision_time=decision_time,
        values={"close": 120.0},
        availability={
            "close": DataAvailability(
                event_time=decision_time - timedelta(minutes=1),
                available_at=decision_time + available_offset,
            )
        },
        adjustment_version=adjustment_version,
    )


def test_builder_freezes_reproducible_point_in_time_snapshot() -> None:
    frozen_at = datetime.now(UTC)
    snapshot = PointInTimeDatasetBuilder().build(
        snapshot_id="snapshot-1",
        target_definition="return.5d.v1",
        feature_set_version="price-volume.v1",
        calendar_version="nyse-2026.v1",
        adjustment_version="split.v1",
        records=(_record(),),
        frozen_at=frozen_at,
        membership_as_of_available=True,
    )

    assert snapshot.market == "US"
    assert snapshot.records[0].record_id == "row-1"
    assert snapshot.frozen_at == frozen_at


@pytest.mark.parametrize(
    ("records", "membership", "code"),
    [
        ((_record(available_offset=timedelta(seconds=1)),), True, QualityViolationCode.FUTURE_DATA),
        (
            (_record(record_id="a"), _record(record_id="b")),
            True,
            QualityViolationCode.DUPLICATE_SAMPLE,
        ),
        (
            (_record(adjustment_version="wrong"),),
            True,
            QualityViolationCode.ADJUSTMENT_MISMATCH,
        ),
        ((_record(),), False, QualityViolationCode.SURVIVORSHIP_BIAS_RISK),
    ],
)
def test_builder_blocks_quality_and_leakage_violations(
    records: tuple[DatasetRecord, ...], membership: bool, code: QualityViolationCode
) -> None:
    with pytest.raises(DatasetQualityError) as error:
        PointInTimeDatasetBuilder().build(
            snapshot_id="snapshot-1",
            target_definition="return.5d.v1",
            feature_set_version="price-volume.v1",
            calendar_version="nyse-2026.v1",
            adjustment_version="split.v1",
            records=records,
            frozen_at=datetime.now(UTC),
            membership_as_of_available=membership,
        )

    assert code in {item.code for item in error.value.violations}


def test_split_validation_blocks_train_test_overlap() -> None:
    now = datetime.now(UTC)
    with pytest.raises(DatasetQualityError) as error:
        validate_split_order(
            train_end=now,
            validation_start=now,
            validation_end=now + timedelta(days=1),
            test_start=now + timedelta(days=1),
        )
    assert error.value.violations[0].code is QualityViolationCode.TIME_OVERLAP
