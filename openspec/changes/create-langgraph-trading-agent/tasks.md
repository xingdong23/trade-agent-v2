## 1. 第一阶段：工程骨架与架构确认门禁

- [x] 1.1 建立 `src/trade_agent` package、`tests` 目录，以及 `core`、`agents`、`capabilities` 三个核心模块和 `adapters`、`apps` 两个边界层；为每个 capability 建立 `domain`、`application`、`ports`、`tools`、`cards` 统一结构，并建立最小 `web/src/features/chat` Vue/TypeScript 工程骨架，仅创建模块、公开类型占位和说明，不实现业务逻辑
- [x] 1.2 增加架构依赖测试，强制 `core` 不反向依赖其他模块、`agents` 只依赖 `core`、tool handler 只调用所属 capability application、capability card presenter 只依赖 `core/presentation` 与本 capability 公开 model、adapter 只实现 port，并禁止 Agent 直连 repository/provider、tool-to-tool 调用和跨 capability 内部导入
- [x] 1.3 配置最小 Python/Web 项目 metadata、依赖锁定、类型检查、lint、格式化与测试命令，使空骨架可以安装、导入和执行架构测试；第一阶段不接入真实外部服务
- [x] 1.4 定义 `core` 公开 contract 骨架，包括 runtime state、`LLMClient`、ToolGateway、HITL、event、presentation 和 security 接口；定义各 capability 的 command/query/port/tool/card 公共边界，只提供类型签名、fake 或明确抛出 `NotImplementedError` 的 stub
- [x] 1.5 定义 supervisor、Research、Strategy、Planning manifest 和最小 LangGraph 拓扑，并用 fake LLM/tool 证明 graph 可编译；定义 API、CLI、worker 与 Web 空入口及 composition root，使依赖装配可检查但不执行业务 workflow
- [x] 1.6 建立 `adapters/llm/litellm`、`adapters/sqlite`、`adapters/market_providers`、`adapters/model_runtime` 等 adapter 骨架；LiteLLM 仅定义配置映射与 `LLMClient` 实现边界，第一阶段禁止真实网络调用
- [x] 1.7 输出全中文架构评审材料，包含模块树、依赖方向、公开接口清单、composition root 装配关系、最小 graph 拓扑、stub/fake 清单和第二阶段 vertical slice 建议；运行 import、typecheck 和架构测试后停止实施，等待用户明确确认

> **实施门禁：** 任务 1.1-1.7 是第一次 `/opsx:apply` 的唯一范围。以下第二阶段任务必须保持未开始，直到用户明确确认实现架构；确认前不得因依赖顺序或自动执行继续实施。

## 2. 第二阶段（架构确认后）：领域模型与持久化基础

- [x] 2.1 在 `core` 定义 `UserContext`、`HumanInteraction`、run event 与 audit primitive，并将规范证券、evidence、strategy/version、watchlist/universe snapshot、model version、prediction、scan、plan、reminder 和 review 分配到各自 capability/domain
- [x] 2.2 建立 SQLite schema 与 migration，加入 owner scope、不可变版本、乐观锁、外键、唯一约束、时间戳和 JSON schema/version 字段
- [x] 2.3 实现 repository interface、SQLAlchemy adapter 和 unit test fake，并用 integration test 证明所有用户资源查询与 command 强制执行 owner 隔离
- [x] 2.4 实现 command idempotency store、payload hash、action result 复用和并发冲突处理，并覆盖“已提交副作用但 checkpoint 尚未推进”的恢复测试
- [x] 2.5 实现 append-only audit 与 outbox/run-event 存储，支持每个 run 内单调递增 sequence、event cursor 重放和事务内写入
- [x] 2.6 实现 SQLite 短事务与串行写入协调、原子 job claim、lock wait 指标、在线 backup、启动完整性检查和 restore smoke test
- [x] 2.7 在 `core/presentation` 定义版本化 `CardEnvelope`、`CardCatalog`、source reference、单调 revision、语义 action、payload hash、过期时间和 text fallback schema，并以 schema/registry test 拒绝未知 kind、版本、字段、组件名、HTML、脚本和任意跳转地址
- [x] 2.8 实现类型化配置加载与启动校验，覆盖 SQLite database path、checkpoint、LiteLLM route/model/provider、量化 model、worker、认证和 observability 配置，并确保 production mode 缺少关键配置时 fail closed
- [x] 2.9 提供 SQLite 初始化与 migration 命令，配置 WAL、foreign key、`busy_timeout`、数据库目录权限和应用/worker 健康检查，验证空仓库可以一条命令完成初始化

