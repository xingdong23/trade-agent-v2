"""Planning capability 到 Card 协议的确定性投影。

同一个计划在不同阶段会产生不同类型的卡片：入口选择、表单、审批和最终 artifact。
本模块不再维护分散的字段标签常量，而是统一消费字段目录与 presenter 配置。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from trade_agent.capabilities.planning.contracts import PlanStatus, TradingPlan
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource


@dataclass(frozen=True, slots=True)
class PlanningChoiceOptionSpec:
    """Choice 卡中的单个可选操作。

    Attributes:
        key: 提交给后端的稳定操作标识。
        label: 面向用户的选项标题。
        description: 选项补充说明。
        disabled: 当前是否不可选。
    """

    key: str
    label: str
    description: str
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class PlanningFieldSpec:
    """Planning 卡片与 Workflow 共用的字段目录项。

    Attributes:
        key: 稳定字段标识。
        label: 展示标题。
        data_type: Card 协议使用的数据类型。
        control_type: 前端建议控件类型。
        required: 是否必填。
        read_only: 是否只读。
        min_length: 最小文本长度；``None`` 表示不声明。
        max_length: 最大文本长度；``None`` 表示不声明。
        plan_attribute: 若字段映射到 ``TradingPlan``，对应的属性名。
        source_fallback: 字段来源缺失时的默认文案。
        include_in_request_form: 是否用于 Workflow 创建 HITL 请求表单。
        include_in_presenter_form: 是否用于 presenter 生成计划表单卡。
        include_in_approval: 是否出现在审批 facts 中。
        approval_severity: 审批 facts 的默认严重度。
    """

    key: str
    label: str
    data_type: str = "string"
    control_type: str = "textarea"
    required: bool = True
    read_only: bool = False
    min_length: int | None = 1
    max_length: int | None = 1_000
    plan_attribute: str | None = None
    source_fallback: str | None = None
    include_in_request_form: bool = False
    include_in_presenter_form: bool = False
    include_in_approval: bool = False
    approval_severity: str = "medium"

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.label.strip():
            raise ValueError("planning 字段目录必须包含非空 key 与 label")
        if self.approval_severity not in {"low", "medium", "high"}:
            raise ValueError("planning 审批严重度只能是 low/medium/high")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError(f"planning 字段 {self.key} 的 min_length 不能大于 max_length")

    def constraints(self) -> dict[str, int | str]:
        """返回写入 Card field 的约束集合。"""

        result: dict[str, int | str] = {}
        if self.min_length is not None:
            result["min_length"] = self.min_length
        if self.max_length is not None:
            result["max_length"] = self.max_length
        return result


@dataclass(frozen=True, slots=True)
class PlanningArtifactSectionSpec:
    """Trading plan artifact 中的一个章节布局定义。

    Attributes:
        title: 章节标题。
        kind: Card 协议允许的章节类型。
        field_keys: 以稳定字段键表达的章节内容顺序。
    """

    title: str
    kind: str
    field_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.kind.strip() or not self.field_keys:
            raise ValueError("planning artifact section 必须包含 title/kind/field_keys")


@dataclass(frozen=True, slots=True)
class PlanningPresenterCopy:
    """Planning presenter 使用的全部稳定展示文案。

    Attributes:
        choice_title: 入口 Choice 卡标题。
        choice_description: 入口 Choice 卡说明。
        choice_text_fallback: 入口 Choice 卡降级文本。
        form_title_template: 计划表单卡标题模板，必须接受 ``security_id``。
        form_description: 计划表单卡说明。
        form_text_fallback_template: 表单降级文案模板，必须接受 ``missing_fields``。
        approval_title: 审批卡标题。
        approval_description: 审批卡说明。
        approval_summary_template: 审批摘要模板，必须接受 ``security_id`` 和 ``version``。
        approval_text_fallback_template: 审批降级文案模板。
        artifact_title_template: artifact 标题模板。
        artifact_summary_template: artifact 摘要模板，必须接受 ``version`` 与 ``status_text``。
        artifact_text_fallback_template: artifact 纯文本降级模板。
        artifact_status_labels: 计划状态到文案的映射。
        unsupported_title: unsupported 卡标题。
        field_provenance_label: 字段来源标签。
        plan_provenance_label: 计划来源标签。
        evidence_provenance_label: 证据来源标签。
        evidence_provenance_value: 证据来源固定说明。
    """

    choice_title: str
    choice_description: str
    choice_text_fallback: str
    form_title_template: str
    form_description: str
    form_text_fallback_template: str
    approval_title: str
    approval_description: str
    approval_summary_template: str
    approval_text_fallback_template: str
    artifact_title_template: str
    artifact_summary_template: str
    artifact_text_fallback_template: str
    artifact_status_labels: Mapping[str, str]
    unsupported_title: str
    field_provenance_label: str
    plan_provenance_label: str
    evidence_provenance_label: str
    evidence_provenance_value: str


@dataclass(frozen=True, slots=True)
class PlanningPresenterConfig:
    """驱动 Planning presenter 的只读配置。

    Attributes:
        choice_options: Choice 卡显示的操作目录。
        field_specs: 字段目录。
        artifact_sections: artifact 章节布局定义。
        copy: 所有稳定展示文案。
    """

    choice_options: tuple[PlanningChoiceOptionSpec, ...]
    field_specs: tuple[PlanningFieldSpec, ...]
    artifact_sections: tuple[PlanningArtifactSectionSpec, ...]
    copy: PlanningPresenterCopy
    _field_index: dict[str, PlanningFieldSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        field_index = {spec.key: spec for spec in self.field_specs}
        if len(field_index) != len(self.field_specs):
            raise ValueError("planning presenter field_specs 不能重复")
        for section in self.artifact_sections:
            missing = [
                field_key for field_key in section.field_keys if field_key not in field_index
            ]
            if missing:
                field_list = ", ".join(missing)
                raise ValueError(f"planning artifact section 引用了未知字段: {field_list}")
        object.__setattr__(self, "_field_index", field_index)

    def field(self, key: str) -> PlanningFieldSpec:
        """按稳定字段键查找字段配置。"""

        try:
            return self._field_index[key]
        except KeyError as exc:
            raise KeyError(f"未知 planning 字段: {key}") from exc

    @property
    def presenter_form_fields(self) -> tuple[PlanningFieldSpec, ...]:
        """返回 presenter 计划表单卡需要展示的字段。"""

        return tuple(spec for spec in self.field_specs if spec.include_in_presenter_form)

    @property
    def approval_fields(self) -> tuple[PlanningFieldSpec, ...]:
        """返回审批 facts 中需要展示的字段。"""

        return tuple(spec for spec in self.field_specs if spec.include_in_approval)


class PlanningCardPresenter:
    """把计划相关状态投影为前端可消费的卡片。"""

    def __init__(self, config: PlanningPresenterConfig) -> None:
        self._config = config

    @property
    def config(self) -> PlanningPresenterConfig:
        """暴露只读配置，供 Workflow 复用字段目录。"""

        return self._config

    def intent_choice(
        self, interaction_id: str, *, interaction_version: int = 1, revision: int = 1
    ) -> CardEnvelope:
        """创建“新增什么内容”的入口选择卡。"""

        data: dict[str, Any] = {
            "title": self._config.copy.choice_title,
            "description": self._config.copy.choice_description,
            "options": [
                {
                    "key": item.key,
                    "label": item.label,
                    "description": item.description,
                    "disabled": item.disabled,
                }
                for item in self._config.choice_options
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"planning-choice:{interaction_id}",
            "interaction.choice",
            1,
            revision,
            CardSource("hitl_interaction", interaction_id, interaction_version),
            "pending",
            data,
            ("continue", "cancel"),
            text_fallback=self._config.copy.choice_text_fallback,
        )

    def plan_form(
        self,
        plan: TradingPlan,
        *,
        revision: int = 1,
        field_errors: Mapping[str, str] | None = None,
        state: str = "pending",
    ) -> CardEnvelope:
        """把计划草稿投影为用户可编辑的表单卡。"""

        errors = field_errors or {}
        fields = [
            self._field_payload(spec, plan, errors.get(spec.key))
            for spec in self._config.presenter_form_fields
        ]
        data: dict[str, Any] = {
            "title": self._config.copy.form_title_template.format(security_id=plan.security_id),
            "description": self._config.copy.form_description,
            "fields": fields,
            "provenance": _provenance(plan, self._config),
        }
        actions = ("continue", "cancel") if state == "pending" else ()
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"planning-form:{plan.plan_id}:v{plan.version}",
            "interaction.form",
            1,
            revision,
            CardSource("plan_draft", plan.plan_id, plan.version),
            state,
            data,
            actions,
            text_fallback=self._config.copy.form_text_fallback_template.format(
                missing_fields=_missing_text(plan, self._config)
            ),
        )

    def plan_approval(
        self, plan: TradingPlan, *, revision: int = 1, state: str = "pending"
    ) -> CardEnvelope:
        """把完整草稿投影为激活审批卡。"""

        if plan.status is not PlanStatus.DRAFT:
            raise ValueError("只有 draft 计划可以生成激活审批卡片")
        if plan.missing_fields:
            raise ValueError(f"计划仍缺少风险关键字段: {', '.join(plan.missing_fields)}")
        facts = [self._approval_fact(spec, plan) for spec in self._config.approval_fields]
        data: dict[str, Any] = {
            "title": self._config.copy.approval_title,
            "description": self._config.copy.approval_description,
            "summary": self._config.copy.approval_summary_template.format(
                security_id=plan.security_id,
                version=plan.version,
            ),
            "facts": facts,
            "provenance": _provenance(plan, self._config),
        }
        actions = ("confirm", "edit", "cancel") if state == "pending" else ()
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"planning-approval:{plan.plan_id}:v{plan.version}",
            "interaction.approval",
            1,
            revision,
            CardSource("plan_draft", plan.plan_id, plan.version),
            state,
            data,
            actions,
            text_fallback=self._config.copy.approval_text_fallback_template.format(
                security_id=plan.security_id,
                version=plan.version,
            ),
        )

    def supersede_after_edit(
        self,
        old_plan: TradingPlan,
        new_plan: TradingPlan,
        *,
        old_revision: int = 2,
        new_revision: int = 1,
    ) -> tuple[CardEnvelope, CardEnvelope]:
        """在 edit 场景下同时返回旧卡 superseded 与新卡。"""

        if new_plan.plan_id != old_plan.plan_id or new_plan.supersedes_version != old_plan.version:
            raise ValueError("edit 必须创建同一计划的新 draft/version")
        old_card = self.plan_approval(old_plan, revision=old_revision, state="superseded")
        if new_plan.missing_fields:
            new_card = self.plan_form(new_plan, revision=new_revision)
        else:
            new_card = self.plan_approval(new_plan, revision=new_revision)
        return old_card, new_card

    def plan_artifact(self, plan: TradingPlan, *, revision: int = 1) -> CardEnvelope:
        """把非草稿计划投影为只读 artifact 卡。"""

        if plan.status is PlanStatus.DRAFT:
            raise ValueError("draft 计划必须先经过审批, 不能作为已激活 artifact")
        status_text = self._artifact_status_text(plan.status)
        data: dict[str, Any] = {
            "title": self._config.copy.artifact_title_template.format(security_id=plan.security_id),
            "summary": self._config.copy.artifact_summary_template.format(
                version=plan.version,
                status_text=status_text,
            ),
            "sections": [
                self._artifact_section(section, plan) for section in self._config.artifact_sections
            ],
            "provenance": _provenance(plan, self._config),
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"trade-plan:{plan.plan_id}",
            "artifact.trade_plan",
            1,
            revision,
            CardSource("trading_plan", plan.plan_id, plan.version),
            "resolved",
            data,
            text_fallback=self._config.copy.artifact_text_fallback_template.format(
                security_id=plan.security_id,
                status_text=status_text,
            ),
        )

    def unsupported(
        self,
        *,
        reference_id: str,
        unsupported_kind: str,
        message: str,
        revision: int = 1,
    ) -> CardEnvelope:
        """生成一张明确说明“不支持”的提示卡。"""

        data: dict[str, Any] = {
            "title": self._config.copy.unsupported_title,
            "message": message,
            "unsupported_kind": unsupported_kind,
            "unsupported_schema_version": 1,
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"planning-unsupported:{reference_id}",
            "notice.unsupported",
            1,
            revision,
            CardSource("planning_request", reference_id, 1),
            "failed",
            data,
            ("refresh",),
            text_fallback=message,
        )

    def _field_payload(
        self, spec: PlanningFieldSpec, plan: TradingPlan, error: str | None
    ) -> dict[str, Any]:
        """把字段目录项和计划值投影为表单字段。"""

        source = _field_source(plan, spec)
        provenance: list[dict[str, str]] | None = None
        if source:
            provenance = [
                {
                    "label": self._config.copy.field_provenance_label,
                    "value": source,
                    "source_id": spec.key,
                    "source_type": "plan_field",
                }
            ]
        return {
            "key": spec.key,
            "label": spec.label,
            "value": _plan_value(plan, spec),
            "data_type": spec.data_type,
            "control_type": spec.control_type,
            "required": spec.required,
            "read_only": spec.read_only,
            "constraints": spec.constraints(),
            "options": None,
            "error": error,
            "provenance": provenance,
            "visible_if": None,
        }

    @staticmethod
    def _approval_fact(spec: PlanningFieldSpec, plan: TradingPlan) -> dict[str, str]:
        """把字段目录项投影为审批 facts。"""

        value = _plan_value(plan, spec)
        return {
            "label": spec.label,
            "detail": value if isinstance(value, str) and value else "未填写",
            "severity": spec.approval_severity,
        }

    def _artifact_status_text(self, status: PlanStatus) -> str:
        """读取计划状态的展示文案。"""

        try:
            return self._config.copy.artifact_status_labels[status.value]
        except KeyError as exc:
            raise ValueError(f"artifact_status_labels 缺少 {status.value} 文案") from exc

    def _artifact_section(
        self, section: PlanningArtifactSectionSpec, plan: TradingPlan
    ) -> dict[str, str]:
        """按章节布局定义拼接 artifact 内容。"""

        items = [
            (
                f"{self._config.field(field_key).label}: "
                f"{_artifact_field_value(plan, self._config.field(field_key))}"
            )
            for field_key in section.field_keys
        ]
        return {
            "title": section.title,
            "content": "; ".join(items),
            "kind": section.kind,
        }


def _plan_value(plan: TradingPlan, spec: PlanningFieldSpec) -> str | None:
    """读取字段目录项在当前计划中的值。"""

    if spec.plan_attribute is None:
        return None
    value = getattr(plan, spec.plan_attribute)
    return value if isinstance(value, str) else None


def _artifact_field_value(plan: TradingPlan, spec: PlanningFieldSpec) -> str:
    """返回 artifact 中应展示的字段值。"""

    value = _plan_value(plan, spec)
    return value if isinstance(value, str) and value else "未填写"


def _field_source(plan: TradingPlan, spec: PlanningFieldSpec) -> str | None:
    """返回字段来源文案。"""

    return plan.field_sources.get(spec.key) or spec.source_fallback


def _provenance(plan: TradingPlan, config: PlanningPresenterConfig) -> list[dict[str, str]]:
    """把 lineage 与 evidence 投影为统一 provenance。"""

    result: list[dict[str, str]] = []
    for source in plan.source_references:
        details = [f"版本 {source.source_version}"]
        if source.strategy_id is not None:
            details.append(f"策略 {source.strategy_id} v{source.strategy_version}")
        if source.model_version_id is not None:
            details.append(f"模型 {source.model_version_id}")
        result.append(
            {
                "label": config.copy.plan_provenance_label,
                "value": "; ".join(details),
                "source_id": source.source_id,
                "source_type": source.source_type,
            }
        )
        result.extend(
            {
                "label": config.copy.evidence_provenance_label,
                "value": config.copy.evidence_provenance_value,
                "source_id": evidence_id,
                "source_type": "evidence_snapshot",
            }
            for evidence_id in source.evidence_ids
        )
    return result


def _missing_text(plan: TradingPlan, config: PlanningPresenterConfig) -> str:
    """把计划缺失字段键转换为用户可读文本。"""

    if not plan.missing_fields:
        return "无"
    return "、".join(config.field(field_name).label for field_name in plan.missing_fields)


__all__ = [
    "PlanningArtifactSectionSpec",
    "PlanningCardPresenter",
    "PlanningChoiceOptionSpec",
    "PlanningFieldSpec",
    "PlanningPresenterConfig",
    "PlanningPresenterCopy",
]
