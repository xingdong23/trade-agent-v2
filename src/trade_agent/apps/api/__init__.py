"""Trade Agent 的 HTTP 适配器。

API 层只负责传输协议：认证用户、校验请求、调用 application service，并把结果
转换为 JSON 或 SSE。业务规则不应写在路由函数中。前端围绕三类接口工作：启动
会话、响应 HITL、订阅 Card 事件；资源接口用于刷新页面后的状态恢复。
"""

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from trade_agent.adapters.authentication import (
    OidcClaimMapping,
    OidcRoleClaim,
    PyJwtOidcTokenVerifier,
)
from trade_agent.adapters.sqlite import SQLiteAggregateRepository
from trade_agent.adapters.sqlite.json_support import load_json, payload_hash
from trade_agent.adapters.sqlite.schema import AggregateRecord, RunEventRecord, RunRecord
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
    """拒绝未声明字段的 API 请求基类，避免客户端拼写错误被静默忽略。

    Attributes:
        model_config: Pydantic 严格字段策略，禁止额外字段。
    """

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
    """挂到 FastAPI app.state 上的进程级依赖。

    Attributes:
        container: 已完成装配的应用容器。
        settings: 当前进程的类型化配置。
    """

    container: ApplicationContainer
    settings: AppSettings


def create_app(
    settings: AppSettings | None = None,
    container: ApplicationContainer | None = None,
    token_verifier: TokenVerifier | None = None,
) -> FastAPI:
    """创建 API 应用，并让所有路由共享同一个 application container。"""

    resolved_settings = settings or AppSettings()
    resolved_container = container or build_application_container(resolved_settings)
    services = ApiServices(resolved_container, resolved_settings)
    app = FastAPI(title=resolved_settings.api.title, version=resolved_settings.api.version)
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
        """把一条用户消息交给会话运行时。

        API 层只负责认证身份、转换 HTTP 请求与响应。意图分类、Agent 路由和业务推进
        全部委托给 ``ConversationRunService``，避免 HTTP endpoint 形成第二套流程。
        """

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
        if result.user_message_id is not None:
            response["user_message_id"] = result.user_message_id
        return response

    @app.get("/api/conversations/{thread_id}/snapshot")
    def conversation_snapshot(
        thread_id: str,
        user: Annotated[UserContext, Depends(user_context)],
    ) -> Mapping[str, JsonValue]:
        """返回一个 thread 的确定性恢复快照。"""

        return _conversation_snapshot(
            container=resolved_container,
            owner_id=user.user_id,
            thread_id=thread_id,
        )

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
        """校验并解决人工交互，然后恢复原会话流程。

        执行顺序：幂等收据 -> 读取交互 -> Card 版本与 payload 校验 -> 解决 HITL ->
        恢复 Workflow -> 保存命令结果。任何一步失败都不能执行后续业务副作用。

        ``payload_hash`` 防止用户确认后端已更新的旧卡片，``revision`` 防止并发覆盖，
        ``idempotency_key`` 保证客户端超时重试不会重复执行审批动作。
        """

        command_store = _required(resolved_container.command_store)
        request_payload: dict[str, JsonValue] = request.model_dump(mode="json")
        digest = payload_hash(request_payload)
        # 幂等收据必须在执行任何业务动作前创建，重试时可以直接返回原结果。
        receipt = command_store.begin(
            owner_id=user.user_id,
            idempotency_key=request.idempotency_key,
            payload_hash=digest,
        )
        if receipt.reused and receipt.result is not None:
            return receipt.result

        # 读取当前持久化交互，不能相信客户端提交的 subject 或展示数据。
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

        # HitlService 只负责安全解决交互；具体下一步仍由创建它的 Workflow 决定。
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
        # 最后保存 HTTP 命令收据，重复请求将直接复用完全相同的结果。
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

    for resource_name in resolved_container.resource_names:
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
    authentication = settings.authentication
    return PyJwtOidcTokenVerifier(
        issuer=str(issuer),
        audience=audience,
        discovery_timeout_seconds=authentication.discovery_timeout_seconds,
        jwks_timeout_seconds=authentication.jwks_timeout_seconds,
        jwks_cache_ttl_seconds=authentication.jwks_cache_ttl_seconds,
        claim_mapping=OidcClaimMapping(
            subject_claim=authentication.subject_claim,
            role_claims=tuple(
                OidcRoleClaim(path=item.path, separator=item.separator)
                for item in authentication.role_claims
            ),
        ),
        required_claims=authentication.required_claims,
        signing_algorithms=authentication.signing_algorithms,
    )


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


