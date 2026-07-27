from pathlib import Path

from trade_agent.adapters.market_providers import FakeMarketProvider
from trade_agent.apps.container import build_application_container
from trade_agent.apps.journeys import ConversationRunResult, ConversationRuntimePort
from trade_agent.apps.journeys.contracts import JourneyStartContext
from trade_agent.core.config import AppSettings, DatabaseSettings
from trade_agent.core.hitl import HumanInteraction
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.testing import FakeLLMClient, FakeToolGateway


class AlternateProvider:
    pass


class CustomJourney:
    """验证组合根可以直接注入的最小 Journey 插件。"""

    @property
    def journey_ids(self) -> tuple[str, ...]:
        return ("custom.lesson",)

    @property
    def subject_types(self) -> tuple[str, ...]:
        return ("custom.lesson.form",)

    def start(
        self,
        context: JourneyStartContext,
        runtime: ConversationRuntimePort,
    ) -> ConversationRunResult:
        del runtime
        return ConversationRunResult(context.run_id, context.thread_id, "completed")

    def resume(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope | None:
        del interaction, runtime
        return None


def test_composition_root_registers_agents_tools_workers_and_adapters(tmp_path: Path) -> None:
    settings = AppSettings(database=DatabaseSettings(path=tmp_path / "composition.db"))
    baseline = build_application_container(settings)
    alternate_llm = FakeLLMClient()
    alternate_gateway = FakeToolGateway()
    alternate_provider = AlternateProvider()
    replaced = build_application_container(
        settings,
        llm_client=alternate_llm,
        tool_gateway=alternate_gateway,
        market_provider=alternate_provider,
    )

    assert tuple(item.agent_id for item in replaced.agents) == (
        "research",
        "strategy",
        "planning",
    )
    assert replaced.capability_tool_ids == baseline.capability_tool_ids
    assert {
        "market_research.research_security",
        "quantitative.submit_scan",
        "planning.create_plan_draft",
        "reminder.set_status",
    } <= set(replaced.capability_tool_ids)
    assert replaced.worker_ids == ("scan-worker", "reminder-worker")
    assert replaced.llm_client is alternate_llm
    assert replaced.tool_gateway is alternate_gateway
    assert replaced.market_provider is alternate_provider
    assert isinstance(baseline.market_provider, FakeMarketProvider)


def test_composition_root_accepts_replacement_journey_set(tmp_path: Path) -> None:
    settings = AppSettings(database=DatabaseSettings(path=tmp_path / "custom-journey.db"))

    container = build_application_container(
        settings,
        conversation_journeys=(CustomJourney(),),
    )

    assert container.conversation_runtime is not None
    assert container.conversation_runtime.registered_journey_ids() == ("custom.lesson",)
