from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from trade_agent.adapters.sqlite import (
    SQLiteDatabase,
    SQLiteHitlRepository,
    SQLiteThreadCheckpointer,
)
from trade_agent.adapters.sqlite.json_support import payload_hash
from trade_agent.core.hitl import (
    DefaultHitlService,
    HumanInteraction,
    InteractionConflictError,
    InteractionExpiredError,
    InteractionStatus,
    InteractionType,
    ResponseValidationError,
)


@pytest.fixture
def database(tmp_path: Path) -> SQLiteDatabase:
    value = SQLiteDatabase(tmp_path / "runtime.db")
    assert value.initialize() == 3
    return value


def _interaction(*, deadline: datetime | None = None) -> HumanInteraction:
    payload = {"title": "确认启用提醒", "summary": "价格到达 100 美元时提醒"}
    return HumanInteraction(
        interaction_id="interaction-1",
        owner_id="owner-a",
        interaction_type=InteractionType.APPROVAL,
        status=InteractionStatus.PENDING,
        payload=payload,
        version=1,
        thread_id="thread-1",
        run_id="run-1",
        subject_type="reminder",
        subject_id="reminder-1",
        subject_version=2,
        payload_hash=payload_hash(payload),
        response_schema={
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
            "additionalProperties": False,
        },
        created_at=datetime.now(UTC),
        deadline=deadline,
    )


def test_checkpoint_storage_key_avoids_owner_thread_delimiter_collisions(
    database: SQLiteDatabase,
) -> None:
    checkpointer = SQLiteThreadCheckpointer(database, namespace="test-checkpoint")

    first_checkpoint = empty_checkpoint()
    first_checkpoint["channel_values"] = {"run_id": "run-collision-a"}
    checkpointer.put_state(
        owner_id="owner-a",
        thread_id="thread:shared",
        checkpoint=first_checkpoint,
        metadata={"source": "input", "step": 0, "parents": {}},
        new_versions={},
    )

    second_checkpoint = empty_checkpoint()
    second_checkpoint["channel_values"] = {"run_id": "run-collision-b"}
    checkpointer.put_state(
        owner_id="owner-a:thread",
        thread_id="shared",
        checkpoint=second_checkpoint,
        metadata={"source": "input", "step": 0, "parents": {}},
        new_versions={},
    )

    first_loaded = checkpointer.get_tuple(owner_id="owner-a", thread_id="thread:shared")
    second_loaded = checkpointer.get_tuple(owner_id="owner-a:thread", thread_id="shared")

    assert first_loaded is not None
    assert second_loaded is not None
    assert first_loaded.checkpoint["channel_values"] == {"run_id": "run-collision-a"}
    assert second_loaded.checkpoint["channel_values"] == {"run_id": "run-collision-b"}
    assert (
        checkpointer.config(owner_id="owner-a", thread_id="thread:shared")["configurable"][
            "checkpoint_ns"
        ]
        == "test-checkpoint"
    )
    checkpointer.close()


def test_checkpoint_is_owner_scoped_and_survives_reopen(database: SQLiteDatabase) -> None:
    first = SQLiteThreadCheckpointer(database, namespace="test-checkpoint")
    first.bind_thread(owner_id="owner-a", thread_id="thread-1")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"run_id": "run-1"}
    first.put_state(
        owner_id="owner-a",
        thread_id="thread-1",
        checkpoint=checkpoint,
        metadata={"source": "input", "step": 0, "parents": {}},
        new_versions={},
    )
    first.close()

    reopened = SQLiteThreadCheckpointer(database, namespace="test-checkpoint")
    loaded = reopened.get_tuple(owner_id="owner-a", thread_id="thread-1")
    assert loaded is not None
    assert loaded.checkpoint["channel_values"] == {"run_id": "run-1"}
    with pytest.raises(PermissionError):
        reopened.get_tuple(owner_id="owner-b", thread_id="thread-1")
    reopened.close()


def test_hitl_resolves_once_with_schema_and_owner_checks(database: SQLiteDatabase) -> None:
    service = DefaultHitlService(SQLiteHitlRepository(database))
    pending = service.create(_interaction(deadline=datetime.now(UTC) + timedelta(minutes=5)))
    resolved = service.respond(
        owner_id="owner-a",
        interaction_id=pending.interaction_id,
        expected_version=1,
        subject_version=2,
        payload_hash=pending.payload_hash,
        actor_id="owner-a",
        response={"approved": True},
        resolution="approved",
    )

    assert resolved.status is InteractionStatus.RESOLVED
    assert resolved.version == 2
    assert resolved.response == {"approved": True}
    with pytest.raises(InteractionConflictError):
        service.respond(
            owner_id="owner-a",
            interaction_id=pending.interaction_id,
            expected_version=1,
            subject_version=2,
            payload_hash=pending.payload_hash,
            actor_id="owner-a",
            response={"approved": True},
            resolution="approved",
        )
    with pytest.raises(PermissionError):
        service.respond(
            owner_id="owner-b",
            interaction_id=pending.interaction_id,
            expected_version=1,
            subject_version=2,
            payload_hash=pending.payload_hash,
            actor_id="owner-b",
            response={"approved": True},
            resolution="approved",
        )


def test_hitl_rejects_invalid_response_and_stale_subject(database: SQLiteDatabase) -> None:
    service = DefaultHitlService(SQLiteHitlRepository(database))
    pending = service.create(_interaction(deadline=datetime.now(UTC) + timedelta(minutes=5)))
    with pytest.raises(ResponseValidationError) as invalid:
        service.respond(
            owner_id="owner-a",
            interaction_id=pending.interaction_id,
            expected_version=1,
            subject_version=2,
            payload_hash=pending.payload_hash,
            actor_id="owner-a",
            response={},
            resolution="approved",
        )
    assert invalid.value.field_errors == {"$": "缺少字段: approved"}

    with pytest.raises(InteractionConflictError):
        service.respond(
            owner_id="owner-a",
            interaction_id=pending.interaction_id,
            expected_version=1,
            subject_version=1,
            payload_hash=pending.payload_hash,
            actor_id="owner-a",
            response={"approved": True},
            resolution="approved",
        )


def test_expired_hitl_never_auto_approves(database: SQLiteDatabase) -> None:
    repository = SQLiteHitlRepository(database)
    expired = _interaction(deadline=datetime.now(UTC) - timedelta(seconds=1))
    repository.create(expired)
    service = DefaultHitlService(repository)

    with pytest.raises(InteractionExpiredError):
        service.respond(
            owner_id="owner-a",
            interaction_id=expired.interaction_id,
            expected_version=1,
            subject_version=2,
            payload_hash=expired.payload_hash,
            actor_id="owner-a",
            response={"approved": True},
            resolution="approved",
        )
    current = repository.get("owner-a", expired.interaction_id)
    assert current is not None
    assert current.status is InteractionStatus.EXPIRED
