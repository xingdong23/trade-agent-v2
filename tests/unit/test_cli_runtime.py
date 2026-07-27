from pathlib import Path

from trade_agent.apps.cli import execute
from trade_agent.apps.container import build_application_container
from trade_agent.core.config import AppSettings, AuthenticationSettings, DatabaseSettings
from trade_agent.core.runtime import Intent, IntentClassification
from trade_agent.core.testing import MappingIntentClassifier


def test_cli_uses_shared_container_for_run_and_hitl(tmp_path: Path) -> None:
    settings = AppSettings(
        database=DatabaseSettings(path=tmp_path / "cli.db"),
        authentication=AuthenticationSettings(development_user_id="owner-a"),
    )
    container = build_application_container(
        settings,
        intent_classifier=MappingIntentClassifier(
            {
                "我要买 NVDA": IntentClassification(
                    Intent.PLANNING,
                    "planning.create_plan",
                    1.0,
                    reason_code="test_fixture",
                    entities=(("symbol", "NVDA"),),
                )
            }
        ),
    )
    run = execute(
        ("run", "--thread", "thread-1", "我要买 NVDA"),
        container=container,
        settings=settings,
    )
    assert run["status"] == "waiting_for_human"
    assert isinstance(run["pending_interaction_id"], str)
    card = run["card"]
    assert isinstance(card, dict)
    assert card["kind"] == "interaction.form"

    service = container.hitl_service
    assert service is not None
    interaction_id = str(run["pending_interaction_id"])
    interaction = service.get("owner-a", interaction_id)
    assert interaction is not None
    pending = execute(("hitl", "list"), container=container, settings=settings)
    assert pending["pending"] == [
        {"interaction_id": interaction_id, "type": "exception_resolution", "version": 1}
    ]
    resolved = execute(
        (
            "hitl",
            "respond",
            interaction_id,
            "--version",
            "1",
            "--subject-version",
            "1",
            "--payload-hash",
            interaction.payload_hash,
            "--action",
            "continue",
            "--values",
            (
                '{"symbol":"NVDA","exchange":"NYSE","direction":"买入研究计划",'
                '"horizon":"20 个交易日","entry_condition":"重新站上关键位",'
                '"invalidation_condition":"跌破失效位","target":"到达目标区间后复核",'
                '"position_notes":"按风险预算分批","risk_notes":"财报风险"}'
            ),
        ),
        container=container,
        settings=settings,
    )
    assert resolved["status"] == "resolved"
    next_card = resolved["card"]
    assert isinstance(next_card, dict)
    assert next_card["kind"] == "interaction.approval"
