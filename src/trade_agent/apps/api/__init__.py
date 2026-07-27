"""FastAPI adapter: authentication、HITL、resource 与 SSE。"""

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from trade_agent.adapters.authentication import PyJwtOidcTokenVerifier
from trade_agent.adapters.sqlite import SQLiteAggregateRepository
from trade_agent.adapters.sqlite.json_support import load_json, payload_hash
from trade_agent.adapters.sqlite.schema import AggregateRecord, RunEventRecord
from trade_agent.apps.container import ApplicationContainer, build_application_container
from trade_agent.core.config import AppSettings
from trade_agent.core.hitl import (
    HumanInteraction,
    InteractionConflictError,
    InteractionExpiredError,
    ResponseValidationError,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.presentation.projection import HitlCardPresenter
from trade_agent.core.security import (
    AuthenticationError,
    TokenVerifier,
    UserContext,
    UserContextResolver,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRequest(StrictModel):
    thread_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=16_384)


class HitlResponseRequest(StrictModel):
    action: str = Field(pattern="^(continue|confirm|edit|cancel)$")
    values: dict[str, JsonValue]
    interaction_version: int = Field(ge=1)
    subject_version: int | None = Field(default=None, ge=1)
    payload_hash: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    card_revision: int = Field(ge=1)


class ResourceWriteRequest(StrictModel):
    resource_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ApiServices:
    container: ApplicationContainer
    settings: AppSettings


def create_app(
    settings: AppSettings | None = None,
    container: ApplicationContainer | None = None,
    token_verifier: TokenVerifier | None = None,
) -> FastAPI:
    resolved_settings = settings or AppSettings()
    resolved_container = container or build_application_container(resolved_settings)
    services = ApiServices(resolved_container, resolved_settings)
    app = FastAPI(title="Trade Agent API", version="0.1.0")
    app.state.services = services
    resolved_verifier = token_verifier or _build_token_verifier(resolved_settings)
    resolver = UserContextResolver(
        resolved_settings,
        resolved_verifier,
        correlation_id_factory=lambda: str(uuid4()),
    )

    def user_context(
        x_user_id: Annotated[str | None, Header(alias="X-User-ID")] = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> UserContext:
        try:
            return resolver.resolve(x_user_id=x_user_id, authorization=authorization)
        except AuthenticationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.get("/health")
    def health() -> Mapping[str, JsonValue]:
        database = _required(resolved_container.database)
        value = database.health()
        return {
            "ready": database.is_ready(),
            "integrity": value.integrity,
            "schema_version": value.schema_version,
        }

    @app.post("/api/conversations/runs")
    def start_run(
        request: RunRequest, user: Annotated[UserContext, Depends(user_context)]
    ) -> Mapping[str, JsonValue]:
        try:
            result = _required(resolved_container.conversation_runtime).start_run(
                owner_id=user.user_id,
                thread_id=request.thread_id,
                message=request.message,
                correlation_id=user.correlation_id,
            )
        except PermissionError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "thread 不存在") from exc
        response: dict[str, JsonValue] = {
            "run_id": result.run_id,
            "thread_id": result.thread_id,
            "status": result.status,
        }
        if result.pending_interaction_id is not None:
            response["pending_interaction_id"] = result.pending_interaction_id
        if result.card is not None:
            response["card"] = result.card.to_mapping()
        return response

    @app.get("/api/hitl/pending")
    def pending_hitl(
        user: Annotated[UserContext, Depends(user_context)],
        thread_id: str | None = Query(default=None),
    ) -> object:
        service = _required(resolved_container.hitl_service)
        items = [
            _interaction_mapping(item)
            for item in service.list_pending(user.user_id)
            if thread_id is None or item.thread_id == thread_id
        ]
        if thread_id is None:
            return items
        items_json: list[JsonValue] = [dict(item) for item in items]
        return {"items": items_json, "cursor": ""}

    @app.get("/api/hitl/{interaction_id}")
    def get_hitl(
        interaction_id: str,
        user: Annotated[UserContext, Depends(user_context)],
    ) -> Mapping[str, JsonValue]:
        service = _required(resolved_container.hitl_service)
        interaction = service.get(user.user_id, interaction_id)
        if interaction is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "interaction 不存在")
        return _interaction_mapping(interaction)

    @app.post("/api/hitl/{interaction_id}/responses")
    def respond_hitl(
        interaction_id: str,
        request: HitlResponseRequest,
        user: Annotated[UserContext, Depends(user_context)],
    ) -> object:
        command_store = _required(resolved_container.command_store)
        request_payload: dict[str, JsonValue] = request.model_dump(mode="json")
        digest = payload_hash(request_payload)
        receipt = command_store.begin(
            owner_id=user.user_id,
            idempotency_key=request.idempotency_key,
            payload_hash=digest,
        )
        if receipt.reused and receipt.result is not None:
            return receipt.result
        service = _required(resolved_container.hitl_service)
        current = service.get(user.user_id, interaction_id)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "interaction 不存在")
        latest_card = _hitl_card(current)
        if request.card_revision != latest_card.revision:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "message": "card revision 已过期",
                    "latest_revision": latest_card.revision,
                    "card": latest_card.to_mapping(),
                },
            )
        if request.payload_hash not in {latest_card.payload_hash, current.payload_hash}:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "message": "card payload 已过期",
                    "latest_revision": latest_card.revision,
                    "card": latest_card.to_mapping(),
                },
            )
        try:
            if request.action == "cancel":
                interaction = service.cancel(
                    owner_id=user.user_id,
                    interaction_id=interaction_id,
                    expected_version=request.interaction_version,
                    actor_id=user.user_id,
                )
            else:
                interaction = service.respond(
                    owner_id=user.user_id,
                    interaction_id=interaction_id,
                    expected_version=request.interaction_version,
                    subject_version=request.subject_version or current.subject_version,
                    payload_hash=current.payload_hash,
                    actor_id=user.user_id,
                    response=request.values,
                    resolution=request.action,
                )
        except ResponseValidationError as exc:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "message": str(exc),
                    "field_errors": exc.field_errors,
                    "card": _hitl_card(current, field_errors=exc.field_errors).to_mapping(),
                },
            )
        except (InteractionConflictError, InteractionExpiredError) as exc:
            refreshed = service.get(user.user_id, interaction_id) or current
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "message": str(exc),
                    "latest_revision": refreshed.version,
                    "card": _hitl_card(refreshed).to_mapping(),
                },
            )
        except PermissionError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "interaction 不存在") from exc
        next_card = _required(resolved_container.conversation_runtime).handle_resolved_interaction(
            interaction
        )
        card = next_card or _hitl_card(interaction)
        result: dict[str, JsonValue] = {
            "interaction_id": interaction.interaction_id,
            "status": interaction.status.value,
            "version": interaction.version,
            "card": card.to_mapping(),
        }
        command_store.complete(owner_id=user.user_id, command_id=receipt.command_id, result=result)
        return result

    @app.post("/api/hitl/{interaction_id}/cancel")
    def cancel_hitl(
        interaction_id: str,
        version: int,
        user: Annotated[UserContext, Depends(user_context)],
    ) -> Mapping[str, JsonValue]:
        service = _required(resolved_container.hitl_service)
        interaction = service.cancel(
            owner_id=user.user_id,
            interaction_id=interaction_id,
            expected_version=version,
            actor_id=user.user_id,
        )
        return _interaction_mapping(interaction)

    resource_names = (
        "cards",
        "artifacts",
        "jobs",
        "strategies",
        "models",
        "scans",
        "watchlists",
        "plans",
        "reminders",
        "reviews",
    )
    for resource_name in resource_names:
        _register_resource_routes(app, resource_name, resolved_container, user_context)

    @app.get("/api/runs/{run_id}/events")
    def stream_events(
        run_id: str,
        user: Annotated[UserContext, Depends(user_context)],
        after: str | None = Query(default=None),
    ) -> StreamingResponse:
        after_sequence = _resolve_after_sequence(
            container=resolved_container,
            owner_id=user.user_id,
            run_id=run_id,
            after=after,
        )
        events = _required(resolved_container.event_store).replay(
            owner_id=user.user_id, run_id=run_id, after_sequence=after_sequence
        )

        def generate() -> Iterator[str]:
            for event in events:
                payload = {
                    "event_id": event.event_id,
                    "run_id": event.run_id,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "type": event.event_type,
                    "payload": event.payload,
                    "occurred_at": event.occurred_at.isoformat(),
                    "cursor": event.event_id,
                }
                data = json.dumps(payload, ensure_ascii=False)
                yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    return app


