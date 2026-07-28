"""Planning 薄 tools 与 Choice -> Form -> Approval -> Artifact 卡片测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from trade_agent.apps.workflows import planning_presenter_config_from_settings
from trade_agent.capabilities.planning.application import PlanDraftRequest, PlanningService
from trade_agent.capabilities.planning.cards import PlanningCardPresenter
from trade_agent.capabilities.planning.contracts import PlanLineage
from trade_agent.capabilities.planning.tools import (
    CreatePlanDraftTool,
    RecordPlanningReviewTool,
    TransitionPlanTool,
)
from trade_agent.core.config.settings import PlanningWorkflowSettings
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import DEFAULT_CARD_CATALOG, CardEnvelope
from trade_agent.core.tools import ToolExecutionContext, ToolExecutionPrincipal, ToolRequest

NOW = datetime(2026, 7, 27, 9, 30, tzinfo=UTC)


def _trusted_context() -> ToolExecutionContext:
    return ToolExecutionContext(ToolExecutionPrincipal(owner_id="owner-a"))


def _lineage() -> PlanLineage:
    return PlanLineage(
        "scan_result",
        "scan-result-1",
        4,
        ("evidence-1",),
        "strategy-1",
        2,
        "lightgbm-v3",
    )


def _request(*, complete: bool, at: datetime = NOW) -> PlanDraftRequest:
    return PlanDraftRequest(
        "plan-1",
        "owner-a",
        "US:NASDAQ:NVDA",
        "多头观察计划",
        at,
        (_lineage(),),
        "20 个交易日" if complete else None,
        "回撤后站回关键位" if complete else None,
        "跌破失效位" if complete else None,
        "到达目标区间复核" if complete else None,
        "风险预算内分批" if complete else None,
        "财报跳空和模型漂移" if complete else None,
        {"direction": "用户输入", "target": "research_artifact"},
    )


def _data(card: CardEnvelope) -> dict[str, Any]:
    return cast(dict[str, Any], card.data)


def _draft_arguments(
    *, complete: bool, expected_version: int | None = None
) -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {
        "plan_id": "plan-1",
        "owner_id": "owner-a",
        "security_id": "US:NASDAQ:NVDA",
        "direction": "多头观察计划",
        "created_at": NOW.isoformat(),
        "source_references": [
            {
                "source_type": "scan_result",
                "source_id": "scan-result-1",
                "source_version": 4,
                "evidence_ids": ["evidence-1"],
                "strategy_id": "strategy-1",
                "strategy_version": 2,
                "model_version_id": "lightgbm-v3",
            }
        ],
        "field_sources": {"direction": "用户输入", "target": "research_artifact"},
    }
    if expected_version is not None:
        arguments["expected_version"] = expected_version
    if complete:
        arguments.update(
            {
                "horizon": "20 个交易日",
                "entry_condition": "回撤后站回关键位",
                "invalidation_condition": "跌破失效位",
                "target": "到达目标区间复核",
                "position_notes": "风险预算内分批",
                "risk_notes": "财报跳空和模型漂移",
            }
        )
    return arguments


def test_planning_tool_manifests_declare_hitl_and_idempotency_boundaries() -> None:
    service = PlanningService()
    draft_tool = CreatePlanDraftTool(service)
    transition_tool = TransitionPlanTool(service)
    review_tool = RecordPlanningReviewTool(service)

    assert draft_tool.manifest.read_only is False
    assert draft_tool.manifest.requires_hitl is False
    assert draft_tool.manifest.requires_idempotency_key is True
    for tool in (transition_tool, review_tool):
        assert tool.manifest.read_only is False
        assert tool.manifest.requires_hitl is True
        assert tool.manifest.risk == "controlled_write"
        assert tool.manifest.idempotent is True
        assert tool.manifest.requires_idempotency_key is True
    assert all(
        forbidden not in tool.manifest.tool_id
        for tool in (draft_tool, transition_tool, review_tool)
        for forbidden in ("order", "fill", "balance", "broker")
    )


def test_tools_delegate_draft_revision_activation_and_require_hitl_metadata() -> None:
    service = PlanningService()
    create = CreatePlanDraftTool(service)
    transition = TransitionPlanTool(service)

    drafted = asyncio.run(
        create.handle(
            ToolRequest(
                "planning.create_plan_draft",
                {
                    key: value
                    for key, value in _draft_arguments(complete=False).items()
                    if key != "owner_id"
                },
                idempotency_key="draft-1",
                context=_trusted_context(),
            )
        )
    )
    assert drafted.status == "drafted"
    assert drafted.payload["missing_fields"] == [
        "horizon",
        "entry_condition",
        "invalidation_condition",
        "target",
        "position_notes",
        "risk_notes",
    ]

    revised = asyncio.run(
        create.handle(
            ToolRequest(
                "planning.create_plan_draft",
                {
                    key: value
                    for key, value in _draft_arguments(complete=True, expected_version=1).items()
                    if key != "owner_id"
                },
                idempotency_key="draft-2",
                context=_trusted_context(),
            )
        )
    )
    assert revised.payload["version"] == 2
    approval_hash = revised.payload["approval_payload_hash"]
    assert isinstance(approval_hash, str)

    transition_arguments: dict[str, JsonValue] = {
        "plan_id": "plan-1",
        "expected_version": 2,
        "target_status": "active",
        "occurred_at": (NOW + timedelta(minutes=2)).isoformat(),
        "approved": True,
        "approved_payload_hash": approval_hash,
    }
    with pytest.raises(ValueError, match="HITL"):
        asyncio.run(
            transition.handle(
                ToolRequest(
                    "planning.transition_plan",
                    transition_arguments,
                    idempotency_key="activate-1",
                    context=_trusted_context(),
                )
            )
        )
    activated = asyncio.run(
        transition.handle(
            ToolRequest(
                "planning.transition_plan",
                transition_arguments,
                idempotency_key="activate-1",
                approval_interaction_id="approval-1",
                context=_trusted_context(),
            )
        )
    )
    assert activated.payload["status"] == "active"
    assert activated.payload["version"] == 3


def test_choice_form_approval_edit_supersede_and_artifact_cards_are_catalog_valid() -> None:
    service = PlanningService()
    incomplete = service.create_draft(_request(complete=False), idempotency_key="draft-1")
    presenter = PlanningCardPresenter(
        planning_presenter_config_from_settings(PlanningWorkflowSettings())
    )

    choice = presenter.intent_choice("interaction-choice-1")
    form = presenter.plan_form(incomplete, field_errors={"horizon": "请选择计划周期"})
    assert choice.kind == "interaction.choice"
    assert _data(choice)["options"][1]["disabled"] is True
    assert form.kind == "interaction.form"
    fields = _data(form)["fields"]
    assert {field["key"] for field in fields} == {
        "security_id",
        "direction",
        "horizon",
        "entry_condition",
        "invalidation_condition",
        "target",
        "position_notes",
        "risk_notes",
    }
    assert next(field for field in fields if field["key"] == "horizon")["error"] == "请选择计划周期"
    with pytest.raises(ValueError, match="缺少风险关键字段"):
        presenter.plan_approval(incomplete)

    complete = service.revise_draft(
        _request(complete=True, at=NOW + timedelta(minutes=1)),
        expected_version=1,
        idempotency_key="draft-2",
    )
    approval = presenter.plan_approval(complete)
    newer = service.revise_draft(
        replace(
            _request(complete=True, at=NOW + timedelta(minutes=2)),
            target="修改后的目标区间",
        ),
        expected_version=2,
        idempotency_key="draft-3",
    )
    old_card, new_card = presenter.supersede_after_edit(complete, newer)
    assert approval.actions == ("confirm", "edit", "cancel")
    assert old_card.state == "superseded"
    assert old_card.actions == ()
    assert new_card.source.version == 3

    active = service.activate(
        owner_id="owner-a",
        plan_id="plan-1",
        expected_version=3,
        actor_id="owner-a",
        approved=True,
        approved_payload_hash=newer.approval_payload_hash,
        approval_interaction_id="approval-3",
        idempotency_key="activate-3",
        occurred_at=NOW + timedelta(minutes=3),
    )
    artifact = presenter.plan_artifact(active)
    unsupported = presenter.unsupported(
        reference_id="request-1",
        unsupported_kind="execute_trade",
        message="首版不能执行真实交易; 可以改为创建美股交易计划。",
    )
    assert artifact.kind == "artifact.trade_plan"
    assert "不提供交易执行能力" in _data(artifact)["summary"]
    assert unsupported.kind == "notice.unsupported"
    assert unsupported.actions == ("refresh",)
    for card in (choice, form, approval, old_card, new_card, artifact, unsupported):
        assert DEFAULT_CARD_CATALOG.supports(card.kind, card.schema_version)
