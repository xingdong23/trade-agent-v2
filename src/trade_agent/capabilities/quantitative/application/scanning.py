"""量化扫描提交校验、确定性前置门禁、专用模型 inference 与排名。"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime

from trade_agent.capabilities.quantitative.contracts import (
    ApprovedModelSnapshot,
    BatchInferenceService,
    ConditionOutcome,
    DataFeatureSnapshot,
    InferenceRequest,
    PredictionStatus,
    RankingDefinition,
    ScanConfiguration,
    ScanDisposition,
    ScanEvaluation,
    ScanResult,
    ScanSubmission,
    ScanUniverseSnapshot,
    StrategyVersionSnapshot,
)


class ScanSubmissionError(ValueError):
    pass


class ScanSubmissionValidator:
    def create(
        self,
        *,
        scan_id: str,
        owner_id: str,
        strategy: StrategyVersionSnapshot,
        universe: ScanUniverseSnapshot,
        data_features: DataFeatureSnapshot,
        model: ApprovedModelSnapshot,
        ranking: RankingDefinition,
        configuration: ScanConfiguration,
        submitted_at: datetime,
    ) -> ScanSubmission:
        if not scan_id.strip() or not owner_id.strip():
            raise ScanSubmissionError("scan_id 与 owner_id 不能为空")
        if submitted_at.tzinfo is None:
            raise ScanSubmissionError("submitted_at 必须包含时区")
        if strategy.owner_id != owner_id or universe.owner_id != owner_id:
            raise ScanSubmissionError("strategy 与 universe 必须属于当前 owner")
        if not strategy.published:
            raise ScanSubmissionError("扫描只能冻结已发布 strategy version")
        if model.market != "US" or not model.approved:
            raise ScanSubmissionError("扫描只能冻结已批准的美股专用模型")
        if (strategy.target, strategy.horizon) != (model.target, model.horizon):
            raise ScanSubmissionError("strategy target/horizon 与 model 不一致")
        if not universe.security_ids:
            raise ScanSubmissionError("不能提交空 universe 扫描")
        if len(universe.security_ids) != len(set(universe.security_ids)):
            raise ScanSubmissionError("universe snapshot 不允许重复证券")
        input_ids = tuple(item.security_id for item in data_features.securities)
        if set(input_ids) != set(universe.security_ids) or len(input_ids) != len(set(input_ids)):
            raise ScanSubmissionError("data/feature snapshot 必须完整覆盖冻结 universe")
        return ScanSubmission(
            scan_id,
            owner_id,
            strategy,
            universe,
            data_features,
            model,
            ranking,
            configuration,
            submitted_at,
        )


class ScanEvaluator:
    def __init__(self, inference: BatchInferenceService) -> None:
        self._inference = inference

    def evaluate(self, submission: ScanSubmission) -> ScanEvaluation:
        results: list[ScanResult] = []
        eligible = []
        eligible_conditions: dict[str, tuple[ConditionOutcome, ...]] = {}
        for security in submission.data_features.securities:
            deterministic = self._deterministic_result(submission, security)
            if deterministic is not None:
                results.append(deterministic)
                continue
            conditions = tuple(
                ConditionOutcome(
                    rule.rule_id,
                    True,
                    security.features[rule.feature_name],
                    rule.expected,
                    "hard rule 命中",
                )
                for rule in submission.strategy.hard_rules
            )
            eligible_conditions[security.security_id] = conditions
            eligible.append(
                InferenceRequest(
                    security.security_id,
                    security.market,
                    submission.strategy.target,
                    submission.strategy.horizon,
                    submission.data_features.as_of,
                    security.feature_snapshot_id,
                    security.features,
                    security.missing_ratio,
                    security.out_of_distribution,
                )
            )

        predictions = self._inference.predict(tuple(eligible)) if eligible else ()
        security_by_id = {item.security_id: item for item in submission.data_features.securities}
        for prediction in predictions:
            security = security_by_id[prediction.security_id]
            matched_conditions = eligible_conditions[prediction.security_id]
            if prediction.status is PredictionStatus.UNAVAILABLE:
                results.append(
                    self._result(
                        submission,
                        security,
                        ScanDisposition.UNAVAILABLE,
                        matched_conditions=matched_conditions,
                        gaps=(*security.gaps, prediction.reason or "专用模型预测不可用"),
                        reason=prediction.reason,
                    )
                )
                continue
            if prediction.model_version_id != submission.model.model_version_id:
                results.append(
                    self._result(
                        submission,
                        security,
                        ScanDisposition.UNAVAILABLE,
                        matched_conditions=matched_conditions,
                        gaps=(*security.gaps, "inference model 与冻结 model version 不一致"),
                        reason="model lineage 不一致",
                    )
                )
                continue
            probability = prediction.distribution.get(submission.ranking.probability_key)
            if (
                probability is None
                or not math.isfinite(probability)
                or not 0.0 <= probability <= 1.0
            ):
                results.append(
                    self._result(
                        submission,
                        security,
                        ScanDisposition.UNAVAILABLE,
                        matched_conditions=matched_conditions,
                        gaps=(*security.gaps, "model output 缺少 ranking probability"),
                        reason="prediction schema 不完整",
                    )
                )
                continue
            score = submission.ranking.score(probability=probability, features=security.features)
            probability_condition = ConditionOutcome(
                "minimum_probability",
                probability >= submission.configuration.minimum_probability,
                probability,
                submission.configuration.minimum_probability,
                "专用模型 probability 门槛",
            )
            if not probability_condition.matched:
                results.append(
                    self._result(
                        submission,
                        security,
                        ScanDisposition.NON_MATCH,
                        probability=probability,
                        score=score,
                        matched_conditions=matched_conditions,
                        excluded_conditions=(probability_condition,),
                        model_version_id=prediction.model_version_id,
                        reason="专用模型 probability 未达门槛",
                    )
                )
                continue
            results.append(
                self._result(
                    submission,
                    security,
                    ScanDisposition.MATCHED,
                    probability=probability,
                    score=score,
                    matched_conditions=(*matched_conditions, probability_condition),
                    model_version_id=prediction.model_version_id,
                )
            )

        matched = sorted(
            (item for item in results if item.disposition is ScanDisposition.MATCHED),
            key=lambda item: (-(item.score or 0.0), item.security_id),
        )
        ranks = {item.security_id: index for index, item in enumerate(matched, start=1)}
        ordered = tuple(
            replace(item, rank=ranks.get(item.security_id))
            for security_id in submission.universe.security_ids
            for item in results
            if item.security_id == security_id
        )
        return ScanEvaluation(submission.scan_id, ordered)

    def _deterministic_result(
        self, submission: ScanSubmission, security: object
    ) -> ScanResult | None:
        from trade_agent.capabilities.quantitative.contracts import ScanSecurityInput

        if not isinstance(security, ScanSecurityInput):
            raise TypeError("scan security input 类型错误")
        exclusions: list[ConditionOutcome] = []
        if security.market != "US":
            exclusions.append(
                ConditionOutcome("market_scope", False, security.market, "US", "仅支持美股")
            )
        if security.exchange not in submission.configuration.allowed_exchanges:
            exclusions.append(
                ConditionOutcome(
                    "exchange_scope",
                    False,
                    security.exchange,
                    list(submission.configuration.allowed_exchanges),
                    "交易所不在扫描范围",
                )
            )
        if security.average_dollar_volume < submission.configuration.minimum_dollar_volume:
            exclusions.append(
                ConditionOutcome(
                    "minimum_dollar_volume",
                    False,
                    security.average_dollar_volume,
                    submission.configuration.minimum_dollar_volume,
                    "流动性未达门槛",
                )
            )
        if exclusions:
            return self._result(
                submission,
                security,
                ScanDisposition.NON_MATCH,
                excluded_conditions=tuple(exclusions),
                reason="适用性或流动性门禁未通过",
            )
        gaps = list(security.gaps)
        if not security.data_available:
            gaps.append("决策时点数据不可用")
        if security.out_of_distribution:
            gaps.append("输入超出模型适用范围")
        if security.missing_ratio > submission.configuration.maximum_missing_ratio or any(
            value is None for value in security.features.values()
        ):
            gaps.append("feature 缺失超过扫描门槛")
        missing_required = tuple(
            name for name in submission.strategy.required_features if name not in security.features
        )
        if missing_required:
            gaps.append(f"缺少策略所需 feature: {', '.join(missing_required)}")
        if gaps != list(security.gaps):
            return self._result(
                submission,
                security,
                ScanDisposition.UNAVAILABLE,
                gaps=tuple(gaps),
                reason="数据或 feature 不可用",
            )
        matched: list[ConditionOutcome] = []
        failed: list[ConditionOutcome] = []
        for rule in submission.strategy.hard_rules:
            actual = security.features.get(rule.feature_name)
            if actual is None:
                failed.append(
                    ConditionOutcome(
                        rule.rule_id, False, None, rule.expected, "hard rule feature 不可用"
                    )
                )
                continue
            outcome = ConditionOutcome(
                rule.rule_id,
                rule.matches(actual),
                actual,
                rule.expected,
                "hard rule 命中" if rule.matches(actual) else "hard rule 未命中",
            )
            (matched if outcome.matched else failed).append(outcome)
        if failed:
            return self._result(
                submission,
                security,
                ScanDisposition.NON_MATCH,
                matched_conditions=tuple(matched),
                excluded_conditions=tuple(failed),
                reason="策略 hard rule 未通过",
            )
        return None

    @staticmethod
    def _result(
        submission: ScanSubmission,
        security: object,
        disposition: ScanDisposition,
        *,
        probability: float | None = None,
        score: float | None = None,
        matched_conditions: tuple[ConditionOutcome, ...] = (),
        excluded_conditions: tuple[ConditionOutcome, ...] = (),
        model_version_id: str | None = None,
        gaps: tuple[str, ...] | None = None,
        reason: str | None = None,
    ) -> ScanResult:
        from trade_agent.capabilities.quantitative.contracts import ScanSecurityInput

        if not isinstance(security, ScanSecurityInput):
            raise TypeError("scan security input 类型错误")
        return ScanResult(
            submission.scan_id,
            security.security_id,
            disposition,
            None,
            probability,
            score,
            matched_conditions,
            excluded_conditions,
            security.evidence_refs,
            submission.data_features.data_snapshot_id,
            security.feature_snapshot_id,
            submission.data_features.feature_set_version,
            model_version_id,
            submission.ranking.version,
            security.risks,
            security.gaps if gaps is None else gaps,
            reason,
        )