## 3. Provider 与证据可信链路

- [x] 3.1 定义仅面向美国交易所上市证券的证券查询、quote、K-line、公司行动、SEC/基本面、新闻/搜索和 notification provider port，以及统一错误、时效、授权和重试契约
- [x] 3.2 实现可重复的 fake provider 与 provider contract test kit，覆盖正常、歧义、过期、冲突、限流、超时和不可用响应
- [x] 3.3 实现美股规范证券解析 service，记录美国市场、交易所、symbol 和 display name，并覆盖唯一匹配、歧义、无结果、非美股 `unsupported_market` 及跨用户输入场景
- [x] 3.4 实现不可变 evidence snapshot、raw payload hash、source reference、observed/published/retrieved time、freshness 和 entitlement metadata
- [x] 3.5 实现 freshness/trust policy、provider 冲突检测与 citation validator，确保缺失或冲突数据不会被 LLM 生成内容替代

## 4. 时间点一致的数据与 Feature Pipeline

- [x] 4.1 定义美股 market scope、target 与预测周期的可配置 contract，并为美国交易日历、复权规则、label 可用时间和数据发布时间建立显式 schema
- [x] 4.2 实现 point-in-time dataset builder，冻结 data snapshot 并验证任何样本均不读取决策时点之后才可获得的数据
- [x] 4.3 建立版本化 feature definition registry，首批覆盖价量、收益、波动率、趋势、流动性及可用基本面 feature，并记录 lineage
- [x] 4.4 实现训练与 inference 共用的 feature 计算路径及 parity test，覆盖缺失值、停牌、公司行动、交易日错位和错误复权
- [x] 4.5 实现数据质量与泄漏检查，包括未来数据、重复样本、异常值、survivorship bias 风险和 train/test 时间重叠，并在违规时阻止下游任务

## 5. 专用量化 Model 训练、评测与注册

- [x] 5.1 实现可复现训练 job contract，记录算法、hyperparameter、随机种子、代码版本、data snapshot、feature set、target、时间区间和 artifact hash
- [x] 5.2 实现确定性规则与简单统计 benchmark，作为所有候选 model 必须比较的最低基线
- [x] 5.3 实现 LightGBM 首版训练 pipeline、概率校准、feature attribution 和 model artifact 序列化，并通过固定数据集重现测试
- [x] 5.4 实现按时间顺序的 train/validation/test、purge/embargo 与 walk-forward evaluation，输出 target 对应指标、稳定性、换手率及交易成本后 benchmark 对比
- [x] 5.5 实现 model registry、候选/批准/停用状态和审批 command，确保未达门槛或未经批准的 model version 无法提供生产 inference
- [x] 5.6 实现可选 LSTM 候选训练 adapter 与相同评测协议，只有在质量、校准、稳定性、成本后指标和延迟均超过 LightGBM 基线门槛时才允许提请发布
- [x] 5.7 实现 prediction schema 与 batch inference service，输出 target、horizon、概率/分位数或区间、校准信息、适用范围、`model_version`、`feature_snapshot_id` 和 `as_of`
- [x] 5.8 实现 out-of-distribution、feature 缺失和适用范围检查；不满足条件时返回 `unavailable`，且测试系统不会调用 LLM 补预测值

## 6. Watchlist 与版本化策略

- [x] 6.1 实现 watchlist、group、membership 和 import command，支持逐行校验结果、规范化去重、metadata 合并及 provenance 保留
- [x] 6.2 实现批量导入审批 payload 与幂等持久化，覆盖用户修改、拒绝、重复提交和并发导入测试
- [x] 6.3 实现 AI 分类 suggestion 与用户接受/编辑流程，确保 suggestion 在批准前不改变 membership
- [x] 6.4 实现不可变 universe snapshot，并测试扫描提交后的 watchlist 修改不会改变既有候选集合
- [x] 6.5 实现 strategy draft、结构化条件、target、horizon、ranking policy、正反例和 immutable strategy version repository
- [x] 6.6 实现策略发布审批与版本保留，确保既有 scan 永远引用原始 strategy version
- [x] 6.7 在 watchlist 与 strategy capability 内分别实现薄 `tools/` adapter，声明输入输出 schema、side effect、risk、HITL 和 idempotency metadata，并只调用对应 application command/query
- [x] 6.8 在 watchlist 与 strategy capability 的 `cards/` 实现批量导入、AI 分类建议和策略发布所需的 Form、Review、Approval presenter，确保逐项校验结果、来源、目标版本和差异在确认前可检查，拒绝或取消不会改变领域状态

## 7. 量化扫描与生产 Model 监控

