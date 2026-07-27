from collections.abc import Mapping

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import (
    CARD_PROTOCOL_VERSION,
    DEFAULT_CARD_CATALOG,
    CardEnvelope,
    CardSource,
    CardValidationError,
)


def _valid_form_data() -> dict[str, JsonValue]:
    return {
        "title": "Create trade plan",
        "description": "Provide the missing fields.",
        "fields": [
            {
                "key": "entry_price",
                "label": "Entry price",
                "value": 145.5,
                "data_type": "number",
                "control_type": "number",
                "required": True,
                "read_only": False,
                "constraints": {"min": 0, "max": 9999},
                "options": None,
                "error": None,
                "provenance": None,
                "visible_if": None,
            }
        ],
        "provenance": [
            {
                "label": "Intent",
                "value": "User asked for a trade plan",
                "source_id": "conversation-1",
                "source_type": "conversation",
            }
        ],
    }


def _valid_envelope(
    *,
    protocol_version: str = CARD_PROTOCOL_VERSION,
    card_id: str = "card-interaction-1",
    kind: str = "interaction.form",
    schema_version: int = 1,
    revision: int = 1,
    source: CardSource | None = None,
    state: str = "pending",
    data: Mapping[str, JsonValue] | None = None,
    actions: tuple[str, ...] = ("continue", "cancel"),
    payload_hash: str = "",
    expires_at: str | None = "2026-07-27T10:00:00+00:00",
    text_fallback: str = "Please provide the missing trade plan fields.",
) -> CardEnvelope:
    return CardEnvelope(
        protocol_version=protocol_version,
        card_id=card_id,
        kind=kind,
        schema_version=schema_version,
        revision=revision,
        source=source or CardSource(source_type="interaction", source_id="int-1", version=1),
        state=state,
        data=data or _valid_form_data(),
        actions=actions,
        payload_hash=payload_hash,
        expires_at=expires_at,
        text_fallback=text_fallback,
    )


def test_card_catalog_accepts_registered_form_card_and_computes_payload_hash() -> None:
    envelope = _valid_envelope()

    assert envelope.payload_hash == envelope.compute_payload_hash()
    assert DEFAULT_CARD_CATALOG.supports("interaction.form", 1)
    assert envelope.to_mapping()["kind"] == "interaction.form"


def test_card_catalog_round_trips_mapping() -> None:
    envelope = _valid_envelope()

    parsed = CardEnvelope.from_mapping(envelope.to_mapping())

    assert parsed == envelope


def test_card_catalog_rejects_unknown_kind() -> None:
    try:
        _valid_envelope(kind="interaction.unknown")
    except CardValidationError as exc:
        assert "不支持 interaction.unknown.v1" in str(exc)
    else:
        raise AssertionError("expected CardValidationError")


def test_card_catalog_rejects_unknown_top_level_field() -> None:
    payload = _valid_envelope().to_mapping()
    payload["component"] = "DangerButton"

    try:
        CardEnvelope.from_mapping(payload)
    except CardValidationError as exc:
        assert "未知字段" in str(exc)
    else:
        raise AssertionError("expected CardValidationError")


def test_card_catalog_rejects_unknown_data_field() -> None:
    data = _valid_form_data()
    data["component"] = "DangerButton"

    try:
        _valid_envelope(data=data)
    except CardValidationError as exc:
        assert "不允许定义 UI 组件" in str(exc)
    else:
        raise AssertionError("expected CardValidationError")


def test_card_catalog_rejects_unknown_action() -> None:
    try:
        _valid_envelope(actions=("launch",))
    except CardValidationError as exc:
        assert "未注册语义 action" in str(exc)
    else:
        raise AssertionError("expected CardValidationError")


def test_card_catalog_rejects_disallowed_html_and_script_content() -> None:
    data = _valid_form_data()
    data["title"] = "<script>alert('x')</script>"

    try:
        _valid_envelope(data=data)
    except CardValidationError as exc:
        assert "不允许包含 HTML" in str(exc)
    else:
        raise AssertionError("expected CardValidationError")


def test_card_catalog_rejects_url_navigation_fields() -> None:
    data: dict[str, JsonValue] = {
        "title": "Unsupported card",
        "message": "Renderer upgrade required.",
        "unsupported_kind": "artifact.research",
        "unsupported_schema_version": 2,
        "url": "https://example.com/upgrade",
    }

    try:
        CardEnvelope(
            protocol_version=CARD_PROTOCOL_VERSION,
            card_id="notice-1",
            kind="notice.unsupported",
            schema_version=1,
            revision=1,
            source=CardSource(source_type="artifact", source_id="art-1", version=1),
            state="failed",
            data=data,
            actions=("refresh",),
            expires_at=None,
            text_fallback="Unsupported card version. Refresh the client.",
        )
    except CardValidationError as exc:
        assert "不允许定义 UI 组件、脚本或跳转" in str(exc)
    else:
        raise AssertionError("expected CardValidationError")


def test_card_catalog_rejects_payload_hash_mismatch() -> None:
    try:
        _valid_envelope(payload_hash="0" * 64)
    except CardValidationError as exc:
        assert "payload_hash 与卡片内容不一致" in str(exc)
    else:
        raise AssertionError("expected CardValidationError")


def test_card_catalog_rejects_duplicate_actions() -> None:
    try:
        _valid_envelope(actions=("continue", "continue"))
    except CardValidationError as exc:
        assert "actions 不允许重复" in str(exc)
    else:
        raise AssertionError("expected CardValidationError")
