"""HITL 命令服务与 LangGraph 暂停/恢复协调层。

这里的核心思想是：人工交互不是“前端弹窗”，而是一个持久化协议。
它有版本、有过期时间、有响应 schema，也有明确的恢复点。这个模块负责把
这些规则变成一组可重放、可校验的方法。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.types import interrupt

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import JsonSchemaValidator, SchemaValidationError

from .contracts import HitlRepository, HitlService, HumanInteraction, InteractionStatus


class ResponseValidationError(ValueError):
    """用户提交的 HITL 响应不满足 schema 时抛出的异常。

    ``field_errors`` 让 API 或 CLI 可以把错误精确挂回到具体字段，而不是只返回
    一句泛化失败消息。
    """

    def __init__(self, message: str, *, field_errors: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.field_errors = dict(field_errors or {})


class DefaultHitlService(HitlService):
    """HITL 聚合的应用服务。

    它不关心请求来自 Web、CLI 还是 LangGraph resume；它只负责：

    - 创建新的 pending interaction；
    - 校验并解决用户响应；
    - 执行取消与过期迁移；
    - 通过 repository 保证 owner/version/payload hash 约束。
    """

    def __init__(
        self, repository: HitlRepository, validator: JsonSchemaValidator | None = None
    ) -> None:
        self._repository = repository
        self._validator = validator or JsonSchemaValidator()

    def create(self, interaction: HumanInteraction) -> HumanInteraction:
        """创建新的人工交互。

        新建 interaction 必须从 ``pending`` 开始，deadline 也必须在未来，
        否则会破坏后续恢复语义。
        """

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
        """校验响应并把 interaction 从 pending 推进到 resolved。"""

        interaction = self._repository.get(owner_id, interaction_id)
        if interaction is None:
            raise PermissionError("interaction 不存在或不属于当前 owner")
        try:
            # 响应必须先通过后端持有的 schema，而不是信任前端表单控件。
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
        """批量推进已过截止时间的 interaction。"""

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
    """把持久化 HITL 协议接到 LangGraph interrupt/resume 机制上。

    Attributes:
        service: 负责创建、校验和解决交互的应用服务。
    """

    service: DefaultHitlService

    def pause(self, interaction: HumanInteraction) -> Mapping[str, JsonValue]:
        """先持久化 interaction，再触发 LangGraph interrupt。"""

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
        """恢复一次 interrupt，并返回被成功解决的 interaction id。"""

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
