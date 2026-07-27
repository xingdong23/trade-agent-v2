"""从环境变量加载的供应商无关配置。"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trade_agent.core.llm import ModelEndpoint


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


class ApiSettings(StrictSettingsModel):
    """HTTP 进程与资源端点的部署配置。

    Attributes:
        host: Uvicorn 监听地址。
        port: Uvicorn 监听端口。
        title: OpenAPI 应用标题。
        version: HTTP API 版本说明。
        resource_names: 需要通过通用资源端点暴露的聚合目录。
    """

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8_000, ge=1, le=65_535)
    title: str = Field(default="Trade Agent API", min_length=1)
    version: str = Field(default="0.1.0", min_length=1)
    resource_names: tuple[str, ...] = (
        "cards",
        "artifacts",
        "jobs",
        "strategies",
        "models",
        "scans",
        "watchlists",
        "plans",
        "reminders",
        "reviews",
    )

    @model_validator(mode="after")
    def validate_resource_names(self) -> "ApiSettings":
        """拒绝可能生成歧义或重复路由的资源名称。"""

        normalized = tuple(name.strip() for name in self.resource_names)
        if any(not name or "/" in name for name in normalized):
            raise ValueError("api.resource_names 必须是非空单段路径")
        if len(set(normalized)) != len(normalized):
            raise ValueError("api.resource_names 不能重复")
        object.__setattr__(self, "resource_names", normalized)
        return self


class CheckpointSettings(StrictSettingsModel):
    """LangGraph checkpoint 存储配置。

    Attributes:
        enabled: 是否启用 checkpoint 持久化。
        namespace: checkpoint 逻辑命名空间，用于隔离不同应用实例。
    """

    enabled: bool = True
    namespace: str = Field(default="trade-agent", min_length=1)


class HitlSettings(StrictSettingsModel):
    """人机交互在当前部署中的统一策略。

    Attributes:
        pending_ttl_seconds: 待处理交互的有效期，单位秒。
        text_field_max_length: HITL 长文本字段允许的最大字符数。
    """

    pending_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    text_field_max_length: int = Field(default=1_000, ge=1, le=100_000)


class ConversationRuntimeSettings(StrictSettingsModel):
    """通用会话运行时的部署级文案与失败策略。

    Attributes:
        unregistered_journey_message: 分类结果没有对应 Journey 插件时返回给用户的提示。
    """

    unregistered_journey_message: str = Field(
        default="当前请求没有已注册的业务旅程, 请补充信息或联系管理员配置能力。",
        min_length=1,
    )


class MarketSettings(StrictSettingsModel):
    """当前产品版本允许使用的证券市场分类快照。

    Attributes:
        market_code: 规范证券标识中的市场代码。
        exchange_codes: 当前部署认可的交易所代码目录。
        aliases: 用户或 provider 可用于指代当前市场的别名。
        symbol_pattern: HITL 表单校验证券代码时使用的正则表达式。
        empty_security_message: 证券输入为空时的用户提示。
        unsupported_market_message: 市场不在产品范围时的用户提示。
        ambiguous_security_message: 多个证券候选同时命中时的用户提示。
        security_not_found_message: 找不到可靠证券候选时的用户提示。

    Notes:
        首版产品仍只支持美股；集中维护交易所目录和代码格式，可以避免 Journey、
        解析器与前端表单各自持有一份容易漂移的列表。
    """

    market_code: Literal["US"] = "US"
    exchange_codes: tuple[str, ...] = ("NASDAQ", "NYSE", "AMEX")
    aliases: tuple[str, ...] = ("US", "USA")
    symbol_pattern: str = r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$"
    empty_security_message: str = Field(default="证券输入为空", min_length=1)
    unsupported_market_message: str = Field(default="首版仅支持美国交易所上市证券", min_length=1)
    ambiguous_security_message: str = Field(
        default="证券输入对应多个美国上市标的, 需要用户澄清", min_length=1
    )
    security_not_found_message: str = Field(default="未找到可可靠匹配的美国上市证券", min_length=1)

    @model_validator(mode="after")
    def validate_exchange_catalog(self) -> "MarketSettings":
        """规范化交易所代码，并拒绝空目录与重复项。"""

        normalized = tuple(code.strip().upper() for code in self.exchange_codes)
        if not normalized or any(not code for code in normalized):
            raise ValueError("market.exchange_codes 不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("market.exchange_codes 不能重复")
        object.__setattr__(self, "exchange_codes", normalized)
        aliases = tuple(alias.strip().upper() for alias in self.aliases)
        if not aliases or any(not alias for alias in aliases):
            raise ValueError("market.aliases 不能为空")
        if len(set(aliases)) != len(aliases):
            raise ValueError("market.aliases 不能重复")
        object.__setattr__(self, "aliases", aliases)
        return self


class LiteLLMRouteSettings(StrictSettingsModel):
    """单个逻辑模型路由的限额与 provider 约束。

    Attributes:
        endpoint: 主模型端点，必须显式声明 provider 与 model。
        allowed_providers: 当前逻辑路由允许使用的 provider 白名单。
        timeout_seconds: 单次模型调用超时，单位秒。
        max_tokens: 单次输出 token 上限。
        concurrency_limit: 该逻辑路由的并发上限。
        max_attempts: 每个物理端点允许的最大尝试次数。
        budget_usd: 可选美元预算；达到后停止继续调用。
        fallback_endpoints: 当前逻辑路由允许尝试的后备物理端点列表。
        fallback_routes: 预留的后备逻辑路由名称列表。
    """

    endpoint: ModelEndpoint
    allowed_providers: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_tokens: int = Field(default=2_048, ge=1)
    concurrency_limit: int = Field(default=4, ge=1)
    max_attempts: int = Field(default=2, ge=1, le=10)
    budget_usd: float | None = Field(default=None, gt=0)
    fallback_endpoints: tuple[ModelEndpoint, ...] = ()
    fallback_routes: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_endpoint_shape(cls, value: object) -> object:
        """在进入字段校验前拦截旧版字符串模型配置，给出明确迁移提示。"""

        if not isinstance(value, dict):
            return value
        if "model" in value and "endpoint" not in value:
            raise ValueError(
                "LiteLLM route 必须改用 endpoint.provider 与 endpoint.model；"
                "不再接受顶层 model 字符串"
            )
        if "fallback_models" in value and "fallback_endpoints" not in value:
            raise ValueError(
                "LiteLLM route 必须改用 fallback_endpoints；不再接受 fallback_models 字符串列表"
            )
        return value

    @model_validator(mode="after")
    def validate_allowed_providers(self) -> "LiteLLMRouteSettings":
        """规范 provider allowlist，并校验主端点与后备端点全部在白名单内。"""

        normalized = tuple(provider.strip() for provider in self.allowed_providers)
        if any(not provider for provider in normalized):
            raise ValueError("litellm.allowed_providers 不能包含空值")
        if len(set(normalized)) != len(normalized):
            raise ValueError("litellm.allowed_providers 不能重复")
        for endpoint in (self.endpoint, *self.fallback_endpoints):
            if endpoint.provider not in normalized:
                raise ValueError(f"LiteLLM endpoint provider '{endpoint.provider}' 未在 allowlist")
        object.__setattr__(self, "allowed_providers", normalized)
        return self


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
        worker_ids: 当前部署启用的后台 worker 类型目录。
        process_count: 进程数；当前版本固定为 1。
        lease_seconds: 作业 lease 时长，单位秒。
        job_max_attempts: 通用后台任务的最大尝试次数。
        scan_unit_max_attempts: 单证券扫描单元的最大尝试次数。
    """

    worker_id: str = Field(default="local-worker", min_length=1)
    worker_ids: tuple[str, ...] = ("scan-worker", "reminder-worker")
    process_count: int = Field(default=1, ge=1, le=1)
    lease_seconds: int = Field(default=60, ge=5, le=3_600)
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    scan_unit_max_attempts: int = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def validate_worker_ids(self) -> "WorkerSettings":
        """拒绝空白或重复的 worker 注册标识。"""

        normalized = tuple(worker_id.strip() for worker_id in self.worker_ids)
        if any(not worker_id for worker_id in normalized):
            raise ValueError("worker.worker_ids 不能包含空值")
        if len(set(normalized)) != len(normalized):
            raise ValueError("worker.worker_ids 不能重复")
        object.__setattr__(self, "worker_ids", normalized)
        return self


