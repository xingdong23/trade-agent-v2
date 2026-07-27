"""HITL 与业务 source 到安全 CardEnvelope 的确定性投影。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256

from trade_agent.core.hitl import HumanInteraction, InteractionType
from trade_agent.core.llm.contracts import JsonValue

from .contracts import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource

_INTERACTION_KIND = {
    InteractionType.CLARIFICATION: "interaction.choice",
    InteractionType.APPROVAL: "interaction.approval",
    InteractionType.REVIEW: "interaction.review",
    InteractionType.CORRECTION: "interaction.correction",
    InteractionType.EXCEPTION_RESOLUTION: "interaction.form",
}
_INTERACTION_ACTIONS = {
    "interaction.form": ("continue", "cancel"),
    "interaction.choice": ("continue", "cancel"),
    "interaction.approval": ("confirm", "edit", "cancel"),
    "interaction.review": ("confirm", "edit", "cancel"),
    "interaction.correction": ("confirm", "edit", "cancel"),
}


def stable_card_id(source_type: str, source_id: str) -> str:
    """同一 source 在所有客户端和进程中得到相同 card id。"""

    digest = sha256(f"{source_type}:{source_id}".encode()).hexdigest()[:24]
    return f"card-{digest}"


@dataclass(slots=True)
class CardProjectionService:
    """保存每张 card 的最新投影, 拒绝 source 回退并生成单调 revision。"""

    _latest: dict[str, CardEnvelope] = field(default_factory=dict)

    def project(
        self,
        *,
        kind: str,
        source: CardSource,
        state: str,
        data: Mapping[str, JsonValue],
        actions: Sequence[str] = (),
        expires_at: str | None = None,
        text_fallback: str,
    ) -> CardEnvelope:
        card_id = stable_card_id(source.source_type, source.source_id)
        previous = self._latest.get(card_id)
        if previous is not None and source.version < previous.source.version:
            raise ValueError("不能投影旧于当前 source version 的卡片")
        revision = 1 if previous is None else previous.revision + 1
        envelope = CardEnvelope(
            protocol_version=CARD_PROTOCOL_VERSION,
            card_id=card_id,
            kind=kind,
            schema_version=1,
            revision=revision,
            source=source,
            state=state,
            data=data,
            actions=tuple(actions) if state == "pending" else (),
            expires_at=expires_at,
            text_fallback=text_fallback,
        )
        self._latest[card_id] = envelope
        return envelope

    def latest(self, card_id: str) -> CardEnvelope | None:
        return self._latest.get(card_id)

    def supersede(self, card_id: str, *, source_version: int) -> CardEnvelope:
        previous = self._latest.get(card_id)
        if previous is None:
            raise KeyError("找不到待 supersede 的卡片")
        return self.project(
            kind=previous.kind,
            source=CardSource(
                previous.source.source_type,
                previous.source.source_id,
                source_version,
            ),
            state="superseded",
            data=previous.data,
            expires_at=previous.expires_at,
            text_fallback=previous.text_fallback,
        )

    @staticmethod
    def unsupported_fallback(
        *, source: CardSource, unsupported_kind: str, unsupported_schema_version: int
    ) -> CardEnvelope:
        message = "当前客户端不支持此卡片版本, 请刷新或升级客户端。"
        return CardEnvelope(
            protocol_version=CARD_PROTOCOL_VERSION,
            card_id=stable_card_id("unsupported", source.source_id),
            kind="notice.unsupported",
            schema_version=1,
            revision=source.version,
            source=source,
            state="resolved",
            data={
                "title": "无法显示交互卡片",
                "message": message,
                "unsupported_kind": unsupported_kind,
                "unsupported_schema_version": unsupported_schema_version,
            },
            actions=(),
            text_fallback=message,
        )


@dataclass(slots=True)
class HitlCardPresenter:
    projection: CardProjectionService = field(default_factory=CardProjectionService)

    def present(
        self,
        interaction: HumanInteraction,
        *,
        allowed_actions: Sequence[str] | None = None,
        field_errors: Mapping[str, str] | None = None,
    ) -> CardEnvelope:
        kind = _INTERACTION_KIND[interaction.interaction_type]
        actions = self._allowed_actions(kind, allowed_actions)
        data = self._data(kind, interaction, field_errors or {})
        return self.projection.project(
            kind=kind,
            source=CardSource("interaction", interaction.interaction_id, interaction.version),
            state=interaction.status.value,
            data=data,
            actions=actions,
            expires_at=(
                interaction.deadline.isoformat() if interaction.deadline is not None else None
            ),
            text_fallback=_string(interaction.payload.get("text_fallback"), "等待用户处理"),
        )

    @staticmethod
    def _allowed_actions(kind: str, policy_actions: Sequence[str] | None) -> tuple[str, ...]:
        schema_actions = _INTERACTION_ACTIONS[kind]
        if policy_actions is None:
            return schema_actions
        requested = set(policy_actions)
        return tuple(action for action in schema_actions if action in requested)

    def _data(
        self,
        kind: str,
        interaction: HumanInteraction,
        errors: Mapping[str, str],
    ) -> Mapping[str, JsonValue]:
        payload = interaction.payload
        title = _string(payload.get("title"), "需要你的处理")
        description = _optional_string(payload.get("description"))
        provenance = _json_list(payload.get("provenance"))
        if kind == "interaction.form":
            return {
                "title": title,
                "description": description,
                "fields": self._fields(interaction.response_schema, errors),
                "provenance": provenance,
            }
        if kind == "interaction.choice":
            return {
                "title": title,
                "description": description,
                "options": _options(interaction.response_schema),
                "provenance": provenance,
            }
        if kind == "interaction.approval":
            return {
                "title": title,
                "description": description,
                "summary": _string(payload.get("summary"), "请确认此操作"),
                "facts": _json_list(payload.get("facts")),
                "provenance": provenance,
            }
        if kind == "interaction.review":
            return {
                "title": title,
                "description": description,
                "findings": _json_list(payload.get("findings")) or [],
                "provenance": provenance,
            }
        return {
            "title": title,
            "description": description,
            "current_value": _string(payload.get("current_value"), ""),
            "suggested_value": _string(payload.get("suggested_value"), ""),
            "reason": _optional_string(payload.get("reason")),
            "provenance": provenance,
        }

    @staticmethod
    def _fields(schema: Mapping[str, JsonValue], errors: Mapping[str, str]) -> list[JsonValue]:
        properties = _json_mapping(schema.get("properties"))
        required_keys = _string_list(schema.get("required"))
        required = set(required_keys)
        field_keys = [key for key in required_keys if key in properties]
        field_keys.extend(sorted(key for key in properties if key not in required))
        fields: list[JsonValue] = []
        for key in field_keys:
            raw_spec = properties[key]
            spec = _json_mapping(raw_spec)
            choices = _enum_options(spec.get("enum"))
            fields.append(
                {
                    "key": key,
                    "label": _string(spec.get("title"), key),
                    "data_type": _data_type(spec.get("type")),
                    "control_type": _control_type(spec, choices),
                    "required": key in required,
                    "read_only": bool(spec.get("readOnly", False)),
                    "value": spec.get("default"),
                    "constraints": _constraints(spec),
                    "options": choices,
                    "error": errors.get(key),
                    "provenance": _json_list(spec.get("provenance")),
                    "visible_if": spec.get("visible_if"),
                }
            )
        return fields


def _options(schema: Mapping[str, JsonValue]) -> list[JsonValue]:
    properties = _json_mapping(schema.get("properties"))
    for spec_value in properties.values():
        spec = _json_mapping(spec_value)
        explicit = _explicit_options(spec.get("x-options"))
        if explicit:
            return explicit
        options = _enum_options(spec.get("enum"))
        if options:
            return options
    return _enum_options(schema.get("enum")) or []


def _explicit_options(value: JsonValue) -> list[JsonValue] | None:
    if not isinstance(value, list):
        return None
    result: list[JsonValue] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        label = item.get("label")
        if not isinstance(key, str) or not key or not isinstance(label, str) or not label:
            continue
        description = item.get("description")
        result.append(
            {
                "key": key,
                "label": label,
                "description": description if isinstance(description, str) else None,
                "disabled": item.get("disabled") is True,
            }
        )
    return result or None


def _enum_options(value: JsonValue) -> list[JsonValue] | None:
    if not isinstance(value, list):
        return None
    return [
        {"key": str(item), "label": str(item), "description": None, "disabled": False}
        for item in value
        if isinstance(item, str | int | float | bool)
    ]


def _constraints(spec: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    mapping = {
        "min_length": spec.get("minLength"),
        "max_length": spec.get("maxLength"),
        "min": spec.get("minimum"),
        "max": spec.get("maximum"),
        "pattern": spec.get("pattern"),
    }
    return {key: value for key, value in mapping.items() if value is not None}


def _data_type(value: JsonValue) -> str:
    return (
        value
        if isinstance(value, str)
        and value
        in {
            "string",
            "integer",
            "number",
            "boolean",
        }
        else "string"
    )


def _control_type(spec: Mapping[str, JsonValue], options: list[JsonValue] | None) -> str:
    if options:
        return "select"
    value = spec.get("format")
    return value if isinstance(value, str) and value in {"date", "datetime", "textarea"} else "text"


def _json_mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    return value if isinstance(value, Mapping) else {}


def _json_list(value: JsonValue) -> list[JsonValue] | None:
    return value if isinstance(value, list) else None


def _string_list(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _string(value: JsonValue, default: str) -> str:
    return value if isinstance(value, str) else default


def _optional_string(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["CardProjectionService", "HitlCardPresenter", "stable_card_id"]
