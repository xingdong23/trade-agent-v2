"""真实 conversation -> HITL -> Card -> plan artifact 纵切面。"""

from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from httpx import Response

from trade_agent.apps.api import create_app
from trade_agent.apps.container import ApplicationContainer, build_application_container
from trade_agent.core.config import AppSettings, AuthenticationSettings, DatabaseSettings
from trade_agent.core.llm import JsonValue
from trade_agent.core.runtime import Intent, IntentClassification
from trade_agent.core.testing import MappingIntentClassifier


def _client(tmp_path: Path) -> tuple[TestClient, ApplicationContainer]:
    settings = AppSettings(
        database=DatabaseSettings(path=tmp_path / "conversation.db"),
        authentication=AuthenticationSettings(mode="development", development_user_id=None),
    )
    classifier = MappingIntentClassifier(
        {
            "新增一个交易": IntentClassification(
                Intent.PLANNING,
                "planning.choose_operation",
                1.0,
                reason_code="test_fixture",
            ),
            "我要买 NVDA": IntentClassification(
                Intent.PLANNING,
                "planning.create_plan",
                1.0,
                reason_code="test_fixture",
                entities=(("symbol", "NVDA"),),
            ),
        }
    )
    container = build_application_container(settings, intent_classifier=classifier)
    return TestClient(create_app(settings, container)), container


def _respond(
    client: TestClient,
    interaction_id: str,
    card: dict[str, JsonValue],
    *,
    action: str,
    values: dict[str, JsonValue],
    idempotency_key: str,
) -> Response:
    source = card["source"]
    assert isinstance(source, dict)
    response = client.post(
        f"/api/hitl/{interaction_id}/responses",
        headers={"X-User-ID": "owner-a"},
        json={
            "action": action,
            "values": values,
            "interaction_version": source["version"],
            "payload_hash": card["payload_hash"],
            "idempotency_key": idempotency_key,
            "card_revision": card["revision"],
        },
    )
    return cast(Response, response)


def _complete_values(*, target: str = "到达目标区间后复核") -> dict[str, JsonValue]:
    return {
        "symbol": "NVDA",
        "exchange": "NYSE",
        "direction": "买入研究计划, 不执行下单",
        "horizon": "20 个交易日",
        "entry_condition": "回踩后重新站上关键位",
        "invalidation_condition": "收盘跌破失效位",
        "target": target,
        "position_notes": "按风险预算分批处理",
        "risk_notes": "财报与模型失效风险需要人工复核",
    }