class ReminderSettings(StrictSettingsModel):
    """提醒通知交付与展示文案配置。

    Attributes:
        delivery_policy_version: 通知重试与模板策略版本。
        notification_template_id: Notification provider 模板标识。
        max_delivery_attempts: 单个 trigger 的最大投递次数。
        retry_delays_seconds: 每次重试前的退避秒数。
        unavailable_error: provider 未返回错误文本时保存的说明。
        trigger_message: 创建提醒触发时使用的用户可见文案。
        execution_disclaimer: 提醒结果必须展示的非成交声明。
    """

    delivery_policy_version: str = Field(default="reminder-delivery.v1", min_length=1)
    notification_template_id: str = Field(default="reminder.triggered.v1", min_length=1)
    max_delivery_attempts: int = Field(default=3, ge=1, le=10)
    retry_delays_seconds: tuple[float, ...] = (1.0, 5.0)
    unavailable_error: str = Field(default="notification unavailable", min_length=1)
    trigger_message: str = Field(
        default="提醒条件已满足: 这只是条件观察 / 不表示已下单或成交。",
        min_length=1,
    )
    execution_disclaimer: str = Field(
        default="提醒仅表示条件观察与通知 / 不表示下单或成交。", min_length=1
    )

    @model_validator(mode="after")
    def validate_retry_schedule(self) -> "ReminderSettings":
        """保证每次重试都有一个非负退避值。"""

        if len(self.retry_delays_seconds) != self.max_delivery_attempts - 1:
            raise ValueError("reminder.retry_delays_seconds 数量必须与重试预算一致")
        if any(delay < 0 for delay in self.retry_delays_seconds):
            raise ValueError("reminder.retry_delays_seconds 不能为负数")
        return self


