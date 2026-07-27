"""从环境变量加载的供应商无关配置。"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class StrictSettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatabaseSettings(StrictSettingsModel):
    path: Path = Path(".data/trade-agent.db")
    busy_timeout_ms: int = Field(default=5_000, ge=1, le=120_000)


class CheckpointSettings(StrictSettingsModel):
    enabled: bool = True
    namespace: str = Field(default="trade-agent", min_length=1)


class LiteLLMRouteSettings(StrictSettingsModel):
    model: str = Field(min_length=1)
    allowed_providers: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_tokens: int = Field(default=2_048, ge=1)
    concurrency_limit: int = Field(default=4, ge=1)
    budget_usd: float | None = Field(default=None, gt=0)
    fallback_routes: tuple[str, ...] = ()


class LiteLLMSettings(StrictSettingsModel):
    routes: dict[str, LiteLLMRouteSettings] = Field(default_factory=dict)


class QuantitativeModelSettings(StrictSettingsModel):
    runtime: Literal["lightgbm", "lstm", "fake"] = "fake"
    registry_path: Path = Path(".data/models")
    approved_model_alias: str | None = None


class WorkerSettings(StrictSettingsModel):
    worker_id: str = Field(default="local-worker", min_length=1)
    process_count: int = Field(default=1, ge=1, le=1)
    lease_seconds: int = Field(default=60, ge=5, le=3_600)


class AuthenticationSettings(StrictSettingsModel):
    mode: Literal["development", "oidc"] = "development"
    development_user_id: str | None = "local-user"
    issuer: HttpUrl | None = None
    audience: str | None = None


class ObservabilitySettings(StrictSettingsModel):
    backend: Literal["local", "otlp"] = "local"
    endpoint: HttpUrl | None = None
    export_enabled: bool = False


class AppSettings(BaseSettings):
    """整个进程唯一的配置入口.

    嵌套项使用双下划线, 例如
    ``TRADE_AGENT_DATABASE__PATH=/var/lib/trade-agent/app.db``.
    """

    model_config = SettingsConfigDict(
        env_prefix="TRADE_AGENT_",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    environment: AppEnvironment = AppEnvironment.DEVELOPMENT
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    checkpoint: CheckpointSettings = Field(default_factory=CheckpointSettings)
    litellm: LiteLLMSettings = Field(default_factory=LiteLLMSettings)
    quantitative_model: QuantitativeModelSettings = Field(default_factory=QuantitativeModelSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    authentication: AuthenticationSettings = Field(default_factory=AuthenticationSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @model_validator(mode="after")
    def validate_startup_policy(self) -> "AppSettings":
        if self.environment is not AppEnvironment.PRODUCTION:
            return self

        failures: list[str] = []
        if not self.database.path.is_absolute():
            failures.append("production database.path 必须是绝对路径")
        if not self.checkpoint.enabled:
            failures.append("production 必须启用 checkpoint")
        if self.authentication.mode != "oidc":
            failures.append("production 必须使用 oidc 认证")
        if self.authentication.issuer is None or not self.authentication.audience:
            failures.append("production 必须配置认证 issuer 和 audience")
        if not self.litellm.routes:
            failures.append("production 必须配置至少一个 LiteLLM 逻辑路由")
        if self.quantitative_model.runtime == "fake":
            failures.append("production 禁止使用 fake 量化模型")
        if not self.quantitative_model.approved_model_alias:
            failures.append("production 必须指定已批准量化模型 alias")
        if self.observability.export_enabled and self.observability.endpoint is None:
            failures.append("启用 observability export 时必须配置 endpoint")
        if failures:
            raise ValueError("; ".join(failures))
        return self
