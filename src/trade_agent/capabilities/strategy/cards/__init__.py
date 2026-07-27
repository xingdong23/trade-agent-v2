"""策略发布 Review/Approval 的确定性 card presenter。"""

from collections.abc import Mapping, Sequence
from typing import Any

from trade_agent.capabilities.strategy.contracts import PublishedStrategy, StrategyDraft
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource


class StrategyCardPresenter:
    def review(
        self,
        draft: StrategyDraft,
        *,
        previous: PublishedStrategy | None = None,
        revision: int = 1,
    ) -> CardEnvelope:
        target_version = previous.version + 1 if previous is not None else 1
        findings: list[dict[str, Any]] = [
            {"label": "目标版本", "detail": f"v{target_version}", "severity": "low"},
            {"label": "目标", "detail": draft.target, "severity": "low"},
            {"label": "周期", "detail": draft.horizon, "severity": "low"},
            {"label": "所需输入", "detail": "、".join(draft.required_inputs), "severity": "medium"},
            {
                "label": "入场条件",
                "detail": f"共 {len(draft.entry_conditions)} 条",
                "severity": "medium",
            },
            {
                "label": "排除条件",
                "detail": f"共 {len(draft.exclusion_conditions)} 条",
                "severity": "medium",
            },
            {
                "label": "排序策略",
                "detail": _format_mapping(draft.ranking_policy),
                "severity": "medium",
            },
            *_diff_findings(draft, previous),
        ]
        provenance = _strategy_provenance(draft, previous, target_version)
        data: dict[str, Any] = {
            "title": "复核策略草稿",
            "description": "确认结构化条件、目标版本和差异; 取消不会发布策略版本。",
            "findings": findings,
            "provenance": provenance,
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"strategy-review:{draft.strategy_id}:v{target_version}",
            "interaction.review",
            1,
            revision,
            CardSource("strategy_draft", draft.strategy_id, target_version),
            "pending",
            data,
            ("confirm", "edit", "cancel"),
            text_fallback=f"请复核策略 {draft.name} 的发布内容。",
        )

    def approval(
        self,
        draft: StrategyDraft,
        *,
        previous: PublishedStrategy | None = None,
        revision: int = 1,
    ) -> CardEnvelope:
        target_version = previous.version + 1 if previous is not None else 1
        facts: list[dict[str, Any]] = [
            {"label": "目标版本", "detail": f"v{target_version}", "severity": "low"},
            {"label": "目标", "detail": draft.target, "severity": "low"},
            {"label": "周期", "detail": draft.horizon, "severity": "low"},
            {
                "label": "正例/反例",
                "detail": f"{len(draft.positive_examples)} / {len(draft.negative_examples)}",
                "severity": "medium",
            },
            *_diff_findings(draft, previous),
        ]
        summary = (
            f"确认后将发布策略 {draft.name} 的 v{target_version}。"
            f" 该版本冻结 target={draft.target}、horizon={draft.horizon}、结构化条件和排序规则。"
        )
        data: dict[str, Any] = {
            "title": "批准策略发布",
            "description": "仅在 confirm 后创建不可变策略版本; edit 或 cancel 不修改历史版本。",
            "summary": summary,
            "facts": facts,
            "provenance": _strategy_provenance(draft, previous, target_version),
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"strategy-approval:{draft.strategy_id}:v{target_version}",
            "interaction.approval",
            1,
            revision,
            CardSource("strategy_draft", draft.strategy_id, target_version),
            "pending",
            data,
            ("confirm", "edit", "cancel"),
            text_fallback=f"确认是否发布策略 {draft.name} v{target_version}",
        )

    def artifact(self, strategy: PublishedStrategy, *, revision: int = 1) -> CardEnvelope:
        data: dict[str, Any] = {
            "title": strategy.draft.name,
            "summary": f"策略版本 v{strategy.version} 已发布并冻结条件、输入与排序规则。",
            "sections": [
                {
                    "title": "策略逻辑",
                    "content": strategy.draft.logic,
                    "kind": "analysis",
                },
                {
                    "title": "版本信息",
                    "content": (
                        f"target={strategy.draft.target}; horizon={strategy.draft.horizon}; "
                        f"approved_by={strategy.approved_by}"
                    ),
                    "kind": "summary",
                },
                {
                    "title": "结构化条件",
                    "content": _format_conditions(
                        strategy.draft.entry_conditions,
                        strategy.draft.exclusion_conditions,
                    ),
                    "kind": "plan",
                },
            ],
            "provenance": [
                {
                    "label": "source_draft_hash",
                    "value": strategy.source_draft_hash,
                    "source_id": strategy.strategy_id,
                    "source_type": "strategy_draft",
                }
            ],
        }
        return CardEnvelope(
            CARD_PROTOCOL_VERSION,
            f"strategy:{strategy.strategy_id}",
            "artifact.strategy",
            1,
            revision,
            CardSource("strategy_version", strategy.strategy_id, strategy.version),
            "resolved",
            data,
            text_fallback=f"策略 {strategy.draft.name} v{strategy.version} 已发布。",
        )