class OidcRoleClaimSettings(StrictSettingsModel):
    """描述一个可提取角色或 scope 的 OIDC claim 路径。

    Attributes:
        path: 支持点号嵌套的 claim 路径，例如 `realm_access.roles`。
        separator: 字符串 claim 的分隔符；数组 claim 应为空。
    """

    path: str = Field(min_length=1)
    separator: str | None = None


class AuthenticationSettings(StrictSettingsModel):
    """认证模式与 OIDC 连接配置。

    Attributes:
        mode: 运行时认证模式，development 或 oidc。
        development_user_id: development 模式下注入的默认用户 ID。
        issuer: OIDC issuer 地址。
        audience: OIDC audience 标识。
        discovery_timeout_seconds: discovery 请求超时。
        jwks_timeout_seconds: JWKS 请求超时。
        jwks_cache_ttl_seconds: JWKS 集合缓存生命周期。
        subject_claim: 映射为 owner subject 的 claim 路径。
        role_claims: 按顺序合并的角色或 scope claim 规则。
        required_claims: JWT 本地校验必须存在的 claims。
        signing_algorithms: 显式签名算法白名单；为空时使用 discovery 声明。
    """

    mode: Literal["development", "oidc"] = "development"
    development_user_id: str | None = "local-user"
    issuer: HttpUrl | None = None
    audience: str | None = None
    discovery_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    jwks_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    jwks_cache_ttl_seconds: int = Field(default=300, ge=1, le=86_400)
    subject_claim: str = Field(default="sub", min_length=1)
    role_claims: tuple[OidcRoleClaimSettings, ...] = (
        OidcRoleClaimSettings(path="roles"),
        OidcRoleClaimSettings(path="scope", separator=" "),
    )
    required_claims: tuple[str, ...] = ("exp", "iss", "aud", "sub")
    signing_algorithms: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_oidc_contract(self) -> "AuthenticationSettings":
        """规范化 claim 与算法目录；顶层 subject 自动加入必需 claim。"""

        subject = self.subject_claim.strip()
        required = tuple(item.strip() for item in self.required_claims)
        algorithms = tuple(item.strip() for item in self.signing_algorithms)
        if any(not item for item in (*required, *algorithms)):
            raise ValueError("OIDC claim 与签名算法配置不能包含空值")
        if "." not in subject and subject not in required:
            required = (*required, subject)
        if len(set(required)) != len(required) or len(set(algorithms)) != len(algorithms):
            raise ValueError("OIDC claim 与签名算法配置不能重复")
        object.__setattr__(self, "subject_claim", subject)
        object.__setattr__(self, "required_claims", required)
        object.__setattr__(self, "signing_algorithms", algorithms)
        return self


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


class PlanningOperationSettings(StrictSettingsModel):
    """Planning 入口菜单中的单项操作配置。

    Attributes:
        operation_id: 前后端共享的稳定操作标识。
        label: 展示给用户的操作名称。
        description: 选项补充说明。
        enabled: 当前部署下该操作是否可继续执行。
        outcome: 选择后进入的流程类型，例如 ``plan_form`` 或 ``unsupported``。
        unsupported_kind: 不支持时返回的稳定问题编码。
        unsupported_message: 不支持时返回的提示文案。
    """

    operation_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    enabled: bool = True
    outcome: Literal["plan_form", "unsupported"]
    unsupported_kind: str | None = None
    unsupported_message: str | None = None

    @model_validator(mode="after")
    def validate_unsupported_policy(self) -> "PlanningOperationSettings":
        if self.outcome == "unsupported" and (
            not self.unsupported_kind or not self.unsupported_message
        ):
            raise ValueError(
                "unsupported planning 操作必须配置 unsupported_kind 和 unsupported_message"
            )
        return self


