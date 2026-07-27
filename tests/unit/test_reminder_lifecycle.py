import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from trade_agent.adapters.notifications import InMemoryNotificationAdapter
from trade_agent.capabilities.contracts import CapabilityResult
from trade_agent.capabilities.reminder.application import (
    ReminderApplication,
    ReminderDeliveryPolicy,
    ReminderWorker,
)
from trade_agent.capabilities.reminder.cards import ReminderCardPresenter
from trade_agent.capabilities.reminder.domain import (
    DeliveryStatus,
    ReminderObservation,
    ReminderRule,
    ReminderRuleType,
    ReminderStatus,
    ReminderTrigger,
    ThresholdDirection,
    build_trigger,
    should_trigger,
)
from trade_agent.capabilities.reminder.tools import (
    CreateReminderTool,
    GetReminderTool,
    SetReminderStatusTool,
)
from trade_agent.core.tools import ToolExecutionContext, ToolExecutionPrincipal, ToolRequest

DISCLAIMER = "提醒仅表示条件观察与通知 / 不表示下单或成交。"
TRIGGER_MESSAGE = "提醒条件已满足: 这只是条件观察 / 不表示已下单或成交。"


def _delivery_policy(
    *, max_attempts: int = 3, retry_delays: tuple[float, ...] = (0.0, 0.0)
) -> ReminderDeliveryPolicy:
    return ReminderDeliveryPolicy(
        "test-reminder-delivery.v1",
        "test.reminder.triggered.v1",
        max_attempts,
        retry_delays,
        "notification unavailable",
        TRIGGER_MESSAGE,
    )


