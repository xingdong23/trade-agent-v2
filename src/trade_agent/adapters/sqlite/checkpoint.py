"""User-scoped LangGraph SQLite checkpointer adapter。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, CheckpointTuple
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from .database import SQLiteDatabase
from .schema import ThreadOwnerRecord


class SQLiteThreadCheckpointer:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._connection = sqlite3.connect(database.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(f"PRAGMA busy_timeout={database.busy_timeout_ms}")
        self._saver = SqliteSaver(self._connection)

    @property
    def saver(self) -> SqliteSaver:
        return self._saver

    def bind_thread(self, *, owner_id: str, thread_id: str) -> None:
        try:
            with self._database.write_transaction() as connection:
                connection.execute(
                    insert(ThreadOwnerRecord).values(
                        thread_id=thread_id,
                        owner_id=owner_id,
                        created_at=datetime.now(UTC).isoformat(),
                    )
                )
        except IntegrityError:
            self._authorize(owner_id, thread_id)

    def put_state(
        self,
        *,
        owner_id: str,
        thread_id: str,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Mapping[str, str | int | float],
    ) -> RunnableConfig:
        self.bind_thread(owner_id=owner_id, thread_id=thread_id)
        config = self.config(owner_id=owner_id, thread_id=thread_id)
        return self._saver.put(config, checkpoint, metadata, dict(new_versions))

    def get_tuple(self, *, owner_id: str, thread_id: str) -> CheckpointTuple | None:
        self._authorize(owner_id, thread_id)
        return self._saver.get_tuple(self.config(owner_id=owner_id, thread_id=thread_id))

    def config(self, *, owner_id: str, thread_id: str) -> RunnableConfig:
        self._authorize(owner_id, thread_id)
        return {
            "configurable": {
                "thread_id": self._storage_thread_id(owner_id, thread_id),
                "checkpoint_ns": "trade-agent",
            }
        }

    def close(self) -> None:
        self._connection.close()

    def _authorize(self, owner_id: str, thread_id: str) -> None:
        with self._database.read_connection() as connection:
            actual_owner = connection.execute(
                select(ThreadOwnerRecord.owner_id).where(ThreadOwnerRecord.thread_id == thread_id)
            ).scalar_one_or_none()
        if actual_owner != owner_id:
            raise PermissionError("thread 不存在或不属于当前 owner")

    @staticmethod
    def _storage_thread_id(owner_id: str, thread_id: str) -> str:
        return f"{owner_id}:{thread_id}"


__all__ = ["SQLiteThreadCheckpointer"]