- [x] 7.1 实现 scan submission validation，冻结 strategy version、universe、data/feature snapshot、model version、ranking function 和全部配置
- [x] 7.2 实现数据库 lease 驱动的 scan job/unit 状态机、worker claim、单证券幂等评估、有界重试、进度聚合与重启恢复
- [x] 7.3 实现确定性适用性、流动性、数据可用性和策略硬规则检查，再调用已批准专用 model 进行 batch inference
- [x] 7.4 实现版本化 ranking function 和结构化 scan result，保存量化 score/probability、条件、排除项、evidence、feature/model lineage、风险和缺口
- [x] 7.5 实现扫描取消与部分结果保留，并覆盖 worker 重启、重复 claim、终态幂等和失败单元可检查性测试
- [x] 7.6 实现 data quality、feature drift、prediction drift、calibration、coverage、latency 和标签回流表现监控，以及停止 model 或降级到已批准基线的 policy
- [x] 7.7 增加 LLM 隔离测试，证明 LLM 只能总结持久化 scan result，无法生成、覆盖或影响预测、score 和排序
- [x] 7.8 在 quantitative capability 内实现 `get_prediction`、`get_quantitative_snapshot`、`submit_scan`、`get_scan_status` 和 `list_scan_results` 薄 `tools/` adapter；只读单股工具加入 Research Agent 白名单，批量扫描工具加入 Strategy Agent 白名单，并验证 handler 不包含 feature、inference 或 ranking 业务逻辑
- [x] 7.9 在 quantitative capability 的 `cards/` 实现 quantitative snapshot、scan result 和 scan progress 的确定性 presenter，验证其只投影持久化 model/job 数据、保留 lineage 与缺口，且不调用 LLM 生成分数、排序或 UI schema

## 8. 市场研究服务

- [x] 8.1 实现证券 research request 与 evidence assembly service，覆盖价量、技术关键位、基本面、催化因素、风险、假设、缺口和失效条件
- [x] 8.2 实现结构化 research artifact 与 claim-to-evidence 校验，未知 citation、无来源数值或 provider 不可用时必须降级或失败
- [x] 8.3 实现行业/主题/产业链 research service，输出角色、候选证券、source、护城河假设、风险及待确认的 watchlist import proposal
- [x] 8.4 实现研究安全 validator，阻止收益承诺、broker/成交声明和使用 model 内容填补缺失 provider 数据
- [x] 8.5 在 market_research capability 内实现证券解析、证券研究和产业链研究的只读 `tools/` adapter，并通过 capability application service 返回结构化 artifact
- [x] 8.6 在 market_research capability 的 `cards/` 实现 research Artifact Card、research Progress Card、data gap 与 failure Notice Card presenter，并验证 citation、时效和缺口不会在投影时丢失

## 9. LangGraph 会话运行时

- [x] 9.1 定义最小类型化 supervisor state、intent schema、artifact reference、`pending_interaction_id`、error summary 和 event cursor，确保大型 evidence 与 HITL payload 不写入 checkpoint
- [x] 9.2 在 `core/tools` 实现 `ToolProtocol`、registry、gateway、policy、schema validation 和统一错误映射，并证明 Agent 只能调用 manifest 白名单中的 tool id
- [x] 9.3 实现 `ingest`、`classify`、`resolve_context`、`route`、`policy_gate`、`execute_command` 和 `render` node 及显式 conditional edge
- [x] 9.4 在 `agents` 定义 supervisor 以及 research、strategy、planning 三类业务子智能体 manifest/prompt/subgraph，使其只依赖 `core` contract 和注入的 ToolGateway；增加架构测试证明不存在 Quantitative Agent，量化能力只能经 tool 调用
- [x] 9.5 集成 SQLite LangGraph checkpointer，强制 thread owner scope，并测试中断、进程重启、恢复、写锁重试和跨用户拒绝
- [x] 9.6 实现 HITL repository 与状态机，覆盖 `clarification`、`approval`、`review`、`correction`、`exception_resolution` 及 `pending`、`resolved`、`expired`、`cancelled` 状态
- [x] 9.7 实现 HITL service 与 server-side action policy，持久化 owner、thread/run、subject version、payload/hash、response schema、deadline、actor 和 resolution，并通过 compare-and-set 保证单次解决
- [x] 9.8 将 HITL service 接入 LangGraph interrupt/resume，覆盖歧义澄清、人工复核、审批、用户修订、跨客户端响应、过期不自动批准和取消流程
- [x] 9.9 实现 node timeout、retry budget、terminal error、command result 复用和 provider failure 映射，验证 checkpoint replay 不产生重复副作用
- [x] 9.10 实现供应商无关的 `LLMClient`、`LLMRequest`、`LLMResponse`、`ModelRoute`、usage 和统一错误，并在 `adapters/llm/litellm` 使用异步 LiteLLM SDK 支持逻辑 route 映射、流式/非流式响应、结构化输出、取消和 provider request metadata
- [x] 9.11 实现 LiteLLM timeout、并发限制、token/成本预算、有界 retry 与显式 fallback policy，确保认证错误、无效请求、上下文超限和未批准 provider 不被盲目重试或静默切换
- [x] 9.12 实现 prompt version 与 tool/schema 校验后的有界修复，并确保模型输出始终经过本地 schema、citation、ToolGateway、policy 和 capability validator，而不是进入自由循环
- [x] 9.13 实现 FakeLLMClient 与 LiteLLM adapter contract test，覆盖 route、chunk 合并、JSON schema、usage、retry/fallback、错误归一化和日志脱敏；验证 Agent 不导入厂商 SDK，LiteLLM 不直接执行 tool，量化 prediction 不经过 LiteLLM
- [x] 9.14 实现 HITL Form、Choice、Approval、Review 和 Correction Card presenter，使 field、constraint、provenance、field error 和可用 action 只来自 response schema、服务端 policy 与当前 interaction 状态
- [x] 9.15 实现从 interaction、artifact 和 job source version 生成稳定 `card_id` 与单调 revision 的 card projection service，覆盖 created、updated、resolved、superseded、expired、cancelled 和 failed 状态转换及未知客户端版本的安全 text fallback

