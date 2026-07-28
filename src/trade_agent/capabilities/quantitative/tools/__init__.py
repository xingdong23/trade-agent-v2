"""量化查询与扫描命令的薄 tool adapters。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolManifest, ToolProtocol, ToolRequest, ToolResult
from trade_agent.core.tools.identity import bind_trusted_identity, identity_fields_for_manifest


class QuantitativeToolApplication(Protocol):
    """供量化工具层调用的应用协议。

    Contract:
        - 每个方法都必须返回可直接序列化的结构化结果, 不在工具层补算业务字段。
        - 写操作必须通过幂等键保证可恢复重试语义。
        - 调用方已完成参数 schema 校验, 实现方负责 owner 隔离与业务约束。

    Implemented by:
        生产量化应用服务与测试 fake application。
    """

    async def get_prediction(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        """读取单只证券的已持久化量化预测。

        Args:
            arguments: 通过工具 schema 校验后的查询参数。

        Returns:
            可直接序列化的预测结果载荷。

        Raises:
            LookupError: 对应证券或预测结果不存在时抛出。
        """

        ...

    async def get_quantitative_snapshot(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        """读取单只证券的量化快照、lineage 与缺口信息。

        Args:
            arguments: 通过工具 schema 校验后的查询参数。

        Returns:
            可直接序列化的量化快照载荷。

        Raises:
            LookupError: 对应证券或快照不存在时抛出。
        """

        ...

    async def submit_scan(
        self, arguments: Mapping[str, JsonValue], *, idempotency_key: str
    ) -> Mapping[str, JsonValue]:
        """提交一次冻结输入的量化扫描任务。

        Args:
            arguments: 通过工具 schema 校验后的扫描提交参数。
            idempotency_key: 幂等键, 用于重试时复用同一提交结果。

        Returns:
            表示已提交任务的结构化结果载荷。

        Raises:
            ValueError: 参数虽符合 schema 但未满足更细业务约束时抛出。
        """

        ...

    async def get_scan_status(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        """读取扫描任务的当前状态与进度。

        Args:
            arguments: 通过工具 schema 校验后的状态查询参数。

        Returns:
            可直接序列化的状态与进度载荷。

        Raises:
            LookupError: 对应扫描任务不存在时抛出。
        """

        ...

    async def list_scan_results(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        """列出指定扫描任务的已持久化结果。

        Args:
            arguments: 通过工具 schema 校验后的结果查询参数。

        Returns:
            可直接序列化的扫描结果列表载荷。

        Raises:
            LookupError: 对应扫描任务不存在或尚无可见结果时抛出。
        """

        ...


_SECURITY_QUERY_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["owner_id", "security_id", "target", "horizon", "as_of"],
    "properties": {
        "owner_id": {"type": "string", "minLength": 1},
        "security_id": {"type": "string", "minLength": 1},
        "target": {"type": "string", "minLength": 1},
        "horizon": {"type": "string", "minLength": 1},
        "as_of": {"type": "string", "format": "date-time"},
    },
}
_SCAN_ID_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["owner_id", "scan_id"],
    "properties": {
        "owner_id": {"type": "string", "minLength": 1},
        "scan_id": {"type": "string", "minLength": 1},
    },
}
_OBJECT_OUTPUT: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": True,
}


@dataclass(frozen=True, slots=True)
class GetPredictionTool(ToolProtocol):
    """读取单只证券已持久化量化预测的工具适配器。

    Attributes:
        application: 实际执行业务查询的量化应用协议实现。
    """

    application: QuantitativeToolApplication

    manifest = ToolManifest(
        "quantitative.get_prediction",
        "读取单只美股的已持久化专用模型预测",
        True,
        False,
        _SECURITY_QUERY_SCHEMA,
        _OBJECT_OUTPUT,
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _validate_request(request, self.manifest)
        return ToolResult("available", await self.application.get_prediction(request.arguments))


@dataclass(frozen=True, slots=True)
class GetQuantitativeSnapshotTool(ToolProtocol):
    """读取单只证券量化快照与 lineage 的工具适配器。

    Attributes:
        application: 实际执行业务查询的量化应用协议实现。
    """

    application: QuantitativeToolApplication

    manifest = ToolManifest(
        "quantitative.get_quantitative_snapshot",
        "读取单只美股的量化快照、lineage 与数据缺口",
        True,
        False,
        _SECURITY_QUERY_SCHEMA,
        _OBJECT_OUTPUT,
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _validate_request(request, self.manifest)
        return ToolResult(
            "available", await self.application.get_quantitative_snapshot(request.arguments)
        )


@dataclass(frozen=True, slots=True)
class SubmitScanTool(ToolProtocol):
    """提交冻结输入扫描任务的工具适配器。

    Attributes:
        application: 实际执行扫描提交的量化应用协议实现。
    """

    application: QuantitativeToolApplication

    manifest = ToolManifest(
        "quantitative.submit_scan",
        "提交冻结输入的可恢复量化扫描任务",
        False,
        True,
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "owner_id",
                "strategy_id",
                "strategy_version",
                "universe_snapshot_id",
                "data_snapshot_id",
                "feature_snapshot_id",
                "model_version_id",
                "ranking_function_version",
                "configuration",
            ],
            "properties": {
                "owner_id": {"type": "string", "minLength": 1},
                "strategy_id": {"type": "string", "minLength": 1},
                "strategy_version": {"type": "integer", "minimum": 1},
                "universe_snapshot_id": {"type": "string", "minLength": 1},
                "data_snapshot_id": {"type": "string", "minLength": 1},
                "feature_snapshot_id": {"type": "string", "minLength": 1},
                "model_version_id": {"type": "string", "minLength": 1},
                "ranking_function_version": {"type": "string", "minLength": 1},
                "configuration": {"type": "object"},
            },
        },
        _OBJECT_OUTPUT,
        side_effect="create_scan_job",
        risk="controlled_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _validate_request(request, self.manifest)
        if request.idempotency_key is None or not request.idempotency_key.strip():
            raise ValueError("扫描提交必须提供 idempotency key")
        payload = await self.application.submit_scan(
            request.arguments, idempotency_key=request.idempotency_key
        )
        return ToolResult("submitted", payload)


@dataclass(frozen=True, slots=True)
class GetScanStatusTool(ToolProtocol):
    """读取扫描任务状态与进度的工具适配器。

    Attributes:
        application: 实际执行状态查询的量化应用协议实现。
    """

    application: QuantitativeToolApplication

    manifest = ToolManifest(
        "quantitative.get_scan_status",
        "读取量化扫描任务状态与进度",
        True,
        False,
        _SCAN_ID_SCHEMA,
        _OBJECT_OUTPUT,
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _validate_request(request, self.manifest)
        return ToolResult("available", await self.application.get_scan_status(request.arguments))


@dataclass(frozen=True, slots=True)
class ListScanResultsTool(ToolProtocol):
    """读取已持久化扫描结果的工具适配器。

    Attributes:
        application: 实际执行结果查询的量化应用协议实现。
    """

    application: QuantitativeToolApplication

    manifest = ToolManifest(
        "quantitative.list_scan_results",
        "读取已持久化量化扫描结果",
        True,
        False,
        _SCAN_ID_SCHEMA,
        _OBJECT_OUTPUT,
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _validate_request(request, self.manifest)
        return ToolResult("available", await self.application.list_scan_results(request.arguments))


def _validate_request(request: ToolRequest, manifest: ToolManifest) -> None:
    if request.tool_id != manifest.tool_id:
        raise ValueError("tool id 与 handler 不匹配")
    required = manifest.input_schema.get("required")
    if isinstance(required, list):
        missing = [key for key in required if isinstance(key, str) and key not in request.arguments]
        if missing:
            raise ValueError(f"tool 参数缺少字段: {', '.join(missing)}")


__all__ = [
    "GetPredictionTool",
    "GetQuantitativeSnapshotTool",
    "GetScanStatusTool",
    "ListScanResultsTool",
    "QuantitativeToolApplication",
    "SubmitScanTool",
]
