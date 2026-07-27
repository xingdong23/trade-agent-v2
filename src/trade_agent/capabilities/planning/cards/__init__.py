"""交易计划渐进式交互与 artifact 的确定性 Card presenter。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trade_agent.capabilities.planning.contracts import PlanStatus, TradingPlan
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource

_FIELD_LABELS: Mapping[str, str] = {
    "horizon": "计划周期",
    "entry_condition": "入场条件",
    "invalidation_condition": "失效或止损条件",
    "target": "目标条件",
    "position_notes": "仓位备注",
    "risk_notes": "风险说明",
}


class PlanningCardPresenter:
    def intent_choice(
        self, interaction_id: str, *, interaction_version: int = 1, revision: int = 1
    ) -> CardEnvelope:
        data: dict[str, Any] = {
            "title": "请选择要新增的内容",
            "description": "首版只支持创建美股交易计划, 不支持记录成交或执行真实交易。",
            "options": [
                {
                    "key": "create_trade_plan",
                    "label": "创建交易计划",
                    "description": "继续补充条件并在审批后激活计划。",
                    "disabled": False,
                },
                {
                    "key": "record_historical_trade",
                    "label": "记录已发生的交易",
                    "description": "首版暂不支持手工成交记录。",
                    "disabled": True,
                },
                {
                    "key": "execute_trade",
                    "label": "执行真实交易",
                    "description": "系统没有下单或账户能力。",
                    "disabled": True,
                },
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
            text_fallback="请选择创建交易计划; 成交记录与真实交易暂不支持。",
        )

    def plan_form(
        self,
        plan: TradingPlan,
        *,
        revision: int = 1,
        field_errors: Mapping[str, str] | None = None,
        state: str = "pending",
    ) -> CardEnvelope:
        errors = field_errors or {}
        fields: list[dict[str, Any]] = [
            _field(
                "security_id",
                "美股证券",
                plan.security_id,
                required=True,
                read_only=True,
                data_type="symbol",
                source=plan.field_sources.get("security_id", "规范证券解析"),
            ),
            _field(
                "direction",
                "方向或逻辑",
                plan.direction,
                required=True,
                read_only=False,
                source=plan.field_sources.get("direction", "用户输入"),
            ),
        ]
        for field_name, label in _FIELD_LABELS.items():
            fields.append(
                _field(
                    field_name,
                    label,
                    getattr(plan, field_name),
                    required=True,
                    read_only=False,
                    source=plan.field_sources.get(field_name),
                    error=errors.get(field_name),
                )
            )
        data: dict[str, Any] = {
            "title": f"补充 {plan.security_id} 交易计划",
            "description": (
                "系统不会执行交易。请一次补齐计划周期、入场、失效、目标、仓位和风险字段;"
                "空白字段不会由模型猜测。"
            ),
            "fields": fields,
            "provenance": _provenance(plan),
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
            text_fallback=f"请补充交易计划; 当前缺失: {_missing_text(plan)}。",
        )

    def plan_approval(
        self, plan: TradingPlan, *, revision: int = 1, state: str = "pending"
    ) -> CardEnvelope:
        if plan.status is not PlanStatus.DRAFT:
            raise ValueError("只有 draft 计划可以生成激活审批卡片")
        if plan.missing_fields:
            raise ValueError(f"计划仍缺少风险关键字段: {', '.join(plan.missing_fields)}")
        facts: list[dict[str, str]] = [
            {"label": "美股证券", "detail": plan.security_id, "severity": "low"},
            {"label": "方向或逻辑", "detail": plan.direction, "severity": "medium"},
        ]
        for field_name, label in _FIELD_LABELS.items():
            value = getattr(plan, field_name)
            facts.append(
                {
                    "label": label,
                    "detail": value if isinstance(value, str) else "未填写",
                    "severity": "high"
                    if field_name in {"invalidation_condition", "risk_notes"}
                    else "medium",
                }
            )
        data: dict[str, Any] = {
            "title": "批准激活交易计划",
            "description": (
                "确认只会激活计划, 不会下单、查询余额或产生任何成交。"
                "选择 edit 会废弃当前卡片并创建新的草稿版本。"
            ),
            "summary": f"确认激活 {plan.security_id} 计划 v{plan.version}。",
            "facts": facts,
            "provenance": _provenance(plan),
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
            text_fallback=f"请确认是否激活 {plan.security_id} 交易计划 v{plan.version}。",
        )

    def supersede_after_edit(
        self,
        old_plan: TradingPlan,
        new_plan: TradingPlan,
        *,
        old_revision: int = 2,
        new_revision: int = 1,
    ) -> tuple[CardEnvelope, CardEnvelope]:
        if new_plan.plan_id != old_plan.plan_id or new_plan.supersedes_version != old_plan.version:
            raise ValueError("edit 必须创建同一计划的新 draft/version")
        old_card = self.plan_approval(old_plan, revision=old_revision, state="superseded")
        if new_plan.missing_fields:
            new_card = self.plan_form(new_plan, revision=new_revision)
        else:
            new_card = self.plan_approval(new_plan, revision=new_revision)
        return old_card, new_card

    def plan_artifact(self, plan: TradingPlan, *, revision: int = 1) -> CardEnvelope:
        if plan.status is PlanStatus.DRAFT:
            raise ValueError("draft 计划必须先经过审批, 不能作为已激活 artifact")
        status_text = {
            PlanStatus.ACTIVE: "已激活",
            PlanStatus.TRIGGERED: "条件已触发, 但不表示已成交",
            PlanStatus.CANCELLED: "已取消",
            PlanStatus.EXPIRED: "已过期",
            PlanStatus.REVIEWED: "已复盘",
        }[plan.status]
        data: dict[str, Any] = {
            "title": f"{plan.security_id} 交易计划",
            "summary": f"计划 v{plan.version} {status_text}。系统不提供交易执行能力。",
            "sections": [
                {
                    "title": "方向、周期与入场",
                    "content": (
                        f"方向或逻辑: {plan.direction}; 周期: {plan.horizon}; "
                        f"入场条件: {plan.entry_condition}"
                    ),
                    "kind": "plan",
                },
                {
                    "title": "失效、目标与仓位",
                    "content": (
                        f"失效或止损: {plan.invalidation_condition}; 目标: {plan.target}; "
                        f"仓位备注: {plan.position_notes}"
                    ),
                    "kind": "risk",
                },
                {"title": "风险", "content": plan.risk_notes or "未填写", "kind": "risk"},
            ],
            "provenance": _provenance(plan),
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
            text_fallback=f"{plan.security_id} 交易计划 {status_text}; 不代表已执行交易。",
        )

    def unsupported(
        self,
        *,
        reference_id: str,
        unsupported_kind: str,
        message: str,
        revision: int = 1,
    ) -> CardEnvelope:
        data: dict[str, Any] = {
            "title": "当前请求不受支持",
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


def _field(
    key: str,
    label: str,
    value: str | None,
    *,
    required: bool,
    read_only: bool,
    source: str | None,
    data_type: str = "string",
    error: str | None = None,
) -> dict[str, Any]:
    provenance: list[dict[str, str]] | None = None
    if source:
        provenance = [
            {
                "label": "字段来源",
                "value": source,
                "source_id": key,
                "source_type": "plan_field",
            }
        ]
    return {
        "key": key,
        "label": label,
        "value": value,
        "data_type": data_type,
        "control_type": "textarea" if key not in {"security_id", "horizon"} else "text",
        "required": required,
        "read_only": read_only,
        "constraints": {"min_length": 1, "max_length": 1000},
        "options": None,
        "error": error,
        "provenance": provenance,
        "visible_if": None,
    }


def _provenance(plan: TradingPlan) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for source in plan.source_references:
        details = [f"版本 {source.source_version}"]
        if source.strategy_id is not None:
            details.append(f"策略 {source.strategy_id} v{source.strategy_version}")
        if source.model_version_id is not None:
            details.append(f"模型 {source.model_version_id}")
        result.append(
            {
                "label": "计划来源",
                "value": "; ".join(details),
                "source_id": source.source_id,
                "source_type": source.source_type,
            }
        )
        result.extend(
            {
                "label": "证据",
                "value": "计划引用的不可变 evidence",
                "source_id": evidence_id,
                "source_type": "evidence_snapshot",
            }
            for evidence_id in source.evidence_ids
        )
    return result


def _missing_text(plan: TradingPlan) -> str:
    if not plan.missing_fields:
        return "无"
    return "、".join(_FIELD_LABELS[field_name] for field_name in plan.missing_fields)


__all__ = ["PlanningCardPresenter"]
