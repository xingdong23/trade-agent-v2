"""Small forward-only migration runner for the initial SQLite release."""

from collections.abc import Callable

from sqlalchemy import Connection, text

from .schema import Base

LATEST_VERSION = 3


def _migration_1(connection: Connection) -> None:
    Base.metadata.create_all(connection)


def _migration_2(connection: Connection) -> None:
    Base.metadata.create_all(connection)


def _migration_3(connection: Connection) -> None:
    Base.metadata.create_all(connection)


MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
}


def migrate(connection: Connection) -> int:
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
    )
    current = connection.execute(
        text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
    ).scalar_one()
    for version in range(int(current) + 1, LATEST_VERSION + 1):
        MIGRATIONS[version](connection)
        connection.execute(
            text(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (:version, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ),
            {"version": version},
        )
    return LATEST_VERSION
