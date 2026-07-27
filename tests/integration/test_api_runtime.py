from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from trade_agent.adapters.sqlite.json_support import payload_hash
from trade_agent.apps.api import create_app
from trade_agent.apps.container import build_application_container
from trade_agent.core.config import AppSettings, AuthenticationSettings, DatabaseSettings
from trade_agent.core.events import RunEvent
from trade_agent.core.hitl import HumanInteraction, InteractionStatus, InteractionType
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.security import AuthenticationError, TokenVerifier, VerifiedToken


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings = AppSettings(
        database=DatabaseSettings(path=tmp_path / "api.db"),
        authentication=AuthenticationSettings(mode="development", development_user_id=None),
    )
    container = build_application_container(settings)
    return create_app(settings, container), container


class FakeTokenVerifier(TokenVerifier):
    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    def verify(self, token: str) -> VerifiedToken:
        subject = self._tokens.get(token)
        if subject is None:
            raise AuthenticationError("OIDC token 校验失败")
        return VerifiedToken(subject=subject)


def _oidc_app(tmp_path: Path, verifier: TokenVerifier):  # type: ignore[no-untyped-def]
    settings = AppSettings.model_validate(
        {
            "database": {"path": tmp_path / "api.db"},
            "authentication": {
                "mode": "oidc",
                "development_user_id": None,
                "issuer": "https://identity.example.com",
                "audience": "trade-agent",
            },
        }
    )
    container = build_application_container(settings)
    return create_app(settings, container, token_verifier=verifier), container


def test_api_registers_resource_routes_from_deployment_catalog(tmp_path: Path) -> None:
    settings = AppSettings.model_validate(
        {
            "database": {"path": tmp_path / "custom-resources.db"},
            "api": {"resource_names": ["lessons"]},
            "authentication": {"development_user_id": "owner-a"},
        }
    )
    container = build_application_container(settings)
    client = TestClient(create_app(settings, container))

    assert client.get("/api/lessons").status_code == 200
    assert client.get("/api/plans").status_code == 404


