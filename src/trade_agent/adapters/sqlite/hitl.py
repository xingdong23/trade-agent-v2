"""SQLite HITL repository, owner scope 与 compare-and-set 状态迁移。"""

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import RowMapping, insert, select, update
from sqlalchemy.exc import IntegrityError

from trade_agent.core.hitl import (
    HumanInteraction,
    InteractionConflictError,
    InteractionExpiredError,
    InteractionStatus,
    InteractionType,
)
from trade_agent.core.llm.contracts import JsonValue

from .database import SQLiteDatabase
from .json_support import dump_json, load_json
from .schema import HitlInteractionRecord


class SQLiteHitlRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def create(self, interaction: HumanInteraction) -> HumanInteraction:
        try:
            with self._database.write_transaction() as connection:
                connection.execute(insert(HitlInteractionRecord).values(**_values(interaction)))
        except IntegrityError as exc:
            raise InteractionConflictError("interaction_id 已存在") from exc
        return interaction

    def get(self, owner_id: str, interaction_id: str) -> HumanInteraction | None:
        with self._database.read_connection() as connection:
            row = (
                connection.execute(
                    select(HitlInteractionRecord.__table__).where(
                        HitlInteractionRecord.owner_id == owner_id,
                        HitlInteractionRecord.interaction_id == interaction_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _interaction(row)

    def list_pending(self, owner_id: str) -> tuple[HumanInteraction, ...]:
        with self._database.read_connection() as connection:
            rows = connection.execute(
                select(HitlInteractionRecord.__table__)
                .where(
                    HitlInteractionRecord.owner_id == owner_id,
                    HitlInteractionRecord.status == InteractionStatus.PENDING.value,
                )
                .order_by(HitlInteractionRecord.created_at)
            ).mappings()
            return tuple(_interaction(row) for row in rows)

    def resolve(
        self,
        *,
        owner_id: str,
        interaction_id: str,
        expected_version: int,
        subject_version: int,
        payload_hash: str,
        actor_id: str,
        response: Mapping[str, JsonValue],
        resolution: str,
    ) -> HumanInteraction:
        now = datetime.now(UTC)
        with self._database.write_transaction() as connection:
            current = (
                connection.execute(
                    select(HitlInteractionRecord.__table__).where(
                        HitlInteractionRecord.owner_id == owner_id,
                        HitlInteractionRecord.interaction_id == interaction_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise PermissionError("interaction 不存在或不属于当前 owner")
            current_deadline = current["deadline"]
            if (
                current_deadline is not None
                and datetime.fromisoformat(str(current_deadline)) <= now
            ):
                connection.execute(
                    update(HitlInteractionRecord)
                    .where(
                        HitlInteractionRecord.interaction_id == interaction_id,
                        HitlInteractionRecord.version == int(current["version"]),
                        HitlInteractionRecord.status == InteractionStatus.PENDING.value,
                    )
                    .values(
                        status=InteractionStatus.EXPIRED.value,
                        version=int(current["version"]) + 1,
                        resolution="deadline_exceeded",
                        resolved_at=now.isoformat(),
                    )
                )
                expired = True
            else:
                expired = False
            if not expired and (
                str(current["status"]) != InteractionStatus.PENDING.value
                or int(current["version"]) != expected_version
                or int(current["subject_version"]) != subject_version
                or str(current["payload_hash"]) != payload_hash
            ):
                raise InteractionConflictError("interaction 状态、版本或 payload hash 已变化")
            if expired:
                changed = None
            else:
                changed = connection.execute(
                    update(HitlInteractionRecord)
                    .where(
                        HitlInteractionRecord.interaction_id == interaction_id,
                        HitlInteractionRecord.owner_id == owner_id,
                        HitlInteractionRecord.status == InteractionStatus.PENDING.value,
                        HitlInteractionRecord.version == expected_version,
                    )
                    .values(
                        status=InteractionStatus.RESOLVED.value,
                        version=expected_version + 1,
                        response_json=dump_json(response),
                        resolved_by=actor_id,
                        resolution=resolution,
                        resolved_at=now.isoformat(),
                    )
                )
            if changed is not None and changed.rowcount != 1:
                raise InteractionConflictError("interaction 已被其他客户端处理")
        if expired:
            raise InteractionExpiredError("interaction 已过期, 不会自动批准")
        resolved = self.get(owner_id, interaction_id)
        if resolved is None:  # pragma: no cover - owner 条件与更新事务相同
            raise RuntimeError("interaction 更新后不可见")
        return resolved

    def transition(
        self,
        *,
        owner_id: str,
        interaction_id: str,
        expected_version: int,
        status: InteractionStatus,
        actor_id: str | None = None,
        resolution: str | None = None,
    ) -> HumanInteraction:
        if status not in {InteractionStatus.EXPIRED, InteractionStatus.CANCELLED}:
            raise ValueError("仅允许 pending 转换到 expired 或 cancelled")
        now = datetime.now(UTC).isoformat()
        with self._database.write_transaction() as connection:
            changed = connection.execute(
                update(HitlInteractionRecord)
                .where(
                    HitlInteractionRecord.owner_id == owner_id,
                    HitlInteractionRecord.interaction_id == interaction_id,
                    HitlInteractionRecord.status == InteractionStatus.PENDING.value,
                    HitlInteractionRecord.version == expected_version,
                )
                .values(
                    status=status.value,
                    version=expected_version + 1,
                    resolved_by=actor_id,
                    resolution=resolution,
                    resolved_at=now,
                )
            )
            if changed.rowcount != 1:
                raise InteractionConflictError("interaction 不是可转换的 pending 状态")
        interaction = self.get(owner_id, interaction_id)
        if interaction is None:  # pragma: no cover
            raise RuntimeError("interaction 更新后不可见")
        return interaction


def _values(interaction: HumanInteraction) -> dict[str, object]:
    return {
        "interaction_id": interaction.interaction_id,
        "owner_id": interaction.owner_id,
        "interaction_type": interaction.interaction_type.value,
        "status": interaction.status.value,
        "version": interaction.version,
        "thread_id": interaction.thread_id,
        "run_id": interaction.run_id,
        "subject_type": interaction.subject_type,
        "subject_id": interaction.subject_id,
        "subject_version": interaction.subject_version,
        "payload_json": dump_json(interaction.payload),
        "payload_hash": interaction.payload_hash,
        "response_schema_json": dump_json(interaction.response_schema),
        "response_json": None if interaction.response is None else dump_json(interaction.response),
        "resolved_by": interaction.resolved_by,
        "resolution": interaction.resolution,
        "created_at": interaction.created_at.isoformat(),
        "deadline": interaction.deadline.isoformat() if interaction.deadline is not None else None,
        "resolved_at": (
            interaction.resolved_at.isoformat() if interaction.resolved_at is not None else None
        ),
    }


def _interaction(row: RowMapping) -> HumanInteraction:
    deadline = row["deadline"]
    response_json = row["response_json"]
    resolved_at = row["resolved_at"]
    return HumanInteraction(
        interaction_id=str(row["interaction_id"]),
        owner_id=str(row["owner_id"]),
        interaction_type=InteractionType(str(row["interaction_type"])),
        status=InteractionStatus(str(row["status"])),
        payload=load_json(str(row["payload_json"])),
        version=int(row["version"]),
        thread_id=str(row["thread_id"]),
        run_id=str(row["run_id"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        subject_version=int(row["subject_version"]),
        payload_hash=str(row["payload_hash"]),
        response_schema=load_json(str(row["response_schema_json"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        deadline=datetime.fromisoformat(str(deadline)) if deadline is not None else None,
        response=load_json(str(response_json)) if response_json is not None else None,
        resolved_by=str(row["resolved_by"]) if row["resolved_by"] is not None else None,
        resolution=str(row["resolution"]) if row["resolution"] is not None else None,
        resolved_at=datetime.fromisoformat(str(resolved_at)) if resolved_at is not None else None,
    )


__all__ = ["SQLiteHitlRepository"]
