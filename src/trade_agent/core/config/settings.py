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
    """所有配置模型共享的严格基类。

    Attributes:
        model_config: Pydantic 冻结与 extra=forbid 策略，保证配置对象不可变且拒绝未知字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class DatabaseSettings(StrictSettingsModel):
    """SQLite 持久层配置。

    Attributes:
        path: 数据库文件路径；生产环境必须是绝对路径。
        busy_timeout_ms: SQLite 忙等待超时，单位毫秒。
    """

    path: Path = Path(".data/trade-agent.db")
    busy_timeout_ms: int = Field(default=5_000, ge=1, le=120_000)


class CheckpointSettings(StrictSettingsModel):
    """LangGraph checkpoint 存储配置。

    Attributes:
        enabled: 是否启用 checkpoint 持久化。
        namespace: checkpoint 逻辑命名空间，用于隔离不同应用实例。
    """

    enabled: bool = True
    namespace: str = Field(default="trade-agent", min_length=1)


class LiteLLMRouteSettings(StrictSettingsModel):
    """单个逻辑模型路由的限额与 provider 约束。

    Attributes:
        model: 主模型标识，通常包含 provider/model 形式的名字。
        allowed_providers: 当前逻辑路由允许使用的 provider 白名单。
        timeout_seconds: 单次模型调用超时，单位秒。
        max_tokens: 单次输出 token 上限。
        concurrency_limit: 该逻辑路由的并发上限。
        budget_usd: 可选美元预算；达到后停止继续调用。
        fallback_routes: 预留的后备逻辑路由名称列表。
    """

    model: str = Field(min_length=1)
    allowed_providers: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_tokens: int = Field(default=2_048, ge=1)
    concurrency_limit: int = Field(default=4, ge=1)
    budget_usd: float | None = Field(default=None, gt=0)
    fallback_routes: tuple[str, ...] = ()


class LiteLLMSettings(StrictSettingsModel):
    """LiteLLM 适配层的全局配置。

    Attributes:
        routes: 逻辑路由名称到路由配置的映射。
    """

    routes: dict[str, LiteLLMRouteSettings] = Field(default_factory=dict)


class QuantitativeModelSettings(StrictSettingsModel):
    """量化模型 runtime 与 registry 配置。

    Attributes:
        runtime: 当前启用的专用量化 runtime 类型。
        registry_path: 模型 artifact registry 的本地目录。
        approved_model_alias: 生产环境允许调用的批准模型别名。
    """

    runtime: Literal["lightgbm", "lstm", "fake"] = "fake"
    registry_path: Path = Path(".data/models")
    approved_model_alias: str | None = None


class WorkerSettings(StrictSettingsModel):
    """后台任务 worker 配置。

    Attributes:
        worker_id: 当前 worker 的稳定标识。
        process_count: 进程数；当前版本固定为 1。
        lease_seconds: 作业 lease 时长，单位秒。
    """

    worker_id: str = Field(default="local-worker", min_length=1)
    process_count: int = Field(default=1, ge=1, le=1)
    lease_seconds: int = Field(default=60, ge=5, le=3_600)


class AuthenticationSettings(StrictSettingsModel):
    """认证模式与 OIDC 连接配置。

    Attributes:
        mode: 运行时认证模式，development 或 oidc。
        development_user_id: development 模式下注入的默认用户 ID。
        issuer: OIDC issuer 地址。
        audience: OIDC audience 标识。
    """

    mode: Literal["development", "oidc"] = "development"
    development_user_id: str | None = "local-user"
    issuer: HttpUrl | None = None
    audience: str | None = None


class ObservabilitySettings(StrictSettingsModel):
    """可观测性导出配置。

    Attributes:
        backend: 可观测性后端类型。
        endpoint: 远端导出地址；仅在启用远程导出时需要。
        export_enabled: 是否真正打开导出，而非仅保留本地 trace。
    """

    backend: Literal["local", "otlp"] = "local"
    endpoint: HttpUrl | None = None
    export_enabled: bool = False


class AppSettings(BaseSettings):
    """整个进程唯一的配置入口.

    嵌套项使用双下划线, 例如
    ``TRADE_AGENT_DATABASE__PATH=/var/lib/trade-agent/app.db``.

    Attributes:
        environment: 当前运行环境。
        database: 数据库配置。
        checkpoint: checkpoint 配置。
        litellm: LiteLLM 路由配置集合。
        quantitative_model: 专用量化模型 runtime 配置。
        worker: 后台 worker 配置。
        authentication: 认证与身份校验配置。
        observability: trace 与导出配置。
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
