"""Integration coverage for the SQLite cross-cutting foundation."""

from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import pytest
from sqlalchemy.exc import IntegrityError

from trade_agent.adapters.sqlite import (
    ConcurrentWriteError,
    EventSequenceError,
    IdempotencyConflictError,
    JobLeaseError,
    SQLiteAggregateRepository,
    SQLiteCommandStore,
    SQLiteDatabase,
    SQLiteEventStore,
    backup_database,
    restore_database,
)
from trade_agent.adapters.sqlite.json_support import payload_hash
from trade_agent.adapters.sqlite.repositories import SQLiteJobStore
from trade_agent.core.events import AuditEvent, RunEvent


@pytest.fixture
def database(tmp_path: Path) -> SQLiteDatabase:
    value = SQLiteDatabase(tmp_path / "trade-agent.db", busy_timeout_ms=1_000)
    value.initialize()
    return value


def test_database_initializes_with_required_pragmas(database: SQLiteDatabase) -> None:
    health = database.health()
    assert health.integrity == "ok"
    assert health.journal_mode == "wal"
    assert health.foreign_keys is True
    assert health.schema_version == 3
    assert database.is_ready()
    assert database.path.stat().st_mode & 0o777 == 0o600


def test_uninitialized_database_reports_not_ready(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "empty.db")

    assert database.health().schema_version == 0
    assert not database.is_ready()


def test_concurrent_writes_are_serialized_and_lock_wait_is_observable(
    database: SQLiteDatabase,
) -> None:
    holder_ready = Event()
    release_holder = Event()
    writer_started = Event()
    writer_finished = Event()
    writer_wait: list[float] = []
    baseline_wait = database.health().lock_wait_seconds

    def hold_transaction() -> None:
        with database.write_transaction():
            holder_ready.set()
            assert release_holder.wait(timeout=2)

    def write_after_holder() -> None:
        writer_started.set()
        started = monotonic()
        with database.write_transaction():
            writer_wait.append(monotonic() - started)
        writer_finished.set()

    holder = Thread(target=hold_transaction)
    writer = Thread(target=write_after_holder)
    holder.start()
    assert holder_ready.wait(timeout=2)
    writer.start()
    assert writer_started.wait(timeout=2)
    assert not writer_finished.wait(timeout=0.05)
    release_holder.set()
    holder.join(timeout=2)
    writer.join(timeout=2)

    assert not holder.is_alive()
    assert not writer.is_alive()
    assert writer_wait and writer_wait[0] >= 0.05
    assert database.health().lock_wait_seconds > baseline_wait


def test_repository_enforces_owner_scope_and_optimistic_version(database: SQLiteDatabase) -> None:
    repository = SQLiteAggregateRepository(database, "watchlist")
    saved = repository.save(
        owner_id="owner-a", aggregate_id="watchlist-1", expected_version=0, payload={"name": "A"}
    )
    assert saved.version == 1
    assert repository.get("owner-a", "watchlist-1") == saved
    assert repository.get("owner-b", "watchlist-1") is None

    with pytest.raises(ConcurrentWriteError):
        repository.save(
            owner_id="owner-a",
            aggregate_id="watchlist-1",
            expected_version=0,
            payload={"name": "stale"},
        )


def test_command_result_is_reused_after_checkpoint_gap(database: SQLiteDatabase) -> None:
    store = SQLiteCommandStore(database)
    payload = {"plan_id": "plan-1"}
    digest = payload_hash(payload)
    pending = store.begin(owner_id="owner-a", idempotency_key="run:node:1", payload_hash=digest)
    completed = store.complete(
        owner_id="owner-a", command_id=pending.command_id, result={"status": "created"}
    )
    replay = store.begin(owner_id="owner-a", idempotency_key="run:node:1", payload_hash=digest)

    assert completed.status == "completed"
    assert replay.reused is True
    assert replay.result == {"status": "created"}

    with pytest.raises(IdempotencyConflictError):
        store.begin(
            owner_id="owner-a",
            idempotency_key="run:node:1",
            payload_hash=payload_hash({"plan_id": "different"}),
        )


