"""类型化配置和生产启动门禁测试。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from trade_agent.apps.journeys import (
    planning_presenter_config_from_settings,
    research_to_plan_journey_config_from_settings,
)
from trade_agent.core.config import AppEnvironment, AppSettings


def test_development_defaults_are_local_and_single_worker() -> None:
    settings = AppSettings.model_validate({})

    assert settings.environment is AppEnvironment.DEVELOPMENT
    assert settings.database.path == Path(".data/trade-agent.db")
    assert settings.checkpoint.enabled
    assert settings.worker.process_count == 1
    assert settings.quantitative_model.runtime == "fake"


def test_nested_environment_values_are_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADE_AGENT_DATABASE__PATH", "/tmp/trade-agent.db")
    monkeypatch.setenv("TRADE_AGENT_DATABASE__BUSY_TIMEOUT_MS", "9000")
    monkeypatch.setenv("TRADE_AGENT_WORKER__LEASE_SECONDS", "120")

    settings = AppSettings()

    assert settings.database.path == Path("/tmp/trade-agent.db")
    assert settings.database.busy_timeout_ms == 9_000
    assert settings.worker.lease_seconds == 120


def test_planning_presenter_config_uses_typed_settings_catalog() -> None:
    settings = AppSettings.model_validate(
        {
            "market": {
                "exchange_codes": ["nyse", "arca"],
                "symbol_pattern": r"^[A-Z.]+$",
            },
            "hitl": {"pending_ttl_seconds": 600, "text_field_max_length": 240},
        }
    )

    presenter_config = planning_presenter_config_from_settings(settings.planning_journey)

    assert settings.market.exchange_codes == ("NYSE", "ARCA")
    assert settings.market.symbol_pattern == r"^[A-Z.]+$"
    assert settings.hitl.pending_ttl_seconds == 600
    assert settings.hitl.text_field_max_length == 240
    assert [field.key for field in presenter_config.field_specs][:3] == [
        "security_id",
        "symbol",
        "exchange",
    ]
    assert presenter_config.field("risk_notes").label == "风险说明"


def test_api_process_and_resource_catalog_are_typed_configuration() -> None:
    settings = AppSettings.model_validate(
        {
            "api": {
                "host": "0.0.0.0",
                "port": 9_001,
                "resource_names": ["lessons", "artifacts"],
            }
        }
    )

    assert settings.api.host == "0.0.0.0"
    assert settings.api.port == 9_001
    assert settings.api.resource_names == ("lessons", "artifacts")


def test_research_to_plan_runtime_uses_typed_deployment_policy() -> None:
    settings = AppSettings.model_validate(
        {
            "checkpoint": {"namespace": "course-runtime"},
            "conversation_runtime": {
                "unregistered_journey_message": "当前部署没有注册对应业务流程"
            },
            "research_to_plan_journey": {
                "reminder_approval": {
                    "notification_channel": "desktop_push",
                    "summary_template": "为计划 {plan_id} 启用桌面复核提醒。",
                },
                "plan_review": {
                    "resource_name": "course_reviews",
                    "feedback_destinations": [{"value": "training_examples", "label": "训练样例"}],
                },
                "plan_lineage": {"source_type": "course_research_artifact"},
            },
        }
    )

    runtime_config = research_to_plan_journey_config_from_settings(
        settings.research_to_plan_journey
    )

    assert settings.checkpoint.namespace == "course-runtime"
    assert settings.conversation_runtime.unregistered_journey_message == (
        "当前部署没有注册对应业务流程"
    )
    assert runtime_config.reminder_approval.notification_channel == "desktop_push"
    assert runtime_config.plan_review.resource_name == "course_reviews"
    assert runtime_config.plan_review.feedback_destinations[0].value == "training_examples"
    assert runtime_config.plan_lineage.source_type == "course_research_artifact"


def test_production_fails_closed_when_critical_configuration_is_missing() -> None:
    with pytest.raises(ValidationError) as error:
        AppSettings.model_validate({"environment": AppEnvironment.PRODUCTION})

    message = str(error.value)
    assert "production database.path 必须是绝对路径" in message
    assert "production 必须使用 oidc 认证" in message
    assert "production 必须配置至少一个 LiteLLM 逻辑路由" in message
    assert "production 禁止使用 fake 量化模型" in message


def test_valid_production_configuration_loads() -> None:
    settings = AppSettings.model_validate(
        {
            "environment": "production",
            "database": {"path": "/var/lib/trade-agent/app.db"},
            "authentication": {
                "mode": "oidc",
                "issuer": "https://identity.example.com",
                "audience": "trade-agent",
                "development_user_id": None,
            },
            "litellm": {
                "routes": {
                    "intent_classifier": {
                        "endpoint": {"provider": "openai", "model": "model-name"},
                        "allowed_providers": ["openai"],
                    }
                }
            },
            "quantitative_model": {
                "runtime": "lightgbm",
                "registry_path": "/var/lib/trade-agent/models",
                "approved_model_alias": "production",
            },
        }
    )

    assert settings.environment is AppEnvironment.PRODUCTION
    assert settings.litellm.routes["intent_classifier"].allowed_providers == ("openai",)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "research-summary-alias"),
        ("azure", "gpt-4o-mini-deployment"),
        ("vertex_ai", "publishers/google/models/gemini-1.5-pro"),
    ],
)
def test_litellm_route_settings_accept_explicit_endpoint_shapes(provider: str, model: str) -> None:
    settings = AppSettings.model_validate(
        {
            "litellm": {
                "routes": {
                    "research_summarizer": {
                        "endpoint": {"provider": provider, "model": model},
                        "allowed_providers": [provider],
                        "fallback_endpoints": [
                            {"provider": provider, "model": f"{model}-fallback"}
                        ],
                    }
                }
            }
        }
    )

    route = settings.litellm.routes["research_summarizer"]
    assert route.endpoint.provider == provider
    assert route.endpoint.model == model
    assert route.fallback_endpoints[0].provider == provider


def test_litellm_route_settings_reject_unapproved_endpoint_provider() -> None:
    with pytest.raises(ValidationError) as error:
        AppSettings.model_validate(
            {
                "litellm": {
                    "routes": {
                        "research_summarizer": {
                            "endpoint": {"provider": "anthropic", "model": "claude"},
                            "allowed_providers": ["openai"],
                        }
                    }
                }
            }
        )

    assert "LiteLLM endpoint provider 'anthropic' 未在 allowlist" in str(error.value)


def test_litellm_route_settings_reject_legacy_model_string_shape() -> None:
    with pytest.raises(ValidationError) as error:
        AppSettings.model_validate(
            {
                "litellm": {
                    "routes": {
                        "research_summarizer": {
                            "model": "openai/gpt-4o-mini",
                            "allowed_providers": ["openai"],
                        }
                    }
                }
            }
        )

    assert "必须改用 endpoint.provider 与 endpoint.model" in str(error.value)


def test_oidc_and_reminder_runtime_policies_are_typed_configuration() -> None:
    settings = AppSettings.model_validate(
        {
            "authentication": {
                "subject_claim": "identity.subject",
                "role_claims": [
                    {"path": "realm_access.roles"},
                    {"path": "permissions", "separator": ","},
                ],
                "required_claims": ["exp", "iss", "aud", "identity"],
                "signing_algorithms": ["RS256"],
                "discovery_timeout_seconds": 7,
                "jwks_timeout_seconds": 11,
                "jwks_cache_ttl_seconds": 900,
            },
            "reminder": {
                "delivery_policy_version": "custom-delivery.v2",
                "notification_template_id": "custom.triggered.v2",
                "max_delivery_attempts": 2,
                "retry_delays_seconds": [3.5],
                "execution_disclaimer": "仅表示条件提醒，不代表任何执行。",
            },
        }
    )

    assert settings.authentication.subject_claim == "identity.subject"
    assert settings.authentication.role_claims[0].path == "realm_access.roles"
    assert settings.authentication.jwks_cache_ttl_seconds == 900
    assert settings.reminder.notification_template_id == "custom.triggered.v2"
    assert settings.reminder.retry_delays_seconds == (3.5,)
