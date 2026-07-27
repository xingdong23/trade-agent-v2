from datetime import UTC, datetime, timedelta
from typing import cast

from trade_agent.core.hitl import HumanInteraction, InteractionStatus, InteractionType
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardProjectionService, CardSource, HitlCardPresenter


def _interaction(interaction_type: InteractionType, *, version: int = 1) -> HumanInteraction:
    return HumanInteraction(
        interaction_id="interaction-1",
        owner_id="owner-1",
        interaction_type=interaction_type,
        status=InteractionStatus.PENDING,
        payload={
            "title": "补充交易计划",
            "description": "请确认关键字段",
            "summary": "激活计划前需要确认",
            "current_value": "100",
            "suggested_value": "95",
            "reason": "风险控制",
            "findings": [{"label": "风险", "detail": "止损缺失", "severity": "high"}],
        },
        version=version,
        thread_id="thread-1",
        run_id="run-1",
        subject_type="plan",
        subject_id="plan-1",
        subject_version=2,
        payload_hash="payload-hash",
        response_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "title": "证券代码",
                    "minLength": 1,
                    "provenance": [
                        {
                            "label": "用户输入",
                            "value": "NVDA",
                            "source_id": "message-1",
                            "source_type": "message",
                        }
                    ],
                },
                "side": {"type": "string", "title": "方向", "enum": ["观察", "买入计划"]},
            },
            "required": ["symbol"],
        },
        created_at=datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(minutes=10),
    )


def test_hitl_presenter_uses_response_schema_and_server_policy() -> None:
    presenter = HitlCardPresenter()
    card = presenter.present(
        _interaction(InteractionType.EXCEPTION_RESOLUTION),
        allowed_actions=("continue",),
        field_errors={"symbol": "不能为空"},
    )

    assert card.kind == "interaction.form"
    assert card.actions == ("continue",)
    fields = cast(list[JsonValue], card.data["fields"])
    assert isinstance(fields, list)
    first = cast(dict[str, JsonValue], fields[0])
    second = cast(dict[str, JsonValue], fields[1])
    options = cast(list[JsonValue], second["options"])
    first_option = cast(dict[str, JsonValue], options[0])
    assert first["key"] == "symbol"
    assert first["error"] == "不能为空"
    assert first_option["key"] == "观察"


def test_all_hitl_interaction_types_have_allowlisted_cards() -> None:
    expected = {
        InteractionType.CLARIFICATION: "interaction.choice",
        InteractionType.APPROVAL: "interaction.approval",
        InteractionType.REVIEW: "interaction.review",
        InteractionType.CORRECTION: "interaction.correction",
        InteractionType.EXCEPTION_RESOLUTION: "interaction.form",
    }
    for interaction_type, kind in expected.items():
        assert HitlCardPresenter().present(_interaction(interaction_type)).kind == kind


def test_projection_keeps_card_id_and_increments_revision() -> None:
    projection = CardProjectionService()
    source = CardSource("job", "job-1", 1)
    first = projection.project(
        kind="progress.scan",
        source=source,
        state="pending",
        data={"title": "扫描", "message": "排队中", "progress": 0},
        actions=("cancel",),
        text_fallback="扫描排队中",
    )
    second = projection.project(
        kind="progress.scan",
        source=CardSource("job", "job-1", 2),
        state="resolved",
        data={"title": "扫描", "message": "已完成", "progress": 100},
        actions=("cancel",),
        text_fallback="扫描已完成",
    )

    assert second.card_id == first.card_id
    assert second.revision == first.revision + 1
    assert second.actions == ()


def test_projection_supersedes_and_falls_back_safely() -> None:
    projection = CardProjectionService()
    first = projection.project(
        kind="progress.research",
        source=CardSource("artifact", "research-1", 1),
        state="pending",
        data={"title": "研究", "message": "进行中"},
        text_fallback="研究进行中",
    )
    superseded = projection.supersede(first.card_id, source_version=2)
    fallback = projection.unsupported_fallback(
        source=CardSource("artifact", "research-1", 2),
        unsupported_kind="artifact.research",
        unsupported_schema_version=9,
    )

    assert superseded.state == "superseded"
    assert superseded.revision == 2
    assert fallback.kind == "notice.unsupported"
    assert fallback.actions == ()
