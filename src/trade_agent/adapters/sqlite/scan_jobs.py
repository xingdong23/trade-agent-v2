"""SQLite lease 驱动的扫描任务与单证券执行单元存储。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Connection, func, insert, select, update

from trade_agent.core.llm.contracts import JsonValue

from .database import SQLiteDatabase
from .json_support import dump_json, load_json, payload_hash
from .schema import ScanJobRecord, ScanUnitRecord


class ScanJobError(RuntimeError):
    """扫描任务状态、owner 或 lease 不满足操作前置条件。"""


class ScanIdempotencyConflictError(RuntimeError):
    """同一扫描或执行单元被不同 payload 重放。"""


@dataclass(frozen=True, slots=True)
class ScanUnitInput:
    security_id: str
    evaluation_key: str
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ClaimedScanUnit:
    unit_id: str
    scan_id: str
    owner_id: str
    security_id: str
    evaluation_key: str
    payload: Mapping[str, JsonValue]
    attempts: int


@dataclass(frozen=True, slots=True)
class ScanProgress:
    scan_id: str
    status: str
    total: int
    queued: int
    running: int
    completed: int
    failed: int
    cancelled: int


@dataclass(frozen=True, slots=True)
class PersistedScanResult:
    scan_id: str
    security_id: str
    status: str
    result: Mapping[str, JsonValue] | None
    error: str | None


class SQLiteScanJobStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def create_scan(
        self,
        *,
        owner_id: str,
        scan_id: str,
        payload: Mapping[str, JsonValue],
        units: Sequence[ScanUnitInput],
        max_attempts: int = 3,
    ) -> bool:
        if not units:
            raise ValueError("扫描至少需要一个证券执行单元")
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        security_ids = [unit.security_id for unit in units]
        evaluation_keys = [unit.evaluation_key for unit in units]
        if len(set(security_ids)) != len(security_ids):
            raise ValueError("扫描证券集合不能重复")
        if len(set(evaluation_keys)) != len(evaluation_keys):
            raise ValueError("扫描 evaluation key 不能重复")
        digest = payload_hash(
            {
                "payload": dict(payload),
                "units": [
                    {
                        "security_id": unit.security_id,
                        "evaluation_key": unit.evaluation_key,
                        "payload": dict(unit.payload),
                    }
                    for unit in units
                ],
                "max_attempts": max_attempts,
            }
        )
        now = _now()
        with self._database.write_transaction() as connection:
            existing = (
                connection.execute(
                    select(ScanJobRecord.owner_id, ScanJobRecord.input_hash).where(
                        ScanJobRecord.scan_id == scan_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if str(existing["owner_id"]) != owner_id or str(existing["input_hash"]) != digest:
                    raise ScanIdempotencyConflictError("scan_id 已绑定其他 owner 或输入")
                return False
            connection.execute(
                insert(ScanJobRecord).values(
                    scan_id=scan_id,
                    owner_id=owner_id,
                    status="queued",
                    input_hash=digest,
                    payload_json=dump_json(payload),
                    total_units=len(units),
                    completed_units=0,
                    failed_units=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                insert(ScanUnitRecord),
                [
                    {
                        "unit_id": str(uuid4()),
                        "scan_id": scan_id,
                        "owner_id": owner_id,
                        "security_id": unit.security_id,
                        "evaluation_key": unit.evaluation_key,
                        "status": "queued",
                        "payload_json": dump_json(unit.payload),
                        "attempts": 0,
                        "max_attempts": max_attempts,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for unit in units
                ],
            )
        return True

    def claim(self, *, worker_id: str, lease_seconds: int = 60) -> ClaimedScanUnit | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._database.write_transaction() as connection:
            exhausted_scan_ids = tuple(
                str(value)
                for value in connection.execute(
                    select(ScanUnitRecord.scan_id)
                    .where(
                        ScanUnitRecord.status == "running",
                        ScanUnitRecord.attempts >= ScanUnitRecord.max_attempts,
                        ScanUnitRecord.lease_expires_at < now.isoformat(),
                    )
                    .distinct()
                ).scalars()
            )
            if exhausted_scan_ids:
                connection.execute(
                    update(ScanUnitRecord)
                    .where(
                        ScanUnitRecord.scan_id.in_(exhausted_scan_ids),
                        ScanUnitRecord.status == "running",
                        ScanUnitRecord.attempts >= ScanUnitRecord.max_attempts,
                        ScanUnitRecord.lease_expires_at < now.isoformat(),
                    )
                    .values(
                        status="failed",
                        last_error="worker lease 过期且重试预算耗尽",
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now.isoformat(),
                        completed_at=now.isoformat(),
                    )
                )
                for scan_id in exhausted_scan_ids:
                    self._refresh_job(connection, scan_id, now.isoformat())
            candidate = connection.execute(
                select(ScanUnitRecord.unit_id)
                .join(ScanJobRecord, ScanJobRecord.scan_id == ScanUnitRecord.scan_id)
                .where(
                    ScanJobRecord.status.in_(("queued", "running")),
                    ScanUnitRecord.status.in_(("queued", "running")),
                    ScanUnitRecord.attempts < ScanUnitRecord.max_attempts,
                    (ScanUnitRecord.lease_expires_at.is_(None))
                    | (ScanUnitRecord.lease_expires_at < now.isoformat()),
                )
                .order_by(ScanUnitRecord.created_at, ScanUnitRecord.security_id)
                .limit(1)
            ).scalar_one_or_none()
            if candidate is None:
                return None
            updated = connection.execute(
                update(ScanUnitRecord)
                .where(
                    ScanUnitRecord.unit_id == candidate,
                    ScanUnitRecord.status.in_(("queued", "running")),
                    ScanUnitRecord.attempts < ScanUnitRecord.max_attempts,
                    (ScanUnitRecord.lease_expires_at.is_(None))
                    | (ScanUnitRecord.lease_expires_at < now.isoformat()),
                )
                .values(
                    status="running",
                    lease_owner=worker_id,
                    lease_expires_at=expires_at.isoformat(),
                    attempts=ScanUnitRecord.attempts + 1,
                    updated_at=now.isoformat(),
                )
            )
            if updated.rowcount != 1:
                return None
            row = (
                connection.execute(
                    select(
                        ScanUnitRecord.unit_id,
                        ScanUnitRecord.scan_id,
                        ScanUnitRecord.owner_id,
                        ScanUnitRecord.security_id,
                        ScanUnitRecord.evaluation_key,
                        ScanUnitRecord.payload_json,
                        ScanUnitRecord.attempts,
                    ).where(ScanUnitRecord.unit_id == candidate)
                )
                .mappings()
                .one()
            )
            connection.execute(
                update(ScanJobRecord)
                .where(ScanJobRecord.scan_id == row["scan_id"], ScanJobRecord.status == "queued")
                .values(status="running", updated_at=now.isoformat())
            )
            return ClaimedScanUnit(
                unit_id=str(row["unit_id"]),
                scan_id=str(row["scan_id"]),
                owner_id=str(row["owner_id"]),
                security_id=str(row["security_id"]),
                evaluation_key=str(row["evaluation_key"]),
                payload=load_json(str(row["payload_json"])),
                attempts=int(row["attempts"]),
            )

    def heartbeat(self, *, unit_id: str, worker_id: str, lease_seconds: int = 60) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        now = datetime.now(UTC)
        with self._database.write_transaction() as connection:
            updated = connection.execute(
                update(ScanUnitRecord)
                .where(
                    ScanUnitRecord.unit_id == unit_id,
                    ScanUnitRecord.status == "running",
                    ScanUnitRecord.lease_owner == worker_id,
                    ScanUnitRecord.lease_expires_at >= now.isoformat(),
                )
                .values(
                    lease_expires_at=(now + timedelta(seconds=lease_seconds)).isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            if updated.rowcount != 1:
                raise ScanJobError("scan unit lease 已失效或不属于当前 worker")

    def complete(
        self,
        *,
        unit_id: str,
        worker_id: str,
        result: Mapping[str, JsonValue],
    ) -> bool:
        result_digest = payload_hash(result)
        now = datetime.now(UTC).isoformat()
        with self._database.write_transaction() as connection:
            existing = (
                connection.execute(
                    select(
                        ScanUnitRecord.scan_id,
                        ScanUnitRecord.status,
                        ScanUnitRecord.result_hash,
                    ).where(ScanUnitRecord.unit_id == unit_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise ScanJobError("scan unit 不存在")
            if existing["status"] == "completed":
                if existing["result_hash"] != result_digest:
                    raise ScanIdempotencyConflictError("已完成 scan unit 的结果发生变化")
                return False
            updated = connection.execute(
                update(ScanUnitRecord)
                .where(
                    ScanUnitRecord.unit_id == unit_id,
                    ScanUnitRecord.status == "running",
                    ScanUnitRecord.lease_owner == worker_id,
                    ScanUnitRecord.lease_expires_at >= now,
                )
                .values(
                    status="completed",
                    result_json=dump_json(result),
                    result_hash=result_digest,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                    completed_at=now,
                )
            )
            if updated.rowcount != 1:
                raise ScanJobError("scan unit lease 已失效或不属于当前 worker")
            self._refresh_job(connection, str(existing["scan_id"]), now)
            return True

    def release_or_fail(self, *, unit_id: str, worker_id: str, error: str) -> str:
        now = datetime.now(UTC).isoformat()
        with self._database.write_transaction() as connection:
            row = (
                connection.execute(
                    select(
                        ScanUnitRecord.scan_id,
                        ScanUnitRecord.attempts,
                        ScanUnitRecord.max_attempts,
                    ).where(
                        ScanUnitRecord.unit_id == unit_id,
                        ScanUnitRecord.status == "running",
                        ScanUnitRecord.lease_owner == worker_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ScanJobError("scan unit lease 不属于当前 worker")
            exhausted = int(row["attempts"]) >= int(row["max_attempts"])
            status = "failed" if exhausted else "queued"
            connection.execute(
                update(ScanUnitRecord)
                .where(ScanUnitRecord.unit_id == unit_id)
                .values(
                    status=status,
                    last_error=error,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                    completed_at=now if exhausted else None,
                )
            )
            self._refresh_job(connection, str(row["scan_id"]), now)
            return status

    def cancel(self, *, owner_id: str, scan_id: str) -> None:
        now = _now()
        with self._database.write_transaction() as connection:
            updated = connection.execute(
                update(ScanJobRecord)
                .where(
                    ScanJobRecord.scan_id == scan_id,
                    ScanJobRecord.owner_id == owner_id,
                    ScanJobRecord.status.in_(("queued", "running")),
                )
                .values(status="cancelled", updated_at=now, completed_at=now)
            )
            if updated.rowcount != 1:
                raise ScanJobError("scan 不存在、不属于当前 owner 或已进入终态")
            connection.execute(
                update(ScanUnitRecord)
                .where(
                    ScanUnitRecord.scan_id == scan_id,
                    ScanUnitRecord.status.in_(("queued", "running")),
                )
                .values(
                    status="cancelled",
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                    completed_at=now,
                )
            )

    def progress(self, *, owner_id: str, scan_id: str) -> ScanProgress:
        with self._database.read_connection() as connection:
            job = (
                connection.execute(
                    select(ScanJobRecord.status, ScanJobRecord.total_units).where(
                        ScanJobRecord.scan_id == scan_id, ScanJobRecord.owner_id == owner_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if job is None:
                raise ScanJobError("scan 不存在或不属于当前 owner")
            counts: dict[str, int] = {
                str(status): int(count)
                for status, count in connection.execute(
                    select(ScanUnitRecord.status, func.count())
                    .where(
                        ScanUnitRecord.scan_id == scan_id,
                        ScanUnitRecord.owner_id == owner_id,
                    )
                    .group_by(ScanUnitRecord.status)
                ).all()
            }
        return ScanProgress(
            scan_id,
            str(job["status"]),
            int(job["total_units"]),
            int(counts.get("queued", 0)),
            int(counts.get("running", 0)),
            int(counts.get("completed", 0)),
            int(counts.get("failed", 0)),
            int(counts.get("cancelled", 0)),
        )

    def list_results(self, *, owner_id: str, scan_id: str) -> tuple[PersistedScanResult, ...]:
        self.progress(owner_id=owner_id, scan_id=scan_id)
        with self._database.read_connection() as connection:
            rows = connection.execute(
                select(
                    ScanUnitRecord.security_id,
                    ScanUnitRecord.status,
                    ScanUnitRecord.result_json,
                    ScanUnitRecord.last_error,
                )
                .where(
                    ScanUnitRecord.scan_id == scan_id,
                    ScanUnitRecord.owner_id == owner_id,
                    ScanUnitRecord.status.in_(("completed", "failed", "cancelled")),
                )
                .order_by(ScanUnitRecord.security_id)
            ).mappings()
            return tuple(
                PersistedScanResult(
                    scan_id,
                    str(row["security_id"]),
                    str(row["status"]),
                    load_json(str(row["result_json"])) if row["result_json"] is not None else None,
                    str(row["last_error"]) if row["last_error"] is not None else None,
                )
                for row in rows
            )

    @staticmethod
    def _refresh_job(connection: Connection, scan_id: str, now: str) -> None:
        counts: dict[str, int] = {
            str(status): int(count)
            for status, count in connection.execute(
                select(ScanUnitRecord.status, func.count())
                .where(ScanUnitRecord.scan_id == scan_id)
                .group_by(ScanUnitRecord.status)
            ).all()
        }
        active = int(counts.get("queued", 0)) + int(counts.get("running", 0))
        failed = int(counts.get("failed", 0))
        completed = int(counts.get("completed", 0))
        values: dict[str, object] = {
            "completed_units": completed,
            "failed_units": failed,
            "updated_at": now,
        }
        if active == 0:
            values["status"] = "failed" if failed else "completed"
            values["completed_at"] = now
        connection.execute(
            update(ScanJobRecord).where(ScanJobRecord.scan_id == scan_id).values(**values)
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()
