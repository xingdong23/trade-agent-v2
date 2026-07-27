"""User-scoped LangGraph SQLite checkpointer adapter。"""

from __future__ import annotations

import hashlib
import json
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
    _STORAGE_KEY_VERSION = "v1"

    def __init__(self, database: SQLiteDatabase, *, namespace: str) -> None:
        """创建按 owner 隔离的 SQLite checkpoint 适配器。

        Args:
            database: 已初始化的 SQLite 连接管理器。
            namespace: 当前部署使用的 LangGraph checkpoint 命名空间。

        Raises:
            ValueError: ``namespace`` 为空白时抛出。
        """

        normalized_namespace = namespace.strip()
        if not normalized_namespace:
            raise ValueError("checkpoint namespace 不能为空")
        self._database = database
        self._namespace = normalized_namespace
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
                "checkpoint_ns": self._namespace,
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
        payload = json.dumps(
            {"owner_id": owner_id, "thread_id": thread_id},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{SQLiteThreadCheckpointer._STORAGE_KEY_VERSION}:sha256:{digest}"


__all__ = ["SQLiteThreadCheckpointer"]
