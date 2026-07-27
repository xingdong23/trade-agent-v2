"""HITL command service 与 LangGraph interrupt/resume 协调。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.types import interrupt

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import JsonSchemaValidator, SchemaValidationError

from .contracts import HitlRepository, HumanInteraction, InteractionStatus


class ResponseValidationError(ValueError):
    def __init__(self, message: str, *, field_errors: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.field_errors = dict(field_errors or {})


class DefaultHitlService:
    def __init__(
        self, repository: HitlRepository, validator: JsonSchemaValidator | None = None
    ) -> None:
        self._repository = repository
        self._validator = validator or JsonSchemaValidator()

    def create(self, interaction: HumanInteraction) -> HumanInteraction:
        if interaction.status is not InteractionStatus.PENDING:
            raise ValueError("新 HITL interaction 必须是 pending")
        if interaction.deadline is not None and interaction.deadline <= datetime.now(UTC):
            raise ValueError("新 HITL interaction 的 deadline 必须在未来")
        return self._repository.create(interaction)

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
    ) -> HumanInteraction:
        interaction = self._repository.get(owner_id, interaction_id)
        if interaction is None:
            raise PermissionError("interaction 不存在或不属于当前 owner")
        try:
            self._validator.validate(response, interaction.response_schema)
        except SchemaValidationError as exc:
            field = exc.path.removeprefix("$.")
            raise ResponseValidationError(str(exc), field_errors={field: exc.message}) from exc
        return self._repository.resolve(
            owner_id=owner_id,
            interaction_id=interaction_id,
            expected_version=expected_version,
            subject_version=subject_version,
            payload_hash=payload_hash,
            actor_id=actor_id,
            response=response,
            resolution=resolution,
        )

    def expire_due(self, owner_id: str) -> tuple[HumanInteraction, ...]:
        now = datetime.now(UTC)
        expired: list[HumanInteraction] = []
        for interaction in self._repository.list_pending(owner_id):
            if interaction.deadline is None or interaction.deadline > now:
                continue
            expired.append(
                self._repository.transition(
                    owner_id=owner_id,
                    interaction_id=interaction.interaction_id,
                    expected_version=interaction.version,
                    status=InteractionStatus.EXPIRED,
                    resolution="deadline_exceeded",
                )
            )
        return tuple(expired)

    def list_pending(self, owner_id: str) -> tuple[HumanInteraction, ...]:
        return self._repository.list_pending(owner_id)

    def get(self, owner_id: str, interaction_id: str) -> HumanInteraction | None:
        return self._repository.get(owner_id, interaction_id)

    def cancel(
        self,
        *,
        owner_id: str,
        interaction_id: str,
        expected_version: int,
        actor_id: str,
    ) -> HumanInteraction:
        return self._repository.transition(
            owner_id=owner_id,
            interaction_id=interaction_id,
            expected_version=expected_version,
            status=InteractionStatus.CANCELLED,
            actor_id=actor_id,
            resolution="cancelled",
        )


@dataclass(frozen=True, slots=True)
class HitlInterruptCoordinator:
    service: DefaultHitlService

    def pause(self, interaction: HumanInteraction) -> Mapping[str, JsonValue]:
        persisted = self.service.create(interaction)
        value = interrupt(
            {
                "interaction_id": persisted.interaction_id,
                "interaction_type": persisted.interaction_type.value,
                "version": persisted.version,
            }
        )
        if not isinstance(value, Mapping):
            raise ResponseValidationError("LangGraph resume value 必须是 JSON object")
        return value

    def resume(
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
    ) -> str:
        interaction = self.service.respond(
            owner_id=owner_id,
            interaction_id=interaction_id,
            expected_version=expected_version,
            subject_version=subject_version,
            payload_hash=payload_hash,
            actor_id=actor_id,
            response=response,
            resolution=resolution,
        )
        return interaction.interaction_id


__all__ = ["DefaultHitlService", "HitlInterruptCoordinator", "ResponseValidationError"]