class PlanningFieldSettings(StrictSettingsModel):
    """Planning 字段目录中的单个字段定义。

    Attributes:
        key: 前后端共享的稳定字段标识。
        label: 面向用户展示的字段标题。
        data_type: Card 协议中的字段数据类型。
        control_type: 默认表单控件类型。
        required: 字段是否必填。
        read_only: 字段是否只读。
        min_length: 文本字段最小长度；``None`` 表示不声明。
        max_length: 文本字段最大长度；``None`` 表示由运行时策略补齐或不声明。
        plan_attribute: 当字段映射到 ``TradingPlan`` 属性时使用的属性名。
        source_fallback: 当计划中缺少字段来源时使用的默认来源文案。
        include_in_request_form: 是否出现在 journey 请求表单中。
        include_in_presenter_form: 是否出现在 presenter 生成的计划表单卡中。
        include_in_approval: 是否出现在审批 facts 中。
        approval_severity: 审批 facts 默认严重度。

    Invariants:
        - ``key`` 与 ``label`` 必须非空。
        - ``approval_severity`` 只能是低/中/高三个稳定等级。
    """

    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    data_type: Literal[
        "string", "integer", "number", "boolean", "date", "datetime", "symbol", "money"
    ] = "string"
    control_type: Literal["text", "textarea", "number", "select", "checkbox", "date"] = "textarea"
    required: bool = True
    read_only: bool = False
    min_length: int | None = Field(default=1, ge=0)
    max_length: int | None = Field(default=1_000, ge=1)
    plan_attribute: str | None = None
    source_fallback: str | None = None
    include_in_request_form: bool = False
    include_in_presenter_form: bool = False
    include_in_approval: bool = False
    approval_severity: Literal["low", "medium", "high"] = "medium"

    @model_validator(mode="after")
    def validate_lengths(self) -> "PlanningFieldSettings":
        """拒绝最小长度大于最大长度的字段配置。"""

        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError(f"planning 字段 {self.key} 的 min_length 不能大于 max_length")
        return self


class PlanningArtifactSectionSettings(StrictSettingsModel):
    """Trading plan artifact 中的一个展示章节定义。

    Attributes:
        title: 章节标题。
        kind: Card 协议允许的章节类型。
        field_keys: 按顺序拼接到章节内容中的字段键列表。
    """

    title: str = Field(min_length=1)
    kind: Literal["text", "summary", "analysis", "risk", "plan"] = "plan"
    field_keys: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_field_keys(self) -> "PlanningArtifactSectionSettings":
        """拒绝空白或重复字段键。"""

        normalized = tuple(field_key.strip() for field_key in self.field_keys)
        if any(not field_key for field_key in normalized):
            raise ValueError("planning artifact section 不能包含空字段键")
        if len(set(normalized)) != len(normalized):
            raise ValueError("planning artifact section 不能重复引用同一字段键")
        object.__setattr__(self, "field_keys", normalized)
        return self


