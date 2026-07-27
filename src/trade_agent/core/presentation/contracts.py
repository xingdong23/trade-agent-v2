"""前后端共享的 CardEnvelope、Schema 和 catalog 协议。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from trade_agent.core.llm.contracts import JsonValue

CARD_PROTOCOL_VERSION: Final[str] = "card.v1"
CARD_STATES: Final[frozenset[str]] = frozenset(
    {"pending", "resolved", "superseded", "expired", "cancelled", "failed"}
)
ACTION_IDS: Final[frozenset[str]] = frozenset(
    {"continue", "confirm", "edit", "cancel", "retry", "refresh"}
)
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "protocol_version",
        "card_id",
        "kind",
        "schema_version",
        "revision",
        "source",
        "state",
        "data",
        "actions",
        "payload_hash",
        "expires_at",
        "text_fallback",
    }
)
_SOURCE_KEYS: Final[frozenset[str]] = frozenset({"source_type", "source_id", "version"})
_DISALLOWED_MAPPING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "component",
        "component_name",
        "html",
        "href",
        "link",
        "navigate_to",
        "onclick",
        "on_click",
        "on_press",
        "route",
        "script",
        "src",
        "to",
        "url",
    }
)
_HTML_PATTERN = re.compile(r"<[^>]+>")
_SCRIPT_PATTERN = re.compile(r"(?:javascript:|data:text/html)", re.IGNORECASE)
_UI_TEXT_PATTERN = re.compile(
    r"(?:<\s*script\b|<\s*iframe\b|<\s*object\b|<\s*embed\b|<\s*form\b|<\s*a\b)",
    re.IGNORECASE,
)


class CardValidationError(ValueError):
    """Raised when a card payload violates the allowlisted wire contract."""


@dataclass(frozen=True, slots=True)
class CardSource:
    """标识一张卡片背后的稳定领域源。

    Attributes:
        source_type: 领域对象类型，例如 interaction。
        source_id: 领域对象的稳定业务主键。
        version: 领域对象版本号，必须从 1 开始递增。
    """

    source_type: str
    source_id: str
    version: int

    def __post_init__(self) -> None:
        if not self.source_type.strip():
            raise CardValidationError("source.source_type 不能为空")
        if not self.source_id.strip():
            raise CardValidationError("source.source_id 不能为空")
        if self.version < 1:
            raise CardValidationError("source.version 必须从 1 开始递增")

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ValueSpec:
    """描述卡片 JSON 字段的白名单 schema。

    Attributes:
        kind: 值类型标签，例如 string、mapping 或 sequence。
        fields: 当 kind=mapping 时允许出现的子字段 schema。
        required_fields: mapping 中必须存在的字段集合。
        item_spec: 当 kind=sequence 时每个元素必须满足的 schema。
        variants: 联合类型允许的多个候选 schema。
        choices: 当 kind=string 时允许出现的枚举值集合。
        allow_none: 是否允许值为 null。
    """

    kind: str
    fields: Mapping[str, ValueSpec] = field(default_factory=dict)
    required_fields: frozenset[str] = field(default_factory=frozenset)
    item_spec: ValueSpec | None = None
    variants: tuple[ValueSpec, ...] = ()
    choices: frozenset[str] = field(default_factory=frozenset)
    allow_none: bool = False

    def validate(self, value: JsonValue, *, path: str) -> None:
        if value is None:
            if self.allow_none:
                return
            raise CardValidationError(f"{path} 不能为空")

        if self.variants:
            errors: list[str] = []
            for variant in self.variants:
                try:
                    variant.validate(value, path=path)
                except CardValidationError as exc:
                    errors.append(str(exc))
                else:
                    return
            detail = "; ".join(errors)
            raise CardValidationError(f"{path} 不匹配任何允许的类型: {detail}")

        if self.kind == "string":
            if not isinstance(value, str):
                raise CardValidationError(f"{path} 必须是字符串")
            _validate_safe_text(value, path=path)
            if self.choices and value not in self.choices:
                raise CardValidationError(f"{path} 包含未注册值: {value}")
            return

        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise CardValidationError(f"{path} 必须是布尔值")
            return

        if self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise CardValidationError(f"{path} 必须是整数")
            return

        if self.kind == "number":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise CardValidationError(f"{path} 必须是数字")
            return

        if self.kind == "mapping":
            if not isinstance(value, Mapping):
                raise CardValidationError(f"{path} 必须是对象")
            _validate_mapping_keys(value, path=path)
            unknown_fields = set(value) - set(self.fields)
            if unknown_fields:
                field_list = ", ".join(sorted(unknown_fields))
                raise CardValidationError(f"{path} 包含未知字段: {field_list}")
            missing_fields = self.required_fields - set(value)
            if missing_fields:
                field_list = ", ".join(sorted(missing_fields))
                raise CardValidationError(f"{path} 缺少必填字段: {field_list}")
            for field_name, field_value in value.items():
                self.fields[field_name].validate(field_value, path=f"{path}.{field_name}")
            return

        if self.kind == "sequence":
            if not isinstance(value, list):
                raise CardValidationError(f"{path} 必须是数组")
            if self.item_spec is None:
                raise CardValidationError(f"{path} 缺少数组元素 schema")
            for index, item in enumerate(value):
                self.item_spec.validate(item, path=f"{path}[{index}]")
            return

        if self.kind == "any":
            _validate_safe_value(value, path=path)
            return

        raise CardValidationError(f"{path} 使用了未知 schema kind: {self.kind}")


def _string_spec(*, choices: Sequence[str] = (), allow_none: bool = False) -> ValueSpec:
    return ValueSpec(kind="string", choices=frozenset(choices), allow_none=allow_none)


def _boolean_spec(*, allow_none: bool = False) -> ValueSpec:
    return ValueSpec(kind="boolean", allow_none=allow_none)


def _integer_spec(*, allow_none: bool = False) -> ValueSpec:
    return ValueSpec(kind="integer", allow_none=allow_none)


def _number_spec(*, allow_none: bool = False) -> ValueSpec:
    return ValueSpec(kind="number", allow_none=allow_none)


def _any_spec(*, allow_none: bool = False) -> ValueSpec:
    return ValueSpec(kind="any", allow_none=allow_none)


def _object_spec(
    fields: Mapping[str, ValueSpec],
    *,
    required_fields: Sequence[str] = (),
    allow_none: bool = False,
) -> ValueSpec:
    return ValueSpec(
        kind="mapping",
        fields=dict(fields),
        required_fields=frozenset(required_fields),
        allow_none=allow_none,
    )


def _list_spec(item_spec: ValueSpec, *, allow_none: bool = False) -> ValueSpec:
    return ValueSpec(kind="sequence", item_spec=item_spec, allow_none=allow_none)


def _union_spec(*variants: ValueSpec, allow_none: bool = False) -> ValueSpec:
    return ValueSpec(kind="union", variants=variants, allow_none=allow_none)


@dataclass(frozen=True, slots=True)
class CardSchema:
    """注册某一类卡片版本的协议合同。

    Attributes:
        kind: 卡片种类标识，属于稳定对外契约。
        schema_version: kind 下的数据 schema 版本。
        data_spec: data 字段必须满足的 JSON schema 白名单。
        allowed_actions: 该卡片版本允许暴露的语义动作集合。
    """

    kind: str
    schema_version: int
    data_spec: ValueSpec
    allowed_actions: frozenset[str]


_PROVENANCE_ITEM_SPEC = _object_spec(
    {
        "label": _string_spec(),
        "value": _string_spec(),
        "source_id": _string_spec(),
        "source_type": _string_spec(),
    },
    required_fields=("label", "value", "source_id", "source_type"),
)
_OPTION_ITEM_SPEC = _object_spec(
    {
        "key": _string_spec(),
        "label": _string_spec(),
        "description": _string_spec(allow_none=True),
        "disabled": _boolean_spec(allow_none=True),
    },
    required_fields=("key", "label"),
)
_VISIBILITY_SPEC = _object_spec(
    {
        "field_key": _string_spec(),
        "equals": _any_spec(allow_none=True),
    },
    required_fields=("field_key", "equals"),
    allow_none=True,
)
_CONSTRAINTS_SPEC = _object_spec(
    {
        "max": _number_spec(allow_none=True),
        "max_length": _integer_spec(allow_none=True),
        "min": _number_spec(allow_none=True),
        "min_length": _integer_spec(allow_none=True),
        "pattern": _string_spec(allow_none=True),
    },
    allow_none=True,
)
_FIELD_ITEM_SPEC = _object_spec(
    {
        "key": _string_spec(),
        "label": _string_spec(),
        "value": _any_spec(allow_none=True),
        "data_type": _string_spec(
            choices=(
                "string",
                "integer",
                "number",
                "boolean",
                "date",
                "datetime",
                "symbol",
                "money",
            )
        ),
        "control_type": _string_spec(
            choices=("text", "textarea", "number", "select", "checkbox", "date")
        ),
        "required": _boolean_spec(),
        "read_only": _boolean_spec(),
        "constraints": _CONSTRAINTS_SPEC,
        "options": _list_spec(_OPTION_ITEM_SPEC, allow_none=True),
        "error": _string_spec(allow_none=True),
        "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
        "visible_if": _VISIBILITY_SPEC,
    },
    required_fields=("key", "label", "data_type", "control_type", "required", "read_only"),
)
_SECTION_ITEM_SPEC = _object_spec(
    {
        "title": _string_spec(),
        "content": _string_spec(),
        "kind": _string_spec(choices=("text", "summary", "analysis", "risk", "plan")),
    },
    required_fields=("title", "content", "kind"),
)
_FINDING_ITEM_SPEC = _object_spec(
    {
        "label": _string_spec(),
        "detail": _string_spec(),
        "severity": _string_spec(choices=("low", "medium", "high")),
    },
    required_fields=("label", "detail", "severity"),
)
_CARD_SCHEMAS: tuple[CardSchema, ...] = (
    CardSchema(
        kind="interaction.form",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "description": _string_spec(allow_none=True),
                "fields": _list_spec(_FIELD_ITEM_SPEC),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "fields"),
        ),
        allowed_actions=frozenset({"continue", "cancel"}),
    ),
    CardSchema(
        kind="interaction.choice",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "description": _string_spec(allow_none=True),
                "options": _list_spec(_OPTION_ITEM_SPEC),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "options"),
        ),
        allowed_actions=frozenset({"continue", "cancel"}),
    ),
    CardSchema(
        kind="interaction.approval",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "description": _string_spec(allow_none=True),
                "summary": _string_spec(),
                "facts": _list_spec(_FINDING_ITEM_SPEC, allow_none=True),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "summary"),
        ),
        allowed_actions=frozenset({"confirm", "edit", "cancel"}),
    ),
    CardSchema(
        kind="interaction.review",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "description": _string_spec(allow_none=True),
                "findings": _list_spec(_FINDING_ITEM_SPEC),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "findings"),
        ),
        allowed_actions=frozenset({"confirm", "edit", "cancel"}),
    ),
    CardSchema(
        kind="interaction.correction",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "description": _string_spec(allow_none=True),
                "current_value": _string_spec(),
                "suggested_value": _string_spec(),
                "reason": _string_spec(allow_none=True),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "current_value", "suggested_value"),
        ),
        allowed_actions=frozenset({"confirm", "edit", "cancel"}),
    ),
    CardSchema(
        kind="artifact.research",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "summary": _string_spec(),
                "sections": _list_spec(_SECTION_ITEM_SPEC, allow_none=True),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "summary"),
        ),
        allowed_actions=frozenset(),
    ),
    CardSchema(
        kind="artifact.strategy",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "summary": _string_spec(),
                "sections": _list_spec(_SECTION_ITEM_SPEC, allow_none=True),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "summary"),
        ),
        allowed_actions=frozenset(),
    ),
    CardSchema(
        kind="artifact.quantitative_snapshot",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "summary": _string_spec(),
                "sections": _list_spec(_SECTION_ITEM_SPEC, allow_none=True),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "summary"),
        ),
        allowed_actions=frozenset(),
    ),
    CardSchema(
        kind="artifact.scan_result",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "summary": _string_spec(),
                "sections": _list_spec(_SECTION_ITEM_SPEC, allow_none=True),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "summary"),
        ),
        allowed_actions=frozenset(),
    ),
    CardSchema(
        kind="artifact.trade_plan",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "summary": _string_spec(),
                "sections": _list_spec(_SECTION_ITEM_SPEC, allow_none=True),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "summary"),
        ),
        allowed_actions=frozenset(),
    ),
    CardSchema(
        kind="artifact.reminder",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "summary": _string_spec(),
                "sections": _list_spec(_SECTION_ITEM_SPEC, allow_none=True),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "summary"),
        ),
        allowed_actions=frozenset(),
    ),
    CardSchema(
        kind="progress.research",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "message": _string_spec(),
                "progress": _integer_spec(allow_none=True),
                "current_step": _string_spec(allow_none=True),
                "eta_seconds": _integer_spec(allow_none=True),
            },
            required_fields=("title", "message"),
        ),
        allowed_actions=frozenset({"cancel", "retry"}),
    ),
    CardSchema(
        kind="progress.scan",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "message": _string_spec(),
                "progress": _integer_spec(allow_none=True),
                "current_step": _string_spec(allow_none=True),
                "eta_seconds": _integer_spec(allow_none=True),
            },
            required_fields=("title", "message"),
        ),
        allowed_actions=frozenset({"cancel", "retry"}),
    ),
    CardSchema(
        kind="notice.unsupported",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "message": _string_spec(),
                "unsupported_kind": _string_spec(),
                "unsupported_schema_version": _integer_spec(),
            },
            required_fields=(
                "title",
                "message",
                "unsupported_kind",
                "unsupported_schema_version",
            ),
        ),
        allowed_actions=frozenset({"refresh"}),
    ),
    CardSchema(
        kind="notice.data_gap",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "message": _string_spec(),
                "missing_fields": _list_spec(_string_spec()),
                "provenance": _list_spec(_PROVENANCE_ITEM_SPEC, allow_none=True),
            },
            required_fields=("title", "message", "missing_fields"),
        ),
        allowed_actions=frozenset({"retry", "cancel"}),
    ),
    CardSchema(
        kind="notice.failure",
        schema_version=1,
        data_spec=_object_spec(
            {
                "title": _string_spec(),
                "message": _string_spec(),
                "error_code": _string_spec(),
                "retryable": _boolean_spec(),
            },
            required_fields=("title", "message", "error_code", "retryable"),
        ),
        allowed_actions=frozenset({"retry", "cancel"}),
    ),
)


def _validate_safe_text(value: str, *, path: str) -> None:
    if _HTML_PATTERN.search(value) or _UI_TEXT_PATTERN.search(value):
        raise CardValidationError(f"{path} 不允许包含 HTML 或组件片段")
    if _SCRIPT_PATTERN.search(value):
        raise CardValidationError(f"{path} 不允许包含脚本协议或 HTML 数据 URI")


def _validate_mapping_keys(value: Mapping[str, JsonValue], *, path: str) -> None:
    for raw_key in value:
        if not isinstance(raw_key, str):
            raise CardValidationError(f"{path} 的字段名必须是字符串")
        if raw_key.lower() in _DISALLOWED_MAPPING_KEYS:
            raise CardValidationError(f"{path}.{raw_key} 不允许定义 UI 组件、脚本或跳转")


def _validate_safe_value(value: JsonValue, *, path: str) -> None:
    if value is None or isinstance(value, bool | int | float):
        return
    if isinstance(value, str):
        _validate_safe_text(value, path=path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_safe_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        _validate_mapping_keys(value, path=path)
        for key, item in value.items():
            _validate_safe_value(item, path=f"{path}.{key}")
        return
    raise CardValidationError(f"{path} 包含不受支持的 JSON 值")


def _normalize_json(value: JsonValue) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _normalize_json(item) for key, item in sorted(value.items())}
    raise CardValidationError("发现无法序列化的 JSON 值")


def _validate_iso_datetime(value: str) -> None:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CardValidationError("expires_at 必须是 ISO 8601 时间") from exc


@dataclass(frozen=True, slots=True)
class CardEnvelope:
    """前后端之间传输的稳定卡片载荷。

    Attributes:
        protocol_version: Card wire protocol 版本。
        card_id: 卡片稳定标识，同一 source 在所有客户端一致。
        kind: 卡片类型标识。
        schema_version: kind 对应的数据 schema 版本。
        revision: 同一 card_id 的单调递增修订号。
        source: 回溯到领域对象的稳定来源。
        state: 卡片状态，例如 pending 或 resolved。
        data: 前端渲染所需的纯 JSON 数据。
        actions: 当前状态下允许执行的动作集合。
        payload_hash: 对核心内容计算得到的防篡改摘要。
        expires_at: 可选过期时间，ISO 8601 字符串。
        text_fallback: 任何客户端都必须能展示的纯文本降级内容。

    Invariants:
        - protocol_version 必须等于 CARD_PROTOCOL_VERSION。
        - revision 与 source.version 只能单调前进，不能回退。
        - 当 payload_hash 已提供时，它必须与卡片内容严格匹配。
    """

    protocol_version: str
    card_id: str
    kind: str
    schema_version: int
    revision: int
    source: CardSource
    state: str
    data: Mapping[str, JsonValue]
    actions: tuple[str, ...] = ()
    payload_hash: str = ""
    expires_at: str | None = None
    text_fallback: str = ""
    catalog: InitVar[CardCatalog | None] = None

    def __post_init__(self, catalog: CardCatalog | None) -> None:
        normalized_actions = tuple(self.actions)
        normalized_data = dict(self.data)
        object.__setattr__(self, "actions", normalized_actions)
        object.__setattr__(self, "data", normalized_data)

        if self.protocol_version != CARD_PROTOCOL_VERSION:
            raise CardValidationError(
                f"仅支持协议版本 {CARD_PROTOCOL_VERSION}, 收到 {self.protocol_version}"
            )
        if not self.card_id.strip():
            raise CardValidationError("card_id 不能为空")
        if self.schema_version < 1:
            raise CardValidationError("schema_version 必须从 1 开始")
        if self.revision < 1:
            raise CardValidationError("revision 必须单调递增且从 1 开始")
        if self.state not in CARD_STATES:
            state_list = ", ".join(sorted(CARD_STATES))
            raise CardValidationError(f"state 必须在允许集合中: {state_list}")
        if not self.text_fallback.strip():
            raise CardValidationError("text_fallback 不能为空")
        _validate_safe_text(self.text_fallback, path="text_fallback")
        if self.expires_at is not None:
            _validate_iso_datetime(self.expires_at)

        (catalog or DEFAULT_CARD_CATALOG).validate_envelope(self)
        computed_hash = self.compute_payload_hash()
        if self.payload_hash:
            if self.payload_hash != computed_hash:
                raise CardValidationError("payload_hash 与卡片内容不一致")
        else:
            object.__setattr__(self, "payload_hash", computed_hash)

    def compute_payload_hash(self) -> str:
        payload = {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "state": self.state,
            "source": self.source.to_mapping(),
            "data": _normalize_json(dict(self.data)),
            "actions": list(self.actions),
            "expires_at": self.expires_at,
            "text_fallback": self.text_fallback,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sha256(encoded.encode("utf-8")).hexdigest()

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": self.protocol_version,
            "card_id": self.card_id,
            "kind": self.kind,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "source": self.source.to_mapping(),
            "state": self.state,
            "data": _normalize_json(dict(self.data)),
            "actions": list(self.actions),
            "payload_hash": self.payload_hash,
            "expires_at": self.expires_at,
            "text_fallback": self.text_fallback,
        }

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, JsonValue],
        *,
        catalog: CardCatalog | None = None,
    ) -> CardEnvelope:
        """使用调用方提供的目录解析 Card；未提供时使用内置目录。"""

        return (catalog or DEFAULT_CARD_CATALOG).parse_mapping(payload)


@dataclass(frozen=True, slots=True)
class CardCatalog:
    """按 kind/version 注册并校验卡片协议。

    Attributes:
        _schemas: ``(kind, schema_version)`` 到协议定义的映射。
    """

    _schemas: Mapping[tuple[str, int], CardSchema]

    @classmethod
    def default(cls) -> CardCatalog:
        return cls.from_schemas(_CARD_SCHEMAS)

    @classmethod
    def from_schemas(cls, schemas: Sequence[CardSchema]) -> CardCatalog:
        """从协议定义构造目录，并拒绝重复 kind/version。"""

        registered: dict[tuple[str, int], CardSchema] = {}
        for schema in schemas:
            key = (schema.kind, schema.schema_version)
            if key in registered:
                raise CardValidationError(
                    f"Card schema 重复注册: {schema.kind}.v{schema.schema_version}"
                )
            registered[key] = schema
        return cls(_schemas=registered)

    def extend(self, schemas: Sequence[CardSchema]) -> CardCatalog:
        """返回包含额外 capability Card 协议的新目录。"""

        additions = tuple(schemas)
        duplicate = next(
            (
                schema
                for schema in additions
                if (schema.kind, schema.schema_version) in self._schemas
            ),
            None,
        )
        if duplicate is not None:
            raise CardValidationError(
                f"Card schema 重复注册: {duplicate.kind}.v{duplicate.schema_version}"
            )
        return CardCatalog.from_schemas((*self._schemas.values(), *additions))

    def known_kinds(self) -> frozenset[str]:
        return frozenset(kind for kind, _ in self._schemas)

    def supports(self, kind: str, schema_version: int) -> bool:
        return (kind, schema_version) in self._schemas

    def schema_for(self, kind: str, schema_version: int) -> CardSchema:
        try:
            return self._schemas[(kind, schema_version)]
        except KeyError as exc:
            raise CardValidationError(f"CardCatalog 不支持 {kind}.v{schema_version}") from exc

    def validate_envelope(self, envelope: CardEnvelope) -> None:
        schema = self.schema_for(envelope.kind, envelope.schema_version)
        if len(set(envelope.actions)) != len(envelope.actions):
            raise CardValidationError("actions 不允许重复")
        for action in envelope.actions:
            if action not in ACTION_IDS:
                raise CardValidationError(f"actions 包含未注册语义 action: {action}")
            if action not in schema.allowed_actions:
                raise CardValidationError(
                    f"{envelope.kind}.v{envelope.schema_version} 不允许 action: {action}"
                )
        schema.data_spec.validate(dict(envelope.data), path="data")

    def parse_mapping(self, payload: Mapping[str, JsonValue]) -> CardEnvelope:
        unknown_fields = set(payload) - _ENVELOPE_KEYS
        if unknown_fields:
            field_list = ", ".join(sorted(unknown_fields))
            raise CardValidationError(f"CardEnvelope 包含未知字段: {field_list}")

        missing_fields = _ENVELOPE_KEYS - set(payload)
        if missing_fields:
            field_list = ", ".join(sorted(missing_fields))
            raise CardValidationError(f"CardEnvelope 缺少字段: {field_list}")

        protocol_version = _expect_string(payload["protocol_version"], path="protocol_version")
        card_id = _expect_string(payload["card_id"], path="card_id")
        kind = _expect_string(payload["kind"], path="kind")
        schema_version = _expect_int(payload["schema_version"], path="schema_version")
        revision = _expect_int(payload["revision"], path="revision")
        source = self._parse_source(payload["source"])
        state = _expect_string(payload["state"], path="state")
        data = _expect_mapping(payload["data"], path="data")
        actions = self._parse_actions(payload["actions"])
        payload_hash = _expect_string(payload["payload_hash"], path="payload_hash")
        expires_at = _expect_optional_string(payload["expires_at"], path="expires_at")
        text_fallback = _expect_string(payload["text_fallback"], path="text_fallback")
        return CardEnvelope(
            protocol_version=protocol_version,
            card_id=card_id,
            kind=kind,
            schema_version=schema_version,
            revision=revision,
            source=source,
            state=state,
            data=data,
            actions=actions,
            payload_hash=payload_hash,
            expires_at=expires_at,
            text_fallback=text_fallback,
            catalog=self,
        )

    def _parse_source(self, raw_source: JsonValue) -> CardSource:
        source = _expect_mapping(raw_source, path="source")
        unknown_fields = set(source) - _SOURCE_KEYS
        if unknown_fields:
            field_list = ", ".join(sorted(unknown_fields))
            raise CardValidationError(f"source 包含未知字段: {field_list}")
        missing_fields = _SOURCE_KEYS - set(source)
        if missing_fields:
            field_list = ", ".join(sorted(missing_fields))
            raise CardValidationError(f"source 缺少字段: {field_list}")
        return CardSource(
            source_type=_expect_string(source["source_type"], path="source.source_type"),
            source_id=_expect_string(source["source_id"], path="source.source_id"),
            version=_expect_int(source["version"], path="source.version"),
        )

    def _parse_actions(self, raw_actions: JsonValue) -> tuple[str, ...]:
        if not isinstance(raw_actions, list):
            raise CardValidationError("actions 必须是字符串数组")
        actions: list[str] = []
        for index, raw_action in enumerate(raw_actions):
            actions.append(_expect_string(raw_action, path=f"actions[{index}]"))
        return tuple(actions)


def _expect_string(value: JsonValue, *, path: str) -> str:
    if not isinstance(value, str):
        raise CardValidationError(f"{path} 必须是字符串")
    return value


def _expect_optional_string(value: JsonValue, *, path: str) -> str | None:
    if value is None:
        return None
    return _expect_string(value, path=path)


def _expect_int(value: JsonValue, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CardValidationError(f"{path} 必须是整数")
    return value


def _expect_mapping(value: JsonValue, *, path: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise CardValidationError(f"{path} 必须是对象")
    for key in value:
        if not isinstance(key, str):
            raise CardValidationError(f"{path} 的字段名必须是字符串")
    return value


DEFAULT_CARD_CATALOG = CardCatalog.default()


class CardPresenter(Protocol):
    """把领域对象投影为稳定 CardEnvelope 的展示协议。

    Contract:
        - 调用方传入的对象必须被实现方识别并转换为已注册的卡片协议。
        - 返回值必须通过 CardCatalog 校验，且包含可用于降级展示的 text_fallback。

    Implemented by:
        trade_agent.core.presentation.projection.HitlCardPresenter
    """

    def present(self, source: object) -> CardEnvelope:
        """把一个领域对象转换成前端可消费的卡片。

        Args:
            source: 待展示的领域对象或中间投影对象。

        Returns:
            满足统一卡片协议的不可变载荷。

        Raises:
            CardValidationError: 投影结果不满足已注册卡片协议。
        """
        ...