def test_events_are_atomic_ordered_and_owner_scoped(database: SQLiteDatabase) -> None:
    store = SQLiteEventStore(database)
    store.start_run(owner_id="owner-a", run_id="run-1", thread_id="thread-1")
    occurred_at = datetime.now(UTC)
    event = RunEvent("event-1", "run-1", 1, "run.started", {"ok": True}, occurred_at)
    audit = AuditEvent(
        "audit-1",
        "owner-a",
        "owner-a",
        "start",
        "run",
        "run-1",
        occurred_at,
    )
    store.append(owner_id="owner-a", event=event, audit=audit, outbox_topic="run.event")

    assert store.replay(owner_id="owner-a", run_id="run-1", after_sequence=0) == [event]
    assert store.replay(owner_id="owner-b", run_id="run-1", after_sequence=0) == []


def test_event_sequence_is_contiguous_and_failed_append_rolls_back(
    database: SQLiteDatabase,
) -> None:
    store = SQLiteEventStore(database)
    store.start_run(owner_id="owner-a", run_id="run-1", thread_id="thread-1")
    occurred_at = datetime.now(UTC)
    first = RunEvent("event-1", "run-1", 1, "run.started", {}, occurred_at)
    audit = AuditEvent("audit-1", "owner-a", "owner-a", "start", "run", "run-1", occurred_at)
    store.append(owner_id="owner-a", event=first, audit=audit)

    with pytest.raises(EventSequenceError):
        store.append(
            owner_id="owner-a",
            event=RunEvent("event-3", "run-1", 3, "run.skipped", {}, occurred_at),
        )

    with pytest.raises(IntegrityError):
        store.append(
            owner_id="owner-a",
            event=RunEvent("event-2", "run-1", 2, "run.progress", {}, occurred_at),
            audit=audit,
            outbox_topic="run.event",
        )

    assert store.replay(owner_id="owner-a", run_id="run-1", after_sequence=0) == [first]


def test_job_claim_is_atomic_and_recoverable(database: SQLiteDatabase) -> None:
    jobs = SQLiteJobStore(database)
    jobs.enqueue(
        owner_id="owner-a",
        job_id="job-1",
        job_type="scan",
        payload={"scan": "s1"},
        max_attempts=3,
    )

    claimed = jobs.claim(worker_id="worker-a", lease_seconds=60)
    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert claimed.attempts == 1
    assert jobs.claim(worker_id="worker-b", lease_seconds=60) is None
    jobs.heartbeat(job_id="job-1", worker_id="worker-a", lease_seconds=60)
    with pytest.raises(JobLeaseError):
        jobs.complete(job_id="job-1", worker_id="worker-b")
    jobs.complete(job_id="job-1", worker_id="worker-a")
    assert jobs.claim(worker_id="worker-b", lease_seconds=60) is None


def test_job_retry_budget_and_owner_scoped_cancel(database: SQLiteDatabase) -> None:
    jobs = SQLiteJobStore(database)
    jobs.enqueue(
        owner_id="owner-a",
        job_id="job-1",
        job_type="scan",
        payload={},
        max_attempts=1,
    )
    claimed = jobs.claim(worker_id="worker-a", lease_seconds=60)
    assert claimed is not None
    jobs.release(job_id="job-1", worker_id="worker-a", error="temporary failure")
    assert jobs.claim(worker_id="worker-b", lease_seconds=60) is None
    with pytest.raises(JobLeaseError):
        jobs.cancel(owner_id="owner-b", job_id="job-1")
    with pytest.raises(JobLeaseError):
        jobs.cancel(owner_id="owner-a", job_id="job-1")

    jobs.enqueue(owner_id="owner-a", job_id="job-2", job_type="scan", payload={}, max_attempts=3)
    jobs.cancel(owner_id="owner-a", job_id="job-2")


def test_online_backup_and_restore_smoke(database: SQLiteDatabase, tmp_path: Path) -> None:
    repository = SQLiteAggregateRepository(database, "strategy")
    repository.save(
        owner_id="owner-a", aggregate_id="strategy-1", expected_version=0, payload={"name": "S"}
    )
    backup = tmp_path / "backups" / "snapshot.db"
    restored = tmp_path / "restored" / "trade-agent.db"

    backup_database(database.path, backup)
    restore_database(backup, restored)
    restored_database = SQLiteDatabase(restored)
    restored_database.initialize()

    assert (
        SQLiteAggregateRepository(restored_database, "strategy").get("owner-a", "strategy-1")
        is not None
    )
    assert restored_database.health().integrity == "ok"

    with pytest.raises(FileExistsError):
        restore_database(backup, restored)