def _diff_findings(
    draft: StrategyDraft, previous: PublishedStrategy | None
) -> list[dict[str, Any]]:
    if previous is None:
        return [{"label": "版本差异", "detail": "首个策略版本, 无历史差异。", "severity": "low"}]

    old = previous.draft
    findings: list[dict[str, Any]] = []
    if draft.name != old.name:
        findings.append(
            {"label": "名称变化", "detail": f"{old.name} -> {draft.name}", "severity": "medium"}
        )
    if draft.logic != old.logic:
        findings.append({"label": "逻辑变化", "detail": "策略逻辑说明已更新", "severity": "medium"})
    if draft.target != old.target or draft.horizon != old.horizon:
        findings.append(
            {
                "label": "目标/周期变化",
                "detail": f"{old.target}/{old.horizon} -> {draft.target}/{draft.horizon}",
                "severity": "high",
            }
        )
    if draft.entry_conditions != old.entry_conditions:
        findings.append(
            {
                "label": "入场条件变化",
                "detail": f"{len(old.entry_conditions)} -> {len(draft.entry_conditions)} 条",
                "severity": "medium",
            }
        )
    if draft.exclusion_conditions != old.exclusion_conditions:
        findings.append(
            {
                "label": "排除条件变化",
                "detail": (
                    f"{len(old.exclusion_conditions)} -> {len(draft.exclusion_conditions)} 条"
                ),
                "severity": "medium",
            }
        )
    if draft.required_inputs != old.required_inputs:
        findings.append(
            {
                "label": "输入集合变化",
                "detail": _tuple_diff(old.required_inputs, draft.required_inputs),
                "severity": "medium",
            }
        )
    if draft.ranking_policy != old.ranking_policy:
        findings.append(
            {
                "label": "排序规则变化",
                "detail": (
                    f"{_format_mapping(old.ranking_policy)} -> "
                    f"{_format_mapping(draft.ranking_policy)}"
                ),
                "severity": "high",
            }
        )
    if not findings:
        findings.append(
            {"label": "版本差异", "detail": "与上一版结构化内容一致。", "severity": "low"}
        )
    return findings


def _strategy_provenance(
    draft: StrategyDraft, previous: PublishedStrategy | None, target_version: int
) -> list[dict[str, Any]]:
    items = [
        {
            "label": "策略草稿",
            "value": f"{draft.strategy_id} -> v{target_version}",
            "source_id": draft.strategy_id,
            "source_type": "strategy_draft",
        }
    ]
    if previous is not None:
        items.append(
            {
                "label": "上一发布版本",
                "value": f"v{previous.version}",
                "source_id": previous.strategy_id,
                "source_type": "strategy_version",
            }
        )
    return items


def _format_mapping(values: Mapping[str, Any]) -> str:
    if not values:
        return "无"
    parts = [f"{key}={values[key]}" for key in sorted(values)]
    return "; ".join(parts)


def _format_conditions(
    entry_conditions: Sequence[Mapping[str, Any]],
    exclusion_conditions: Sequence[Mapping[str, Any]],
) -> str:
    return (
        f"entry={len(entry_conditions)} 条, exclusion={len(exclusion_conditions)} 条; "
        f"entry detail={list(entry_conditions)}; exclusion detail={list(exclusion_conditions)}"
    )


def _tuple_diff(previous: Sequence[str], current: Sequence[str]) -> str:
    removed = [value for value in previous if value not in current]
    added = [value for value in current if value not in previous]
    parts: list[str] = []
    if added:
        parts.append(f"新增 {', '.join(added)}")
    if removed:
        parts.append(f"移除 {', '.join(removed)}")
    return "; ".join(parts) if parts else "无变化"


__all__ = ["StrategyCardPresenter"]
