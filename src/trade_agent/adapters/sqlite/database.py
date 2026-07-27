"""SQLite engine configuration and short transaction boundary."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event, inspect, text

from .migrations import LATEST_VERSION, migrate


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    integrity: str
    journal_mode: str
    foreign_keys: bool
    schema_version: int
    lock_wait_seconds: float


class SQLiteDatabase:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = path.expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._write_lock = threading.Lock()
        self._lock_wait_seconds = 0.0
        self._engine = self._create_engine()

    @property
    def engine(self) -> Engine:
        return self._engine

    def _create_engine(self) -> Engine:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        engine = create_engine(f"sqlite+pysqlite:///{self.path}", future=True)

        @event.listens_for(engine, "connect")
        def configure_connection(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            cursor.close()

        return engine

    def initialize(self) -> int:
        with self.write_transaction() as connection:
            version = migrate(connection)
        self.path.chmod(0o600)
        return version

    @contextmanager
    def read_connection(self) -> Iterator[Connection]:
        with self._engine.connect() as connection:
            yield connection

    @contextmanager
    def write_transaction(self) -> Iterator[Connection]:
        started = time.monotonic()
        self._write_lock.acquire()
        self._lock_wait_seconds += time.monotonic() - started
        try:
            with self._engine.begin() as connection:
                yield connection
        finally:
            self._write_lock.release()

    def health(self) -> DatabaseHealth:
        with self.read_connection() as connection:
            integrity = str(connection.execute(text("PRAGMA quick_check")).scalar_one())
            journal_mode = str(connection.execute(text("PRAGMA journal_mode")).scalar_one())
            foreign_keys = bool(connection.execute(text("PRAGMA foreign_keys")).scalar_one())
            if inspect(connection).has_table("schema_migrations"):
                version = connection.execute(
                    text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
                ).scalar_one()
            else:
                version = 0
        return DatabaseHealth(
            integrity=integrity,
            journal_mode=journal_mode.lower(),
            foreign_keys=foreign_keys,
            schema_version=int(version),
            lock_wait_seconds=self._lock_wait_seconds,
        )

    def is_ready(self) -> bool:
        health = self.health()
        return (
            health.integrity == "ok"
            and health.journal_mode == "wal"
            and health.foreign_keys
            and health.schema_version == LATEST_VERSION
        )