## 10. 交易计划、提醒与复盘

- [x] 10.1 实现与 research/scan evidence 关联的 plan draft command，保留缺失的风险关键字段且禁止 model 猜测
- [x] 10.2 实现 plan 状态机与激活审批，覆盖 `draft`、`active`、`triggered`、`cancelled`、`expired`、`reviewed` 的合法和非法迁移
- [x] 10.3 实现 price threshold、scheduled review 和 invalidation reminder rule，以及需要审批的启用/停用 command
- [x] 10.4 实现定时 reminder worker、crossing、cooldown、trigger event 去重和 notification delivery 重试，确保提醒触发不表示成交
- [x] 10.5 实现 plan/scan review 与 strategy/model/evidence lineage 关联，使反馈只能进入新策略草稿或未来训练数据，不修改历史版本
- [x] 10.6 增加结构性安全测试，证明 repository、command catalog、graph tool 和 API 中不存在 order、fill、balance 或 broker sync 能力
- [x] 10.7 在 planning 与 reminder capability 内实现 plan draft、plan transition、reminder 和 review 的薄 `tools/` adapter，所有受控写操作携带 HITL 与幂等 metadata
- [x] 10.8 在 planning 与 reminder capability 的 `cards/` 实现 trade plan、reminder Artifact Card 及 unsupported Notice Card presenter，并实现 Choice -> 合并字段 Form -> Approval -> Artifact 的渐进式计划交互，edit 必须 supersede 旧卡片并创建新 draft/version

## 11. API、CLI、事件流与可观测性

- [x] 11.1 在 `apps` composition root 装配 core、agent manifest、capability toolset、SQLite/provider adapter 与 worker，并验证替换 fake adapter 不影响 agents/capabilities
- [x] 11.2 实现 FastAPI 认证与 `UserContext` 注入，以及 conversation/run、HITL pending/query/cancel、card/artifact、strategy、model、scan、watchlist、plan、reminder 和 review endpoint
- [x] 11.3 实现基于持久化 run event 的 SSE endpoint，发送 `card.created`、`card.updated`、`card.resolved`、`card.superseded` 和 `card.failed` 生命周期事件，并通过 cursor、run sequence、`card_id` 与 revision contract test 验证重连、顺序保证和去重
- [x] 11.4 实现使用同一 application layer 的 CLI，支持会话、流式进度、结构化 artifact、HITL 待办查询/响应/取消、scan status 和失败恢复
- [x] 11.5 实现结构化日志与本地 trace，记录 correlation id、graph/node、command、provider、quant model、LLM/prompt、token、latency、retry、approval 和 outcome
- [x] 11.6 实现统一 redaction layer 与 secret/sensitive-field test，并确保第三方 observability export 只接收脱敏数据
- [x] 11.7 实现 `POST /api/hitl/{interaction_id}/responses`，校验 owner、interaction 状态/version、subject version、payload hash、field schema 和 idempotency key；返回字段级错误，并证明重复点击、旧 revision 和并发响应不会重复执行 command
- [x] 11.8 实现最小 Vue/TypeScript Web chat shell、消息与卡片时间线、白名单 Card registry、Interaction/Artifact/Progress/Notice renderer、未知 `kind + schema_version` fallback，以及基于 pending HITL、artifact/job query 和 SSE cursor 的刷新恢复
- [x] 11.9 实现 Web HITL response client 与表单状态，覆盖键盘提交与取消、焦点保存/恢复、label 和字段错误关联、提交中禁用、重复点击保护、响应冲突刷新以及窄屏移动端无重叠布局