def _register_resource_routes(
    app: FastAPI,
    resource_name: str,
    container: ApplicationContainer,
    authenticate_user: Callable[..., UserContext],
) -> None:
    repository = SQLiteAggregateRepository(_required(container.database), resource_name)

    async def get_resource(
        resource_id: str,
        user: Annotated[UserContext, Depends(authenticate_user)],
    ) -> Mapping[str, JsonValue]:
        value = repository.get(user.user_id, resource_id)
        if value is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")
        return {
            "resource_id": value.reference_id,
            "version": value.version,
            "payload": dict(value.payload),
        }

    async def list_resources(
        user: Annotated[UserContext, Depends(authenticate_user)],
        thread_id: str | None = Query(default=None),
    ) -> Mapping[str, JsonValue]:
        items = _list_resource_items(
            container=container,
            resource_name=resource_name,
            owner_id=user.user_id,
            thread_id=thread_id,
        )
        items_json: list[JsonValue] = [dict(item) for item in items]
        return {"items": items_json, "cursor": ""}

    async def put_resource(
        request: ResourceWriteRequest,
        user: Annotated[UserContext, Depends(authenticate_user)],
    ) -> Mapping[str, JsonValue]:
        value = repository.save(
            owner_id=user.user_id,
            aggregate_id=request.resource_id,
            expected_version=request.expected_version,
            payload=request.payload,
        )
        return {
            "resource_id": value.reference_id,
            "version": value.version,
            "payload": dict(value.payload),
        }

    app.add_api_route(f"/api/{resource_name}", list_resources, methods=["GET"])
    app.add_api_route(f"/api/{resource_name}/{{resource_id}}", get_resource, methods=["GET"])
    app.add_api_route(f"/api/{resource_name}", put_resource, methods=["POST"])


