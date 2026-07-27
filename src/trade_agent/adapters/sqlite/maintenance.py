"""Recoverable SQLite backup and restore operations."""

import os
import sqlite3
import tempfile
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (
        sqlite3.connect(source) as source_connection,
        sqlite3.connect(destination) as destination_connection,
    ):
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"备份完整性检查失败: {result}")


def restore_database(backup: Path, destination: Path) -> None:
    if not backup.is_file():
        raise FileNotFoundError(backup)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖现有数据库: {destination}")

    file_descriptor, staging_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".restore", dir=destination.parent
    )
    os.close(file_descriptor)
    staging = Path(staging_name)
    try:
        with (
            sqlite3.connect(backup) as source_connection,
            sqlite3.connect(staging) as destination_connection,
        ):
            result = source_connection.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError(f"源备份完整性检查失败: {result}")
            source_connection.backup(destination_connection)
            restored = destination_connection.execute("PRAGMA integrity_check").fetchone()
            if restored != ("ok",):
                raise RuntimeError(f"恢复数据库完整性检查失败: {restored}")
        staging.chmod(0o600)
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)
