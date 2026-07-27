"""记录生产监控决策的确定性 application service。"""

from collections.abc import Sequence

from trade_agent.capabilities.quantitative.domain.monitoring import (
    MonitoringDecision,
    MonitoringPolicy,
    ProductionObservation,
)


class QuantitativeMonitoringService:
    def __init__(self, policy: MonitoringPolicy) -> None:
        self._policy = policy
        self._events: list[MonitoringDecision] = []

    def observe(
        self,
        observation: ProductionObservation,
        *,
        approved_baseline_model_version_id: str | None,
    ) -> MonitoringDecision:
        decision = self._policy.evaluate(
            observation,
            approved_baseline_model_version_id=approved_baseline_model_version_id,
        )
        self._events.append(decision)
        return decision

    @property
    def events(self) -> Sequence[MonitoringDecision]:
        return tuple(self._events)