class PlanningJourneySettings(StrictSettingsModel):
    """Planning 会话旅程的部署级入口配置。

    Attributes:
        choice_title: 入口 Choice Card 标题。
        choice_description: 入口 Choice Card 说明。
        choice_text_fallback: 纯文本降级文案。
        choice_field_title: 入口 Choice 表单字段标题。
        operations: 当前部署暴露的操作目录。
        direct_plan_unsupported_kind: 用户表达真实交易意图时的稳定问题编码。
        direct_plan_notice_message: 直达 planning 路径的边界提示。
        direct_plan_direction_template: 生成默认方向文案的模板，必须接受 ``symbol`` 占位符。
        request_form_default_direction: 从入口表单创建计划时的默认方向文案。
        form_title: 计划表单标题。
        form_description: 计划表单说明。
        form_text_fallback: 计划表单纯文本降级文案。
        unknown_operation_message: 入口选择无法映射到受支持流程时的提示文案。
        fields: Planning 字段目录与表单投影配置。
        artifact_sections: 计划 artifact 的章节布局定义。
        card_form_title_template: presenter 计划表单卡标题模板。
        card_form_description: presenter 计划表单卡说明。
        card_form_text_fallback_template: presenter 计划表单卡降级文案模板。
        approval_title: presenter 审批卡标题。
        approval_description: presenter 审批卡说明。
        approval_summary_template: presenter 审批摘要模板。
        approval_text_fallback_template: presenter 审批降级文案模板。
        artifact_title_template: presenter artifact 标题模板。
        artifact_summary_template: presenter artifact 摘要模板。
        artifact_text_fallback_template: presenter artifact 纯文本降级模板。
        artifact_status_labels: presenter artifact 状态文案映射。
        unsupported_title: presenter unsupported 卡标题。
        field_provenance_label: 字段来源说明标签。
        plan_provenance_label: 计划来源说明标签。
        evidence_provenance_label: 证据来源说明标签。
        evidence_provenance_value: 证据来源固定说明文案。
    """

    choice_title: str = "请选择要新增的内容"
    choice_description: str = "首版只支持创建美股交易计划, 不支持成交记录或真实下单。"
    choice_text_fallback: str = "请选择创建交易计划; 其他交易操作暂不支持。"
    choice_field_title: str = "操作类型"
    operations: tuple[PlanningOperationSettings, ...] = (
        PlanningOperationSettings(
            operation_id="create_trade_plan",
            label="创建交易计划",
            description="补充条件并在确认后激活计划。",
            enabled=True,
            outcome="plan_form",
        ),
        PlanningOperationSettings(
            operation_id="record_historical_trade",
            label="记录已发生的交易",
            description="首版暂不支持手工成交记录。",
            enabled=False,
            outcome="unsupported",
            unsupported_kind="record_historical_trade",
            unsupported_message="首版只支持创建交易计划, 不支持成交记录或真实下单。",
        ),
        PlanningOperationSettings(
            operation_id="execute_trade",
            label="执行真实交易",
            description="系统没有下单或账户能力。",
            enabled=False,
            outcome="unsupported",
            unsupported_kind="execute_trade",
            unsupported_message="首版只支持创建交易计划, 不支持成交记录或真实下单。",
        ),
    )
    direct_plan_unsupported_kind: str = "execute_trade"
    direct_plan_notice_message: str = "系统不能下单; 可以继续创建仅用于研究与决策的美股交易计划。"
    direct_plan_direction_template: str = "为 {symbol} 创建买入研究计划, 不执行下单"
    request_form_default_direction: str = "创建买入研究计划, 不执行下单"
    form_title: str = "补充美股交易计划"
    form_description: str = "系统不会下单。请一次补齐证券、周期、入场、失效、目标、仓位和风险。"
    form_text_fallback: str = "请补充完整的美股交易计划字段。"
    unknown_operation_message: str = "当前选择没有映射到受支持的 planning 流程。"
    fields: tuple[PlanningFieldSettings, ...] = (
        PlanningFieldSettings(
            key="security_id",
            label="美股证券",
            data_type="symbol",
            control_type="text",
            read_only=True,
            plan_attribute="security_id",
            source_fallback="规范证券解析",
            include_in_presenter_form=True,
            include_in_approval=True,
            approval_severity="low",
        ),
        PlanningFieldSettings(
            key="symbol",
            label="美股代码",
            control_type="text",
            min_length=1,
            max_length=16,
            include_in_request_form=True,
        ),
        PlanningFieldSettings(
            key="exchange",
            label="交易所",
            control_type="select",
            min_length=None,
            max_length=None,
            include_in_request_form=True,
        ),
        PlanningFieldSettings(
            key="direction",
            label="方向或逻辑",
            plan_attribute="direction",
            source_fallback="用户输入",
            include_in_request_form=True,
            include_in_presenter_form=True,
            include_in_approval=True,
        ),
        PlanningFieldSettings(
            key="horizon",
            label="计划周期",
            control_type="text",
            plan_attribute="horizon",
            include_in_request_form=True,
            include_in_presenter_form=True,
            include_in_approval=True,
        ),
        PlanningFieldSettings(
            key="entry_condition",
            label="入场条件",
            plan_attribute="entry_condition",
            include_in_request_form=True,
            include_in_presenter_form=True,
            include_in_approval=True,
        ),
        PlanningFieldSettings(
            key="invalidation_condition",
            label="失效或止损条件",
            plan_attribute="invalidation_condition",
            include_in_request_form=True,
            include_in_presenter_form=True,
            include_in_approval=True,
            approval_severity="high",
        ),
        PlanningFieldSettings(
            key="target",
            label="目标条件",
            plan_attribute="target",
            include_in_request_form=True,
            include_in_presenter_form=True,
            include_in_approval=True,
        ),
        PlanningFieldSettings(
            key="position_notes",
            label="仓位备注",
            plan_attribute="position_notes",
            include_in_request_form=True,
            include_in_presenter_form=True,
            include_in_approval=True,
        ),
        PlanningFieldSettings(
            key="risk_notes",
            label="风险说明",
            plan_attribute="risk_notes",
            include_in_request_form=True,
            include_in_presenter_form=True,
            include_in_approval=True,
            approval_severity="high",
        ),
    )
    artifact_sections: tuple[PlanningArtifactSectionSettings, ...] = (
        PlanningArtifactSectionSettings(
            title="方向、周期与入场",
            kind="plan",
            field_keys=("direction", "horizon", "entry_condition"),
        ),
        PlanningArtifactSectionSettings(
            title="失效、目标与仓位",
            kind="risk",
            field_keys=("invalidation_condition", "target", "position_notes"),
        ),
        PlanningArtifactSectionSettings(
            title="风险",
            kind="risk",
            field_keys=("risk_notes",),
        ),
    )
    card_form_title_template: str = "补充 {security_id} 交易计划"
    card_form_description: str = (
        "系统不会执行交易。请一次补齐计划周期、入场、失效、目标、仓位和风险字段;"
        "空白字段不会由模型猜测。"
    )
    card_form_text_fallback_template: str = "请补充交易计划; 当前缺失: {missing_fields}。"
    approval_title: str = "批准激活交易计划"
    approval_description: str = (
        "确认只会激活计划, 不会下单、查询余额或产生任何成交。"
        "选择 edit 会废弃当前卡片并创建新的草稿版本。"
    )
    approval_summary_template: str = "确认激活 {security_id} 计划 v{version}。"
    approval_text_fallback_template: str = "请确认是否激活 {security_id} 交易计划 v{version}。"
    artifact_title_template: str = "{security_id} 交易计划"
    artifact_summary_template: str = "计划 v{version} {status_text}。系统不提供交易执行能力。"
    artifact_text_fallback_template: str = (
        "{security_id} 交易计划 {status_text}; 不代表已执行交易。"
    )
    artifact_status_labels: dict[str, str] = {
        "active": "已激活",
        "triggered": "条件已触发, 但不表示已成交",
        "cancelled": "已取消",
        "expired": "已过期",
        "reviewed": "已复盘",
    }
    unsupported_title: str = "当前请求不受支持"
    field_provenance_label: str = "字段来源"
    plan_provenance_label: str = "计划来源"
    evidence_provenance_label: str = "证据"
    evidence_provenance_value: str = "计划引用的不可变 evidence"

    @model_validator(mode="after")
    def validate_direction_template(self) -> "PlanningJourneySettings":
        if not self.operations:
            raise ValueError("planning journey 至少需要一个操作定义")
        normalized_keys = tuple(field.key.strip() for field in self.fields)
        if not normalized_keys or any(not key for key in normalized_keys):
            raise ValueError("planning journey.fields 不能为空")
        if len(set(normalized_keys)) != len(normalized_keys):
            raise ValueError("planning journey.fields 不能重复")
        known = set(normalized_keys)
        for section in self.artifact_sections:
            unknown = set(section.field_keys) - known
            if unknown:
                field_list = ", ".join(sorted(unknown))
                raise ValueError(f"planning artifact section 引用了未知字段: {field_list}")
        try:
            self.direct_plan_direction_template.format(symbol="SYMBOL")
        except KeyError as exc:
            raise ValueError("direct_plan_direction_template 必须包含 {symbol} 占位符") from exc
        try:
            self.card_form_title_template.format(security_id="SECURITY_ID")
            self.card_form_text_fallback_template.format(missing_fields="MISSING_FIELDS")
            self.approval_summary_template.format(security_id="SECURITY_ID", version=1)
            self.approval_text_fallback_template.format(security_id="SECURITY_ID", version=1)
            self.artifact_title_template.format(security_id="SECURITY_ID")
            self.artifact_summary_template.format(version=1, status_text="STATUS")
            self.artifact_text_fallback_template.format(
                security_id="SECURITY_ID", status_text="STATUS"
            )
        except KeyError as exc:
            raise ValueError("planning presenter 模板缺少必须占位符") from exc
        return self


