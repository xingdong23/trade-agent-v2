"""时间点一致的数据集构建与泄漏门禁。"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trade_agent.capabilities.quantitative.contracts import DataAvailability


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    record_id: str
    security_id: str
    decision_time: datetime
    values: Mapping[str, float | None]
    availability: Mapping[str, DataAvailability]
    adjustment_version: str

    def __post_init__(self) -> None:
        if self.decision_time.tzinfo is None:
            raise ValueError("decision_time 必须包含时区")
        if set(self.values) != set(self.availability):
            raise ValueError("每个输入字段必须有 availability metadata")


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    snapshot_id: str
    market: str
    target_definition: str
    feature_set_version: str
    calendar_version: str
    adjustment_version: str
    records: tuple[DatasetRecord, ...]
    frozen_at: datetime


class QualityViolationCode(StrEnum):
    FUTURE_DATA = "future_data"
    DUPLICATE_SAMPLE = "duplicate_sample"
    INVALID_VALUE = "invalid_value"
    ADJUSTMENT_MISMATCH = "adjustment_mismatch"
    TIME_OVERLAP = "time_overlap"
    SURVIVORSHIP_BIAS_RISK = "survivorship_bias_risk"


@dataclass(frozen=True, slots=True)
class QualityViolation:
    code: QualityViolationCode
    record_id: str
    field: str | None
    message: str


class DatasetQualityError(ValueError):
    def __init__(self, violations: Sequence[QualityViolation]) -> None:
        super().__init__("量化数据质量门禁失败")
        self.violations = tuple(violations)


class PointInTimeDatasetBuilder:
    def build(
        self,
        *,
        snapshot_id: str,
        target_definition: str,
        feature_set_version: str,
        calendar_version: str,
        adjustment_version: str,
        records: Sequence[DatasetRecord],
        frozen_at: datetime,
        membership_as_of_available: bool,
    ) -> DatasetSnapshot:
        violations = validate_dataset(
            records,
            expected_adjustment_version=adjustment_version,
            membership_as_of_available=membership_as_of_available,
        )
        if violations:
            raise DatasetQualityError(violations)
        return DatasetSnapshot(
            snapshot_id=snapshot_id,
            market="US",
            target_definition=target_definition,
            feature_set_version=feature_set_version,
            calendar_version=calendar_version,
            adjustment_version=adjustment_version,
            records=tuple(records),
            frozen_at=frozen_at,
        )


def validate_dataset(
    records: Sequence[DatasetRecord],
    *,
    expected_adjustment_version: str,
    membership_as_of_available: bool,
) -> tuple[QualityViolation, ...]:
    violations: list[QualityViolation] = []
    counts = Counter((record.security_id, record.decision_time) for record in records)
    for record in records:
        if counts[(record.security_id, record.decision_time)] > 1:
            violations.append(
                QualityViolation(
                    QualityViolationCode.DUPLICATE_SAMPLE,
                    record.record_id,
                    None,
                    "相同证券与决策时点出现重复样本",
                )
            )
        if record.adjustment_version != expected_adjustment_version:
            violations.append(
                QualityViolation(
                    QualityViolationCode.ADJUSTMENT_MISMATCH,
                    record.record_id,
                    None,
                    "样本复权版本与 snapshot 不一致",
                )
            )
        for field_name, availability in record.availability.items():
            if availability.available_at > record.decision_time:
                violations.append(
                    QualityViolation(
                        QualityViolationCode.FUTURE_DATA,
                        record.record_id,
                        field_name,
                        "字段在决策时点之后才可获得",
                    )
                )
        for field_name, value in record.values.items():
            if value is not None and (value != value or abs(value) == float("inf")):
                violations.append(
                    QualityViolation(
                        QualityViolationCode.INVALID_VALUE,
                        record.record_id,
                        field_name,
                        "字段包含 NaN 或无穷值",
                    )
                )
    if records and not membership_as_of_available:
        violations.append(
            QualityViolation(
                QualityViolationCode.SURVIVORSHIP_BIAS_RISK,
                records[0].record_id,
                None,
                "缺少决策时点的历史 universe membership",
            )
        )
    return tuple(violations)


def validate_split_order(
    *,
    train_end: datetime,
    validation_start: datetime,
    validation_end: datetime,
    test_start: datetime,
) -> None:
    if train_end >= validation_start or validation_end >= test_start:
        raise DatasetQualityError(
            (
                QualityViolation(
                    QualityViolationCode.TIME_OVERLAP,
                    "dataset-split",
                    None,
                    "train/validation/test 时间区间重叠或顺序错误",
                ),
            )
        )