def test_choice_exposes_supported_and_disabled_paths(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    headers = {"X-User-ID": "owner-a"}
    run = client.post(
        "/api/conversations/runs",
        headers=headers,
        json={"thread_id": "choice-thread", "message": "新增一个交易"},
    )
    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "waiting_for_human"
    card = body["card"]
    assert card["kind"] == "interaction.choice"
    assert [option["disabled"] for option in card["data"]["options"]] == [
        False,
        True,
        True,
    ]

    unsupported = _respond(
        client,
        body["pending_interaction_id"],
        card,
        action="continue",
        values={"choice": "execute_trade"},
        idempotency_key="choice-unsupported",
    )
    assert unsupported.status_code == 200
    assert unsupported.json()["card"]["kind"] == "notice.unsupported"


def test_buy_plan_runs_form_edit_supersede_confirm_and_refresh_recovery(
    tmp_path: Path,
) -> None:
    client, container = _client(tmp_path)
    headers = {"X-User-ID": "owner-a"}
    started = client.post(
        "/api/conversations/runs",
        headers=headers,
        json={"thread_id": "plan-thread", "message": "我要买 NVDA"},
    )
    assert started.status_code == 200
    run = started.json()
    assert run["status"] == "waiting_for_human"
    assert isinstance(run["user_message_id"], str)
    form_id = run["pending_interaction_id"]
    form_card = run["card"]
    assert form_card["kind"] == "interaction.form"
    fields = {field["key"]: field for field in form_card["data"]["fields"]}
    assert fields["exchange"]["value"] is None

    invalid = _respond(
        client,
        form_id,
        form_card,
        action="continue",
        values={"symbol": "NVDA"},
        idempotency_key="form-invalid",
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["field_errors"]

    continued = _respond(
        client,
        form_id,
        form_card,
        action="continue",
        values=_complete_values(),
        idempotency_key="form-complete",
    )
    assert continued.status_code == 200
    approval_card = continued.json()["card"]
    assert approval_card["kind"] == "interaction.approval"
    approval_id = approval_card["source"]["source_id"]

    edited = _respond(
        client,
        approval_id,
        approval_card,
        action="edit",
        values={},
        idempotency_key="approval-edit",
    )
    assert edited.status_code == 200
    revised_form = edited.json()["card"]
    assert revised_form["kind"] == "interaction.form"

    revised = _respond(
        client,
        revised_form["source"]["source_id"],
        revised_form,
        action="continue",
        values=_complete_values(target="先看前高, 再人工复核"),
        idempotency_key="form-revised",
    )
    assert revised.status_code == 200
    latest_approval = revised.json()["card"]
    assert latest_approval["kind"] == "interaction.approval"
    latest_approval_id = latest_approval["source"]["source_id"]
    confirm_body = {
        "action": "confirm",
        "values": {},
        "interaction_version": latest_approval["source"]["version"],
        "payload_hash": latest_approval["payload_hash"],
        "idempotency_key": "approval-confirm",
        "card_revision": latest_approval["revision"],
    }
    confirmed = client.post(
        f"/api/hitl/{latest_approval_id}/responses", headers=headers, json=confirm_body
    )
    replay = client.post(
        f"/api/hitl/{latest_approval_id}/responses", headers=headers, json=confirm_body
    )
    assert confirmed.status_code == replay.status_code == 200
    assert confirmed.json() == replay.json()
    artifact = confirmed.json()["card"]
    assert artifact["kind"] == "artifact.trade_plan"
    assert artifact["data"]["title"].startswith("US:NYSE:NVDA")
    assert "不提供交易执行能力" in artifact["data"]["summary"]

    pending = client.get("/api/hitl/pending?thread_id=plan-thread", headers=headers).json()
    assert pending["items"] == []
    artifacts = client.get("/api/artifacts?thread_id=plan-thread", headers=headers).json()
    assert artifacts["items"][0]["card"]["card_id"] == artifact["card_id"]
    snapshot = client.get(
        "/api/conversations/plan-thread/snapshot",
        headers=headers,
    ).json()
    assert snapshot["messages"] == [
        {
            "id": run["user_message_id"],
            "role": "user",
            "content": "我要买 NVDA",
            "sequence": 1,
            "created_at": snapshot["messages"][0]["created_at"],
        }
    ]
    assert artifact["card_id"] in {card["card_id"] for card in snapshot["cards"]}
    other_owner_snapshot = client.get(
        "/api/conversations/plan-thread/snapshot",
        headers={"X-User-ID": "owner-b"},
    ).json()
    assert other_owner_snapshot == {"cards": [], "messages": [], "cursor": ""}
    events = client.get(f"/api/runs/{run['run_id']}/events?after=0", headers=headers).text
    assert "event: card.superseded" in events
    assert "event: card.resolved" in events
    assert events.count("event: card.created") >= 5

    tracer = container.tracer
    assert tracer is not None
    assert any(event.event_type == "card.created" for event in tracer.events())


def test_runtime_uses_injected_classification_instead_of_message_keywords(tmp_path: Path) -> None:
    settings = AppSettings(
        database=DatabaseSettings(path=tmp_path / "injected-routing.db"),
        authentication=AuthenticationSettings(mode="development", development_user_id=None),
    )
    classifier = MappingIntentClassifier(
        {
            "任意自定义输入": IntentClassification(
                Intent.PLANNING,
                "planning.create_plan",
                1.0,
                reason_code="test_fixture",
                entities=(("symbol", "MSFT"),),
            )
        }
    )
    container = build_application_container(settings, intent_classifier=classifier)
    client = TestClient(create_app(settings, container))

    response = client.post(
        "/api/conversations/runs",
        headers={"X-User-ID": "owner-a"},
        json={"thread_id": "injected-route", "message": "任意自定义输入"},
    )

    assert response.status_code == 200
    assert response.json()["card"]["kind"] == "interaction.form"
    assert response.json()["card"]["data"]["fields"][0]["value"] == "MSFT"
    assert response.json()["card"]["data"]["fields"][1]["value"] is None