class SecurityClarificationSettings(StrictSettingsModel):
    """Research-to-plan 证券澄清步骤的部署配置。

    Attributes:
        option_title: 证券选择字段标题。
        title: 澄清卡片标题。
        description: 澄清卡片说明。
        text_fallback: 不支持结构化卡片时的降级文本。
        unsupported_kind: 无法解析证券时的稳定问题编码。
        unsupported_message: 无法解析证券时的用户提示。
        unsupported_source_type: unsupported Card 的来源类型。
    """

    option_title: str = Field(default="候选证券", min_length=1)
    title: str = Field(default="请选择具体美股证券", min_length=1)
    description: str = Field(default="同一代码对应多个美国上市标的, 需要先澄清。", min_length=1)
    text_fallback: str = Field(default="请选择具体美股证券。", min_length=1)
    unsupported_kind: str = Field(default="security_not_found", min_length=1)
    unsupported_message: str = Field(
        default="无法解析为受支持的美股证券, 请补充交易所与代码。", min_length=1
    )
    unsupported_source_type: str = Field(default="research_request", min_length=1)


class ScanReviewSettings(StrictSettingsModel):
    """Research-to-plan 扫描复核步骤的部署配置。

    Attributes:
        title: 扫描复核卡片标题。
        description: 人工确认前的说明。
        finding_label: 扫描结论标签。
        text_fallback: 不支持结构化卡片时的降级文本。
    """

    title: str = Field(default="请复核量化扫描结论", min_length=1)
    description: str = Field(
        default="确认后才会让 LLM 总结持久化结果并生成计划草稿。", min_length=1
    )
    finding_label: str = Field(default="扫描候选", min_length=1)
    text_fallback: str = Field(default="请复核量化扫描结论。", min_length=1)