## 12. 端到端验收与交付门禁

- [x] 12.1 完成默认 unit、类型检查、lint、格式化和架构检查，并将命令写入项目开发入口
- [x] 12.2 完成 SQLite repository、checkpointer、单 worker、migration、WAL、lock contention、backup/restore 和并发 idempotency integration test
- [x] 12.3 完成量化数据泄漏、feature parity、LightGBM 可复现、walk-forward 门禁、model registry、inference lineage、drift 和 LLM 隔离测试
- [x] 12.4 完成 API/CLI/Web contract test，以及“美股歧义 HITL 澄清 -> 有 citation 的研究 -> 冻结 watchlist -> 量化扫描 -> Progress/Review Card -> LLM 总结 -> 审批计划/提醒 -> Artifact Card -> 复盘”的端到端测试
- [x] 12.5 完成 thread/资源跨用户拒绝、HITL payload 防篡改/重复响应/超时、日志脱敏、非美股拒绝、broker 执行拒绝和 provider 故障降级安全测试
- [x] 12.6 编写全中文运行与运维文档，说明 SQLite 初始化/backup/restore、本地启动、migration、HITL 待办处理、训练/评测、model 发布、单 worker、美股 provider 配置、故障恢复和已知非目标
- [x] 12.7 运行 `openspec validate`、完整非 live test suite 和 migration smoke test，记录验证证据及尚未覆盖的 live provider 风险
- [x] 12.8 完成“新增一个交易”Choice Card 澄清与不支持路径，以及“我要买 NVDA”不能下单提示 -> 合并字段 Form Card -> 字段错误 -> Approval Card edit/supersede -> 幂等 confirm -> TradingPlan Artifact Card 的端到端测试，并覆盖刷新恢复、重复响应、键盘操作和移动端布局

## 13. Agent 中台解耦与课程注释门禁

- [x] 13.1 将自然语言分类抽象为可替换 `IntentClassifier`，运行时只消费结构化 intent、journey ID 与实体，不包含固定业务短语判断
- [x] 13.2 将启动、HITL subject 和恢复处理统一封装为可注册 `ConversationJourney` 插件，并把 Planning/Research-to-plan 业务编排移出通用会话运行时
- [x] 13.3 使用类型化 Tool 执行异常替代异常消息关键词解析，确保中台控制流只依赖协议、枚举、类型和注册表
- [x] 13.4 按中文 Docstring 规范补齐公共模型实体与 Protocol，并增加全仓 AST 架构门禁
- [x] 13.5 运行格式化、lint、mypy、全量测试、硬编码扫描和 OpenSpec strict validation

## 14. 全仓硬编码审计与部署配置收敛

- [x] 14.1 将量化 task 类型、objective/metric、label schema、runtime 参数、评测协议和 model lineage 改为显式输入，删除伪造的 strategy/model 来源
- [x] 14.2 将 OIDC claim/JWKS、worker registry/lease/retry、reminder delivery、research assembly/conflict policy 和 Agent Tool allowlist 收敛到类型化配置或可注入 registry
- [x] 14.3 将 Planning 与 Research-to-plan 的操作目录、字段 schema、presenter 文案、审批 payload、提醒渠道、复盘目录和 lineage 策略接入唯一 `AppSettings` 配置源
- [x] 14.4 强制 composition root 显式注入 checkpoint namespace、未注册 Journey 提示和共享 Planning presenter，禁止生产路径使用实现层兼容默认
- [x] 14.5 实现 owner 隔离的 conversation snapshot endpoint、动态 thread ID、统一 `VITE_API_BASE_URL` 和协议 family renderer，删除固定 thread 与猜测式恢复 URL
- [x] 14.6 二次扫描固定证券、用户、租户、模型、策略、地址、自然语言控制流和伪造 lineage；仅保留集中管理的协议常量、产品边界与测试 fixture
- [x] 14.7 运行 Python/Web 全量格式化、lint、mypy、测试、build、OpenSpec strict validation 和 migration smoke test，并记录 live provider 未覆盖风险
