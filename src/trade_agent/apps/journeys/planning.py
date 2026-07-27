"""Planning 会话旅程插件。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from trade_agent.apps.journeys.contracts import (
    ConversationJourney,
    ConversationRunResult,
    ConversationRuntimePort,
    JourneyStartContext,
)
from trade_agent.capabilities.planning.application import PlanDraftRequest, PlanningService
from trade_agent.capabilities.planning.cards import PlanningCardPresenter
from trade_agent.capabilities.planning.contracts import PlanLineage, TradingPlan
from trade_agent.core.hitl import (
    DefaultHitlService,
    HumanInteraction,
    InteractionStatus,
    InteractionType,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope

_PLAN_FIELDS = (
    "horizon",
    "entry_condition",
    "invalidation_condition",
    "target",
    "position_notes",
    "risk_notes",
)

JOURNEY_PLANNING_CHOOSE_OPERATION = "planning.choose_operation"
JOURNEY_PLANNING_CREATE_PLAN = "planning.create_plan"

_SUBJECT_PLANNING_CHOICE = "planning_choice"
_SUBJECT_PLANNING_REQUEST = "planning_request"
_SUBJECT_PLAN_FORM = "plan_form"
_SUBJECT_PLAN_APPROVAL = "plan_approval"


@dataclass(frozen=True, slots=True)
class PlanningOperationSpec:
    """定义 planning 入口可暴露的一项用户操作。

    Attributes:
        operation_id: 前后端共享的稳定操作标识。
        label: 面向用户展示的操作名称。
        description: 用户在做选择前可见的补充说明。
        enabled: 当前环境下该操作是否可执行。
        outcome: 选择后进入的流程类型，例如 ``plan_form`` 或 ``unsupported``。
        unsupported_kind: 当 ``outcome`` 为 ``unsupported`` 时返回的稳定问题编码。
        unsupported_message: 当 ``outcome`` 为 ``unsupported`` 时返回的提示文案。

    Invariants:
        - ``operation_id`` 不能为空。
        - ``unsupported`` 流程必须同时提供 ``unsupported_kind`` 和 ``unsupported_message``。
    """

    operation_id: str
    label: str
    description: str
    enabled: bool
    outcome: str
    unsupported_kind: str | None = None
    unsupported_message: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise ValueError("operation_id 不能为空")
        if self.outcome == "unsupported" and (
            not self.unsupported_kind or not self.unsupported_message
        ):
            raise ValueError("unsupported 操作必须提供 kind 与 message")


@dataclass(frozen=True, slots=True)
class PlanningJourneyConfig:
    """描述 planning journey 当前暴露给平台的可配置入口。

    Attributes:
        choice_title: 入口 Choice Card 标题。
        choice_description: 入口 Choice Card 说明。
        choice_text_fallback: 不支持富渲染时使用的降级文本。
        operations: 当前环境允许注册的操作集合。
        direct_plan_unsupported_kind: 直接创建计划路径上的不支持问题编码。
        direct_plan_notice_message: 用户表达“要买某股票”时返回的边界提示。
        direct_plan_direction_template: 直接建计划时生成默认方向文案的模板。
        request_form_default_direction: 从入口 Choice 进入表单时使用的默认方向文案。
        form_title: 计划表单卡标题。
        form_description: 计划表单卡说明。
        form_text_fallback: 计划表单卡纯文本降级文案。

    Invariants:
        - 至少包含一个操作定义。
        - ``direct_plan_direction_template`` 必须能接受 ``symbol`` 占位符。
    """

    choice_title: str
    choice_description: str
    choice_text_fallback: str
    operations: tuple[PlanningOperationSpec, ...]
    direct_plan_unsupported_kind: str
    direct_plan_notice_message: str
    direct_plan_direction_template: str
    request_form_default_direction: str
    form_title: str
    form_description: str
    form_text_fallback: str

    def __post_init__(self) -> None:
        if not self.operations:
            raise ValueError("planning journey 至少需要一个操作定义")
        try:
            self.direct_plan_direction_template.format(symbol="NVDA")
        except KeyError as exc:
            raise ValueError("direct_plan_direction_template 必须包含 {symbol} 占位符") from exc


def default_planning_journey_config() -> PlanningJourneyConfig:
    """返回第一版 planning journey 使用的默认入口配置。"""

    return PlanningJourneyConfig(
        choice_title="请选择要新增的内容",
        choice_description="首版只支持创建美股交易计划, 不支持成交记录或真实下单。",
        choice_text_fallback="请选择创建交易计划; 其他交易操作暂不支持。",
        operations=(
            PlanningOperationSpec(
                "create_trade_plan",
                "创建交易计划",
                "补充条件并在确认后激活计划。",
                True,
                "plan_form",
            ),
            PlanningOperationSpec(
                "record_historical_trade",
                "记录已发生的交易",
                "首版暂不支持手工成交记录。",
                False,
                "unsupported",
                unsupported_kind="record_historical_trade",
                unsupported_message="首版只支持创建交易计划, 不支持成交记录或真实下单。",
            ),
            PlanningOperationSpec(
                "execute_trade",
                "执行真实交易",
                "系统没有下单或账户能力。",
                False,
                "unsupported",
                unsupported_kind="execute_trade",
                unsupported_message="首版只支持创建交易计划, 不支持成交记录或真实下单。",
            ),
        ),
        direct_plan_unsupported_kind="execute_trade",
        direct_plan_notice_message="系统不能下单; 可以继续创建仅用于研究与决策的美股交易计划。",
        direct_plan_direction_template="为 {symbol} 创建买入研究计划, 不执行下单",
        request_form_default_direction="创建买入研究计划, 不执行下单",
        form_title="补充美股交易计划",
        form_description="系统不会下单。请一次补齐证券、周期、入场、失效、目标、仓位和风险。",
        form_text_fallback="请补充完整的美股交易计划字段。",
    )


class PlanningConversationJourney(ConversationJourney):
    """负责交易计划入口、表单与审批闭环的会话旅程。

    Contract:
        - 只管理“选择操作”和“直接创建计划”两类 planning 启动旅程。
        - 所有表单值都必须通过 HITL schema 校验后，才能进入 ``PlanningService``。
        - 该旅程只生成研究/决策用计划，不提供下单、账户或成交记录能力。

    Implemented by:
        ``apps/container.py`` 在组合根中装配的 planning journey。
    """

    def __init__(
        self,
        *,
        planning: PlanningService,
        hitl_service: DefaultHitlService,
        presenter: PlanningCardPresenter | None = None,
        config: PlanningJourneyConfig | None = None,
    ) -> None:
        self._planning = planning
        self._hitl = hitl_service
        self._presenter = presenter or PlanningCardPresenter()
        self._config = config or default_planning_journey_config()

    @property
    def journey_ids(self) -> tuple[str, ...]:
        """返回本旅程负责的启动 ID。"""

        return (
            JOURNEY_PLANNING_CHOOSE_OPERATION,
            JOURNEY_PLANNING_CREATE_PLAN,
        )

    @property
    def subject_types(self) -> tuple[str, ...]:
        """返回本旅程负责恢复的 subject type。"""

        return (
            _SUBJECT_PLANNING_CHOICE,
            _SUBJECT_PLANNING_REQUEST,
            _SUBJECT_PLAN_FORM,
            _SUBJECT_PLAN_APPROVAL,
        )

    def start(
        self,
        context: JourneyStartContext,
        runtime: ConversationRuntimePort,
    ) -> ConversationRunResult:
        """根据 journey_id 启动对应的 planning 流程。"""

        if context.classification.journey_id == JOURNEY_PLANNING_CHOOSE_OPERATION:
            return self._start_choice(context, runtime)
        if context.classification.journey_id == JOURNEY_PLANNING_CREATE_PLAN:
            return self._start_create_plan(context, runtime)
        raise ValueError(f"planning journey 不支持 {context.classification.journey_id}")

    def resume(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope | None:
        """从 planning 旅程的暂停点恢复执行。"""

        if interaction.subject_type == _SUBJECT_PLANNING_CHOICE:
            runtime.publish_interaction(interaction, "card.resolved")
            return self._handle_choice(interaction, runtime)
        if interaction.subject_type in {_SUBJECT_PLANNING_REQUEST, _SUBJECT_PLAN_FORM}:
            runtime.publish_interaction(interaction, "card.resolved")
            return self._handle_form(interaction, runtime)
        if interaction.subject_type == _SUBJECT_PLAN_APPROVAL:
            return self._handle_approval(interaction, runtime)
        return None

    def _start_choice(
        self,
        context: JourneyStartContext,
        runtime: ConversationRuntimePort,
    ) -> ConversationRunResult:
        interaction = self._create_choice(context.owner_id, context.thread_id, context.run_id)
        card = runtime.publish_interaction(interaction, "card.created")
        return ConversationRunResult(
            context.run_id,
            context.thread_id,
            "waiting_for_human",
            interaction.interaction_id,
            card,
        )

    def _start_create_plan(
        self,
        context: JourneyStartContext,
        runtime: ConversationRuntimePort,
    ) -> ConversationRunResult:
        symbol = runtime.required_entity(context.classification, "symbol")
        notice = runtime.create_unsupported_notice(
            reference_id=context.run_id,
            unsupported_kind=self._config.direct_plan_unsupported_kind,
            message=self._config.direct_plan_notice_message,
            source_type="planning_request",
        )
        runtime.publish_card(
            context.owner_id,
            context.thread_id,
            context.run_id,
            notice,
            "card.created",
        )
        plan = self._planning.create_draft(
            PlanDraftRequest(
                plan_id=str(uuid4()),
                owner_id=context.owner_id,
                security_id=f"US:NASDAQ:{symbol}",
                direction=self._config.direct_plan_direction_template.format(symbol=symbol),
                created_at=datetime.now(UTC),
                source_references=(PlanLineage("user_request", context.run_id, 1),),
                field_sources={"security_id": "用户输入", "direction": "用户输入"},
            ),
            idempotency_key=f"{context.run_id}:create-draft",
        )
        interaction = self._create_plan_form(
            plan,
            thread_id=context.thread_id,
            run_id=context.run_id,
        )
        card = runtime.publish_interaction(interaction, "card.created")
        return ConversationRunResult(
            context.run_id,
            context.thread_id,
            "waiting_for_human",
            interaction.interaction_id,
            card,
        )

    def _handle_choice(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        response = interaction.response or {}
        operation = self._operation_spec(str(response.get("choice", "")))
        if operation is None or operation.outcome != "plan_form" or not operation.enabled:
            card = runtime.create_unsupported_notice(
                reference_id=interaction.interaction_id,
                unsupported_kind=(operation.unsupported_kind if operation else None)
                or "unknown_operation",
                message=(operation.unsupported_message if operation else None)
                or "当前选择没有映射到受支持的 planning 流程。",
                source_type="planning_request",
            )
            return runtime.publish_card(
                interaction.owner_id,
                interaction.thread_id,
                interaction.run_id,
                card,
                "card.failed",
            )
        next_interaction = self._create_request_form(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            str(uuid4()),
        )
        return runtime.publish_interaction(next_interaction, "card.created")

    def _handle_form(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        values = interaction.response or {}
        if interaction.subject_type == _SUBJECT_PLANNING_REQUEST:
            symbol = _required_text(values, "symbol").upper()
            plan = self._planning.create_draft(
                _request_from_values(
                    values,
                    owner_id=interaction.owner_id,
                    plan_id=interaction.subject_id,
                    security_id=f"US:NASDAQ:{symbol}",
                    run_id=interaction.run_id,
                ),
                idempotency_key=f"{interaction.interaction_id}:create-draft",
            )
        else:
            current = self._planning.get_plan(
                owner_id=interaction.owner_id,
                plan_id=interaction.subject_id,
            )
            plan = self._planning.revise_draft(
                _request_from_values(
                    values,
                    owner_id=interaction.owner_id,
                    plan_id=current.plan_id,
                    security_id=current.security_id,
                    run_id=interaction.run_id,
                ),
                expected_version=current.version,
                idempotency_key=f"{interaction.interaction_id}:revise-draft",
            )
        approval = self._create_plan_approval(
            plan,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
        )
        return runtime.publish_interaction(approval, "card.created")

    def _handle_approval(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        current = self._planning.get_plan(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
        )
        if interaction.resolution == "edit":
            superseded = replace(
                _interaction_card(interaction),
                state="superseded",
                actions=(),
                payload_hash="",
            )
            runtime.publish_card(
                interaction.owner_id,
                interaction.thread_id,
                interaction.run_id,
                superseded,
                "card.superseded",
            )
            revised = self._planning.revise_draft(
                PlanDraftRequest(
                    plan_id=current.plan_id,
                    owner_id=current.owner_id,
                    security_id=current.security_id,
                    direction=current.direction,
                    created_at=datetime.now(UTC),
                    source_references=current.source_references,
                    horizon=current.horizon,
                    entry_condition=current.entry_condition,
                    invalidation_condition=current.invalidation_condition,
                    target=current.target,
                    position_notes=current.position_notes,
                    risk_notes=current.risk_notes,
                    field_sources=current.field_sources,
                ),
                expected_version=current.version,
                idempotency_key=f"{interaction.interaction_id}:edit-draft",
            )
            form = self._create_plan_form(
                revised,
                thread_id=interaction.thread_id,
                run_id=interaction.run_id,
            )
            return runtime.publish_interaction(form, "card.created")

        runtime.publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        active = self._planning.activate(
            owner_id=current.owner_id,
            plan_id=current.plan_id,
            expected_version=current.version,
            actor_id=interaction.owner_id,
            approved=True,
            approved_payload_hash=current.approval_payload_hash,
            approval_interaction_id=interaction.interaction_id,
            idempotency_key=f"{interaction.interaction_id}:activate",
            occurred_at=datetime.now(UTC),
        )
        artifact = self._presenter.plan_artifact(active)
        return runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            artifact,
            "card.created",
            artifact=True,
        )

    def _create_choice(self, owner_id: str, thread_id: str, run_id: str) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": self._config.choice_title,
            "description": self._config.choice_description,
            "text_fallback": self._config.choice_text_fallback,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                owner_id,
                InteractionType.CLARIFICATION,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                _SUBJECT_PLANNING_CHOICE,
                run_id,
                1,
                _payload_hash(payload),
                {
                    "type": "object",
                    "properties": {
                        "choice": {
                            "type": "string",
                            "title": "操作类型",
                            "enum": [item.operation_id for item in self._config.operations],
                            "x-options": [
                                {
                                    "key": item.operation_id,
                                    "label": item.label,
                                    "description": item.description,
                                    "disabled": not item.enabled,
                                }
                                for item in self._config.operations
                            ],
                        }
                    },
                    "required": ["choice"],
                    "additionalProperties": False,
                },
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_request_form(
        self,
        owner_id: str,
        thread_id: str,
        run_id: str,
        plan_id: str,
    ) -> HumanInteraction:
        return self._create_form_interaction(
            owner_id=owner_id,
            thread_id=thread_id,
            run_id=run_id,
            subject_type=_SUBJECT_PLANNING_REQUEST,
            plan_id=plan_id,
            plan_version=1,
            symbol=None,
            direction=self._config.request_form_default_direction,
            defaults={},
        )

    def _create_plan_form(
        self,
        plan: TradingPlan,
        *,
        thread_id: str,
        run_id: str,
    ) -> HumanInteraction:
        return self._create_form_interaction(
            owner_id=plan.owner_id,
            thread_id=thread_id,
            run_id=run_id,
            subject_type=_SUBJECT_PLAN_FORM,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            symbol=plan.security_id.rsplit(":", 1)[-1],
            direction=plan.direction,
            defaults={field: getattr(plan, field) for field in _PLAN_FIELDS},
        )

    def _create_form_interaction(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        subject_type: str,
        plan_id: str,
        plan_version: int,
        symbol: str | None,
        direction: str,
        defaults: Mapping[str, str | None],
    ) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": self._config.form_title,
            "description": self._config.form_description,
            "text_fallback": self._config.form_text_fallback,
        }
        properties: dict[str, JsonValue] = {
            "symbol": {
                "type": "string",
                "title": "美股代码",
                "pattern": "^[A-Za-z]{1,5}$",
                "default": symbol,
                "readOnly": symbol is not None,
            },
            "direction": {
                "type": "string",
                "title": "方向或逻辑",
                "minLength": 1,
                "default": direction,
            },
        }
        labels = {
            "horizon": "计划周期",
            "entry_condition": "入场条件",
            "invalidation_condition": "失效或止损条件",
            "target": "目标条件",
            "position_notes": "仓位备注",
            "risk_notes": "风险说明",
        }
        for field, label in labels.items():
            properties[field] = {
                "type": "string",
                "title": label,
                "minLength": 1,
                "maxLength": 1000,
                "default": defaults.get(field),
                "format": "textarea" if field != "horizon" else "text",
            }
        schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": properties,
            "required": ["symbol", "direction", *_PLAN_FIELDS],
            "additionalProperties": False,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                owner_id,
                InteractionType.EXCEPTION_RESOLUTION,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                subject_type,
                plan_id,
                plan_version,
                _payload_hash(payload),
                schema,
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _operation_spec(self, operation_id: str) -> PlanningOperationSpec | None:
        """按稳定操作 ID 查找当前注册的 planning 入口定义。"""

        normalized = operation_id.strip()
        for item in self._config.operations:
            if item.operation_id == normalized:
                return item
        return None

    def _create_plan_approval(
        self,
        plan: TradingPlan,
        *,
        thread_id: str,
        run_id: str,
    ) -> HumanInteraction:
        preview = self._presenter.plan_approval(plan)
        payload: dict[str, JsonValue] = {
            "title": preview.data["title"],
            "description": preview.data["description"],
            "summary": preview.data["summary"],
            "facts": preview.data["facts"],
            "provenance": preview.data["provenance"],
            "text_fallback": preview.text_fallback,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                plan.owner_id,
                InteractionType.APPROVAL,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                _SUBJECT_PLAN_APPROVAL,
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )


def _interaction_card(interaction: HumanInteraction) -> CardEnvelope:
    """把 HITL 聚合投影为统一 Card。"""

    from trade_agent.core.presentation.projection import HitlCardPresenter

    projected = HitlCardPresenter().present(interaction)
    return replace(projected, revision=interaction.version)


def _required_text(values: Mapping[str, JsonValue], field: str) -> str:
    """读取一个必须存在且非空的文本字段。"""

    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"计划字段缺少 {field}")
    return value.strip()


def _request_from_values(
    values: Mapping[str, JsonValue],
    *,
    owner_id: str,
    plan_id: str,
    security_id: str,
    run_id: str,
) -> PlanDraftRequest:
    """把人工表单值转换为 Planning capability 的明确输入契约。"""

    return PlanDraftRequest(
        plan_id=plan_id,
        owner_id=owner_id,
        security_id=security_id,
        direction=_required_text(values, "direction"),
        created_at=datetime.now(UTC),
        source_references=(PlanLineage("user_request", run_id, 1),),
        horizon=_required_text(values, "horizon"),
        entry_condition=_required_text(values, "entry_condition"),
        invalidation_condition=_required_text(values, "invalidation_condition"),
        target=_required_text(values, "target"),
        position_notes=_required_text(values, "position_notes"),
        risk_notes=_required_text(values, "risk_notes"),
        field_sources={field: "用户输入" for field in ("direction", *_PLAN_FIELDS)},
    )


def _payload_hash(payload: Mapping[str, JsonValue]) -> str:
    """计算一个 HITL payload 的稳定哈希。"""

    from trade_agent.adapters.sqlite.json_support import payload_hash

    return payload_hash(payload)


__all__ = [
    "PlanningConversationJourney",
    "PlanningJourneyConfig",
    "PlanningOperationSpec",
    "default_planning_journey_config",
]