def _conversation_snapshot(
    *,
    container: ApplicationContainer,
    owner_id: str,
    thread_id: str,
) -> Mapping[str, JsonValue]:
    """从持久化事件、HITL 与资源目录重建会话视图。

    Args:
        container: 已完成装配的应用容器。
        owner_id: 已认证资源所有者。
        thread_id: 需要恢复的会话线程。

    Returns:
        包含最新 Card、消息和 SSE 游标的统一前端快照。

    Notes:
        当前返回空 cursor，使客户端只对活跃 run 从头重放事件；Card revision 与
        message ID 会负责去重。这样不会把其他 run 的 cursor 错用于当前 SSE。
    """

    cards: dict[str, CardEnvelope] = {}
    messages: list[JsonValue] = []
    database = _required(container.database)
    with database.read_connection() as connection:
        rows = connection.execute(
            select(
                RunEventRecord.event_id,
                RunEventRecord.event_type,
                RunEventRecord.payload_json,
                RunEventRecord.occurred_at,
            )
            .join(RunRecord, RunRecord.run_id == RunEventRecord.run_id)
            .where(
                RunRecord.owner_id == owner_id,
                RunRecord.thread_id == thread_id,
                RunEventRecord.owner_id == owner_id,
            )
            .order_by(
                RunRecord.created_at,
                RunRecord.run_id,
                RunEventRecord.sequence,
            )
        ).mappings()
        for row in rows:
            payload = load_json(str(row["payload_json"]))
            card = _extract_card(payload)
            if card is not None:
                current = cards.get(card.card_id)
                if current is None or card.revision > current.revision:
                    cards[card.card_id] = card
            message = _event_message(
                event_id=str(row["event_id"]),
                event_type=str(row["event_type"]),
                payload=payload,
                occurred_at=str(row["occurred_at"]),
                sequence=len(messages) + 1,
            )
            if message is not None:
                messages.append(dict(message))

    hitl_service = _required(container.hitl_service)
    for interaction in hitl_service.list_pending(owner_id):
        if interaction.thread_id != thread_id:
            continue
        card = _hitl_card(interaction)
        current = cards.get(card.card_id)
        if current is None or card.revision > current.revision:
            cards[card.card_id] = card

    for resource_name in container.resource_names:
        for item in _list_resource_items(
            container=container,
            resource_name=resource_name,
            owner_id=owner_id,
            thread_id=thread_id,
        ):
            card = _extract_card(item)
            if card is None:
                continue
            current = cards.get(card.card_id)
            if current is None or card.revision > current.revision:
                cards[card.card_id] = card

    return {
        "cards": [card.to_mapping() for card in cards.values()],
        "messages": messages,
        "cursor": "",
    }


def _event_message(
    *,
    event_id: str,
    event_type: str,
    payload: Mapping[str, JsonValue],
    occurred_at: str,
    sequence: int,
) -> Mapping[str, JsonValue] | None:
    """把受支持的持久化消息事件转换为前端消息合同。"""

    if event_type not in {"message.created", "assistant.message"}:
        return None
    raw_message = payload.get("message")
    message = raw_message if isinstance(raw_message, Mapping) else payload
    content = message.get("content")
    if not isinstance(content, str):
        return None
    raw_role = message.get("role")
    role = (
        raw_role
        if isinstance(raw_role, str) and raw_role in {"user", "assistant", "system"}
        else "assistant"
    )
    raw_id = message.get("id")
    return {
        "id": raw_id if isinstance(raw_id, str) and raw_id else event_id,
        "role": role,
        "content": content,
        "sequence": sequence,
        "created_at": occurred_at,
    }


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

    settings = AppSettings()
    uvicorn.run(
        create_app(settings),
        host=settings.api.host,
        port=settings.api.port,
    )


__all__ = ["create_app", "main"]
