"""Watchlist 导入、分类审批和冻结 universe 测试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from trade_agent.capabilities.watchlist.application import (
    IdempotencyConflictError,
    WatchlistService,
)
from trade_agent.capabilities.watchlist.contracts import (
    ClassificationSuggestion,
    ImportStatus,
    Membership,
)

NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _service() -> WatchlistService:
    return WatchlistService(owner_id="owner-a", watchlist_id="watchlist-1")


def _approve(service: WatchlistService, rows: object, *, key: str = "import-1") -> object:
    assert isinstance(rows, tuple)
    return service.approve_import(
        rows,
        actor_owner_id="owner-a",
        approved=True,
        idempotency_key=key,
        source_type="paste",
        source_reference="conversation-1",
        imported_at=NOW,
        tags={"NASDAQ:NVDA": frozenset({"AI"})},
        notes={"NASDAQ:NVDA": ("首批导入",)},
    )


def test_mixed_import_reports_every_row_and_deduplicates_canonical_security() -> None:
    service = _service()
    rows = service.classify_import(
        (
            ("NVDA", "NASDAQ:NVDA", ImportStatus.ACCEPTED),
            ("NVDA.US", "NASDAQ:NVDA", ImportStatus.ACCEPTED),
            ("ABC", None, ImportStatus.AMBIGUOUS),
            ("600519", None, ImportStatus.UNSUPPORTED_MARKET),
            ("???", None, ImportStatus.REJECTED),
        )
    )
    assert tuple(row.status for row in rows) == (
        ImportStatus.ACCEPTED,
        ImportStatus.DUPLICATE,
        ImportStatus.AMBIGUOUS,
        ImportStatus.UNSUPPORTED_MARKET,
        ImportStatus.REJECTED,
    )
    _approve(service, rows)
    assert tuple(item.security_id for item in service.memberships) == ("NASDAQ:NVDA",)


def test_import_rejection_does_not_mutate_and_approved_metadata_is_merged() -> None:
    service = _service()
    rows = service.classify_import((("NVDA", "NASDAQ:NVDA", ImportStatus.ACCEPTED),))
    with pytest.raises(PermissionError):
        service.approve_import(
            rows,
            actor_owner_id="owner-a",
            approved=False,
            idempotency_key="rejected",
            source_type="paste",
            source_reference="conversation-1",
            imported_at=NOW,
        )
    assert service.memberships == ()

    first = _approve(service, rows)
    assert _approve(service, rows) is first
    second_rows = service.classify_import((("NVDA", "NASDAQ:NVDA", ImportStatus.ACCEPTED),))
    service.approve_import(
        second_rows,
        actor_owner_id="owner-a",
        approved=True,
        idempotency_key="import-2",
        source_type="research",
        source_reference="artifact-9",
        imported_at=NOW,
        tags={"NASDAQ:NVDA": frozenset({"semiconductor"})},
        notes={"NASDAQ:NVDA": ("首批导入", "研究候选")},
    )
    membership: Membership = next(iter(service.memberships))
    assert membership.tags == frozenset({"AI", "semiconductor"})
    assert membership.notes == ("首批导入", "研究候选")
    assert tuple(item.source_type for item in membership.provenance) == ("paste", "research")


def test_import_idempotency_is_atomic_under_concurrency_and_binds_payload() -> None:
    service = _service()
    rows = service.classify_import((("NVDA", "NASDAQ:NVDA", ImportStatus.ACCEPTED),))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: _approve(service, rows), range(2)))
    assert results[0] is results[1]
    assert len(service.memberships) == 1

    altered = service.classify_import((("MSFT", "NASDAQ:MSFT", ImportStatus.ACCEPTED),))
    with pytest.raises(IdempotencyConflictError):
        _approve(service, altered)
    assert tuple(item.security_id for item in service.memberships) == ("NASDAQ:NVDA",)


def test_classification_suggestion_changes_group_only_after_acceptance_and_can_be_edited() -> None:
    service = _service()
    rows = service.classify_import((("NVDA", "NASDAQ:NVDA", ImportStatus.ACCEPTED),))
    _approve(service, rows)
    service.create_group(actor_owner_id="owner-a", group_id="ai", name="AI")
    service.create_group(actor_owner_id="owner-a", group_id="chips", name="芯片")
    suggestion = ClassificationSuggestion(
        "suggestion-1", "NASDAQ:NVDA", "ai", "research-artifact-1"
    )
    assert service.get_group(actor_owner_id="owner-a", group_id="ai").security_ids == frozenset()

    accepted = service.accept_suggestion(
        suggestion,
        actor_owner_id="owner-a",
        idempotency_key="classification-1",
        group_id="chips",
    )
    assert accepted.accepted_group_id == "chips"
    assert accepted.decided_by == "owner-a"
    assert (
        service.get_suggestion(actor_owner_id="owner-a", suggestion_id="suggestion-1") == accepted
    )
    assert service.get_group(actor_owner_id="owner-a", group_id="chips").security_ids == frozenset(
        {"NASDAQ:NVDA"}
    )


def test_frozen_universe_is_immutable_and_owner_scoped() -> None:
    service = _service()
    rows = service.classify_import((("NVDA", "NASDAQ:NVDA", ImportStatus.ACCEPTED),))
    _approve(service, rows)
    snapshot = service.freeze_universe(actor_owner_id="owner-a", created_at=NOW)

    more_rows = service.classify_import((("MSFT", "NASDAQ:MSFT", ImportStatus.ACCEPTED),))
    _approve(service, more_rows, key="import-2")
    assert snapshot.security_ids == ("NASDAQ:NVDA",)
    assert service.get_snapshot(
        actor_owner_id="owner-a", snapshot_id=snapshot.snapshot_id
    ).security_ids == ("NASDAQ:NVDA",)
    assert service.freeze_universe(actor_owner_id="owner-a", created_at=NOW).security_ids == (
        "NASDAQ:MSFT",
        "NASDAQ:NVDA",
    )
    with pytest.raises(PermissionError):
        service.get_snapshot(actor_owner_id="owner-b", snapshot_id=snapshot.snapshot_id)


def test_classification_approval_is_idempotent_and_concurrent_group_updates_are_atomic() -> None:
    service = _service()
    rows = service.classify_import(
        (
            ("NVDA", "NASDAQ:NVDA", ImportStatus.ACCEPTED),
            ("MSFT", "NASDAQ:MSFT", ImportStatus.ACCEPTED),
        )
    )
    _approve(service, rows)
    service.create_group(actor_owner_id="owner-a", group_id="ai", name="AI")
    suggestions = (
        ClassificationSuggestion("s-1", "NASDAQ:NVDA", "ai", "artifact-1"),
        ClassificationSuggestion("s-2", "NASDAQ:MSFT", "ai", "artifact-1"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda item: service.accept_suggestion(
                    item,
                    actor_owner_id="owner-a",
                    idempotency_key=f"accept:{item.suggestion_id}",
                ),
                suggestions,
            )
        )
    assert service.get_group(actor_owner_id="owner-a", group_id="ai").security_ids == frozenset(
        {"NASDAQ:MSFT", "NASDAQ:NVDA"}
    )
    assert (
        service.accept_suggestion(
            suggestions[0], actor_owner_id="owner-a", idempotency_key="accept:s-1"
        )
        is results[0]
    )
    with pytest.raises(IdempotencyConflictError):
        service.accept_suggestion(
            ClassificationSuggestion("s-1", "NASDAQ:NVDA", "ai", "artifact-2"),
            actor_owner_id="owner-a",
            idempotency_key="accept:s-1",
        )
