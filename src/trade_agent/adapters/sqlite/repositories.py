"""Owner-scoped repositories and cross-cutting stores."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Select, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from trade_agent.capabilities.contracts import CapabilityResult, ConcurrentWriteError
from trade_agent.core.events import AuditEvent, RunEvent
from trade_agent.core.llm.contracts import JsonValue

from .database import SQLiteDatabase
from .json_support import dump_json, load_json
from .schema import (
    AggregateRecord,
    AuditRecord,
    CommandRecord,
    JobRecord,
    OutboxRecord,
    RunEventRecord,
    RunRecord,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class IdempotencyConflictError(RuntimeError):
    pass


class EventSequenceError(RuntimeError):
    pass


class JobLeaseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    status: str
    result: Mapping[str, JsonValue] | None
    reused: bool


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: str
    owner_id: str
    job_type: str
    payload: Mapping[str, JsonValue]
    attempts: int


class SQLiteAggregateRepository:
    def __init__(self, database: SQLiteDatabase, aggregate_type: str) -> None:
        self._database = database
        self._aggregate_type = aggregate_type

    def save(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
        expected_version: int,
        payload: Mapping[str, JsonValue],
        schema_version: int = 1,
    ) -> CapabilityResult:
        new_version = expected_version + 1
        with self._database.write_transaction() as connection:
            latest = connection.execute(self._latest(owner_id, aggregate_id)).scalar_one_or_none()
            actual = 0 if latest is None else int(latest)
            if actual != expected_version:
                raise ConcurrentWriteError(
                    f"版本冲突: expected={expected_version}, actual={actual}"
                )
            connection.execute(
                insert(AggregateRecord).values(
                    owner_id=owner_id,
                    aggregate_type=self._aggregate_type,
                    aggregate_id=aggregate_id,
                    version=new_version,
                    schema_version=schema_version,
                    payload_json=dump_json(payload),
                    created_at=_now(),
                )
            )
        return CapabilityResult(aggregate_id, new_version, dict(payload))

    def get(self, owner_id: str, aggregate_id: str) -> CapabilityResult | None:
        with self._database.read_connection() as connection:
            row = (
                connection.execute(
                    select(
                        AggregateRecord.aggregate_id,
                        AggregateRecord.version,
                        AggregateRecord.payload_json,
                    )
                    .where(
                        AggregateRecord.owner_id == owner_id,
                        AggregateRecord.aggregate_type == self._aggregate_type,
                        AggregateRecord.aggregate_id == aggregate_id,
                    )
                    .order_by(AggregateRecord.version.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return CapabilityResult(
            str(row["aggregate_id"]),
            int(row["version"]),
            load_json(str(row["payload_json"])),
        )

    def _latest(self, owner_id: str, aggregate_id: str) -> Select[tuple[int]]:
        return select(func.max(AggregateRecord.version)).where(
            AggregateRecord.owner_id == owner_id,
            AggregateRecord.aggregate_type == self._aggregate_type,
            AggregateRecord.aggregate_id == aggregate_id,
        )


class SQLiteCommandStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def begin(self, *, owner_id: str, idempotency_key: str, payload_hash: str) -> CommandReceipt:
        command_id = str(uuid4())
        try:
            with self._database.write_transaction() as connection:
                connection.execute(
                    insert(CommandRecord).values(
                        command_id=command_id,
                        owner_id=owner_id,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                        status="pending",
                        created_at=_now(),
                    )
                )
        except IntegrityError:
            with self._database.read_connection() as connection:
                existing = (
                    connection.execute(
                        select(
                            CommandRecord.command_id,
                            CommandRecord.payload_hash,
                            CommandRecord.status,
                            CommandRecord.result_json,
                        ).where(
                            CommandRecord.owner_id == owner_id,
                            CommandRecord.idempotency_key == idempotency_key,
                        )
                    )
                    .mappings()
                    .one()
                )
            if existing["payload_hash"] != payload_hash:
                raise IdempotencyConflictError("幂等 key 已绑定不同 payload") from None
            result_json = existing["result_json"]
            result = None if result_json is None else load_json(str(result_json))
            return CommandReceipt(
                str(existing["command_id"]), str(existing["status"]), result, True
            )
        return CommandReceipt(command_id, "pending", None, False)

    def complete(
        self, *, owner_id: str, command_id: str, result: Mapping[str, JsonValue]
    ) -> CommandReceipt:
        with self._database.write_transaction() as connection:
            updated = connection.execute(
                update(CommandRecord)
                .where(
                    CommandRecord.owner_id == owner_id,
                    CommandRecord.command_id == command_id,
                    CommandRecord.status == "pending",
                )
                .values(status="completed", result_json=dump_json(result), completed_at=_now())
            )
            if updated.rowcount == 0:
                current = (
                    connection.execute(
                        select(
                            CommandRecord.command_id,
                            CommandRecord.status,
                            CommandRecord.result_json,
                        ).where(
                            CommandRecord.owner_id == owner_id,
                            CommandRecord.command_id == command_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if current is None:
                    raise PermissionError("command 不存在或不属于当前 owner")
                result_json = current["result_json"]
                existing = None if result_json is None else load_json(str(result_json))
                return CommandReceipt(
                    str(current["command_id"]), str(current["status"]), existing, True
                )
        return CommandReceipt(command_id, "completed", dict(result), False)


class SQLiteEventStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def start_run(self, *, owner_id: str, run_id: str, thread_id: str) -> None:
        with self._database.write_transaction() as connection:
            connection.execute(
                insert(RunRecord).values(
                    run_id=run_id,
                    owner_id=owner_id,
                    thread_id=thread_id,
                    status="running",
                    created_at=_now(),
                )
            )

    def append(
        self,
        *,
        owner_id: str,
        event: RunEvent,
        audit: AuditEvent | None = None,
        outbox_topic: str | None = None,
    ) -> None:
        with self._database.write_transaction() as connection:
            run_owner = connection.execute(
                select(RunRecord.owner_id).where(RunRecord.run_id == event.run_id)
            ).scalar_one_or_none()
            if run_owner != owner_id:
                raise PermissionError("run 不存在或不属于当前 owner")
            latest = connection.execute(
                select(func.max(RunEventRecord.sequence)).where(
                    RunEventRecord.run_id == event.run_id
                )
            ).scalar_one_or_none()
            expected_sequence = 1 if latest is None else int(latest) + 1
            if event.sequence != expected_sequence:
                raise EventSequenceError(
                    "event sequence 必须连续: "
                    f"expected={expected_sequence}, actual={event.sequence}"
                )
            if audit is not None and audit.owner_id != owner_id:
                raise PermissionError("audit owner 与 event owner 不一致")
            connection.execute(
                insert(RunEventRecord).values(
                    event_id=event.event_id,
                    run_id=event.run_id,
                    owner_id=owner_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    schema_version=event.schema_version,
                    payload_json=dump_json(event.payload),
                    occurred_at=event.occurred_at.isoformat(),
                )
            )
            if audit is not None:
                connection.execute(
                    insert(AuditRecord).values(
                        audit_id=audit.audit_id,
                        owner_id=audit.owner_id,
                        actor_id=audit.actor_id,
                        action=audit.action,
                        subject_type=audit.subject_type,
                        subject_id=audit.subject_id,
                        subject_version=audit.subject_version,
                        payload_hash=audit.payload_hash,
                        occurred_at=audit.occurred_at.isoformat(),
                    )
                )
            if outbox_topic is not None:
                connection.execute(
                    insert(OutboxRecord).values(
                        outbox_id=str(uuid4()),
                        owner_id=owner_id,
                        topic=outbox_topic,
                        payload_json=dump_json(event.payload),
                        schema_version=event.schema_version,
                        created_at=_now(),
                    )
                )

    def replay(self, *, owner_id: str, run_id: str, after_sequence: int) -> list[RunEvent]:
        with self._database.read_connection() as connection:
            rows = connection.execute(
                select(
                    RunEventRecord.event_id,
                    RunEventRecord.run_id,
                    RunEventRecord.sequence,
                    RunEventRecord.event_type,
                    RunEventRecord.payload_json,
                    RunEventRecord.schema_version,
                    RunEventRecord.occurred_at,
                )
                .where(
                    RunEventRecord.owner_id == owner_id,
                    RunEventRecord.run_id == run_id,
                    RunEventRecord.sequence > after_sequence,
                )
                .order_by(RunEventRecord.sequence)
            ).mappings()
            return [
                RunEvent(
                    event_id=str(row["event_id"]),
                    run_id=str(row["run_id"]),
                    sequence=int(row["sequence"]),
                    event_type=str(row["event_type"]),
                    payload=load_json(str(row["payload_json"])),
                    schema_version=int(row["schema_version"]),
                    occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                )
                for row in rows
            ]


class SQLiteJobStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def enqueue(
        self,
        *,
        owner_id: str,
        job_id: str,
        job_type: str,
        payload: Mapping[str, JsonValue],
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        now = _now()
        with self._database.write_transaction() as connection:
            connection.execute(
                insert(JobRecord).values(
                    job_id=job_id,
                    owner_id=owner_id,
                    job_type=job_type,
                    status="queued",
                    payload_json=dump_json(payload),
                    attempts=0,
                    max_attempts=max_attempts,
                    created_at=now,
                    updated_at=now,
                )
            )

    def claim(self, *, worker_id: str, lease_seconds: int = 60) -> ClaimedJob | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        with self._database.write_transaction() as connection:
            candidate = connection.execute(
                select(JobRecord.job_id)
                .where(
                    JobRecord.status.in_(("queued", "running")),
                    JobRecord.attempts < JobRecord.max_attempts,
                    (JobRecord.lease_expires_at.is_(None))
                    | (JobRecord.lease_expires_at < now.isoformat()),
                )
                .order_by(JobRecord.created_at)
                .limit(1)
            ).scalar_one_or_none()
            if candidate is None:
                return None
            updated = connection.execute(
                update(JobRecord)
                .where(
                    JobRecord.job_id == candidate,
                    JobRecord.status.in_(("queued", "running")),
                    JobRecord.attempts < JobRecord.max_attempts,
                    (JobRecord.lease_expires_at.is_(None))
                    | (JobRecord.lease_expires_at < now.isoformat()),
                )
                .values(
                    status="running",
                    lease_owner=worker_id,
                    lease_expires_at=expires.isoformat(),
                    attempts=JobRecord.attempts + 1,
                    updated_at=now.isoformat(),
                )
            )
            if updated.rowcount != 1:
                return None
            row = (
                connection.execute(
                    select(
                        JobRecord.job_id,
                        JobRecord.owner_id,
                        JobRecord.job_type,
                        JobRecord.payload_json,
                        JobRecord.attempts,
                    ).where(JobRecord.job_id == candidate)
                )
                .mappings()
                .one()
            )
            return ClaimedJob(
                str(row["job_id"]),
                str(row["owner_id"]),
                str(row["job_type"]),
                load_json(str(row["payload_json"])),
                int(row["attempts"]),
            )

    def heartbeat(self, *, job_id: str, worker_id: str, lease_seconds: int = 60) -> None:
        now = datetime.now(UTC)
        with self._database.write_transaction() as connection:
            updated = connection.execute(
                update(JobRecord)
                .where(
                    JobRecord.job_id == job_id,
                    JobRecord.status == "running",
                    JobRecord.lease_owner == worker_id,
                    JobRecord.lease_expires_at >= now.isoformat(),
                )
                .values(
                    lease_expires_at=(now + timedelta(seconds=lease_seconds)).isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            if updated.rowcount != 1:
                raise JobLeaseError("job lease 已失效或不属于当前 worker")

    def complete(self, *, job_id: str, worker_id: str) -> None:
        self._finish(job_id=job_id, worker_id=worker_id, status="completed")

    def fail(self, *, job_id: str, worker_id: str, error: str) -> None:
        self._finish(job_id=job_id, worker_id=worker_id, status="failed", error=error)

    def release(self, *, job_id: str, worker_id: str, error: str) -> None:
        """释放可重试任务; 达到预算后直接进入失败终态。"""
        with self._database.write_transaction() as connection:
            row = (
                connection.execute(
                    select(JobRecord.attempts, JobRecord.max_attempts).where(
                        JobRecord.job_id == job_id,
                        JobRecord.status == "running",
                        JobRecord.lease_owner == worker_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise JobLeaseError("job lease 不属于当前 worker")
            exhausted = int(row["attempts"]) >= int(row["max_attempts"])
            now = _now()
            connection.execute(
                update(JobRecord)
                .where(JobRecord.job_id == job_id, JobRecord.lease_owner == worker_id)
                .values(
                    status="failed" if exhausted else "queued",
                    last_error=error,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                    completed_at=now if exhausted else None,
                )
            )

    def cancel(self, *, owner_id: str, job_id: str) -> None:
        with self._database.write_transaction() as connection:
            updated = connection.execute(
                update(JobRecord)
                .where(
                    JobRecord.job_id == job_id,
                    JobRecord.owner_id == owner_id,
                    JobRecord.status.in_(("queued", "running")),
                )
                .values(
                    status="cancelled",
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=_now(),
                    completed_at=_now(),
                )
            )
            if updated.rowcount != 1:
                raise JobLeaseError("job 不存在、不属于当前 owner 或已进入终态")

    def _finish(
        self, *, job_id: str, worker_id: str, status: str, error: str | None = None
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._database.write_transaction() as connection:
            updated = connection.execute(
                update(JobRecord)
                .where(
                    JobRecord.job_id == job_id,
                    JobRecord.status == "running",
                    JobRecord.lease_owner == worker_id,
                    JobRecord.lease_expires_at >= now,
                )
                .values(
                    status=status,
                    last_error=error,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                    completed_at=now,
                )
            )
            if updated.rowcount != 1:
                raise JobLeaseError("job lease 已失效或不属于当前 worker")
