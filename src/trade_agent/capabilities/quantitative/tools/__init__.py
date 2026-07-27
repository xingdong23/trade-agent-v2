"""量化查询与扫描命令的薄 tool adapters。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolManifest, ToolRequest, ToolResult


class QuantitativeToolApplication(Protocol):
    async def get_prediction(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...

    async def get_quantitative_snapshot(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...

    async def submit_scan(
        self, arguments: Mapping[str, JsonValue], *, idempotency_key: str
    ) -> Mapping[str, JsonValue]: ...

    async def get_scan_status(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...

    async def list_scan_results(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...


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
class GetPredictionTool:
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
        _validate_request(request, self.manifest)
        return ToolResult("available", await self.application.get_prediction(request.arguments))


@dataclass(frozen=True, slots=True)
class GetQuantitativeSnapshotTool:
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
        _validate_request(request, self.manifest)
        return ToolResult(
            "available", await self.application.get_quantitative_snapshot(request.arguments)
        )


@dataclass(frozen=True, slots=True)
class SubmitScanTool:
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
        _validate_request(request, self.manifest)
        if request.idempotency_key is None or not request.idempotency_key.strip():
            raise ValueError("扫描提交必须提供 idempotency key")
        payload = await self.application.submit_scan(
            request.arguments, idempotency_key=request.idempotency_key
        )
        return ToolResult("submitted", payload)


@dataclass(frozen=True, slots=True)
class GetScanStatusTool:
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
        _validate_request(request, self.manifest)
        return ToolResult("available", await self.application.get_scan_status(request.arguments))


@dataclass(frozen=True, slots=True)
class ListScanResultsTool:
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
