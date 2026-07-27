"""量化能力的查询、训练与扫描应用入口。"""

from trade_agent.capabilities.quantitative.contracts import (
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
)

from .dataset import (
    DatasetQualityError,
    DatasetRecord,
    DatasetSnapshot,
    PointInTimeDatasetBuilder,
    QualityViolation,
    QualityViolationCode,
    validate_dataset,
    validate_split_order,
)


class QuantitativeApplication:
    """量化能力的一阶段公共应用边界, 不是业务 Agent。"""

    async def execute(self, command: CapabilityCommand) -> CapabilityResult:
        raise NotImplementedError(f"quantitative command 尚未实现: {command.command_id}")

    async def query(self, query: CapabilityQuery) -> CapabilityResult:
        raise NotImplementedError(f"quantitative query 尚未实现: {query.query_id}")


__all__ = [
    "DatasetQualityError",
    "DatasetRecord",
    "DatasetSnapshot",
    "PointInTimeDatasetBuilder",
    "QualityViolation",
    "QualityViolationCode",
    "QuantitativeApplication",
    "validate_dataset",
    "validate_split_order",
]
