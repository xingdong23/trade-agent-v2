"""Planning 会话工作流插件。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from trade_agent.apps.workflows.contracts import (
    ConversationRunResult,
    ConversationWorkflow,
    WorkflowRuntime,
    WorkflowStartContext,
)
from trade_agent.capabilities.planning.application import PlanDraftRequest, PlanningService
from trade_agent.capabilities.planning.cards import (
    PlanningArtifactSectionSpec,
    PlanningCardPresenter,
    PlanningChoiceOptionSpec,
    PlanningFieldSpec,
    PlanningPresenterConfig,
    PlanningPresenterCopy,
)
from trade_agent.capabilities.planning.contracts import PlanLineage, TradingPlan
from trade_agent.core.config import HitlSettings, MarketSettings, PlanningWorkflowSettings
from trade_agent.core.hitl import (
    DefaultHitlService,
    HumanInteraction,
    InteractionStatus,
    InteractionType,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.runtime import Intent, IntentClassification

WORKFLOW_PLANNING_CHOOSE_OPERATION = "planning.choose_operation"
WORKFLOW_PLANNING_CREATE_PLAN = "planning.create_plan"

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
    outcome: Literal["plan_form", "unsupported"]
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
class PlanningWorkflowConfig:
    """描述 planning workflow 当前暴露给平台的可配置入口。

    Attributes:
        choice_title: 入口 Choice Card 标题。
        choice_description: 入口 Choice Card 说明。
        choice_text_fallback: 不支持富渲染时使用的降级文本。
        choice_field_title: 入口 Choice 字段标题。
        operations: 当前环境允许注册的操作集合。
        direct_plan_unsupported_kind: 直接创建计划路径上的不支持问题编码。
        direct_plan_notice_message: 用户表达“要买某股票”时返回的边界提示。
        direct_plan_direction_template: 直接建计划时生成默认方向文案的模板。
        request_form_default_direction: 从入口 Choice 进入表单时使用的默认方向文案。
        form_title: 计划表单卡标题。
        form_description: 计划表单卡说明。
        form_text_fallback: 计划表单卡纯文本降级文案。
        market_code: 规范证券标识中的市场代码。
        exchange_codes: 当前部署允许选择的交易所代码。
        symbol_pattern: 证券代码字段的部署级校验规则。
        interaction_ttl_seconds: HITL 交互有效期，单位秒。
        text_field_max_length: 计划长文本字段的最大字符数。
        unknown_operation_message: 选项无法映射时的默认提示文案。
        presenter_config: Presenter 与字段目录的统一只读配置。

    Invariants:
        - 至少包含一个操作定义。
        - ``direct_plan_direction_template`` 必须能接受 ``symbol`` 占位符。
    """

    choice_title: str
    choice_description: str
    choice_text_fallback: str
    choice_field_title: str
    operations: tuple[PlanningOperationSpec, ...]
    direct_plan_unsupported_kind: str
    direct_plan_notice_message: str
    direct_plan_direction_template: str
    request_form_default_direction: str
    form_title: str
    form_description: str
    form_text_fallback: str
    market_code: str
    exchange_codes: tuple[str, ...]
    symbol_pattern: str
    interaction_ttl_seconds: int
    text_field_max_length: int
    unknown_operation_message: str
    presenter_config: PlanningPresenterConfig

    def __post_init__(self) -> None:
        if not self.operations:
            raise ValueError("planning workflow 至少需要一个操作定义")
        if not self.market_code.strip() or not self.exchange_codes:
            raise ValueError("planning workflow 必须配置市场代码与交易所目录")
        if self.interaction_ttl_seconds < 60 or self.text_field_max_length < 1:
            raise ValueError("planning workflow 的 HITL 策略无效")
        try:
            self.direct_plan_direction_template.format(symbol="SYMBOL")
        except KeyError as exc:
            raise ValueError("direct_plan_direction_template 必须包含 {symbol} 占位符") from exc

    @property
    def request_form_fields(self) -> tuple[PlanningFieldSpec, ...]:
        """返回 Workflow 请求表单需要投影的字段目录。"""

        return tuple(
            spec for spec in self.presenter_config.field_specs if spec.include_in_request_form
        )

    @property
    def editable_plan_fields(self) -> tuple[PlanningFieldSpec, ...]:
        """返回会写入 ``PlanDraftRequest`` 的字段目录。"""

        return tuple(
            spec
            for spec in self.request_form_fields
            if spec.plan_attribute is not None and spec.plan_attribute != "security_id"
        )


def default_planning_workflow_config() -> PlanningWorkflowConfig:
    """从类型化应用默认值生成 planning workflow 配置。"""

    return planning_workflow_config_from_settings(
        PlanningWorkflowSettings(),
        MarketSettings(),
        HitlSettings(),
    )


def planning_presenter_config_from_settings(
    settings: PlanningWorkflowSettings,
) -> PlanningPresenterConfig:
    """把 Planning 部署配置转换为 capability presenter 运行时配置。"""

    return PlanningPresenterConfig(
        choice_options=tuple(
            PlanningChoiceOptionSpec(
                key=item.operation_id,
                label=item.label,
                description=item.description,
                disabled=not item.enabled,
            )
            for item in settings.operations
        ),
        field_specs=tuple(
            PlanningFieldSpec(
                key=item.key,
                label=item.label,
                data_type=item.data_type,
                control_type=item.control_type,
                required=item.required,
                read_only=item.read_only,
                min_length=item.min_length,
                max_length=item.max_length,
                plan_attribute=item.plan_attribute,
                source_fallback=item.source_fallback,
                include_in_request_form=item.include_in_request_form,
                include_in_presenter_form=item.include_in_presenter_form,
                include_in_approval=item.include_in_approval,
                approval_severity=item.approval_severity,
            )
            for item in settings.fields
        ),
        artifact_sections=tuple(
            PlanningArtifactSectionSpec(
                title=item.title,
                kind=item.kind,
                field_keys=item.field_keys,
            )
            for item in settings.artifact_sections
        ),
        copy=PlanningPresenterCopy(
            choice_title=settings.choice_title,
            choice_description=settings.choice_description,
            choice_text_fallback=settings.choice_text_fallback,
            form_title_template=settings.card_form_title_template,
            form_description=settings.card_form_description,
            form_text_fallback_template=settings.card_form_text_fallback_template,
            approval_title=settings.approval_title,
            approval_description=settings.approval_description,
            approval_summary_template=settings.approval_summary_template,
            approval_text_fallback_template=settings.approval_text_fallback_template,
            artifact_title_template=settings.artifact_title_template,
            artifact_summary_template=settings.artifact_summary_template,
            artifact_text_fallback_template=settings.artifact_text_fallback_template,
            artifact_status_labels=dict(settings.artifact_status_labels),
            unsupported_title=settings.unsupported_title,
            field_provenance_label=settings.field_provenance_label,
            plan_provenance_label=settings.plan_provenance_label,
            evidence_provenance_label=settings.evidence_provenance_label,
            evidence_provenance_value=settings.evidence_provenance_value,
        ),
    )


def planning_workflow_config_from_settings(
    settings: PlanningWorkflowSettings,
    market: MarketSettings,
    hitl: HitlSettings,
) -> PlanningWorkflowConfig:
    """把部署配置转换为 Workflow 只读运行配置。

    Args:
        settings: Planning 入口、文案与操作目录。
        market: 美股市场代码、交易所目录与 symbol 规则。
        hitl: 交互有效期与文本字段限制。

    Returns:
        不依赖 Pydantic 的 Planning Workflow 配置值对象。
    """

    return PlanningWorkflowConfig(
        choice_title=settings.choice_title,
        choice_description=settings.choice_description,
        choice_text_fallback=settings.choice_text_fallback,
        choice_field_title=settings.choice_field_title,
        operations=tuple(
            PlanningOperationSpec(
                operation_id=item.operation_id,
                label=item.label,
                description=item.description,
                enabled=item.enabled,
                outcome=item.outcome,
                unsupported_kind=item.unsupported_kind,
                unsupported_message=item.unsupported_message,
            )
            for item in settings.operations
        ),
        direct_plan_unsupported_kind=settings.direct_plan_unsupported_kind,
        direct_plan_notice_message=settings.direct_plan_notice_message,
        direct_plan_direction_template=settings.direct_plan_direction_template,
        request_form_default_direction=settings.request_form_default_direction,
        form_title=settings.form_title,
        form_description=settings.form_description,
        form_text_fallback=settings.form_text_fallback,
        market_code=market.market_code,
        exchange_codes=market.exchange_codes,
        symbol_pattern=market.symbol_pattern,
        interaction_ttl_seconds=hitl.pending_ttl_seconds,
        text_field_max_length=hitl.text_field_max_length,
        unknown_operation_message=settings.unknown_operation_message,
        presenter_config=planning_presenter_config_from_settings(settings),
    )


class PlanningConversationWorkflow(ConversationWorkflow):
    """负责交易计划入口、表单与审批闭环的会话工作流。

    Contract:
        - 只管理“选择操作”和“直接创建计划”两类 planning 启动工作流。
        - 所有表单值都必须通过 HITL schema 校验后，才能进入 ``PlanningService``。
        - 该工作流只生成研究/决策用计划，不提供下单、账户或成交记录能力。

    Implemented by:
        ``apps/container.py`` 在组合根中装配的 planning workflow。
    """

    def __init__(
        self,
        *,
        planning: PlanningService,
        hitl_service: DefaultHitlService,
        presenter: PlanningCardPresenter | None = None,
        config: PlanningWorkflowConfig | None = None,
    ) -> None:
        self._planning = planning
        self._hitl = hitl_service
        self._config = config or default_planning_workflow_config()
        self._presenter = presenter or PlanningCardPresenter(self._config.presenter_config)

    @property
    def agent_id(self) -> str:
        """返回负责 Planning 工作流的 Agent ID。"""

        return Intent.PLANNING.value

    @property
    def workflow_ids(self) -> tuple[str, ...]:
        """返回本工作流负责的启动 ID。"""

        return (
            WORKFLOW_PLANNING_CHOOSE_OPERATION,
            WORKFLOW_PLANNING_CREATE_PLAN,
        )

    @property
    def subject_types(self) -> tuple[str, ...]:
        """返回本工作流负责恢复的 subject type。"""

        return (
            _SUBJECT_PLANNING_CHOICE,
            _SUBJECT_PLANNING_REQUEST,
            _SUBJECT_PLAN_FORM,
            _SUBJECT_PLAN_APPROVAL,
        )

    def start(
        self,
        context: WorkflowStartContext,
        runtime: WorkflowRuntime,
    ) -> ConversationRunResult:
        """根据 workflow_id 启动对应的 planning 流程。"""

        if context.classification.workflow_id == WORKFLOW_PLANNING_CHOOSE_OPERATION:
            return self._start_choice(context, runtime)
        if context.classification.workflow_id == WORKFLOW_PLANNING_CREATE_PLAN:
            return self._start_create_plan(context, runtime)
        raise ValueError(f"planning workflow 不支持 {context.classification.workflow_id}")

    def resume(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope | None:
        """从 planning 工作流的暂停点恢复执行。"""

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
        context: WorkflowStartContext,
        runtime: WorkflowRuntime,
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
        context: WorkflowStartContext,
        runtime: WorkflowRuntime,
    ) -> ConversationRunResult:
        symbol, exchange, identifier_locked = _classified_identifier(context.classification)
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
        interaction = self._create_request_form(
            context.owner_id,
            context.thread_id,
            context.run_id,
            str(uuid4()),
            symbol=symbol,
            exchange=exchange,
            identifier_locked=identifier_locked,
            direction=(
                self._config.direct_plan_direction_template.format(symbol=symbol)
                if symbol is not None
                else self._config.request_form_default_direction
            ),
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
        runtime: WorkflowRuntime,
    ) -> CardEnvelope:
        response = interaction.response or {}
        operation = self._operation_spec(str(response.get("choice", "")))
        if operation is None or operation.outcome != "plan_form" or not operation.enabled:
            card = runtime.create_unsupported_notice(
                reference_id=interaction.interaction_id,
                unsupported_kind=(operation.unsupported_kind if operation else None)
                or "unknown_operation",
                message=(operation.unsupported_message if operation else None)
                or self._config.unknown_operation_message,
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
            symbol=None,
            exchange=None,
            identifier_locked=False,
            direction=self._config.request_form_default_direction,
        )
        return runtime.publish_interaction(next_interaction, "card.created")

    def _handle_form(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope:
        values = interaction.response or {}
        if interaction.subject_type == _SUBJECT_PLANNING_REQUEST:
            plan = self._planning.create_draft(
                _request_from_values(
                    values,
                    field_specs=self._config.editable_plan_fields,
                    owner_id=interaction.owner_id,
                    plan_id=interaction.subject_id,
                    security_id=self._security_id_from_values(values),
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
                    field_specs=self._config.editable_plan_fields,
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
        runtime: WorkflowRuntime,
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
                            "title": self._config.choice_field_title,
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
                self._deadline(),
            )
        )

    def _create_request_form(
        self,
        owner_id: str,
        thread_id: str,
        run_id: str,
        plan_id: str,
        *,
        symbol: str | None,
        exchange: str | None,
        identifier_locked: bool,
        direction: str,
    ) -> HumanInteraction:
        return self._create_form_interaction(
            owner_id=owner_id,
            thread_id=thread_id,
            run_id=run_id,
            subject_type=_SUBJECT_PLANNING_REQUEST,
            plan_id=plan_id,
            plan_version=1,
            symbol=symbol,
            exchange=exchange,
            identifier_locked=identifier_locked,
            direction=direction,
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
            symbol=_security_symbol(plan.security_id),
            exchange=_security_exchange(plan.security_id),
            identifier_locked=True,
            direction=plan.direction,
            defaults={
                spec.key: getattr(plan, spec.plan_attribute)
                for spec in self._config.editable_plan_fields
                if spec.plan_attribute is not None
            },
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
        exchange: str | None,
        identifier_locked: bool,
        direction: str,
        defaults: Mapping[str, str | None],
    ) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": self._config.form_title,
            "description": self._config.form_description,
            "text_fallback": self._config.form_text_fallback,
        }
        properties = self._form_properties(
            symbol=symbol,
            exchange=exchange,
            identifier_locked=identifier_locked,
            direction=direction,
            defaults=defaults,
        )
        schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": properties,
            "required": [spec.key for spec in self._config.request_form_fields if spec.required],
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
                self._deadline(),
            )
        )

    def _operation_spec(self, operation_id: str) -> PlanningOperationSpec | None:
        """按稳定操作 ID 查找当前注册的 planning 入口定义。"""

        normalized = operation_id.strip()
        for item in self._config.operations:
            if item.operation_id == normalized:
                return item
        return None

    def _form_properties(
        self,
        *,
        symbol: str | None,
        exchange: str | None,
        identifier_locked: bool,
        direction: str,
        defaults: Mapping[str, str | None],
    ) -> dict[str, JsonValue]:
        """按字段目录生成 HITL request form 的 schema properties。"""

        properties: dict[str, JsonValue] = {}
        for spec in self._config.request_form_fields:
            field: dict[str, JsonValue] = {
                "type": "string",
                "title": spec.label,
            }
            if spec.key == "symbol":
                field["pattern"] = self._config.symbol_pattern
                field["default"] = symbol
                field["readOnly"] = identifier_locked
            elif spec.key == "exchange":
                field["enum"] = list(self._config.exchange_codes)
                field["default"] = exchange
                field["readOnly"] = identifier_locked
            elif spec.key == "direction":
                field["default"] = direction
            else:
                field["default"] = defaults.get(spec.key)
            if spec.min_length is not None:
                field["minLength"] = spec.min_length
            if spec.max_length is not None:
                max_length = min(spec.max_length, self._config.text_field_max_length)
                field["maxLength"] = max_length
            if spec.control_type == "textarea":
                field["format"] = "textarea"
            elif spec.control_type == "text":
                field["format"] = "text"
            properties[spec.key] = field
        return properties

    def _deadline(self) -> datetime:
        """根据部署级 HITL 策略计算新交互的截止时间。"""

        return datetime.now(UTC) + timedelta(seconds=self._config.interaction_ttl_seconds)

    def _security_id_from_values(self, values: Mapping[str, JsonValue]) -> str:
        """把人工表单中的交易所与代码组合为规范证券标识。"""

        symbol = _required_text(values, "symbol").upper()
        exchange = _required_text(values, "exchange").upper()
        if exchange not in self._config.exchange_codes:
            raise ValueError("交易所不在当前部署允许目录中")
        return f"{self._config.market_code}:{exchange}:{symbol}"

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
                self._deadline(),
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


def _classified_identifier(
    classification: IntentClassification,
) -> tuple[str | None, str | None, bool]:
    """从分类结果中提取可直接用于表单的证券标识信息。

    Returns:
        ``(symbol, exchange, identifier_locked)`` 三元组；如果分类器已给出
        ``security_id`` 或 ``exchange + symbol``，则返回锁定后的默认值。
    """

    security_id = classification.entity("security_id")
    if security_id is not None and security_id.strip():
        return _security_symbol(security_id), _security_exchange(security_id), True
    symbol = classification.entity("symbol")
    exchange = classification.entity("exchange")
    normalized_symbol = symbol.strip().upper() if symbol is not None and symbol.strip() else None
    normalized_exchange = (
        exchange.strip().upper() if exchange is not None and exchange.strip() else None
    )
    return (
        normalized_symbol,
        normalized_exchange,
        normalized_symbol is not None and normalized_exchange is not None,
    )


def _security_exchange(security_id: str) -> str:
    """从稳定 security_id 中提取交易所。"""

    parts = [segment for segment in security_id.split(":") if segment]
    if len(parts) >= 3:
        return parts[-2].upper()
    if len(parts) == 2:
        return parts[0].upper()
    raise ValueError(f"无法从 security_id 解析交易所: {security_id}")


def _security_symbol(security_id: str) -> str:
    """从稳定 security_id 中提取代码。"""

    parts = [segment for segment in security_id.split(":") if segment]
    if len(parts) >= 2:
        return parts[-1].upper()
    raise ValueError(f"无法从 security_id 解析代码: {security_id}")


def _request_from_values(
    values: Mapping[str, JsonValue],
    *,
    field_specs: tuple[PlanningFieldSpec, ...],
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
        field_sources={
            spec.key: "用户输入"
            for spec in field_specs
            if spec.plan_attribute is not None and spec.key != "security_id"
        },
    )


def _payload_hash(payload: Mapping[str, JsonValue]) -> str:
    """计算一个 HITL payload 的稳定哈希。"""

    from trade_agent.adapters.sqlite.json_support import payload_hash

    return payload_hash(payload)


__all__ = [
    "PlanningConversationWorkflow",
    "PlanningOperationSpec",
    "PlanningWorkflowConfig",
    "default_planning_workflow_config",
    "planning_presenter_config_from_settings",
    "planning_workflow_config_from_settings",
]
