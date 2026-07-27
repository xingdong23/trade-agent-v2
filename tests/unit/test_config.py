"""类型化配置和生产启动门禁测试。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

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
                        "model": "openai/model-name",
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
