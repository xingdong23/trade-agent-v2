import asyncio
from pathlib import Path

import pytest

from trade_agent.adapters.llm.litellm import LiteLLMClient
from trade_agent.adapters.market_providers import FakeMarketProvider
from trade_agent.apps.container import build_application_container
from trade_agent.apps.journeys import ConversationRunResult, ConversationRuntimePort
from trade_agent.apps.journeys.contracts import JourneyStartContext
from trade_agent.core.config import AppSettings, DatabaseSettings
from trade_agent.core.hitl import HumanInteraction
from trade_agent.core.llm import ModelRoute
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.runtime import AgentManifest
from trade_agent.core.testing import FakeLLMClient, FakeToolGateway
from trade_agent.core.tools import ToolManifest, ToolRequest, ToolResult


class AlternateProvider:
    pass


class CustomTool:
    """验证组合根按注入实例生成 Tool 注册目录。"""

    manifest = ToolManifest(
        "lesson.echo",
        "返回自定义示例输入",
        True,
        False,
        {"type": "object", "properties": {}, "additionalProperties": False},
        {"type": "object", "properties": {}, "additionalProperties": False},
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        del request
        return ToolResult("ok")


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
    assert set(replaced.capability_tool_ids) == {
        "planning.create_plan_draft",
        "planning.transition_plan",
        "planning.record_review",
    }
    assert replaced.worker_ids == ("scan-worker", "reminder-worker")
    assert replaced.llm_client is alternate_llm
    assert replaced.tool_gateway is alternate_gateway
    assert replaced.market_provider is alternate_provider
    assert isinstance(baseline.market_provider, FakeMarketProvider)


def test_composition_root_accepts_registered_agents_tools_and_workers(tmp_path: Path) -> None:
    settings = AppSettings(database=DatabaseSettings(path=tmp_path / "registries.db"))
    custom_agent = AgentManifest(
        "lesson",
        "自定义 Agent",
        ModelRoute("lesson_route"),
        ("lesson.echo",),
        "lesson.prompt",
        "v1",
    )

    container = build_application_container(
        settings,
        agents=(custom_agent,),
        capability_tools=(CustomTool(),),
        worker_ids=("lesson-worker",),
    )

    assert tuple(agent.agent_id for agent in container.agents) == ("lesson",)
    assert container.capability_tool_ids == ("lesson.echo",)
    assert container.worker_ids == ("lesson-worker",)
    assert "lesson" in container.graph.get_graph().nodes


def test_deployment_policy_can_replace_manifest_tool_allowlist(tmp_path: Path) -> None:
    settings = AppSettings.model_validate(
        {
            "database": {"path": tmp_path / "tool-policy.db"},
            "agent_tool_policy": {"allowlists": {"lesson": []}},
        }
    )
    custom_agent = AgentManifest(
        "lesson",
        "自定义 Agent",
        ModelRoute("lesson_route"),
        ("lesson.echo",),
        "lesson.prompt",
        "v1",
    )
    container = build_application_container(
        settings,
        agents=(custom_agent,),
        capability_tools=(CustomTool(),),
    )

    result = asyncio.run(
        container.tool_gateway.invoke(ToolRequest("lesson.echo", {}, agent_id="lesson"))
    )

    assert result.error is not None
    assert result.error.code.value == "forbidden"


def test_deployment_policy_rejects_unknown_agent_id(tmp_path: Path) -> None:
    settings = AppSettings.model_validate(
        {
            "database": {"path": tmp_path / "unknown-agent-policy.db"},
            "agent_tool_policy": {"allowlists": {"not-registered": ["lesson.echo"]}},
        }
    )

    with pytest.raises(ValueError, match="未注册 Agent"):
        build_application_container(settings)


def test_composition_root_accepts_replacement_journey_set(tmp_path: Path) -> None:
    settings = AppSettings(database=DatabaseSettings(path=tmp_path / "custom-journey.db"))

    container = build_application_container(
        settings,
        conversation_journeys=(CustomJourney(),),
    )

    assert container.conversation_runtime is not None
    assert container.conversation_runtime.registered_journey_ids() == ("custom.lesson",)


def _production_settings(tmp_path: Path) -> AppSettings:
    """构造不包含任何凭据的 production 装配测试配置。"""

    return AppSettings.model_validate(
        {
            "environment": "production",
            "database": {"path": tmp_path / "production.db"},
            "authentication": {
                "mode": "oidc",
                "issuer": "https://identity.example.com",
                "audience": "trade-agent",
                "development_user_id": None,
            },
            "litellm": {
                "routes": {
                    "intent_classifier": {
                        "endpoint": {"provider": "openai", "model": "configured-alias"},
                        "allowed_providers": ["openai"],
                    }
                }
            },
            "quantitative_model": {
                "runtime": "lightgbm",
                "registry_path": tmp_path / "models",
                "approved_model_alias": "production",
            },
        }
    )


def test_production_container_builds_litellm_and_requires_market_provider(tmp_path: Path) -> None:
    settings = _production_settings(tmp_path)

    with pytest.raises(ValueError, match="真实 market provider"):
        build_application_container(settings)

    provider = AlternateProvider()
    container = build_application_container(settings, market_provider=provider)

    assert isinstance(container.llm_client, LiteLLMClient)
    assert container.market_provider is provider
