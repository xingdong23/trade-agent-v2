"""Watchlist 状态与人工确认步骤的确定性 Card 投影。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any

from trade_agent.capabilities.watchlist.contracts import (
    CapabilityResult,
    ClassificationSuggestion,
    ImportRow,
    ImportStatus,
    Membership,
    Provenance,
    Watchlist,
    WatchlistGroup,
)
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource

_ROW_SEVERITY: dict[ImportStatus, str] = {
    ImportStatus.ACCEPTED: "low",
    ImportStatus.DUPLICATE: "low",
    ImportStatus.AMBIGUOUS: "medium",
    ImportStatus.UNSUPPORTED_MARKET: "high",
    ImportStatus.REJECTED: "high",
}
_ROW_LABEL: dict[ImportStatus, str] = {
    ImportStatus.ACCEPTED: "可导入",
    ImportStatus.DUPLICATE: "重复",
    ImportStatus.AMBIGUOUS: "存在歧义",
    ImportStatus.UNSUPPORTED_MARKET: "不支持的市场",
    ImportStatus.REJECTED: "已拒绝",
}


class WatchlistCardPresenter:
    def import_form(
        self,
        watchlist: Watchlist,
        *,
        draft_symbols: str = "",
        source_type: str = "manual_input",
        source_reference: str = "",
        groups: Sequence[WatchlistGroup] = (),
        revision: int = 1,
    ) -> CardEnvelope:
        group_options = [
            {
                "key": group.group_id,
                "label": group.name,
                "description": f"{len(group.security_ids)} 只证券",
                "disabled": None,
            }
            for group in groups
        ]
        data: dict[str, Any] = {
            "title": f"导入自选列表到 {watchlist.name}",
            "description": "仅支持美股; 提交后仍需人工审批才会写入 membership。",
            "fields": [
                {
                    "key": "symbols_text",
                    "label": "证券代码列表",
                    "value": draft_symbols,
                    "data_type": "string",
                    "control_type": "textarea",
                    "required": True,
                    "read_only": False,
                    "constraints": {"min_length": 1, "max_length": 20000},
                    "options": None,
                    "error": None,
                    "provenance": None,
                    "visible_if": None,
                },
                {
                    "key": "source_type",
                    "label": "来源类型",
                    "value": source_type,
                    "data_type": "string",
                    "control_type": "text",
                    "required": True,
                    "read_only": False,
                    "constraints": {"min_length": 1, "max_length": 100},
                    "options": None,
                    "error": None,
                    "provenance": None,
                    "visible_if": None,
                },
                {
                    "key": "source_reference",
                    "label": "来源说明",
                    "value": source_reference,
                    "data_type": "string",
                    "control_type": "textarea",
                    "required": True,
                    "read_only": False,
                    "constraints": {"min_length": 1, "max_length": 1000},
                    "options": None,
                    "error": None,
                    "provenance": None,
                    "visible_if": None,
                },
                {
                    "key": "target_group_id",
                    "label": "导入后分组",
                    "value": None,
                    "data_type": "string",
                    "control_type": "select",
                    "required": False,
                    "read_only": False,
                    "constraints": None,
                    "options": group_options or None,
                    "error": None,
                    "provenance": None,
                    "visible_if": None,
                },
            ],
            "provenance": [
                {
                    "label": "目标 watchlist",
                    "value": f"{watchlist.name} (v{watchlist.version})",
                    "source_id": watchlist.watchlist_id,
                    "source_type": "watchlist",
                }
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"watchlist-import-form:{watchlist.watchlist_id}",
            "interaction.form",
            1,
            revision,
            CardSource("watchlist", watchlist.watchlist_id, watchlist.version),
            "pending",
            data,
            ("continue", "cancel"),
            text_fallback=f"请填写 {watchlist.name} 的导入内容与来源信息。",
        )

    def import_review(
        self,
        watchlist: Watchlist,
        rows: Sequence[ImportRow],
        *,
        source_type: str,
        source_reference: str,
        target_group: WatchlistGroup | None = None,
        revision: int = 1,
    ) -> CardEnvelope:
        digest = _stable_digest(
            watchlist.watchlist_id,
            source_type,
            source_reference,
            *(f"{row.row_number}:{row.raw_value}:{row.status}:{row.security_id}" for row in rows),
        )
        findings = [
            {
                "label": "目标 watchlist",
                "detail": f"{watchlist.name} (当前 v{watchlist.version})",
                "severity": "low",
            },
            {
                "label": "目标分组",
                "detail": target_group.name if target_group is not None else "不直接分组",
                "severity": "low",
            },
            {
                "label": "导入摘要",
                "detail": _summarize_import_rows(rows),
                "severity": "medium" if _blocked_rows(rows) else "low",
            },
            *[_import_row_finding(row) for row in rows],
        ]
        data: dict[str, Any] = {
            "title": "复核批量导入结果",
            "description": "确认逐行校验结果、来源和目标位置; 取消或拒绝不会写入领域状态。",
            "findings": findings,
            "provenance": [
                {
                    "label": "导入来源",
                    "value": f"{source_type}: {source_reference}",
                    "source_id": watchlist.watchlist_id,
                    "source_type": "watchlist_import",
                }
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"watchlist-import-review:{watchlist.watchlist_id}:{digest}",
            "interaction.review",
            1,
            revision,
            CardSource("watchlist_import", watchlist.watchlist_id, watchlist.version),
            "pending",
            data,
            ("confirm", "edit", "cancel"),
            text_fallback=f"请复核 {watchlist.name} 的批量导入结果。",
        )

    def import_approval(
        self,
        watchlist: Watchlist,
        rows: Sequence[ImportRow],
        *,
        source_type: str,
        source_reference: str,
        target_group: WatchlistGroup | None = None,
        imported_at: datetime | None = None,
        revision: int = 1,
    ) -> CardEnvelope:
        accepted = [row.security_id for row in rows if row.status is ImportStatus.ACCEPTED]
        duplicates = [row.security_id for row in rows if row.status is ImportStatus.DUPLICATE]
        blocked = [
            row.raw_value
            for row in rows
            if row.status not in {ImportStatus.ACCEPTED, ImportStatus.DUPLICATE}
        ]
        imported_label = (
            imported_at.isoformat(timespec="seconds") if imported_at is not None else "待确认写入"
        )
        summary = (
            f"将向 {watchlist.name} 写入 {len(accepted)} 只新证券, "
            f"合并 {len(duplicates)} 只已有证券, 目标版本 v{watchlist.version + 1}。"
        )
        facts: list[dict[str, str]] = [
            {"label": "目标版本", "detail": f"v{watchlist.version + 1}", "severity": "low"},
            {
                "label": "新增证券",
                "detail": _join_or_none(accepted),
                "severity": "low",
            },
            {
                "label": "合并已有",
                "detail": _join_or_none(duplicates),
                "severity": "low",
            },
            {
                "label": "阻塞项",
                "detail": _join_or_none(blocked),
                "severity": "high" if blocked else "low",
            },
            {
                "label": "目标分组",
                "detail": target_group.name if target_group is not None else "不直接分组",
                "severity": "low",
            },
        ]
        data: dict[str, Any] = {
            "title": "批准批量导入",
            "description": (
                "仅在 confirm 后持久化; cancel 或拒绝只会结束交互, 不修改 membership。"
            ),
            "summary": summary,
            "facts": facts,
            "provenance": [
                {
                    "label": "导入来源",
                    "value": f"{source_type}: {source_reference}",
                    "source_id": watchlist.watchlist_id,
                    "source_type": "watchlist_import",
                },
                {
                    "label": "计划导入时间",
                    "value": imported_label,
                    "source_id": watchlist.watchlist_id,
                    "source_type": "watchlist_import",
                },
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"watchlist-import-approval:{watchlist.watchlist_id}:{watchlist.version + 1}",
            "interaction.approval",
            1,
            revision,
            CardSource("watchlist_import", watchlist.watchlist_id, watchlist.version),
            "pending",
            data,
            ("confirm", "edit", "cancel"),
            text_fallback=f"请确认是否向 {watchlist.name} 写入批量导入结果。",
        )

    def suggestion_review(
        self,
        watchlist: Watchlist,
        suggestion: ClassificationSuggestion,
        *,
        membership: Membership,
        proposed_group: WatchlistGroup,
        current_groups: Sequence[WatchlistGroup] = (),
        revision: int = 1,
    ) -> CardEnvelope:
        current_group_names = [
            group.name for group in current_groups if membership.security_id in group.security_ids
        ]
        findings = [
            {
                "label": "目标 watchlist",
                "detail": f"{watchlist.name} (当前 v{watchlist.version})",
                "severity": "low",
            },
            {
                "label": "证券",
                "detail": membership.security_id,
                "severity": "low",
            },
            {
                "label": "当前分组",
                "detail": _join_or_none(current_group_names, empty="未归类"),
                "severity": "medium",
            },
            {
                "label": "建议新增分组",
                "detail": proposed_group.name,
                "severity": "medium",
            },
            {
                "label": "建议来源",
                "detail": suggestion.source_reference,
                "severity": "medium",
            },
        ]
        data: dict[str, Any] = {
            "title": "复核 AI 分类建议",
            "description": "建议在 confirm 前不会修改 group; cancel 只关闭当前建议。",
            "findings": findings,
            "provenance": _membership_provenance(membership),
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"classification-review:{suggestion.suggestion_id}",
            "interaction.review",
            1,
            revision,
            CardSource("classification_suggestion", suggestion.suggestion_id, 1),
            "pending",
            data,
            ("confirm", "edit", "cancel"),
            text_fallback=f"请复核 {membership.security_id} 的分类建议。",
        )

    def suggestion_approval(
        self,
        watchlist: Watchlist,
        suggestion: ClassificationSuggestion,
        *,
        membership: Membership,
        proposed_group: WatchlistGroup,
        current_groups: Sequence[WatchlistGroup] = (),
        revision: int = 1,
    ) -> CardEnvelope:
        current_group_names = [
            group.name for group in current_groups if membership.security_id in group.security_ids
        ]
        summary = (
            f"确认后将把 {membership.security_id} 追加到分组 {proposed_group.name}, "
            f"目标 watchlist 版本为 v{watchlist.version + 1}。"
        )
        facts = [
            {"label": "目标版本", "detail": f"v{watchlist.version + 1}", "severity": "low"},
            {
                "label": "当前分组",
                "detail": _join_or_none(current_group_names, empty="未归类"),
                "severity": "medium",
            },
            {
                "label": "分组差异",
                "detail": f"新增到 {proposed_group.name}",
                "severity": "medium",
            },
            {
                "label": "建议来源",
                "detail": suggestion.source_reference,
                "severity": "medium",
            },
        ]
        data: dict[str, Any] = {
            "title": "批准分类建议",
            "description": "只有 confirm 会写入 group; edit 或 cancel 不改变领域状态。",
            "summary": summary,
            "facts": facts,
            "provenance": _membership_provenance(membership),
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"classification-approval:{suggestion.suggestion_id}",
            "interaction.approval",
            1,
            revision,
            CardSource("classification_suggestion", suggestion.suggestion_id, 1),
            "pending",
            data,
            ("confirm", "edit", "cancel"),
            text_fallback=f"请确认是否接受 {membership.security_id} 的分类建议。",
        )

    def present(self, result: CapabilityResult) -> CardEnvelope:
        card_type = _require_string(result.payload, "card_type")
        watchlist = _watchlist_from_mapping(_require_mapping(result.payload, "watchlist"))
        revision = result.version
        if card_type == "import_form":
            groups = _groups_from_payload(result.payload.get("groups"))
            return self.import_form(
                watchlist,
                draft_symbols=_optional_string(result.payload.get("draft_symbols")),
                source_type=_optional_string(result.payload.get("source_type"), "manual_input"),
                source_reference=_optional_string(result.payload.get("source_reference")),
                groups=groups,
                revision=revision,
            )
        if card_type in {"import_review", "import_approval"}:
            rows = _rows_from_payload(_require_list(result.payload, "rows"))
            target_group = _group_or_none(result.payload.get("target_group"))
            source_type = _optional_string(result.payload.get("source_type"), "manual_input")
            source_reference = _optional_string(result.payload.get("source_reference"))
            if card_type == "import_review":
                return self.import_review(
                    watchlist,
                    rows,
                    source_type=source_type,
                    source_reference=source_reference,
                    target_group=target_group,
                    revision=revision,
                )
            imported_at = _datetime_or_none(result.payload.get("imported_at"))
            return self.import_approval(
                watchlist,
                rows,
                source_type=source_type,
                source_reference=source_reference,
                target_group=target_group,
                imported_at=imported_at,
                revision=revision,
            )
        if card_type in {"classification_review", "classification_approval"}:
            suggestion = _suggestion_from_mapping(_require_mapping(result.payload, "suggestion"))
            membership = _membership_from_mapping(_require_mapping(result.payload, "membership"))
            proposed_group = _group_from_mapping(_require_mapping(result.payload, "proposed_group"))
            current_groups = _groups_from_payload(result.payload.get("current_groups"))
            if card_type == "classification_review":
                return self.suggestion_review(
                    watchlist,
                    suggestion,
                    membership=membership,
                    proposed_group=proposed_group,
                    current_groups=current_groups,
                    revision=revision,
                )
            return self.suggestion_approval(
                watchlist,
                suggestion,
                membership=membership,
                proposed_group=proposed_group,
                current_groups=current_groups,
                revision=revision,
            )
        raise NotImplementedError(f"watchlist card 尚未实现: {result.reference_id}")


def _import_row_finding(row: ImportRow) -> dict[str, str]:
    normalized = row.security_id or "未规范化"
    detail = f"{_ROW_LABEL[row.status]} -> {normalized}"
    if row.message:
        detail = f"{detail}; {row.message}"
    return {
        "label": f"第 {row.row_number} 行 {row.raw_value}",
        "detail": detail,
        "severity": _ROW_SEVERITY[row.status],
    }


def _summarize_import_rows(rows: Sequence[ImportRow]) -> str:
    counts = {status: 0 for status in ImportStatus}
    for row in rows:
        counts[row.status] += 1
    parts = [
        f"accepted {counts[ImportStatus.ACCEPTED]}",
        f"duplicate {counts[ImportStatus.DUPLICATE]}",
        f"ambiguous {counts[ImportStatus.AMBIGUOUS]}",
        f"unsupported {counts[ImportStatus.UNSUPPORTED_MARKET]}",
        f"rejected {counts[ImportStatus.REJECTED]}",
    ]
    return ", ".join(parts)


def _blocked_rows(rows: Sequence[ImportRow]) -> tuple[ImportRow, ...]:
    return tuple(
        row for row in rows if row.status not in {ImportStatus.ACCEPTED, ImportStatus.DUPLICATE}
    )


def _membership_provenance(membership: Membership) -> list[dict[str, str]] | None:
    items = [
        {
            "label": provenance.source_type,
            "value": (
                f"{provenance.source_reference} @ "
                f"{provenance.imported_at.isoformat(timespec='seconds')}"
            ),
            "source_id": provenance.source_reference,
            "source_type": provenance.source_type,
        }
        for provenance in membership.provenance
    ]
    return items or None


def _join_or_none(values: Sequence[str | None], *, empty: str = "无") -> str:
    normalized = [value for value in values if value]
    return "、".join(normalized) if normalized else empty


def _stable_digest(*parts: str) -> str:
    payload = "|".join(parts)
    return sha256(payload.encode("utf-8")).hexdigest()[:12]


def _require_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    return value


def _optional_string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("期望字符串")
    return value


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} 必须是对象")
    return value


def _require_list(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} 必须是数组")
    return value


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("imported_at 必须是 ISO 时间字符串")
    return datetime.fromisoformat(value)


def _watchlist_from_mapping(payload: Mapping[str, Any]) -> Watchlist:
    return Watchlist(
        watchlist_id=_require_string(payload, "watchlist_id"),
        owner_id=_require_string(payload, "owner_id"),
        name=_require_string(payload, "name"),
        version=_require_int(payload, "version"),
    )


def _row_from_mapping(payload: Mapping[str, Any]) -> ImportRow:
    status = ImportStatus(_require_string(payload, "status"))
    return ImportRow(
        row_number=_require_int(payload, "row_number"),
        raw_value=_require_string(payload, "raw_value"),
        status=status,
        security_id=_nullable_string(payload.get("security_id")),
        message=_optional_string(payload.get("message")),
        metadata=_mapping_or_empty(payload.get("metadata")),
    )


def _rows_from_payload(raw_rows: Sequence[Any]) -> tuple[ImportRow, ...]:
    rows: list[ImportRow] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise ValueError("rows 元素必须是对象")
        rows.append(_row_from_mapping(raw_row))
    return tuple(rows)


def _group_from_mapping(payload: Mapping[str, Any]) -> WatchlistGroup:
    security_ids = payload.get("security_ids")
    if security_ids is None:
        parsed_security_ids: frozenset[str] = frozenset()
    elif isinstance(security_ids, list):
        parsed_security_ids = frozenset(_list_of_strings(security_ids))
    else:
        raise ValueError("security_ids 必须是数组")
    return WatchlistGroup(
        group_id=_require_string(payload, "group_id"),
        name=_require_string(payload, "name"),
        security_ids=parsed_security_ids,
    )


def _group_or_none(value: Any) -> WatchlistGroup | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("group 必须是对象")
    return _group_from_mapping(value)


def _groups_from_payload(value: Any) -> tuple[WatchlistGroup, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("groups 必须是数组")
    groups: list[WatchlistGroup] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("groups 元素必须是对象")
        groups.append(_group_from_mapping(item))
    return tuple(groups)


def _suggestion_from_mapping(payload: Mapping[str, Any]) -> ClassificationSuggestion:
    return ClassificationSuggestion(
        suggestion_id=_require_string(payload, "suggestion_id"),
        security_id=_require_string(payload, "security_id"),
        proposed_group_id=_require_string(payload, "proposed_group_id"),
        source_reference=_require_string(payload, "source_reference"),
        accepted=_require_bool(payload, "accepted"),
    )


def _membership_from_mapping(payload: Mapping[str, Any]) -> Membership:
    return Membership(
        security_id=_require_string(payload, "security_id"),
        tags=frozenset(_list_of_strings(_require_list(payload, "tags"))),
        notes=tuple(_list_of_strings(_require_list(payload, "notes"))),
        provenance=tuple(_provenance_from_payload(_require_list(payload, "provenance"))),
    )


def _provenance_from_payload(raw_items: Sequence[Any]) -> list[Provenance]:
    items: list[Provenance] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise ValueError("provenance 元素必须是对象")
        imported_at = _require_string(raw_item, "imported_at")
        items.append(
            Provenance(
                source_type=_require_string(raw_item, "source_type"),
                source_reference=_require_string(raw_item, "source_reference"),
                imported_at=datetime.fromisoformat(imported_at),
            )
        )
    return items


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("metadata 必须是对象")
    return value


def _nullable_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("期望字符串或 null")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    return value


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是布尔值")
    return value


def _list_of_strings(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("数组元素必须是字符串")
        result.append(value)
    return result


__all__ = ["WatchlistCardPresenter"]