class FakeReminderRepository:
    def __init__(self) -> None:
        self.rules: dict[tuple[str, str], ReminderRule] = {}
        self.observations: dict[tuple[str, int], ReminderObservation] = {}
        self.triggers: dict[str, ReminderTrigger] = {}
        self.idempotency: dict[str, ReminderRule] = {}

    def save_rule(
        self,
        rule: ReminderRule,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderRule:
        replay = self.idempotency.get(idempotency_key)
        if replay is not None:
            return replay
        current = self.rules.get((rule.owner_id, rule.reminder_id))
        actual_version = 0 if current is None else current.version
        if actual_version != expected_version:
            raise RuntimeError("concurrent write")
        self.rules[(rule.owner_id, rule.reminder_id)] = rule
        self.idempotency[idempotency_key] = rule
        return rule

    def get_rule(self, owner_id: str, reminder_id: str) -> ReminderRule | None:
        return self.rules.get((owner_id, reminder_id))

    def list_active_rules(self) -> tuple[ReminderRule, ...]:
        return tuple(rule for rule in self.rules.values() if rule.status is ReminderStatus.ACTIVE)

    def previous_observation(
        self, reminder_id: str, rule_version: int
    ) -> ReminderObservation | None:
        return self.observations.get((reminder_id, rule_version))

    def save_observation(
        self, reminder_id: str, rule_version: int, observation: ReminderObservation
    ) -> None:
        self.observations[(reminder_id, rule_version)] = observation

    def latest_trigger(self, reminder_id: str, rule_version: int) -> ReminderTrigger | None:
        candidates = [
            trigger
            for trigger in self.triggers.values()
            if trigger.reminder_id == reminder_id and trigger.rule_version == rule_version
        ]
        return max(candidates, key=lambda item: item.observed_at, default=None)

    def record_trigger(self, trigger: ReminderTrigger) -> bool:
        if trigger.trigger_id in self.triggers:
            return False
        self.triggers[trigger.trigger_id] = trigger
        return True

    def update_trigger(self, trigger: ReminderTrigger) -> ReminderTrigger:
        if trigger.trigger_id not in self.triggers:
            raise LookupError(trigger.trigger_id)
        self.triggers[trigger.trigger_id] = trigger
        return trigger


class QueuedObservationProvider:
    def __init__(self, observations: list[ReminderObservation]) -> None:
        self.observations = observations

    async def observe(self, rule: ReminderRule, *, now: datetime) -> ReminderObservation:
        del rule, now
        return self.observations.pop(0)


def _price_rule(*, status: ReminderStatus = ReminderStatus.ACTIVE) -> ReminderRule:
    return ReminderRule(
        reminder_id="reminder-1",
        owner_id="owner-1",
        plan_id="plan-1",
        version=1,
        status=status,
        rule_type=ReminderRuleType.PRICE_THRESHOLD,
        condition={
            "security_id": "NASDAQ:NVDA",
            "threshold": 100.0,
            "direction": ThresholdDirection.CROSSES_ABOVE.value,
        },
        notification_channel="in_app",
        cooldown=timedelta(minutes=5),
        approved_by="owner-1" if status is ReminderStatus.ACTIVE else None,
        approved_payload_hash="approved-hash" if status is ReminderStatus.ACTIVE else None,
    )


def test_three_rule_types_have_deterministic_trigger_semantics() -> None:
    at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    price = _price_rule()
    below = ReminderObservation("reminder-1", at, "quote-1", 99.0)
    above = ReminderObservation("reminder-1", at + timedelta(minutes=1), "quote-2", 100.0)
    assert should_trigger(price, above, previous=below, latest_trigger=None)
    assert not should_trigger(price, above, previous=None, latest_trigger=None)

    scheduled = ReminderRule(
        reminder_id="scheduled-1",
        owner_id="owner-1",
        plan_id="plan-1",
        version=1,
        status=ReminderStatus.ACTIVE,
        rule_type=ReminderRuleType.SCHEDULED_REVIEW,
        condition={"scheduled_at": at.isoformat()},
        notification_channel="test_channel",
        cooldown=timedelta(),
        approved_by="owner-1",
        approved_payload_hash="approved-hash",
    )
    scheduled_observation = ReminderObservation("scheduled-1", at, "clock-1", None)
    assert should_trigger(scheduled, scheduled_observation, previous=None, latest_trigger=None)
    prior_trigger = build_trigger(scheduled, scheduled_observation, message=TRIGGER_MESSAGE)
    assert not should_trigger(
        scheduled,
        replace(scheduled_observation, observation_reference="clock-2"),
        previous=scheduled_observation,
        latest_trigger=prior_trigger,
    )

    invalidation = ReminderRule(
        reminder_id="invalidation-1",
        owner_id="owner-1",
        plan_id="plan-1",
        version=1,
        status=ReminderStatus.ACTIVE,
        rule_type=ReminderRuleType.INVALIDATION,
        condition={"condition_key": "close_below_support"},
        notification_channel="test_channel",
        cooldown=timedelta(),
        approved_by="owner-1",
        approved_payload_hash="approved-hash",
    )
    clear = ReminderObservation("invalidation-1", at, "signal-1", False)
    invalid = ReminderObservation("invalidation-1", at + timedelta(minutes=1), "signal-2", True)
    assert should_trigger(invalidation, invalid, previous=clear, latest_trigger=None)


def test_status_transition_requires_approval_and_preserves_hash() -> None:
    draft = _price_rule(status=ReminderStatus.DRAFT)
    with pytest.raises(PermissionError, match="明确审批"):
        draft.transition(
            ReminderStatus.ACTIVE,
            approved=False,
            actor_id="owner-1",
            payload_hash="hash-1",
        )

    active = draft.transition(
        ReminderStatus.ACTIVE,
        approved=True,
        actor_id="owner-1",
        payload_hash="hash-1",
    )
    disabled = active.transition(
        ReminderStatus.DISABLED,
        approved=True,
        actor_id="owner-1",
        payload_hash="hash-2",
    )

    assert active.version == 2
    assert active.approved_payload_hash == "hash-1"
    assert disabled.status is ReminderStatus.DISABLED
    assert disabled.version == 3


def test_worker_applies_crossing_cooldown_dedupe_and_bounded_delivery_retry() -> None:
    at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    repository = FakeReminderRepository()
    rule = _price_rule()
    repository.rules[(rule.owner_id, rule.reminder_id)] = rule
    observations = QueuedObservationProvider(
        [
            ReminderObservation("reminder-1", at, "quote-1", 99.0),
            ReminderObservation("reminder-1", at + timedelta(minutes=1), "quote-2", 101.0),
            ReminderObservation("reminder-1", at + timedelta(minutes=2), "quote-3", 102.0),
        ]
    )
    notifications = InMemoryNotificationAdapter(failures_before_success=2)
    worker = ReminderWorker(
        repository, observations, notifications, delivery_policy=_delivery_policy()
    )

    assert asyncio.run(worker.run_once(now=at)) == ()
    triggered = asyncio.run(worker.run_once(now=at + timedelta(minutes=1)))
    assert len(triggered) == 1
    assert triggered[0].delivery_status is DeliveryStatus.DELIVERED
    assert triggered[0].delivery_attempts == 3
    assert triggered[0].indicates_execution is False
    assert triggered[0].message == TRIGGER_MESSAGE
    assert len(repository.triggers) == 1
    assert len(notifications.attempts) == 3

    assert asyncio.run(worker.run_once(now=at + timedelta(minutes=2))) == ()
    assert len(repository.triggers) == 1
    assert repository.record_trigger(triggered[0]) is False


def test_worker_records_failed_delivery_after_retry_budget() -> None:
    at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    repository = FakeReminderRepository()
    rule = _price_rule()
    repository.rules[(rule.owner_id, rule.reminder_id)] = rule
    repository.save_observation(
        rule.reminder_id,
        rule.version,
        ReminderObservation("reminder-1", at, "quote-1", 99.0),
    )
    observations = QueuedObservationProvider(
        [ReminderObservation("reminder-1", at + timedelta(minutes=1), "quote-2", 101.0)]
    )
    notifications = InMemoryNotificationAdapter(failures_before_success=10)
    worker = ReminderWorker(
        repository,
        observations,
        notifications,
        delivery_policy=_delivery_policy(max_attempts=2, retry_delays=(0.0,)),
    )

    result = asyncio.run(worker.run_once(now=at + timedelta(minutes=1)))

    assert result[0].delivery_status is DeliveryStatus.FAILED
    assert result[0].delivery_attempts == 2
    assert len(notifications.attempts) == 2


def test_reminder_tools_declare_hitl_and_idempotency_metadata() -> None:
    repository = FakeReminderRepository()
    application = ReminderApplication(repository, execution_disclaimer=DISCLAIMER)
    create = CreateReminderTool(application)
    transition = SetReminderStatusTool(application)
    get = GetReminderTool(application)
    context = ToolExecutionContext(ToolExecutionPrincipal(owner_id="owner-1"))
    create_request = ToolRequest(
        "reminder.create",
        {
            "reminder_id": "reminder-1",
            "plan_id": "plan-1",
            "rule_type": "price_threshold",
            "condition": {
                "security_id": "NASDAQ:NVDA",
                "threshold": 100.0,
                "direction": "crosses_above",
            },
            "notification_channel": "in_app",
            "cooldown_seconds": 300,
        },
        idempotency_key="create-key",
        context=context,
    )
    drafted = asyncio.run(create.handle(create_request))
    replay = asyncio.run(create.handle(create_request))

    assert drafted.payload == replay.payload
    assert create.manifest.requires_idempotency_key
    assert transition.manifest.requires_hitl
    assert transition.manifest.requires_idempotency_key
    with pytest.raises(PermissionError, match="HITL approval"):
        asyncio.run(
            transition.handle(
                ToolRequest(
                    "reminder.set_status",
                    {
                        "reminder_id": "reminder-1",
                        "target_status": "active",
                        "approved": True,
                        "payload_hash": "hash-1",
                    },
                    idempotency_key="activate-key",
                    context=context,
                )
            )
        )
    activated = asyncio.run(
        transition.handle(
            ToolRequest(
                "reminder.set_status",
                {
                    "reminder_id": "reminder-1",
                    "target_status": "active",
                    "approved": True,
                    "payload_hash": "hash-1",
                },
                idempotency_key="activate-key",
                approval_interaction_id="approval-1",
                context=context,
            )
        )
    )
    found = asyncio.run(
        get.handle(ToolRequest("reminder.get", {"reminder_id": "reminder-1"}, context=context))
    )
    assert activated.payload["status"] == "active"
    assert found.payload["status"] == "active"


def test_reminder_and_unsupported_cards_are_allowlisted_and_deterministic() -> None:
    presenter = ReminderCardPresenter()
    reminder = presenter.present(
        CapabilityResult(
            "reminder-1",
            2,
            {
                "card_type": "reminder",
                "reminder_id": "reminder-1",
                "plan_id": "plan-1",
                "status": "active",
                "rule_type": "price_threshold",
                "condition": {
                    "security_id": "NASDAQ:NVDA",
                    "threshold": 100.0,
                    "direction": "crosses_above",
                },
                "notification_channel": "in_app",
                "execution_disclaimer": DISCLAIMER,
            },
        )
    )
    unsupported = presenter.present(
        CapabilityResult(
            "request-1",
            1,
            {
                "card_type": "unsupported",
                "title": "不支持真实下单",
                "message": "首版只能创建计划与提醒 / 不能下单或声明成交。",
                "unsupported_kind": "broker_order",
                "unsupported_schema_version": 1,
            },
        )
    )

    assert reminder.kind == "artifact.reminder"
    assert reminder.source.source_id == "reminder-1"
    assert "不表示下单或成交" in reminder.text_fallback
    assert unsupported.kind == "notice.unsupported"
    assert unsupported.actions == ("refresh",)
