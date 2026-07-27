"""时间点一致的数据集构建与泄漏门禁。"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trade_agent.capabilities.quantitative.contracts import DataAvailability


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    """表示一个决策时点可用于训练的数据样本。

    Attributes:
        record_id: 样本稳定标识。
        security_id: 证券稳定标识。
        decision_time: 该样本可被策略或模型使用的决策时间。
        values: 输入字段到数值的映射。
        availability: 每个输入字段对应的数据可用时间元数据。
        adjustment_version: 该样本采用的复权版本。

    Invariants:
        - `decision_time` 必须带时区。
        - `values` 与 `availability` 必须逐字段一一对应。
    """

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
    """冻结一次时间点一致数据集构建结果。

    Attributes:
        snapshot_id: 数据集快照标识。
        market: 数据集适用市场, 首版固定为美股。
        target_definition: 目标定义版本或标识。
        feature_set_version: 该数据集对应的特征集版本。
        calendar_version: 交易日日历版本。
        adjustment_version: 复权版本。
        records: 被纳入快照的全部样本记录。
        frozen_at: 快照冻结时间。
    """

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
    """描述一条阻止数据集通过门禁的质量违规。

    Attributes:
        code: 违规类型代码。
        record_id: 触发违规的样本标识。
        field: 相关字段名称; 若为样本级问题则可为空。
        message: 面向调用方的违规解释。
    """

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
