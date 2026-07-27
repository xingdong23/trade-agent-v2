"""扫描 lease、幂等结果、恢复、取消和部分结果保留测试。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from trade_agent.adapters.sqlite import (
    ScanIdempotencyConflictError,
    ScanJobError,
    ScanUnitInput,
    SQLiteDatabase,
    SQLiteScanJobStore,
)
from trade_agent.adapters.sqlite.schema import ScanUnitRecord
from trade_agent.core.llm.contracts import JsonValue


@pytest.fixture
def store(tmp_path: Path) -> SQLiteScanJobStore:
    database = SQLiteDatabase(tmp_path / "scan.db")
    database.initialize()
    return SQLiteScanJobStore(database)


def _create(store: SQLiteScanJobStore, *, max_attempts: int = 2) -> None:
    created = store.create_scan(
        owner_id="owner-a",
        scan_id="scan-1",
        payload={"strategy_version": 3, "universe_snapshot_id": "universe-1"},
        units=(
            ScanUnitInput("US:NASDAQ:MSFT", "scan-1:MSFT:v3", {"symbol": "MSFT"}),
            ScanUnitInput("US:NASDAQ:NVDA", "scan-1:NVDA:v3", {"symbol": "NVDA"}),
        ),
        max_attempts=max_attempts,
    )
    assert created


def test_scan_claim_complete_replay_and_restart_skip_completed(
    store: SQLiteScanJobStore,
) -> None:
    _create(store)
    assert not store.create_scan(
        owner_id="owner-a",
        scan_id="scan-1",
        payload={"strategy_version": 3, "universe_snapshot_id": "universe-1"},
        units=(
            ScanUnitInput("US:NASDAQ:MSFT", "scan-1:MSFT:v3", {"symbol": "MSFT"}),
            ScanUnitInput("US:NASDAQ:NVDA", "scan-1:NVDA:v3", {"symbol": "NVDA"}),
        ),
        max_attempts=2,
    )
    with pytest.raises(ScanIdempotencyConflictError):
        store.create_scan(
            owner_id="owner-a",
            scan_id="scan-1",
            payload={"strategy_version": 3, "universe_snapshot_id": "universe-1"},
            units=(ScanUnitInput("US:NASDAQ:AAPL", "scan-1:AAPL:v3", {}),),
            max_attempts=2,
        )
    first = store.claim(worker_id="worker-a", lease_seconds=60)
    assert first is not None
    result: dict[str, JsonValue] = {
        "status": "match",
        "score": 0.82,
        "model_version_id": "model-7",
    }
    assert store.complete(unit_id=first.unit_id, worker_id="worker-a", result=result)
    assert not store.complete(unit_id=first.unit_id, worker_id="worker-a", result=result)
    with pytest.raises(ScanIdempotencyConflictError):
        store.complete(
            unit_id=first.unit_id,
            worker_id="worker-a",
            result={"status": "match", "score": 0.12},
        )

    after_restart = store.claim(worker_id="worker-b", lease_seconds=60)
    assert after_restart is not None
    assert after_restart.security_id != first.security_id
    store.complete(
        unit_id=after_restart.unit_id,
        worker_id="worker-b",
        result={"status": "non_match", "score": 0.3},
    )
    progress = store.progress(owner_id="owner-a", scan_id="scan-1")
    assert progress.status == "completed"
    assert progress.completed == 2


def test_scan_retry_budget_and_cancel_preserve_completed_results(
    store: SQLiteScanJobStore,
) -> None:
    _create(store, max_attempts=1)
    first = store.claim(worker_id="worker-a", lease_seconds=60)
    assert first is not None
    store.complete(unit_id=first.unit_id, worker_id="worker-a", result={"status": "match"})
    second = store.claim(worker_id="worker-a", lease_seconds=60)
    assert second is not None
    assert (
        store.release_or_fail(
            unit_id=second.unit_id, worker_id="worker-a", error="provider unavailable"
        )
        == "failed"
    )
    assert store.progress(owner_id="owner-a", scan_id="scan-1").status == "failed"
    results = store.list_results(owner_id="owner-a", scan_id="scan-1")
    assert {item.status for item in results} == {"completed", "failed"}

    store.create_scan(
        owner_id="owner-a",
        scan_id="scan-2",
        payload={"strategy_version": 3},
        units=(ScanUnitInput("US:NASDAQ:AAPL", "scan-2:AAPL:v3", {}),),
        max_attempts=2,
    )
    with pytest.raises(ScanJobError):
        store.cancel(owner_id="owner-b", scan_id="scan-2")
    store.cancel(owner_id="owner-a", scan_id="scan-2")
    progress = store.progress(owner_id="owner-a", scan_id="scan-2")
    assert progress.status == "cancelled"
    assert progress.cancelled == 1
    with pytest.raises(ScanJobError):
        store.progress(owner_id="owner-b", scan_id="scan-2")


def test_scan_lease_rejects_other_worker(store: SQLiteScanJobStore) -> None:
    _create(store)
    claimed = store.claim(worker_id="worker-a", lease_seconds=60)
    assert claimed is not None
    with pytest.raises(ScanJobError):
        store.complete(unit_id=claimed.unit_id, worker_id="worker-b", result={"ok": True})
    store.heartbeat(unit_id=claimed.unit_id, worker_id="worker-a", lease_seconds=60)


def test_expired_lease_is_reclaimed_and_exhausted_unit_becomes_failed(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "recovery.db")
    database.initialize()
    store = SQLiteScanJobStore(database)
    store.create_scan(
        owner_id="owner-a",
        scan_id="scan-recover",
        payload={"strategy_version": 1},
        units=(ScanUnitInput("US:NASDAQ:NVDA", "recover:NVDA:v1", {}),),
        max_attempts=2,
    )
    first = store.claim(worker_id="worker-a", lease_seconds=60)
    assert first is not None
    with database.write_transaction() as connection:
        connection.execute(
            update(ScanUnitRecord)
            .where(ScanUnitRecord.unit_id == first.unit_id)
            .values(lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        )
    reclaimed = store.claim(worker_id="worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.unit_id == first.unit_id
    assert reclaimed.attempts == 2
    with database.write_transaction() as connection:
        connection.execute(
            update(ScanUnitRecord)
            .where(ScanUnitRecord.unit_id == first.unit_id)
            .values(lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        )
    assert store.claim(worker_id="worker-c", lease_seconds=60) is None
    progress = store.progress(owner_id="owner-a", scan_id="scan-recover")
    assert progress.status == "failed"
    assert progress.failed == 1