def test_api_auth_resource_owner_scope_and_sse_cursor(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert (
        client.post(
            "/api/plans",
            json={"resource_id": "plan-1", "expected_version": 0, "payload": {}},
        ).status_code
        == 401
    )
    headers = {"X-User-ID": "owner-a"}
    saved = client.post(
        "/api/plans",
        headers=headers,
        json={"resource_id": "plan-1", "expected_version": 0, "payload": {"status": "draft"}},
    )
    assert saved.status_code == 200
    assert client.get("/api/plans/plan-1", headers=headers).status_code == 200
    assert client.get("/api/plans/plan-1", headers={"X-User-ID": "owner-b"}).status_code == 404

    run = client.post(
        "/api/conversations/runs",
        headers=headers,
        json={"thread_id": "thread-1", "message": "研究 NVDA"},
    ).json()
    stream = client.get(f"/api/runs/{run['run_id']}/events?after=0", headers=headers)
    assert stream.status_code == 200
    assert "event: run.started" in stream.text
    assert "id: 1" in stream.text
    assert "event: card.failed" in stream.text
    assert client.get(f"/api/runs/{run['run_id']}/events?after=3", headers=headers).text == ""

    container = app.state.services.container
    event_store = container.event_store
    assert event_store is not None
    for sequence, event_type, revision in (
        (4, "card.created", 1),
        (5, "card.updated", 2),
        (6, "card.resolved", 3),
        (7, "card.superseded", 4),
        (8, "card.failed", 5),
    ):
        event_store.append(
            owner_id="owner-a",
            event=RunEvent(
                f"event-{sequence}",
                str(run["run_id"]),
                sequence,
                event_type,
                {"card_id": "card-1", "revision": revision},
                datetime.now(UTC),
            ),
        )
    resumed = client.get(f"/api/runs/{run['run_id']}/events?after=5", headers=headers)
    assert [line for line in resumed.text.splitlines() if line.startswith("id:")] == [
        "id: 6",
        "id: 7",
        "id: 8",
    ]
    assert resumed.text.count('"card_id": "card-1"') == 3
    cursor_resumed = client.get(f"/api/runs/{run['run_id']}/events?after=event-5", headers=headers)
    assert [line for line in cursor_resumed.text.splitlines() if line.startswith("id:")] == [
        "id: 6",
        "id: 7",
        "id: 8",
    ]
    assert '"event_type": "card.resolved"' in cursor_resumed.text
    assert '"type": "card.resolved"' in cursor_resumed.text


def test_oidc_mode_rejects_spoofed_header_and_invalid_token(tmp_path: Path) -> None:
    app, _ = _oidc_app(tmp_path, FakeTokenVerifier({"token-owner-a": "owner-a"}))
    client = TestClient(app)
    body = {"resource_id": "plan-1", "expected_version": 0, "payload": {"status": "draft"}}

    spoofed = client.post(
        "/api/plans",
        headers={"Authorization": "Bearer token-owner-a", "X-User-ID": "owner-b"},
        json=body,
    )
    invalid = client.post(
        "/api/plans",
        headers={"Authorization": "Bearer invalid-token"},
        json=body,
    )
    missing = client.post("/api/plans", json=body)

    assert spoofed.status_code == 401
    assert spoofed.json()["detail"] == "oidc 模式禁止使用 X-User-ID"
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "OIDC token 校验失败"
    assert missing.status_code == 401
    assert missing.json()["detail"] == "缺少 Authorization Bearer token"


def test_oidc_mode_uses_verified_subject_for_owner_scope(tmp_path: Path) -> None:
    verifier = FakeTokenVerifier({"token-owner-a": "owner-a", "token-owner-b": "owner-b"})
    app, _ = _oidc_app(tmp_path, verifier)
    client = TestClient(app)
    owner_a = {"Authorization": "Bearer token-owner-a"}
    owner_b = {"Authorization": "Bearer token-owner-b"}

    saved = client.post(
        "/api/plans",
        headers=owner_a,
        json={"resource_id": "plan-1", "expected_version": 0, "payload": {"status": "draft"}},
    )

    assert saved.status_code == 200
    assert client.get("/api/plans/plan-1", headers=owner_a).status_code == 200
    assert client.get("/api/plans/plan-1", headers=owner_b).status_code == 404
    assert client.get("/api/plans", headers=owner_a).json()["items"][0]["resource_id"] == "plan-1"


def test_hitl_response_validates_revision_and_is_idempotent(tmp_path: Path) -> None:
    app, container = _app(tmp_path)
    service = container.hitl_service
    assert service is not None
    payload = {"title": "确认计划", "summary": "激活 NVDA 计划"}
    interaction = HumanInteraction(
        interaction_id="interaction-1",
        owner_id="owner-a",
        interaction_type=InteractionType.APPROVAL,
        status=InteractionStatus.PENDING,
        payload=payload,
        version=1,
        thread_id="thread-1",
        run_id="run-1",
        subject_type="plan",
        subject_id="plan-1",
        subject_version=3,
        payload_hash=payload_hash(payload),
        response_schema={
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
            "additionalProperties": False,
        },
        created_at=datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    service.create(interaction)
    client = TestClient(app)
    headers = {"X-User-ID": "owner-a"}
    pending = client.get("/api/hitl/pending?thread_id=thread-1", headers=headers)
    assert pending.status_code == 200
    pending_card = pending.json()["items"][0]["card"]
    body = {
        "action": "confirm",
        "values": {"approved": True},
        "interaction_version": 1,
        "payload_hash": pending_card["payload_hash"],
        "idempotency_key": "confirm-plan-1",
        "card_revision": pending_card["revision"],
    }
    first = client.post("/api/hitl/interaction-1/responses", headers=headers, json=body)
    replay = client.post("/api/hitl/interaction-1/responses", headers=headers, json=body)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["status"] == "resolved"
    assert first.json()["card"]["state"] == "resolved"

    refreshed = client.get("/api/hitl/interaction-1", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["card"]["state"] == "resolved"


def test_hitl_returns_field_errors_and_rejects_stale_card(tmp_path: Path) -> None:
    app, container = _app(tmp_path)
    service = container.hitl_service
    assert service is not None
    payload = {"title": "补充字段"}
    service.create(
        HumanInteraction(
            "interaction-2",
            "owner-a",
            InteractionType.EXCEPTION_RESOLUTION,
            InteractionStatus.PENDING,
            payload,
            1,
            "thread-1",
            "run-1",
            "plan",
            "plan-1",
            1,
            payload_hash(payload),
            {
                "type": "object",
                "properties": {"horizon": {"type": "string"}},
                "required": ["horizon"],
            },
            datetime.now(UTC),
        )
    )
    client = TestClient(app)
    headers = {"X-User-ID": "owner-a"}
    pending = client.get("/api/hitl/pending?thread_id=thread-1", headers=headers)
    pending_card = pending.json()["items"][0]["card"]
    base = {
        "action": "continue",
        "values": {},
        "interaction_version": 1,
        "payload_hash": pending_card["payload_hash"],
        "idempotency_key": "form-1",
        "card_revision": pending_card["revision"],
    }
    invalid = client.post("/api/hitl/interaction-2/responses", headers=headers, json=base)
    assert invalid.status_code == 422
    assert invalid.json()["field_errors"]
    assert invalid.json()["card"]["kind"] == "interaction.form"
    stale = client.post(
        "/api/hitl/interaction-2/responses",
        headers=headers,
        json={**base, "idempotency_key": "form-2", "card_revision": 2},
    )
    assert stale.status_code == 409
    assert stale.json()["card"]["revision"] == 1


def test_recovery_collections_return_card_envelopes_by_thread(tmp_path: Path) -> None:
    app, container = _app(tmp_path)
    service = container.hitl_service
    assert service is not None
    payload = {"title": "计划审批", "summary": "确认计划"}
    service.create(
        HumanInteraction(
            interaction_id="interaction-3",
            owner_id="owner-a",
            interaction_type=InteractionType.APPROVAL,
            status=InteractionStatus.PENDING,
            payload=payload,
            version=1,
            thread_id="thread-1",
            run_id="run-1",
            subject_type="plan",
            subject_id="plan-1",
            subject_version=1,
            payload_hash=payload_hash(payload),
            response_schema={
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
                "additionalProperties": False,
            },
            created_at=datetime.now(UTC),
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    client = TestClient(app)
    headers = {"X-User-ID": "owner-a"}
    card = client.get("/api/hitl/interaction-3", headers=headers).json()["card"]
    artifact_saved = client.post(
        "/api/artifacts",
        headers=headers,
        json={
            "resource_id": "artifact-1",
            "expected_version": 0,
            "payload": {"thread_id": "thread-1", "card": card},
        },
    )
    job_saved = client.post(
        "/api/jobs",
        headers=headers,
        json={
            "resource_id": "job-1",
            "expected_version": 0,
            "payload": {"thread_id": "thread-1", "card": card},
        },
    )
    other_thread = client.post(
        "/api/artifacts",
        headers=headers,
        json={
            "resource_id": "artifact-2",
            "expected_version": 0,
            "payload": {"thread_id": "thread-2", "card": card},
        },
    )
    assert artifact_saved.status_code == job_saved.status_code == other_thread.status_code == 200

    artifact_items = client.get("/api/artifacts?thread_id=thread-1", headers=headers).json()[
        "items"
    ]
    job_items = client.get("/api/jobs?thread_id=thread-1", headers=headers).json()["items"]
    pending_items = client.get("/api/hitl/pending?thread_id=thread-1", headers=headers).json()[
        "items"
    ]

    assert len(artifact_items) == len(job_items) == len(pending_items) == 1
    assert artifact_items[0]["resource_id"] == "artifact-1"
    assert job_items[0]["resource_id"] == "job-1"
    assert artifact_items[0]["card"]["card_id"] == card["card_id"]
    assert pending_items[0]["card"]["card_id"] == card["card_id"]


def test_hitl_cancel_bypasses_required_form_fields_and_is_idempotent(tmp_path: Path) -> None:
    app, container = _app(tmp_path)
    service = container.hitl_service
    assert service is not None
    payload: dict[str, JsonValue] = {"title": "补充计划", "fields": []}
    interaction = service.create(
        HumanInteraction(
            "interaction-cancel",
            "owner-a",
            InteractionType.CORRECTION,
            InteractionStatus.PENDING,
            payload,
            1,
            "thread-1",
            "run-1",
            "plan",
            "plan-1",
            1,
            payload_hash(payload),
            {
                "type": "object",
                "properties": {"horizon": {"type": "string"}},
                "required": ["horizon"],
            },
            datetime.now(UTC),
        )
    )
    client = TestClient(app)
    card = client.get("/api/hitl/interaction-cancel", headers={"X-User-ID": "owner-a"}).json()[
        "card"
    ]
    body = {
        "action": "cancel",
        "values": {},
        "interaction_version": interaction.version,
        "payload_hash": card["payload_hash"],
        "idempotency_key": "cancel-plan-1",
        "card_revision": card["revision"],
    }
    first = client.post(
        "/api/hitl/interaction-cancel/responses",
        headers={"X-User-ID": "owner-a"},
        json=body,
    )
    replay = client.post(
        "/api/hitl/interaction-cancel/responses",
        headers={"X-User-ID": "owner-a"},
        json=body,
    )

    assert card["state"] == "pending"
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["status"] == "cancelled"
    assert first.json()["card"]["state"] == "cancelled"