class PlanApprovalPayloadSettings(StrictSettingsModel):
    """Research-to-plan 计划审批 payload 的投影配置。

    Attributes:
        payload_fields: 从计划审批 Card 转发到 HITL payload 的字段。
        include_text_fallback: 是否转发纯文本降级文案。
    """

    payload_fields: tuple[str, ...] = (
        "title",
        "description",
        "summary",
        "facts",
        "provenance",
    )
    include_text_fallback: bool = True

    @model_validator(mode="after")
    def validate_payload_fields(self) -> "PlanApprovalPayloadSettings":
        """规范字段目录，并拒绝空值与重复项。"""

        normalized = tuple(item.strip() for item in self.payload_fields)
        if not normalized or any(not item for item in normalized):
            raise ValueError("research_to_plan.plan_approval.payload_fields 不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("research_to_plan.plan_approval.payload_fields 不能重复")
        object.__setattr__(self, "payload_fields", normalized)
        return self


class ReminderApprovalSettings(StrictSettingsModel):
    """Research-to-plan 提醒审批步骤的部署配置。

    Attributes:
        title: 提醒审批卡片标题。
        description: 提醒审批说明。
        summary_template: 审批摘要模板，必须接受 ``plan_id``。
        plan_fact_label: 计划标识标签。
        channel_fact_label: 通知渠道标签。
        notification_channel: 激活提醒时传给通知 provider 的渠道标识。
        text_fallback: 不支持结构化卡片时的降级文本。
    """

    title: str = Field(default="批准启用计划复核提醒", min_length=1)
    description: str = Field(default="提醒只表示条件观察与通知, 不表示下单或成交。", min_length=1)
    summary_template: str = Field(default="为计划 {plan_id} 启用应用内定时复核提醒。", min_length=1)
    plan_fact_label: str = Field(default="计划", min_length=1)
    channel_fact_label: str = Field(default="渠道", min_length=1)
    notification_channel: str = Field(default="in_app", min_length=1)
    text_fallback: str = Field(default="请确认启用计划复核提醒。", min_length=1)

    @model_validator(mode="after")
    def validate_summary_template(self) -> "ReminderApprovalSettings":
        """验证提醒摘要模板包含所需占位符。"""

        try:
            self.summary_template.format(plan_id="PLAN_ID")
        except KeyError as exc:
            raise ValueError("reminder_approval.summary_template 必须包含 {plan_id}") from exc
        return self


class ReviewFeedbackDestinationSettings(StrictSettingsModel):
    """计划复盘允许写入的一个反馈去向。

    Attributes:
        value: 写入领域对象的稳定目标值。
        label: 展示给用户的目标名称。
    """

    value: str = Field(min_length=1)
    label: str = Field(min_length=1)


class PlanReviewSettings(StrictSettingsModel):
    """Research-to-plan 计划复盘步骤的部署配置。

    Attributes:
        title: 复盘卡片标题。
        description: 复盘说明。
        finding_label: 闭环状态标签。
        finding_detail: 闭环来源关系说明。
        text_fallback: 不支持结构化卡片时的降级文本。
        feedback_destinations: 可选择的反馈目标目录。
        resource_name: 保存复盘结果的资源集合名称。
    """

    title: str = Field(default="完成本次计划复盘", min_length=1)
    description: str = Field(
        default="复盘只写入未来策略草稿或训练数据, 不修改历史版本。", min_length=1
    )
    finding_label: str = Field(default="闭环状态", min_length=1)
    finding_detail: str = Field(default="研究、扫描、计划和提醒均已保留来源关系。", min_length=1)
    text_fallback: str = Field(default="请确认完成本次计划复盘。", min_length=1)
    feedback_destinations: tuple[ReviewFeedbackDestinationSettings, ...] = (
        ReviewFeedbackDestinationSettings(value="future_strategy_draft", label="未来策略草稿"),
        ReviewFeedbackDestinationSettings(value="future_training_data", label="未来训练数据"),
    )
    resource_name: str = Field(default="reviews", min_length=1)

    @model_validator(mode="after")
    def validate_feedback_destinations(self) -> "PlanReviewSettings":
        """拒绝空目录与重复反馈目标。"""

        values = tuple(item.value for item in self.feedback_destinations)
        if not values:
            raise ValueError("research_to_plan.plan_review.feedback_destinations 不能为空")
        if len(set(values)) != len(values):
            raise ValueError("research_to_plan.plan_review.feedback_destinations 不能重复")
        return self


class PlanLineageSettings(StrictSettingsModel):
    """Research-to-plan 创建计划时的来源策略。

    Attributes:
        source_type: 写入计划 lineage 的稳定来源类型。
    """

    source_type: str = Field(default="scan_result", min_length=1)


class ResearchToPlanJourneySettings(StrictSettingsModel):
    """Research-to-plan Journey 的全部部署级策略。

    Attributes:
        security_clarification: 证券澄清策略。
        scan_review: 扫描复核策略。
        plan_approval: 计划审批 payload 投影策略。
        reminder_approval: 提醒审批与通知渠道策略。
        plan_review: 复盘交互与资源目录策略。
        plan_lineage: 计划来源关系策略。
    """

    security_clarification: SecurityClarificationSettings = Field(
        default_factory=SecurityClarificationSettings
    )
    scan_review: ScanReviewSettings = Field(default_factory=ScanReviewSettings)
    plan_approval: PlanApprovalPayloadSettings = Field(default_factory=PlanApprovalPayloadSettings)
    reminder_approval: ReminderApprovalSettings = Field(default_factory=ReminderApprovalSettings)
    plan_review: PlanReviewSettings = Field(default_factory=PlanReviewSettings)
    plan_lineage: PlanLineageSettings = Field(default_factory=PlanLineageSettings)


class AgentToolPolicySettings(StrictSettingsModel):
    """定义部署级 Agent Tool 授权覆盖。

    Attributes:
        allowlists: Agent 协议 ID 到最终 Tool ID 白名单的映射；未出现的 Agent
            继续使用随 manifest 发布的默认声明。

    Invariants:
        - Agent ID 和 Tool ID 均必须为非空字符串。
        - 同一个 Agent 的 Tool ID 不允许重复。
        - Agent 是否已注册由 composition root 在获得最终 manifest 集合后校验。
    """

    allowlists: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_allowlists(self) -> "AgentToolPolicySettings":
        """规范化授权标识并拒绝空值或重复 Tool。"""

        normalized: dict[str, tuple[str, ...]] = {}
        for raw_agent_id, raw_tool_ids in self.allowlists.items():
            agent_id = raw_agent_id.strip()
            tool_ids = tuple(tool_id.strip() for tool_id in raw_tool_ids)
            if not agent_id or any(not tool_id for tool_id in tool_ids):
                raise ValueError("agent_tool_policy 不允许空 Agent ID 或 Tool ID")
            if len(set(tool_ids)) != len(tool_ids):
                raise ValueError(f"Agent Tool allowlist 存在重复项: {agent_id}")
            normalized[agent_id] = tool_ids
        object.__setattr__(self, "allowlists", normalized)
        return self


class AppSettings(BaseSettings):
    """整个进程唯一的配置入口.

    嵌套项使用双下划线, 例如
    ``TRADE_AGENT_DATABASE__PATH=/var/lib/trade-agent/app.db``.

    Attributes:
        environment: 当前运行环境。
        api: HTTP 进程与资源端点配置。
        database: 数据库配置。
        checkpoint: checkpoint 配置。
        conversation_runtime: 通用会话运行时文案与失败策略。
        hitl: 人机交互的有效期与字段策略。
        market: 当前版本允许的市场与交易所分类快照。
        litellm: LiteLLM 路由配置集合。
        quantitative_model: 专用量化模型 runtime 配置。
        planning_journey: planning 会话旅程的部署级入口配置。
        research_to_plan_journey: research-to-plan 会话旅程的部署级策略。
        agent_tool_policy: 部署级 Agent Tool 授权覆盖。
        worker: 后台 worker 配置。
        reminder: 提醒交付策略与展示文案。
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
    api: ApiSettings = Field(default_factory=ApiSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    checkpoint: CheckpointSettings = Field(default_factory=CheckpointSettings)
    conversation_runtime: ConversationRuntimeSettings = Field(
        default_factory=ConversationRuntimeSettings
    )
    hitl: HitlSettings = Field(default_factory=HitlSettings)
    market: MarketSettings = Field(default_factory=MarketSettings)
    litellm: LiteLLMSettings = Field(default_factory=LiteLLMSettings)
    quantitative_model: QuantitativeModelSettings = Field(default_factory=QuantitativeModelSettings)
    planning_journey: PlanningJourneySettings = Field(default_factory=PlanningJourneySettings)
    research_to_plan_journey: ResearchToPlanJourneySettings = Field(
        default_factory=ResearchToPlanJourneySettings
    )
    agent_tool_policy: AgentToolPolicySettings = Field(default_factory=AgentToolPolicySettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    reminder: ReminderSettings = Field(default_factory=ReminderSettings)
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
