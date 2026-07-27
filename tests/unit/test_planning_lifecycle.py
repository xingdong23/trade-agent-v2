"""交易计划草稿、状态机、审批幂等与复盘 lineage 测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from trade_agent.capabilities.planning.application import (
    PlanDraftRequest,
    PlanningConflictError,
    PlanningService,
)
from trade_agent.capabilities.planning.contracts import PlanLineage, PlanStatus, ReviewOutcome

NOW = datetime(2026, 7, 27, 9, 30, tzinfo=UTC)


def _lineage(source_type: str = "scan_result") -> PlanLineage:
    return PlanLineage(
        source_type=source_type,
        source_id="scan-result-1" if source_type == "scan_result" else "research-1",
        source_version=3,
        evidence_ids=("evidence-price-1", "evidence-filing-1"),
        strategy_id="strategy-1" if source_type == "scan_result" else None,
        strategy_version=7 if source_type == "scan_result" else None,
        model_version_id="lightgbm-us-5d-v4" if source_type == "scan_result" else None,
    )


def _request(*, complete: bool, created_at: datetime = NOW) -> PlanDraftRequest:
    return PlanDraftRequest(
        plan_id="plan-1",
        owner_id="owner-a",
        security_id="US:NASDAQ:NVDA",
        direction="观察回撤后的多头计划",
        created_at=created_at,
        source_references=(_lineage(),),
        horizon="20 个交易日" if complete else None,
        entry_condition="回撤后重新站上关键位" if complete else None,
        invalidation_condition="收盘跌破失效位" if complete else None,
        target="到达研究目标区间后复核" if complete else None,
        position_notes="单一计划风险预算不超过既定上限" if complete else None,
        risk_notes="财报、跳空和模型失效风险" if complete else None,
        field_sources={"direction": "用户输入", "target": "research_artifact"},
    )


def test_draft_keeps_missing_risk_fields_without_model_guessing() -> None:
    service = PlanningService()
    plan = service.create_draft(_request(complete=False), idempotency_key="draft-1")

    assert plan.status is PlanStatus.DRAFT
    assert plan.horizon is None
    assert plan.entry_condition is None
    assert plan.invalidation_condition is None
    assert plan.target is None
    assert plan.position_notes is None
    assert plan.risk_notes is None
    assert plan.missing_fields == (
        "horizon",
        "entry_condition",
        "invalidation_condition",
        "target",
        "position_notes",
        "risk_notes",
    )
    assert plan.source_references[0].evidence_ids == (
        "evidence-price-1",
        "evidence-filing-1",
    )


def test_draft_rejects_non_us_security_and_reuses_identical_command() -> None:
    service = PlanningService()
    first = service.create_draft(_request(complete=False), idempotency_key="draft-1")
    replay = service.create_draft(_request(complete=False), idempotency_key="draft-1")
    assert replay is first

    changed = replace(_request(complete=False), direction="被修改的内容")
    with pytest.raises(PlanningConflictError, match="payload 已改变"):
        service.create_draft(changed, idempotency_key="draft-1")

    unsupported = PlanDraftRequest(
        plan_id="plan-hk",
        owner_id="owner-a",
        security_id="HK:HKEX:00700",
        direction="观察",
        created_at=NOW,
        source_references=(_lineage("research_artifact"),),
    )
    with pytest.raises(ValueError, match="unsupported_market"):
        service.create_draft(unsupported, idempotency_key="draft-hk")


def test_edit_creates_new_draft_version_and_keeps_history_immutable() -> None:
    service = PlanningService()
    first = service.create_draft(_request(complete=False), idempotency_key="draft-1")
    revised = service.revise_draft(
        _request(complete=True, created_at=NOW + timedelta(minutes=1)),
        expected_version=1,
        idempotency_key="draft-2",
    )

    assert revised.version == 2
    assert revised.supersedes_version == 1
    assert revised.status is PlanStatus.DRAFT
    assert not revised.missing_fields
    historical = service.get_plan_version(owner_id="owner-a", plan_id="plan-1", version=1)
    assert historical is first
    assert historical.horizon is None
    assert historical.status is PlanStatus.DRAFT


def test_activation_requires_complete_latest_payload_hitl_owner_and_idempotency() -> None:
    incomplete_service = PlanningService()
    incomplete = incomplete_service.create_draft(
        _request(complete=False), idempotency_key="draft-incomplete"
    )
    with pytest.raises(ValueError, match="缺少风险关键字段"):
        incomplete_service.activate(
            owner_id="owner-a",
            plan_id="plan-1",
            expected_version=1,
            actor_id="owner-a",
            approved=True,
            approved_payload_hash=incomplete.approval_payload_hash,
            approval_interaction_id="approval-incomplete",
            idempotency_key="activate-incomplete",
            occurred_at=NOW,
        )

    service = PlanningService()
    draft = service.create_draft(_request(complete=True), idempotency_key="draft-1")
    with pytest.raises(PermissionError, match="owner"):
        service.activate(
            owner_id="owner-a",
            plan_id="plan-1",
            expected_version=1,
            actor_id="owner-b",
            approved=True,
            approved_payload_hash=draft.approval_payload_hash,
            approval_interaction_id="approval-1",
            idempotency_key="activate-wrong-owner",
            occurred_at=NOW,
        )
    with pytest.raises(PlanningConflictError, match="payload hash"):
        service.activate(
            owner_id="owner-a",
            plan_id="plan-1",
            expected_version=1,
            actor_id="owner-a",
            approved=True,
            approved_payload_hash="stale",
            approval_interaction_id="approval-1",
            idempotency_key="activate-stale",
            occurred_at=NOW,
        )

    activated = service.activate(
        owner_id="owner-a",
        plan_id="plan-1",
        expected_version=1,
        actor_id="owner-a",
        approved=True,
        approved_payload_hash=draft.approval_payload_hash,
        approval_interaction_id="approval-1",
        idempotency_key="activate-1",
        occurred_at=NOW,
    )
    replay = service.activate(
        owner_id="owner-a",
        plan_id="plan-1",
        expected_version=1,
        actor_id="owner-a",
        approved=True,
        approved_payload_hash=draft.approval_payload_hash,
        approval_interaction_id="approval-1",
        idempotency_key="activate-1",
        occurred_at=NOW,
    )
    assert replay is activated
    assert activated.status is PlanStatus.ACTIVE
    assert activated.version == 2
    assert activated.transitions[-1].approval_interaction_id == "approval-1"


def test_plan_state_machine_accepts_legal_and_rejects_illegal_transitions() -> None:
    service = PlanningService()
    draft = service.create_draft(_request(complete=True), idempotency_key="draft-1")
    active = service.activate(
        owner_id="owner-a",
        plan_id="plan-1",
        expected_version=1,
        actor_id="owner-a",
        approved=True,
        approved_payload_hash=draft.approval_payload_hash,
        approval_interaction_id="approval-1",
        idempotency_key="activate-1",
        occurred_at=NOW,
    )
    triggered = service.transition(
        owner_id="owner-a",
        plan_id="plan-1",
        expected_version=active.version,
        target_status=PlanStatus.TRIGGERED,
        actor_id="owner-a",
        reason="价格条件被观察到; 不表示成交",
        approval_interaction_id="review-trigger-1",
        idempotency_key="trigger-1",
        occurred_at=NOW + timedelta(minutes=1),
    )
    assert triggered.status is PlanStatus.TRIGGERED

    with pytest.raises(ValueError, match="不允许"):
        service.transition(
            owner_id="owner-a",
            plan_id="plan-1",
            expected_version=triggered.version,
            target_status=PlanStatus.DRAFT,
            actor_id="owner-a",
            reason="非法回退",
            approval_interaction_id="approval-illegal",
            idempotency_key="illegal-1",
            occurred_at=NOW + timedelta(minutes=2),
        )


def test_review_freezes_strategy_model_evidence_lineage_without_rewriting_history() -> None:
    service = PlanningService()
    draft = service.create_draft(_request(complete=True), idempotency_key="draft-1")
    active = service.activate(
        owner_id="owner-a",
        plan_id="plan-1",
        expected_version=1,
        actor_id="owner-a",
        approved=True,
        approved_payload_hash=draft.approval_payload_hash,
        approval_interaction_id="approval-1",
        idempotency_key="activate-1",
        occurred_at=NOW,
    )
    result = service.record_review(
        owner_id="owner-a",
        review_id="review-1",
        subject_type="plan",
        subject_id="plan-1",
        subject_version=active.version,
        outcome=ReviewOutcome.FALSE_POSITIVE,
        annotations={"comment": "量价信号未持续"},
        lineage=(),
        feedback_destinations=("future_strategy_draft", "future_training_data"),
        actor_id="owner-a",
        approval_interaction_id="review-interaction-1",
        idempotency_key="review-command-1",
        created_at=NOW + timedelta(days=10),
    )

    assert result.review.lineage == draft.source_references
    assert result.review.lineage[0].strategy_version == 7
    assert result.review.lineage[0].model_version_id == "lightgbm-us-5d-v4"
    assert result.review.lineage[0].evidence_ids == (
        "evidence-price-1",
        "evidence-filing-1",
    )
    assert result.reviewed_plan is not None
    assert result.reviewed_plan.status is PlanStatus.REVIEWED
    assert service.get_plan_version(owner_id="owner-a", plan_id="plan-1", version=1) is draft
    historical_active = service.get_plan_version(owner_id="owner-a", plan_id="plan-1", version=2)
    assert historical_active.status is PlanStatus.ACTIVE
    assert historical_active.source_references == draft.source_references
