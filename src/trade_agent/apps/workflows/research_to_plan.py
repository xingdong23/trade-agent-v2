"""Research -> plan 会话工作流插件。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from trade_agent.apps.workflows.contracts import (
    ConversationRunResult,
    ConversationWorkflow,
    WorkflowRuntime,
    WorkflowStartContext,
)
from trade_agent.capabilities.planning.application import (
    PlanDraftRequest,
    PlanningService,
    review_payload,
)
from trade_agent.capabilities.planning.cards import PlanningCardPresenter
from trade_agent.capabilities.planning.contracts import PlanLineage, ReviewOutcome, TradingPlan
from trade_agent.core.config import ResearchToPlanWorkflowSettings
from trade_agent.core.hitl import (
    DefaultHitlService,
    HumanInteraction,
    InteractionStatus,
    InteractionType,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.runtime import Intent

WORKFLOW_RESEARCH_TO_PLAN = "research_to_plan"

_SUBJECT_SECURITY_CHOICE = "research_security_choice"
_SUBJECT_SCAN_REVIEW = "research_scan_review"
_SUBJECT_PLAN_FORM = "research_plan_form"
_SUBJECT_PLAN_APPROVAL = "research_plan_approval"
_SUBJECT_REMINDER_APPROVAL = "research_reminder_approval"
_SUBJECT_PLAN_REVIEW = "research_plan_review"


def _require_non_empty_config_text(value: str, field_name: str) -> None:
    """拒绝配置中的空白文本字段。"""

    if not value.strip():
        raise ValueError(f"{field_name} 不能为空")


@dataclass(frozen=True, slots=True)
class SecurityCandidate:
    """证券代码无法唯一解析时展示给用户的候选项。

    Attributes:
        security_id: 已规范化的稳定证券标识。
        label: 可供用户辨认的展示名称。
    """

    security_id: str
    label: str


@dataclass(frozen=True, slots=True)
class ResearchWorkflowResult:
    """Research capability 完成一次准备后交回编排层的结果集合。

    Attributes:
        security_id: 本次研究绑定的规范证券标识。
        research_card: 已完成研究产物 Card。
        scan_progress_started: 扫描开始时的进度 Card。
        scan_progress_completed: 扫描结束时的进度 Card。
        scan_result_card: 已持久化扫描结果的只读 Card。
        plan_values: 后续生成计划草稿所需的结构化事实。

    Invariants:
        - Card 必须来自 capability presenter，workflow 不能重算预测或排名。
    """

    security_id: str
    research_card: CardEnvelope
    scan_progress_started: CardEnvelope
    scan_progress_completed: CardEnvelope
    scan_result_card: CardEnvelope
    plan_values: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class SecurityClarificationConfig:
    """证券澄清与未命中提示的部署级配置。

    Attributes:
        option_title: 证券选择字段在 HITL schema 中的标题。
        title: 用户需要从多个候选证券中做出选择时的卡片标题。
        description: 澄清步骤的说明文案。
        text_fallback: 非结构化客户端展示的降级文案。
        unsupported_kind: 找不到证券时返回的稳定问题编码。
        unsupported_message: 无法解析证券时返回的用户提示。
        unsupported_source_type: unsupported notice 在 CardSource 中记录的来源类型。
    """

    option_title: str
    title: str
    description: str
    text_fallback: str
    unsupported_kind: str
    unsupported_message: str
    unsupported_source_type: str

    def __post_init__(self) -> None:
        _require_non_empty_config_text(self.option_title, "security_clarification.option_title")
        _require_non_empty_config_text(self.title, "security_clarification.title")
        _require_non_empty_config_text(self.description, "security_clarification.description")
        _require_non_empty_config_text(self.text_fallback, "security_clarification.text_fallback")
        _require_non_empty_config_text(
            self.unsupported_kind, "security_clarification.unsupported_kind"
        )
        _require_non_empty_config_text(
            self.unsupported_message, "security_clarification.unsupported_message"
        )
        _require_non_empty_config_text(
            self.unsupported_source_type, "security_clarification.unsupported_source_type"
        )


@dataclass(frozen=True, slots=True)
class ScanReviewConfig:
    """扫描复核交互的部署级文案配置。

    Attributes:
        title: 扫描复核卡片标题。
        description: 人工确认前的说明文案。
        finding_label: findings 中展示扫描结论的标签。
        text_fallback: 非结构化客户端展示的降级文案。
    """

    title: str
    description: str
    finding_label: str
    text_fallback: str

    def __post_init__(self) -> None:
        _require_non_empty_config_text(self.title, "scan_review.title")
        _require_non_empty_config_text(self.description, "scan_review.description")
        _require_non_empty_config_text(self.finding_label, "scan_review.finding_label")
        _require_non_empty_config_text(self.text_fallback, "scan_review.text_fallback")


@dataclass(frozen=True, slots=True)
class PlanApprovalPayloadStrategy:
    """计划审批交互 payload 的投影策略。

    Attributes:
        payload_fields: 从 ``PlanningCardPresenter.plan_approval`` 结果中转发到审批交互的字段。
        include_text_fallback: 是否一并转发 presenter 生成的 ``text_fallback``。

    Notes:
        Workflow 只负责决定在何时请求审批；具体暴露哪些审批字段属于部署策略。
    """

    payload_fields: tuple[str, ...]
    include_text_fallback: bool

    def __post_init__(self) -> None:
        if not self.payload_fields:
            raise ValueError("plan_approval.payload_fields 不能为空")
        normalized = tuple(field_name.strip() for field_name in self.payload_fields)
        if any(not field_name for field_name in normalized):
            raise ValueError("plan_approval.payload_fields 不能包含空值")
        if len(set(normalized)) != len(normalized):
            raise ValueError("plan_approval.payload_fields 不能重复")
        object.__setattr__(self, "payload_fields", normalized)

    def build_payload(self, preview: CardEnvelope) -> dict[str, JsonValue]:
        """从审批预览卡投影出审批交互 payload。"""

        payload: dict[str, JsonValue] = {}
        for field_name in self.payload_fields:
            if field_name not in preview.data:
                raise ValueError(f"计划审批预览缺少字段 {field_name}")
            payload[field_name] = preview.data[field_name]
        if self.include_text_fallback:
            payload["text_fallback"] = preview.text_fallback
        return payload


@dataclass(frozen=True, slots=True)
class ReminderApprovalConfig:
    """计划提醒审批与激活时使用的部署级配置。

    Attributes:
        title: 提醒审批卡片标题。
        description: 提醒审批说明。
        summary_template: 提醒审批摘要模板，必须接受 ``plan_id`` 占位符。
        plan_fact_label: 审批 facts 中展示计划标识的标签。
        channel_fact_label: 审批 facts 中展示提醒渠道的标签。
        notification_channel: Workflow 请求 backend 激活提醒时传入的渠道标识。
        text_fallback: 非结构化客户端展示的降级文案。
    """

    title: str
    description: str
    summary_template: str
    plan_fact_label: str
    channel_fact_label: str
    notification_channel: str
    text_fallback: str

    def __post_init__(self) -> None:
        _require_non_empty_config_text(self.title, "reminder_approval.title")
        _require_non_empty_config_text(self.description, "reminder_approval.description")
        _require_non_empty_config_text(self.summary_template, "reminder_approval.summary_template")
        _require_non_empty_config_text(self.plan_fact_label, "reminder_approval.plan_fact_label")
        _require_non_empty_config_text(
            self.channel_fact_label, "reminder_approval.channel_fact_label"
        )
        _require_non_empty_config_text(
            self.notification_channel, "reminder_approval.notification_channel"
        )
        _require_non_empty_config_text(self.text_fallback, "reminder_approval.text_fallback")
        try:
            self.summary_template.format(plan_id="PLAN_ID")
        except KeyError as exc:
            raise ValueError(
                "reminder_approval.summary_template 必须包含 {plan_id} 占位符"
            ) from exc


@dataclass(frozen=True, slots=True)
class ReviewFeedbackDestinationOption:
    """计划复盘可选反馈去向。

    Attributes:
        value: 写入 planning capability 的稳定反馈目标值。
        label: 展示给用户的可读标签。
    """

    value: str
    label: str

    def __post_init__(self) -> None:
        _require_non_empty_config_text(self.value, "plan_review.feedback_destination.value")
        _require_non_empty_config_text(self.label, "plan_review.feedback_destination.label")


@dataclass(frozen=True, slots=True)
class PlanReviewConfig:
    """计划复盘交互与资源落库的部署级配置。

    Attributes:
        title: 复盘卡片标题。
        description: 复盘说明文案。
        finding_label: findings 中展示闭环状态的标签。
        finding_detail: findings 中关于闭环来源关系的说明。
        text_fallback: 非结构化客户端展示的降级文案。
        feedback_destinations: 允许写入 planning review 的反馈目标目录。
        resource_name: 复盘结果通过 runtime 保存到的资源集合名。
    """

    title: str
    description: str
    finding_label: str
    finding_detail: str
    text_fallback: str
    feedback_destinations: tuple[ReviewFeedbackDestinationOption, ...]
    resource_name: str

    def __post_init__(self) -> None:
        _require_non_empty_config_text(self.title, "plan_review.title")
        _require_non_empty_config_text(self.description, "plan_review.description")
        _require_non_empty_config_text(self.finding_label, "plan_review.finding_label")
        _require_non_empty_config_text(self.finding_detail, "plan_review.finding_detail")
        _require_non_empty_config_text(self.text_fallback, "plan_review.text_fallback")
        _require_non_empty_config_text(self.resource_name, "plan_review.resource_name")
        if not self.feedback_destinations:
            raise ValueError("plan_review.feedback_destinations 不能为空")
        values = tuple(item.value for item in self.feedback_destinations)
        if len(set(values)) != len(values):
            raise ValueError("plan_review.feedback_destinations 不能重复")

    def response_schema(self, text_field_max_length: int) -> dict[str, JsonValue]:
        """生成计划复盘所需的结构化响应 schema。"""

        values: list[JsonValue] = [item.value for item in self.feedback_destinations]
        options: list[JsonValue] = [
            {"key": item.value, "label": item.label, "description": item.value}
            for item in self.feedback_destinations
        ]
        return {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "title": "复盘结论",
                    "enum": [item.value for item in ReviewOutcome],
                },
                "note": {
                    "type": "string",
                    "title": "复盘说明",
                    "minLength": 1,
                    "maxLength": text_field_max_length,
                },
                "feedback_destinations": {
                    "type": "array",
                    "title": "反馈用途",
                    "items": {
                        "type": "string",
                        "enum": values,
                    },
                    "minItems": 1,
                    "uniqueItems": True,
                    "x-options": options,
                },
            },
            "required": ["outcome", "note", "feedback_destinations"],
            "additionalProperties": False,
        }


@dataclass(frozen=True, slots=True)
class PlanLineageConfig:
    """研究工作流生成计划草稿时使用的 lineage 策略。

    Attributes:
        source_type: 写入 ``PlanLineage.source_type`` 的稳定来源类型。
    """

    source_type: str

    def __post_init__(self) -> None:
        _require_non_empty_config_text(self.source_type, "plan_lineage.source_type")


@dataclass(frozen=True, slots=True)
class ResearchToPlanWorkflowConfig:
    """Research-to-plan Workflow 的部署级策略集合。

    Attributes:
        security_clarification: 证券歧义澄清与证券未命中提示策略。
        scan_review: 扫描复核步骤的文案策略。
        plan_approval: 计划审批交互的 payload 投影策略。
        reminder_approval: 提醒审批与提醒渠道策略。
        plan_review: 计划复盘交互与 review 资源保存策略。
        plan_lineage: 计划草稿 lineage 的来源类型策略。
    """

    security_clarification: SecurityClarificationConfig
    scan_review: ScanReviewConfig
    plan_approval: PlanApprovalPayloadStrategy
    reminder_approval: ReminderApprovalConfig
    plan_review: PlanReviewConfig
    plan_lineage: PlanLineageConfig


def research_to_plan_workflow_config_from_settings(
    settings: ResearchToPlanWorkflowSettings,
) -> ResearchToPlanWorkflowConfig:
    """把类型化部署配置转换为 Research-to-plan Workflow 运行时策略。

    Args:
        settings: 从 ``AppSettings`` 读取的只读部署配置。

    Returns:
        不依赖 Pydantic 的 Workflow 运行时配置对象。
    """

    clarification = settings.security_clarification
    scan_review = settings.scan_review
    plan_approval = settings.plan_approval
    reminder_approval = settings.reminder_approval
    plan_review = settings.plan_review
    return ResearchToPlanWorkflowConfig(
        security_clarification=SecurityClarificationConfig(
            option_title=clarification.option_title,
            title=clarification.title,
            description=clarification.description,
            text_fallback=clarification.text_fallback,
            unsupported_kind=clarification.unsupported_kind,
            unsupported_message=clarification.unsupported_message,
            unsupported_source_type=clarification.unsupported_source_type,
        ),
        scan_review=ScanReviewConfig(
            title=scan_review.title,
            description=scan_review.description,
            finding_label=scan_review.finding_label,
            text_fallback=scan_review.text_fallback,
        ),
        plan_approval=PlanApprovalPayloadStrategy(
            payload_fields=plan_approval.payload_fields,
            include_text_fallback=plan_approval.include_text_fallback,
        ),
        reminder_approval=ReminderApprovalConfig(
            title=reminder_approval.title,
            description=reminder_approval.description,
            summary_template=reminder_approval.summary_template,
            plan_fact_label=reminder_approval.plan_fact_label,
            channel_fact_label=reminder_approval.channel_fact_label,
            notification_channel=reminder_approval.notification_channel,
            text_fallback=reminder_approval.text_fallback,
        ),
        plan_review=PlanReviewConfig(
            title=plan_review.title,
            description=plan_review.description,
            finding_label=plan_review.finding_label,
            finding_detail=plan_review.finding_detail,
            text_fallback=plan_review.text_fallback,
            feedback_destinations=tuple(
                ReviewFeedbackDestinationOption(item.value, item.label)
                for item in plan_review.feedback_destinations
            ),
            resource_name=plan_review.resource_name,
        ),
        plan_lineage=PlanLineageConfig(source_type=settings.plan_lineage.source_type),
    )


class ResearchWorkflowBackend(Protocol):
    """研究工作流依赖的应用边界。

    Contract:
        - 实现方负责解析证券、准备研究、总结扫描和激活提醒。
        - ``summarize`` 只能读取已持久化扫描结果，不能参与预测、评分或排序。
        - 所有读取和写入必须使用 ``owner_id`` 隔离，并遵守 idempotency key。

    Implemented by:
        生产 capability façade 和端到端验收测试的 fake backend。
    """

    def resolve(self, symbol: str, *, owner_id: str, run_id: str) -> tuple[SecurityCandidate, ...]:
        """把一个证券代码解析为一个或多个美国上市候选项。"""

    def prepare(self, security_id: str, *, owner_id: str, run_id: str) -> ResearchWorkflowResult:
        """准备研究与量化扫描结果。"""

    def summarize(
        self,
        scan_result: Mapping[str, JsonValue],
        *,
        owner_id: str,
        run_id: str,
    ) -> str:
        """对持久化扫描结果做只读总结。"""

    def activate_reminder(
        self,
        *,
        owner_id: str,
        plan_id: str,
        interaction_id: str,
        idempotency_key: str,
        notification_channel: str,
    ) -> CardEnvelope:
        """激活一个仅用于复核的提醒。"""


class ResearchToPlanWorkflow(ConversationWorkflow):
    """负责研究、扫描、计划、提醒与复盘闭环的会话工作流。

    Contract:
        - 研究结果必须先持久化，再允许人工复核与后续计划生成。
        - LLM 只能总结扫描结果，不参与价格预测、评分或排序。
        - 该工作流生成的所有计划、提醒与复盘都必须保留来源关系。

    Implemented by:
        组合根装配了 ``ResearchWorkflowBackend`` 后注册到 runtime 的 research 插件。
    """

    def __init__(
        self,
        *,
        backend: ResearchWorkflowBackend,
        planning: PlanningService,
        hitl_service: DefaultHitlService,
        interaction_ttl_seconds: int,
        text_field_max_length: int,
        config: ResearchToPlanWorkflowConfig,
        presenter: PlanningCardPresenter,
    ) -> None:
        self._backend = backend
        self._planning = planning
        self._hitl = hitl_service
        if interaction_ttl_seconds < 60 or text_field_max_length < 1:
            raise ValueError("research workflow 的 HITL 策略无效")
        self._interaction_ttl_seconds = interaction_ttl_seconds
        self._text_field_max_length = text_field_max_length
        self._config = config
        self._presenter = presenter

    @property
    def agent_id(self) -> str:
        """返回负责研究到计划工作流的 Agent ID。"""

        return Intent.RESEARCH.value

    @property
    def workflow_ids(self) -> tuple[str, ...]:
        """返回本工作流负责的启动 ID。"""

        return (WORKFLOW_RESEARCH_TO_PLAN,)

    @property
    def subject_types(self) -> tuple[str, ...]:
        """返回本工作流负责恢复的 subject type。"""

        return (
            _SUBJECT_SECURITY_CHOICE,
            _SUBJECT_SCAN_REVIEW,
            _SUBJECT_PLAN_FORM,
            _SUBJECT_PLAN_APPROVAL,
            _SUBJECT_REMINDER_APPROVAL,
            _SUBJECT_PLAN_REVIEW,
        )

    def start(
        self,
        context: WorkflowStartContext,
        runtime: WorkflowRuntime,
    ) -> ConversationRunResult:
        """启动 research -> plan 流程。"""

        symbol = context.require_entity("symbol")
        candidates = self._backend.resolve(
            symbol,
            owner_id=context.owner_id,
            run_id=context.run_id,
        )
        if not candidates:
            clarification = self._config.security_clarification
            notice = runtime.create_unsupported_notice(
                reference_id=context.run_id,
                unsupported_kind=clarification.unsupported_kind,
                message=clarification.unsupported_message,
                source_type=clarification.unsupported_source_type,
            )
            runtime.publish_card(
                context.owner_id,
                context.thread_id,
                context.run_id,
                notice,
                "card.failed",
            )
            return ConversationRunResult(
                context.run_id,
                context.thread_id,
                "unsupported",
                card=notice,
            )
        if len(candidates) > 1:
            interaction = self._create_security_choice(
                context.owner_id,
                context.thread_id,
                context.run_id,
                candidates,
            )
            card = runtime.publish_interaction(interaction, "card.created")
            return ConversationRunResult(
                context.run_id,
                context.thread_id,
                "waiting_for_human",
                interaction.interaction_id,
                card,
            )
        card = self._prepare_research_workflow(
            owner_id=context.owner_id,
            thread_id=context.thread_id,
            run_id=context.run_id,
            security_id=candidates[0].security_id,
            runtime=runtime,
        )
        return ConversationRunResult(
            context.run_id,
            context.thread_id,
            "waiting_for_human",
            card.source.source_id,
            card,
        )

    def resume(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope | None:
        """从 research 工作流的暂停点恢复执行。"""

        if interaction.subject_type == _SUBJECT_SECURITY_CHOICE:
            runtime.publish_interaction(interaction, "card.resolved")
            return self._handle_security_choice(interaction, runtime)
        if interaction.subject_type == _SUBJECT_SCAN_REVIEW:
            return self._handle_scan_review(interaction, runtime)
        if interaction.subject_type == _SUBJECT_PLAN_FORM:
            runtime.publish_interaction(interaction, "card.resolved")
            return self._handle_plan_form(interaction, runtime)
        if interaction.subject_type == _SUBJECT_PLAN_APPROVAL:
            return self._handle_plan_approval(interaction, runtime)
        if interaction.subject_type == _SUBJECT_REMINDER_APPROVAL:
            return self._handle_reminder_approval(interaction, runtime)
        if interaction.subject_type == _SUBJECT_PLAN_REVIEW:
            return self._handle_plan_review(interaction, runtime)
        return None

    def _handle_security_choice(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope:
        selected = _required_text(interaction.response or {}, "selected_security")
        return self._prepare_research_workflow(
            owner_id=interaction.owner_id,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
            security_id=selected,
            runtime=runtime,
        )

    def _prepare_research_workflow(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        security_id: str,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope:
        result = self._backend.prepare(security_id, owner_id=owner_id, run_id=run_id)
        runtime.publish_card(
            owner_id,
            thread_id,
            run_id,
            result.research_card,
            "card.created",
            artifact=True,
        )
        runtime.publish_card(
            owner_id,
            thread_id,
            run_id,
            result.scan_progress_started,
            "card.created",
        )
        runtime.publish_card(
            owner_id,
            thread_id,
            run_id,
            result.scan_progress_completed,
            "card.resolved",
        )
        runtime.publish_card(
            owner_id,
            thread_id,
            run_id,
            result.scan_result_card,
            "card.created",
            artifact=True,
        )
        runtime.save_run_context(
            owner_id=owner_id,
            run_id=run_id,
            thread_id=thread_id,
            payload={
                "security_id": result.security_id,
                "scan_result": result.scan_result_card.to_mapping(),
                "plan_values": dict(result.plan_values),
            },
        )
        review = self._create_scan_review(
            owner_id=owner_id,
            thread_id=thread_id,
            run_id=run_id,
            scan_result=result.scan_result_card,
        )
        return runtime.publish_interaction(review, "card.created")

    def _handle_scan_review(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope:
        runtime.publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        workflow = runtime.require_run_context(interaction.owner_id, interaction.run_id)
        scan_result = workflow["scan_result"]
        plan_values = workflow["plan_values"]
        if not isinstance(scan_result, Mapping) or not isinstance(plan_values, Mapping):
            raise RuntimeError("research run context 格式无效")
        summary = self._backend.summarize(
            scan_result,
            owner_id=interaction.owner_id,
            run_id=interaction.run_id,
        )
        plan = self._planning.create_draft(
            _workflow_plan_request(
                plan_values,
                plan_id=str(uuid4()),
                owner_id=interaction.owner_id,
                security_id=_required_text(workflow, "security_id"),
                run_id=interaction.run_id,
                summary=summary,
                lineage_config=self._config.plan_lineage,
            ),
            idempotency_key=f"{interaction.interaction_id}:research-plan-draft",
        )
        approval = self._create_plan_approval(
            plan,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
        )
        return runtime.publish_interaction(approval, "card.created")

    def _handle_plan_form(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope:
        current = self._planning.get_plan(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
        )
        revised = self._planning.revise_draft(
            _request_from_values(
                interaction.response or {},
                owner_id=interaction.owner_id,
                plan_id=current.plan_id,
                security_id=current.security_id,
                run_id=interaction.run_id,
                source_references=current.source_references,
            ),
            expected_version=current.version,
            idempotency_key=f"{interaction.interaction_id}:revise-draft",
        )
        approval = self._create_plan_approval(
            revised,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
        )
        return runtime.publish_interaction(approval, "card.created")

    def _handle_plan_approval(
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
        published = runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            artifact,
            "card.created",
            artifact=True,
        )
        reminder = self._create_reminder_approval(active, interaction)
        runtime.publish_interaction(reminder, "card.created")
        return published

    def _handle_reminder_approval(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope:
        runtime.publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        reminder = self._backend.activate_reminder(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
            interaction_id=interaction.interaction_id,
            idempotency_key=f"{interaction.interaction_id}:activate-reminder",
            notification_channel=self._config.reminder_approval.notification_channel,
        )
        published = runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            reminder,
            "card.created",
            artifact=True,
        )
        plan = self._planning.get_plan(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
        )
        review = self._create_plan_review(plan, interaction)
        runtime.publish_interaction(review, "card.created")
        return published

    def _handle_plan_review(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope:
        runtime.publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        response = interaction.response or {}
        result = self._planning.record_review(
            owner_id=interaction.owner_id,
            review_id=str(uuid4()),
            subject_type="plan",
            subject_id=interaction.subject_id,
            subject_version=interaction.subject_version,
            outcome=ReviewOutcome(_required_text(response, "outcome")),
            annotations={"note": _required_text(response, "note")},
            lineage=(),
            feedback_destinations=_required_string_tuple(response, "feedback_destinations"),
            actor_id=interaction.owner_id,
            approval_interaction_id=interaction.interaction_id,
            idempotency_key=f"{interaction.interaction_id}:record-review",
            created_at=datetime.now(UTC),
        )
        runtime.save_resource(
            owner_id=interaction.owner_id,
            resource_name=self._config.plan_review.resource_name,
            resource_id=result.review.review_id,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
            payload=review_payload(result),
        )
        reviewed = result.reviewed_plan
        if reviewed is None:  # pragma: no cover
            raise RuntimeError("计划复盘没有返回 reviewed plan")
        return runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            self._presenter.plan_artifact(reviewed),
            "card.created",
            artifact=True,
        )

    def _create_security_choice(
        self,
        owner_id: str,
        thread_id: str,
        run_id: str,
        candidates: tuple[SecurityCandidate, ...],
    ) -> HumanInteraction:
        clarification = self._config.security_clarification
        payload: dict[str, JsonValue] = {
            "title": clarification.title,
            "description": clarification.description,
            "text_fallback": clarification.text_fallback,
        }
        options: list[JsonValue] = [
            {
                "key": item.security_id,
                "label": item.label,
                "description": item.security_id,
                "disabled": False,
            }
            for item in candidates
        ]
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
                _SUBJECT_SECURITY_CHOICE,
                run_id,
                1,
                _payload_hash(payload),
                {
                    "type": "object",
                    "properties": {
                        "selected_security": {
                            "type": "string",
                            "title": clarification.option_title,
                            "enum": [item.security_id for item in candidates],
                            "x-options": options,
                        }
                    },
                    "required": ["selected_security"],
                    "additionalProperties": False,
                },
                datetime.now(UTC),
                self._deadline(),
            )
        )

    def _create_scan_review(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        scan_result: CardEnvelope,
    ) -> HumanInteraction:
        scan_review = self._config.scan_review
        payload: dict[str, JsonValue] = {
            "title": scan_review.title,
            "description": scan_review.description,
            "findings": [
                {
                    "label": scan_review.finding_label,
                    "detail": scan_result.text_fallback,
                    "severity": "medium",
                }
            ],
            "text_fallback": scan_review.text_fallback,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                owner_id,
                InteractionType.REVIEW,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                _SUBJECT_SCAN_REVIEW,
                scan_result.source.source_id,
                scan_result.source.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                self._deadline(),
            )
        )

    def _create_plan_form(
        self,
        plan: TradingPlan,
        *,
        thread_id: str,
        run_id: str,
    ) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": "补充研究生成的美股交易计划",
            "description": "系统不会下单。请人工复核并补齐周期、入场、失效、目标、仓位和风险。",
            "text_fallback": "请补充研究生成的交易计划字段。",
        }
        properties: dict[str, JsonValue] = {
            "direction": {
                "type": "string",
                "title": "方向或逻辑",
                "minLength": 1,
                "default": plan.direction,
            },
            "horizon": {
                "type": "string",
                "title": "计划周期",
                "minLength": 1,
                "default": plan.horizon,
            },
            "entry_condition": {
                "type": "string",
                "title": "入场条件",
                "minLength": 1,
                "default": plan.entry_condition,
            },
            "invalidation_condition": {
                "type": "string",
                "title": "失效或止损条件",
                "minLength": 1,
                "default": plan.invalidation_condition,
            },
            "target": {
                "type": "string",
                "title": "目标条件",
                "minLength": 1,
                "default": plan.target,
            },
            "position_notes": {
                "type": "string",
                "title": "仓位备注",
                "minLength": 1,
                "default": plan.position_notes,
            },
            "risk_notes": {
                "type": "string",
                "title": "风险说明",
                "minLength": 1,
                "default": plan.risk_notes,
            },
        }
        schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                plan.owner_id,
                InteractionType.EXCEPTION_RESOLUTION,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                _SUBJECT_PLAN_FORM,
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                schema,
                datetime.now(UTC),
                self._deadline(),
            )
        )

    def _create_plan_approval(
        self,
        plan: TradingPlan,
        *,
        thread_id: str,
        run_id: str,
    ) -> HumanInteraction:
        preview = self._presenter.plan_approval(plan)
        payload = self._config.plan_approval.build_payload(preview)
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

    def _create_reminder_approval(
        self,
        plan: TradingPlan,
        source: HumanInteraction,
    ) -> HumanInteraction:
        reminder_config = self._config.reminder_approval
        payload: dict[str, JsonValue] = {
            "title": reminder_config.title,
            "description": reminder_config.description,
            "summary": reminder_config.summary_template.format(plan_id=plan.plan_id),
            "facts": [
                {
                    "label": reminder_config.plan_fact_label,
                    "detail": plan.plan_id,
                    "severity": "low",
                },
                {
                    "label": reminder_config.channel_fact_label,
                    "detail": reminder_config.notification_channel,
                    "severity": "low",
                },
            ],
            "text_fallback": reminder_config.text_fallback,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                plan.owner_id,
                InteractionType.APPROVAL,
                InteractionStatus.PENDING,
                payload,
                1,
                source.thread_id,
                source.run_id,
                _SUBJECT_REMINDER_APPROVAL,
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                self._deadline(),
            )
        )

    def _create_plan_review(self, plan: TradingPlan, source: HumanInteraction) -> HumanInteraction:
        review_config = self._config.plan_review
        payload: dict[str, JsonValue] = {
            "title": review_config.title,
            "description": review_config.description,
            "findings": [
                {
                    "label": review_config.finding_label,
                    "detail": review_config.finding_detail,
                    "severity": "low",
                }
            ],
            "text_fallback": review_config.text_fallback,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                plan.owner_id,
                InteractionType.REVIEW,
                InteractionStatus.PENDING,
                payload,
                1,
                source.thread_id,
                source.run_id,
                _SUBJECT_PLAN_REVIEW,
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                review_config.response_schema(self._text_field_max_length),
                datetime.now(UTC),
                self._deadline(),
            )
        )

    def _deadline(self) -> datetime:
        """根据部署级 HITL 策略计算新交互的截止时间。"""

        return datetime.now(UTC) + timedelta(seconds=self._interaction_ttl_seconds)


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
    source_references: tuple[PlanLineage, ...],
) -> PlanDraftRequest:
    """把研究流程中的人工修订值转换为 Planning capability 输入。"""

    return PlanDraftRequest(
        plan_id=plan_id,
        owner_id=owner_id,
        security_id=security_id,
        direction=_required_text(values, "direction"),
        created_at=datetime.now(UTC),
        source_references=source_references,
        horizon=_required_text(values, "horizon"),
        entry_condition=_required_text(values, "entry_condition"),
        invalidation_condition=_required_text(values, "invalidation_condition"),
        target=_required_text(values, "target"),
        position_notes=_required_text(values, "position_notes"),
        risk_notes=_required_text(values, "risk_notes"),
        field_sources={
            "direction": "人工修订",
            "horizon": "人工修订",
            "entry_condition": "人工修订",
            "invalidation_condition": "人工修订",
            "target": "人工修订",
            "position_notes": "人工修订",
            "risk_notes": "人工修订",
        },
    )


def _workflow_plan_request(
    values: Mapping[str, JsonValue],
    *,
    plan_id: str,
    owner_id: str,
    security_id: str,
    run_id: str,
    summary: str,
    lineage_config: PlanLineageConfig,
) -> PlanDraftRequest:
    """使用扫描 lineage 和 LLM 摘要构造研究工作流中的计划草稿。"""

    scan_result_id = _required_text(values, "scan_result_id")
    scan_result_version = _required_int(values, "scan_result_version")
    evidence_ids = _required_string_tuple(values, "evidence_ids")
    strategy_id = _required_text(values, "strategy_id")
    strategy_version = _required_int(values, "strategy_version")
    model_version_id = _required_text(values, "model_version_id")
    return PlanDraftRequest(
        plan_id=plan_id,
        owner_id=owner_id,
        security_id=security_id,
        direction=_required_text(values, "direction"),
        created_at=datetime.now(UTC),
        source_references=(
            PlanLineage(
                lineage_config.source_type,
                scan_result_id,
                scan_result_version,
                evidence_ids=evidence_ids,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                model_version_id=model_version_id,
            ),
        ),
        horizon=_required_text(values, "horizon"),
        entry_condition=_required_text(values, "entry_condition"),
        invalidation_condition=_required_text(values, "invalidation_condition"),
        target=_required_text(values, "target"),
        position_notes=_required_text(values, "position_notes"),
        risk_notes=f"{_required_text(values, 'risk_notes')} LLM 摘要: {summary}",
        field_sources={
            "direction": "用户请求",
            "horizon": "strategy_version",
            "entry_condition": "research_artifact",
            "invalidation_condition": "research_artifact",
            "target": "scan_result",
            "position_notes": "用户策略",
            "risk_notes": "research_summary",
        },
    )


def _required_int(values: Mapping[str, JsonValue], field: str) -> int:
    """读取一个必须存在且大于零的版本号字段。"""

    value = values.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"计划 lineage 缺少有效版本字段 {field}")
    return value


def _required_string_tuple(values: Mapping[str, JsonValue], field: str) -> tuple[str, ...]:
    """读取一个必须存在且元素非空的字符串数组。"""

    value = values.get(field)
    if not isinstance(value, list) or not value:
        raise ValueError(f"计划 lineage 缺少非空数组字段 {field}")
    normalized = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(normalized) != len(value):
        raise ValueError(f"计划 lineage 字段 {field} 包含无效值")
    return normalized


def _payload_hash(payload: Mapping[str, JsonValue]) -> str:
    """计算一个 HITL payload 的稳定哈希。"""

    from trade_agent.adapters.sqlite.json_support import payload_hash

    return payload_hash(payload)


__all__ = [
    "PlanApprovalPayloadStrategy",
    "PlanLineageConfig",
    "PlanReviewConfig",
    "ReminderApprovalConfig",
    "ResearchToPlanWorkflow",
    "ResearchToPlanWorkflowConfig",
    "ResearchWorkflowBackend",
    "ResearchWorkflowResult",
    "ReviewFeedbackDestinationOption",
    "ScanReviewConfig",
    "SecurityCandidate",
    "SecurityClarificationConfig",
    "research_to_plan_workflow_config_from_settings",
]
