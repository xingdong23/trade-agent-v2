"""仅调用 Watchlist application service 的请求适配器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trade_agent.capabilities.watchlist.contracts import (
    ClassificationSuggestion,
    ImportRow,
    ImportStatus,
    UniverseSnapshot,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolManifest, ToolRequest, ToolResult
from trade_agent.core.tools.identity import bind_trusted_identity, identity_fields_for_manifest


class WatchlistToolApplication(Protocol):
    def classify_import(
        self, rows: Sequence[tuple[str, str | None, ImportStatus]]
    ) -> tuple[ImportRow, ...]: ...

    def approve_import(
        self,
        rows: Sequence[ImportRow],
        *,
        actor_owner_id: str,
        approved: bool,
        idempotency_key: str,
        source_type: str,
        source_reference: str,
        imported_at: datetime,
        tags: Mapping[str, frozenset[str]] | None = None,
        notes: Mapping[str, tuple[str, ...]] | None = None,
    ) -> tuple[ImportRow, ...]: ...

    def accept_suggestion(
        self,
        suggestion: ClassificationSuggestion,
        *,
        actor_owner_id: str,
        idempotency_key: str,
        group_id: str | None = None,
    ) -> ClassificationSuggestion: ...

    def freeze_universe(
        self, *, actor_owner_id: str, created_at: datetime, group_id: str | None = None
    ) -> UniverseSnapshot: ...


_IMPORT_ROW_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["raw_value", "status"],
    "properties": {
        "row_number": {"type": "integer", "minimum": 1},
        "raw_value": {"type": "string", "minLength": 1},
        "security_id": {"type": ["string", "null"]},
        "message": {"type": "string"},
        "metadata": {"type": "object"},
        "status": {
            "type": "string",
            "enum": [status.value for status in ImportStatus],
        },
    },
}


@dataclass(frozen=True, slots=True)
class ValidateWatchlistImportTool:
    application: WatchlistToolApplication

    manifest = ToolManifest(
        tool_id="watchlist.validate_import",
        description="逐行校验并规范化美股自选列表导入内容",
        read_only=True,
        requires_hitl=False,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["rows"],
            "properties": {"rows": {"type": "array", "minItems": 1, "items": _IMPORT_ROW_SCHEMA}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["rows"],
            "properties": {"rows": {"type": "array", "items": _IMPORT_ROW_SCHEMA}},
        },
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _require_tool(request, self.manifest)
        input_rows = _require_list(request, "rows")
        rows = tuple(_parse_import_candidate(item) for item in input_rows)
        result = self.application.classify_import(rows)
        return ToolResult(
            "validated",
            {
                "rows": [
                    _import_row_payload(row, source=input_rows[index])
                    for index, row in enumerate(result)
                ]
            },
        )


@dataclass(frozen=True, slots=True)
class ApproveWatchlistImportTool:
    application: WatchlistToolApplication

    manifest = ToolManifest(
        tool_id="watchlist.approve_import",
        description="按人工审批结果幂等写入自选列表 membership",
        read_only=False,
        requires_hitl=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "rows",
                "actor_owner_id",
                "approved",
                "source_type",
                "source_reference",
                "imported_at",
            ],
            "properties": {
                "rows": {"type": "array", "minItems": 1, "items": _IMPORT_ROW_SCHEMA},
                "actor_owner_id": {"type": "string", "minLength": 1},
                "approved": {"const": True},
                "source_type": {"type": "string", "minLength": 1},
                "source_reference": {"type": "string", "minLength": 1},
                "imported_at": {"type": "string", "format": "date-time"},
                "tags": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                    },
                },
                "notes": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["rows"],
            "properties": {"rows": {"type": "array", "items": _IMPORT_ROW_SCHEMA}},
        },
        side_effect="upsert_watchlist_memberships",
        risk="controlled_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _require_tool(request, self.manifest)
        if request.idempotency_key is None or not request.idempotency_key.strip():
            raise ValueError("批量导入审批必须提供 idempotency key")
        rows = tuple(
            _parse_import_row(item, index)
            for index, item in enumerate(_require_list(request, "rows"), start=1)
        )
        result = self.application.approve_import(
            rows,
            actor_owner_id=_require_string(request, "actor_owner_id"),
            approved=_require_boolean(request, "approved"),
            idempotency_key=request.idempotency_key,
            source_type=_require_string(request, "source_type"),
            source_reference=_require_string(request, "source_reference"),
            imported_at=_require_datetime(request, "imported_at"),
            tags=_parse_string_sets(request, "tags"),
            notes=_parse_string_tuples(request, "notes"),
        )
        return ToolResult("imported", {"rows": [_import_row_payload(row) for row in result]})


@dataclass(frozen=True, slots=True)
class AcceptClassificationSuggestionTool:
    application: WatchlistToolApplication

    manifest = ToolManifest(
        tool_id="watchlist.accept_classification",
        description="按人工确认接受一条自选列表分类建议",
        read_only=False,
        requires_hitl=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "suggestion_id",
                "security_id",
                "proposed_group_id",
                "source_reference",
                "actor_owner_id",
            ],
            "properties": {
                "suggestion_id": {"type": "string", "minLength": 1},
                "security_id": {"type": "string", "minLength": 1},
                "proposed_group_id": {"type": "string", "minLength": 1},
                "source_reference": {"type": "string", "minLength": 1},
                "actor_owner_id": {"type": "string", "minLength": 1},
                "accepted_group_id": {"type": "string", "minLength": 1},
                "group_id": {"type": "string", "minLength": 1},
            },
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["suggestion_id", "security_id", "group_id", "accepted"],
            "properties": {
                "suggestion_id": {"type": "string"},
                "security_id": {"type": "string"},
                "group_id": {"type": "string"},
                "accepted": {"type": "boolean"},
            },
        },
        side_effect="assign_watchlist_group",
        risk="controlled_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _require_tool(request, self.manifest)
        idempotency_key = _require_idempotency_key(request, "分类建议确认")
        suggestion = ClassificationSuggestion(
            _require_string(request, "suggestion_id"),
            _require_string(request, "security_id"),
            _require_string(request, "proposed_group_id"),
            _require_string(request, "source_reference"),
        )
        accepted_group_id = _optional_request_string(request, "accepted_group_id")
        group_id_alias = _optional_request_string(request, "group_id")
        if (
            accepted_group_id is not None
            and group_id_alias is not None
            and accepted_group_id != group_id_alias
        ):
            raise ValueError("accepted_group_id 与 group_id 不能指向不同分组")
        result = self.application.accept_suggestion(
            suggestion,
            actor_owner_id=_require_string(request, "actor_owner_id"),
            idempotency_key=idempotency_key,
            group_id=accepted_group_id or group_id_alias,
        )
        if result.accepted_group_id is None:
            raise ValueError("application 未返回实际接受的 group_id")
        return ToolResult(
            "accepted",
            {
                "suggestion_id": result.suggestion_id,
                "security_id": result.security_id,
                "group_id": result.accepted_group_id,
                "accepted": result.accepted,
            },
        )


@dataclass(frozen=True, slots=True)
class FreezeUniverseTool:
    application: WatchlistToolApplication

    manifest = ToolManifest(
        tool_id="watchlist.freeze_universe",
        description="冻结供量化扫描使用的不可变自选证券集合",
        read_only=False,
        requires_hitl=False,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["actor_owner_id", "created_at"],
            "properties": {
                "actor_owner_id": {"type": "string", "minLength": 1},
                "created_at": {"type": "string", "format": "date-time"},
                "group_id": {"type": ["string", "null"]},
            },
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["snapshot_id", "security_ids"],
            "properties": {
                "snapshot_id": {"type": "string"},
                "security_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
        side_effect="create_universe_snapshot",
        risk="low_write",
        idempotent=False,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _require_tool(request, self.manifest)
        group_id = request.arguments.get("group_id")
        if group_id is not None and not isinstance(group_id, str):
            raise ValueError("group_id 必须是字符串或 null")
        result = self.application.freeze_universe(
            actor_owner_id=_require_string(request, "actor_owner_id"),
            created_at=_require_datetime(request, "created_at"),
            group_id=group_id,
        )
        return ToolResult(
            "frozen",
            {"snapshot_id": result.snapshot_id, "security_ids": list(result.security_ids)},
        )


def _require_tool(request: ToolRequest, manifest: ToolManifest) -> None:
    if request.tool_id != manifest.tool_id:
        raise ValueError("tool id 与 handler 不匹配")


def _require_idempotency_key(request: ToolRequest, operation: str) -> str:
    if request.idempotency_key is None or not request.idempotency_key.strip():
        raise ValueError(f"{operation}必须提供 idempotency key")
    return request.idempotency_key


def _require_list(request: ToolRequest, field: str) -> list[JsonValue]:
    value = request.arguments.get(field)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} 必须是非空数组")
    return value


def _require_string(request: ToolRequest, field: str) -> str:
    value = request.arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def _optional_request_string(request: ToolRequest, field: str) -> str | None:
    value = request.arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def _require_boolean(request: ToolRequest, field: str) -> bool:
    value = request.arguments.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是 boolean")
    return value


def _require_datetime(request: ToolRequest, field: str) -> datetime:
    try:
        value = datetime.fromisoformat(_require_string(request, field).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} 必须是 ISO 8601 date-time") from error
    if value.tzinfo is None:
        raise ValueError(f"{field} 必须包含时区")
    return value


def _parse_import_candidate(value: JsonValue) -> tuple[str, str | None, ImportStatus]:
    item = _require_object(value, "rows item")
    return (
        _object_string(item, "raw_value"),
        _object_optional_string(item, "security_id"),
        _object_status(item),
    )


def _parse_import_row(value: JsonValue, row_number: int) -> ImportRow:
    raw_value, security_id, status = _parse_import_candidate(value)
    item = _require_object(value, "rows item")
    explicit_row_number = item.get("row_number", row_number)
    if not isinstance(explicit_row_number, int) or isinstance(explicit_row_number, bool):
        raise ValueError("row_number 必须是正整数")
    if explicit_row_number < 1:
        raise ValueError("row_number 必须是正整数")
    message = item.get("message", "")
    if not isinstance(message, str):
        raise ValueError("message 必须是字符串")
    return ImportRow(
        explicit_row_number,
        raw_value,
        status,
        security_id,
        message,
        _object_metadata(item),
    )


def _require_object(value: JsonValue, field: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 object")
    return value


def _object_string(item: Mapping[str, JsonValue], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def _object_optional_string(item: Mapping[str, JsonValue], field: str) -> str | None:
    value = item.get(field)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串或 null")
    return value


def _object_status(item: Mapping[str, JsonValue]) -> ImportStatus:
    try:
        return ImportStatus(_object_string(item, "status"))
    except ValueError as error:
        raise ValueError("status 不是受支持的导入状态") from error


def _object_metadata(item: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    value = item.get("metadata", {})
    if not isinstance(value, dict):
        raise ValueError("metadata 必须是 object")
    return value


def _parse_string_sets(request: ToolRequest, field: str) -> Mapping[str, frozenset[str]] | None:
    parsed = _parse_string_arrays(request, field)
    if parsed is None:
        return None
    return {key: frozenset(values) for key, values in parsed.items()}


def _parse_string_tuples(request: ToolRequest, field: str) -> Mapping[str, tuple[str, ...]] | None:
    parsed = _parse_string_arrays(request, field)
    if parsed is None:
        return None
    return {key: tuple(values) for key, values in parsed.items()}


def _parse_string_arrays(request: ToolRequest, field: str) -> dict[str, list[str]] | None:
    value = request.arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 security_id 到 string[] 的 object")
    parsed: dict[str, list[str]] = {}
    for security_id, items in value.items():
        if not security_id.strip() or not isinstance(items, list):
            raise ValueError(f"{field} 必须是 security_id 到 string[] 的 object")
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"{field} 数组只能包含非空字符串")
        parsed[security_id] = [item for item in items if isinstance(item, str)]
    return parsed


def _import_row_payload(row: ImportRow, *, source: JsonValue | None = None) -> dict[str, JsonValue]:
    metadata = row.metadata
    if source is not None:
        metadata = _object_metadata(_require_object(source, "rows item"))
    return {
        "row_number": row.row_number,
        "raw_value": row.raw_value,
        "security_id": row.security_id,
        "status": row.status.value,
        "message": row.message,
        "metadata": dict(metadata),
    }
