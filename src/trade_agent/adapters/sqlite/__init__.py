"""SQLite persistence, migrations, coordination, and recovery utilities."""

from trade_agent.capabilities.contracts import ConcurrentWriteError

from .checkpoint import SQLiteThreadCheckpointer
from .database import DatabaseHealth, SQLiteDatabase
from .hitl import SQLiteHitlRepository
from .maintenance import backup_database, restore_database
from .repositories import (
    EventSequenceError,
    IdempotencyConflictError,
    JobLeaseError,
    SQLiteAggregateRepository,
    SQLiteCommandStore,
    SQLiteEventStore,
    SQLiteJobStore,
)
from .scan_jobs import (
    ClaimedScanUnit,
    PersistedScanResult,
    ScanIdempotencyConflictError,
    ScanJobError,
    ScanProgress,
    ScanUnitInput,
    SQLiteScanJobStore,
)

__all__ = [
    "ClaimedScanUnit",
    "ConcurrentWriteError",
    "DatabaseHealth",
    "EventSequenceError",
    "IdempotencyConflictError",
    "JobLeaseError",
    "PersistedScanResult",
    "SQLiteAggregateRepository",
    "SQLiteCommandStore",
    "SQLiteDatabase",
    "SQLiteEventStore",
    "SQLiteHitlRepository",
    "SQLiteJobStore",
    "SQLiteScanJobStore",
    "SQLiteThreadCheckpointer",
    "ScanIdempotencyConflictError",
    "ScanJobError",
    "ScanProgress",
    "ScanUnitInput",
    "backup_database",
    "restore_database",
]
