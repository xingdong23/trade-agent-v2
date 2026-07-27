"""Persistable HITL boundary values without persistence behavior."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue


class InteractionType(StrEnum):
    CLARIFICATION = "clarification"
    APPROVAL = "approval"
    REVIEW = "review"
    CORRECTION = "correction"
    EXCEPTION_RESOLUTION = "exception_resolution"


class InteractionStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class HumanInteraction:
    interaction_id: str
    owner_id: str
    interaction_type: InteractionType
    status: InteractionStatus
    payload: Mapping[str, JsonValue]
    version: int
    thread_id: str
    run_id: str
    subject_type: str
    subject_id: str
    subject_version: int
    payload_hash: str
    response_schema: Mapping[str, JsonValue]
    created_at: datetime
    deadline: datetime | None = None
    response: Mapping[str, JsonValue] | None = None
    resolved_by: str | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.version < 1 or self.subject_version < 1:
            raise ValueError("interaction 与 subject version 必须从 1 开始")
        if self.created_at.tzinfo is None or (
            self.deadline is not None and self.deadline.tzinfo is None
        ):
            raise ValueError("HITL 时间必须包含时区")


class InteractionConflictError(RuntimeError):
    pass


class InteractionExpiredError(RuntimeError):
    pass


class HitlRepository(Protocol):
    def create(self, interaction: HumanInteraction) -> HumanInteraction: ...

    def get(self, owner_id: str, interaction_id: str) -> HumanInteraction | None: ...

    def list_pending(self, owner_id: str) -> tuple[HumanInteraction, ...]: ...

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
    ) -> HumanInteraction: ...

    def transition(
        self,
        *,
        owner_id: str,
        interaction_id: str,
        expected_version: int,
        status: InteractionStatus,
        actor_id: str | None = None,
        resolution: str | None = None,
    ) -> HumanInteraction: ...


class HitlService(Protocol):
    def create(self, interaction: HumanInteraction) -> HumanInteraction: ...

    def respond(
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
    ) -> HumanInteraction: ...
