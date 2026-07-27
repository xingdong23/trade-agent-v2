"""Watchlist 导入、分类与 snapshot 的确定性 application service。"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from threading import Lock
from uuid import uuid4

from trade_agent.capabilities.watchlist.contracts import (
    ClassificationSuggestion,
    ImportRow,
    ImportStatus,
    Membership,
    Provenance,
    UniverseSnapshot,
    WatchlistGroup,
)


class IdempotencyConflictError(RuntimeError):
    """同一幂等键被用于不同的导入审批 payload。"""


class WatchlistService:
    def __init__(self, *, owner_id: str, watchlist_id: str) -> None:
        self.owner_id = owner_id
        self.watchlist_id = watchlist_id
        self._memberships: dict[str, Membership] = {}
        self._groups: dict[str, WatchlistGroup] = {}
        self._suggestions: dict[str, ClassificationSuggestion] = {}
        self._commands: dict[str, tuple[str, tuple[ImportRow, ...]]] = {}
        self._classification_commands: dict[str, tuple[str, ClassificationSuggestion]] = {}
        self._snapshots: dict[str, UniverseSnapshot] = {}
        self._lock = Lock()

    @property
    def memberships(self) -> tuple[Membership, ...]:
        with self._lock:
            return tuple(self._memberships.values())

    def classify_import(
        self,
        rows: Sequence[tuple[str, str | None, ImportStatus]],
    ) -> tuple[ImportRow, ...]:
        with self._lock:
            seen = set(self._memberships)
        results: list[ImportRow] = []
        for row_number, (raw_value, security_id, status) in enumerate(rows, start=1):
            effective = status
            if status == ImportStatus.ACCEPTED and security_id is None:
                effective = ImportStatus.REJECTED
            elif status == ImportStatus.ACCEPTED and security_id in seen:
                effective = ImportStatus.DUPLICATE
            if effective == ImportStatus.ACCEPTED and security_id is not None:
                seen.add(security_id)
            results.append(ImportRow(row_number, raw_value, effective, security_id))
        return tuple(results)

    def approve_import(
        self,
        rows: Sequence[ImportRow],
        *,
        actor_owner_id: str,
        approved: bool,
        idempotency_key: str,
        source_type: str,
        source_reference: str,
        imported_at: datetime,
        tags: Mapping[str, frozenset[str]] | None = None,
        notes: Mapping[str, tuple[str, ...]] | None = None,
    ) -> tuple[ImportRow, ...]:
        self._authorize(actor_owner_id)
        if not approved:
            raise PermissionError("批量导入必须经过明确审批")
        normalized_tags = tags or {}
        normalized_notes = notes or {}
        payload_hash = self._import_payload_hash(
            rows,
            source_type=source_type,
            source_reference=source_reference,
            imported_at=imported_at,
            tags=normalized_tags,
            notes=normalized_notes,
        )
        with self._lock:
            previous = self._commands.get(idempotency_key)
            if previous is not None:
                if previous[0] != payload_hash:
                    raise IdempotencyConflictError("幂等键对应的导入 payload 已改变")
                return previous[1]

            staged = dict(self._memberships)
            for row in rows:
                if row.status not in (ImportStatus.ACCEPTED, ImportStatus.DUPLICATE):
                    continue
                if row.security_id is None:
                    raise ValueError("可导入行必须包含规范 security_id")
                existing = staged.get(row.security_id)
                provenance = Provenance(source_type, source_reference, imported_at)
                previous_tags = existing.tags if existing else frozenset()
                previous_notes = existing.notes if existing else ()
                previous_provenance = existing.provenance if existing else ()
                staged[row.security_id] = Membership(
                    row.security_id,
                    previous_tags | normalized_tags.get(row.security_id, frozenset()),
                    tuple(
                        dict.fromkeys(previous_notes + normalized_notes.get(row.security_id, ()))
                    ),
                    tuple(dict.fromkeys((*previous_provenance, provenance))),
                )
            result = tuple(rows)
            self._memberships = staged
            self._commands[idempotency_key] = (payload_hash, result)
            return result

    def create_group(self, *, actor_owner_id: str, group_id: str, name: str) -> WatchlistGroup:
        self._authorize(actor_owner_id)
        group = WatchlistGroup(group_id, name, frozenset())
        with self._lock:
            self._groups[group_id] = group
        return group

    def accept_suggestion(
        self,
        suggestion: ClassificationSuggestion,
        *,
        actor_owner_id: str,
        idempotency_key: str,
        group_id: str | None = None,
    ) -> ClassificationSuggestion:
        self._authorize(actor_owner_id)
        selected_group_id = group_id or suggestion.proposed_group_id
        payload_hash = sha256(
            json.dumps(
                {
                    "suggestion_id": suggestion.suggestion_id,
                    "security_id": suggestion.security_id,
                    "proposed_group_id": suggestion.proposed_group_id,
                    "selected_group_id": selected_group_id,
                    "source_reference": suggestion.source_reference,
                    "actor_owner_id": actor_owner_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self._lock:
            previous = self._classification_commands.get(idempotency_key)
            if previous is not None:
                if previous[0] != payload_hash:
                    raise IdempotencyConflictError("幂等键对应的分类审批 payload 已改变")
                return previous[1]
            if suggestion.security_id not in self._memberships:
                raise ValueError("分类建议引用未知 membership")
            group = self._groups[selected_group_id]
            self._groups[group.group_id] = replace(
                group, security_ids=group.security_ids | {suggestion.security_id}
            )
            accepted = replace(
                suggestion,
                accepted=True,
                decided_by=actor_owner_id,
                accepted_group_id=selected_group_id,
            )
            self._suggestions[suggestion.suggestion_id] = accepted
            self._classification_commands[idempotency_key] = (payload_hash, accepted)
            return accepted

    def get_suggestion(
        self, *, actor_owner_id: str, suggestion_id: str
    ) -> ClassificationSuggestion:
        self._authorize(actor_owner_id)
        with self._lock:
            return self._suggestions[suggestion_id]

    def get_group(self, *, actor_owner_id: str, group_id: str) -> WatchlistGroup:
        self._authorize(actor_owner_id)
        with self._lock:
            return self._groups[group_id]

    def freeze_universe(
        self,
        *,
        actor_owner_id: str,
        created_at: datetime,
        group_id: str | None = None,
    ) -> UniverseSnapshot:
        self._authorize(actor_owner_id)
        with self._lock:
            security_ids = (
                self._groups[group_id].security_ids
                if group_id is not None
                else tuple(self._memberships)
            )
            frozen = tuple(sorted(security_ids))
            if not frozen:
                raise ValueError("不能冻结空 universe")
            snapshot = UniverseSnapshot(
                str(uuid4()), self.owner_id, self.watchlist_id, frozen, created_at, group_id
            )
            self._snapshots[snapshot.snapshot_id] = snapshot
            return snapshot

    def get_snapshot(self, *, actor_owner_id: str, snapshot_id: str) -> UniverseSnapshot:
        self._authorize(actor_owner_id)
        with self._lock:
            return self._snapshots[snapshot_id]

    @staticmethod
    def _import_payload_hash(
        rows: Sequence[ImportRow],
        *,
        source_type: str,
        source_reference: str,
        imported_at: datetime,
        tags: Mapping[str, frozenset[str]],
        notes: Mapping[str, tuple[str, ...]],
    ) -> str:
        payload = {
            "rows": [
                {
                    "row_number": row.row_number,
                    "raw_value": row.raw_value,
                    "status": row.status.value,
                    "security_id": row.security_id,
                }
                for row in rows
            ],
            "source_type": source_type,
            "source_reference": source_reference,
            "imported_at": imported_at.isoformat(),
            "tags": {key: sorted(value) for key, value in sorted(tags.items())},
            "notes": {key: list(value) for key, value in sorted(notes.items())},
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode()).hexdigest()

    def _authorize(self, actor_owner_id: str) -> None:
        if actor_owner_id != self.owner_id:
            raise PermissionError("watchlist 不属于当前 owner")