def _required[T](value: T | None) -> T:
    if value is None:
        raise RuntimeError("ApplicationContainer 缺少已装配依赖")
    return value


def _build_token_verifier(settings: AppSettings) -> TokenVerifier | None:
    if settings.authentication.mode != "oidc":
        return None
    issuer = settings.authentication.issuer
    audience = settings.authentication.audience
    if issuer is None or audience is None:
        return None
    return PyJwtOidcTokenVerifier(issuer=str(issuer), audience=audience)


def _interaction_mapping(interaction: HumanInteraction) -> Mapping[str, JsonValue]:
    card = _hitl_card(interaction)
    return {
        "interaction_id": interaction.interaction_id,
        "type": interaction.interaction_type.value,
        "status": interaction.status.value,
        "version": interaction.version,
        "subject_version": interaction.subject_version,
        "payload_hash": interaction.payload_hash,
        "thread_id": interaction.thread_id,
        "run_id": interaction.run_id,
        "subject_type": interaction.subject_type,
        "subject_id": interaction.subject_id,
        "card": card.to_mapping(),
    }


def _hitl_card(
    interaction: HumanInteraction,
    *,
    field_errors: Mapping[str, str] | None = None,
) -> CardEnvelope:
    card = HitlCardPresenter().present(interaction, field_errors=field_errors)
    return replace(card, revision=interaction.version)


def _resolve_after_sequence(
    *,
    container: ApplicationContainer,
    owner_id: str,
    run_id: str,
    after: str | None,
) -> int:
    if after is None or after == "":
        return 0
    if after.isdigit():
        return int(after)
    database = _required(container.database)
    with database.read_connection() as connection:
        row = (
            connection.execute(
                select(RunEventRecord.sequence).where(
                    RunEventRecord.owner_id == owner_id,
                    RunEventRecord.run_id == run_id,
                    RunEventRecord.event_id == after,
                )
            )
            .mappings()
            .one_or_none()
        )
    return 0 if row is None else int(row["sequence"])


def _list_resource_items(
    *,
    container: ApplicationContainer,
    resource_name: str,
    owner_id: str,
    thread_id: str | None,
) -> list[Mapping[str, JsonValue]]:
    database = _required(container.database)
    with database.read_connection() as connection:
        rows = connection.execute(
            select(
                AggregateRecord.aggregate_id,
                AggregateRecord.version,
                AggregateRecord.payload_json,
            )
            .where(
                AggregateRecord.owner_id == owner_id,
                AggregateRecord.aggregate_type == resource_name,
            )
            .order_by(AggregateRecord.aggregate_id, AggregateRecord.version.desc())
        ).mappings()
        latest: dict[str, dict[str, object]] = {}
        for row in rows:
            aggregate_id = str(row["aggregate_id"])
            latest.setdefault(aggregate_id, dict(row))
    items: list[Mapping[str, JsonValue]] = []
    for aggregate_id, latest_row in latest.items():
        payload = load_json(str(latest_row["payload_json"]))
        payload_thread_id = _resource_thread_id(payload)
        if thread_id is not None and payload_thread_id != thread_id:
            continue
        item: dict[str, JsonValue] = {
            "resource_id": aggregate_id,
            "version": int(str(latest_row["version"])),
            "payload": payload,
        }
        if payload_thread_id is not None:
            item["thread_id"] = payload_thread_id
        card = _extract_card(payload)
        if card is not None:
            item["card"] = card.to_mapping()
        items.append(item)
    return items


def _resource_thread_id(payload: Mapping[str, JsonValue]) -> str | None:
    value = payload.get("thread_id")
    return value if isinstance(value, str) and value else None


def _extract_card(payload: Mapping[str, JsonValue]) -> CardEnvelope | None:
    try:
        return CardEnvelope.from_mapping(payload)
    except Exception:
        pass
    raw_card = payload.get("card")
    if isinstance(raw_card, Mapping):
        try:
            return CardEnvelope.from_mapping(raw_card)
        except Exception:
            return None
    return None


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


__all__ = ["create_app", "main"]
