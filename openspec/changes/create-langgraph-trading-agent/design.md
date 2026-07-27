## 背景

`trade-agent-v2` 当前只有示例 `main.py` 和 OpenSpec 配置。同级项目 `/Users/xingdong/workspace/trade-agent` 提供业务参考：一个 Chat-first 的研究与计划工作台，覆盖证券研究、产业链发现、策略定义、watchlist、扫描、交易计划、提醒和复盘。该项目也建立了不可妥协的安全规则：事实必须有来源、明确表达不确定性、不承诺收益，并且在接入真实 broker connector 前不得宣称执行交易。

本次变更重建上述核心闭环，不迁移参考项目的自定义 turn loop。LangGraph 负责对话编排与恢复，确定性的领域 service 负责业务约束与持久化。首个交付版本仅支持美股，使用 SQLite 进行单机持久化，并提供后端、CLI、API 以及只覆盖 chat shell、版本化卡片渲染和 HITL 响应的最小 Web 客户端；完整交易工作台与多市场支持留待后续变更。

主要利益相关者包括使用研究与计划 workflow 的主动交易者、扩展数据或 model provider 的开发者，以及诊断失败或高成本 run 的运维人员。

## 目标与非目标

**目标：**

- 交付从对话请求到有来源研究、策略扫描以及经过确认的交易计划或提醒的端到端路径。
- 确保每个长耗时或有副作用的操作均可恢复、幂等、用户隔离、可观察，并能在不调用真实 LLM 或市场数据服务的情况下测试。
- 在 provider evidence、model 输出、strategy version、scan input、交易计划、提醒和复盘之间保留精确来源关系。
- 保持 LangGraph node 足够轻量，使业务行为能够同时被对话入口和直接 API/CLI 入口复用。
- 从系统结构上确保不存在 broker 执行能力，而非只依靠 prompt 约束。
- 从系统结构上隔离 LLM 与量化预测：LLM 只负责意图、总结、解释和计划草稿，专用量化 model 负责数值预测、扫描评分与排序输入。
- 将歧义澄清、审批、人工复核和修订统一为可持久化、可恢复、可审计的 HITL 交互，而不是 graph 内的临时回调。
- 通过版本化 Card Protocol 和白名单 CardCatalog，让 Web chat 能恢复并渲染 HITL 表单、审批、领域 artifact 和后台进度，而不接受 LLM 生成的任意 UI。

**非目标：**

- 自动或手动 broker 下单、账户同步、投资组合核算或任何成交声明。
- 完整 Web 工作台、iOS 或管理端客户端，复杂图表编辑以及社区策略分享；首版 Web 只提供 chat shell 与卡片交互。
- 完整回测、参数优化、投资组合构建或策略自主变更。
- 复制参考仓库的旧 runtime、兼容别名、provider 实现或数据库表。
- 使用 LLM 直接预测股价、收益、涨跌概率，或让 LLM 覆盖专用量化 model 的结果。
- 中国内地、香港及其他非美国交易所市场，以及首版的分布式或多节点部署。

## 架构决策

### 1. 采用具有 port 与 adapter 边界的模块化单体

项目使用 `src/trade_agent` package，并将代码所有权收敛为三个核心模块和两个边界层：

```text
src/trade_agent/
├── core/                         # 与交易业务无关的 Agent 框架
│   ├── runtime/                  # LangGraph state、graph、checkpoint contract
│   ├── hitl/                     # HumanInteraction、policy、interrupt/resume
│   ├── llm/                      # LLM port、结构化输出、预算
│   ├── tools/                    # ToolProtocol、Registry、Gateway、Policy
│   ├── presentation/             # CardEnvelope、CardCatalog、wire schema
│   ├── events/                   # run event、SSE contract、audit primitive
│   └── security/                 # UserContext、scope、redaction contract
├── agents/                       # supervisor 与子智能体定义
│   ├── supervisor/
│   ├── research/
│   ├── strategy/
│   └── planning/
├── capabilities/                 # 确定性业务能力，按 vertical slice 组织
│   ├── market_research/
│   ├── quantitative/
│   ├── watchlist/
│   ├── strategy/
│   ├── planning/
│   └── reminder/
├── adapters/                     # 外部实现
│   ├── sqlite/
│   ├── llm/
│   │   └── litellm/              # LiteLLM SDK adapter、路由与错误映射
│   ├── market_providers/
│   ├── model_runtime/
│   ├── notifications/
│   └── observability/
└── apps/                         # composition root 与进程入口
    ├── api/
    ├── cli/
    └── worker/

web/                              # 最小 Vue/TypeScript chat client
└── src/features/chat/
    ├── cards/                    # 白名单组件与 registry
    ├── hitl/                     # form state 与 response client
    └── events/                   # SSE reducer 与 reconnect
```

每个 capability 内部使用统一结构：`domain/` 保存 entity、value object 和状态机，`application/` 保存 command/query service，`ports/` 声明 repository/provider port，`tools/` 保存面向 Agent 的薄 adapter，`cards/` 保存领域对象或 HITL payload 到 CardSpec 的确定性 presenter。Tool 不是业务能力本身：一个 capability 可以向 Agent 暴露多个 tool，也可以被 API、CLI、worker 直接调用而不经过 tool。

依赖方向必须由架构测试强制执行：

```text
apps ────────────────> core / agents / capabilities / adapters
agents ──────────────> core
capabilities/*/tools -> core/tools + 本 capability/application
capabilities/*/cards -> core/presentation + 本 capability 的公开 model
capabilities/application -> 本 capability/domain + ports + 必需的 core contract
adapters ────────────> core port 或 capabilities/*/ports
core ────────────────> 不依赖 agents、capabilities、adapters、apps
```

Agent 不得直接导入 capability、repository 或 provider，而是通过注入的 `ToolGateway` 和 tool id 调用。Tool handler 只负责 schema 校验、policy/HITL metadata、调用一个 application command/query 及结果映射；不得包含业务规则、直接访问 SQLite/provider、调用其他 tool，或绕过 capability 状态机。跨 capability 协作通过公开 application port 或由 composition root 编排，不共享内部 repository。

该设计在保持初始部署和事务简单的同时，避免顶层 `tools/` 演变成混合业务逻辑、数据库、provider 和后台任务的超大目录，并建立可在未来拆分 worker 或 provider adapter 的边界。暂不采用微服务，因为目前没有明确负载画像，在领域契约形成前引入分布式一致性只会增加复杂度。不复制参考仓库的自定义 Agent runtime，因为用户明确要求 LangGraph，且 LangGraph 原生提供 checkpoint 与 interrupt 语义。

### 2. 使用一个对话 supervisor graph 与多个有边界的 subgraph

顶层 graph state 只保存对话与编排数据：`user_id`、`thread_id`、`run_id`、经过校验的 message、已选择 intent、规范实体 reference、`pending_interaction_id`、artifact reference、错误摘要和 event cursor。大型 evidence payload、HITL payload 与领域对象存放在 repository 中，graph state 仅引用其标识符。

supervisor graph 使用以下流程：

```text
ingest -> classify -> resolve_context -> route
                                      |-> research subgraph
                                      |-> strategy command subgraph
                                      |-> watchlist command subgraph
                                      |-> scan submission/status subgraph
                                      |-> planning/reminder subgraph
                                      `-> clarification
subgraph -> policy gate -> optional interrupt -> execute command -> render -> end
```

node 使用结构化输入输出 model 和显式 conditional edge。专用 subgraph 可以调用 model 和白名单 tool，但必须返回类型化 artifact proposal，不得直接修改共享状态。

`Quantitative` 明确不是子智能体。它不需要 LLM 自主推理，而是 `capabilities/quantitative` 中由确定性代码、LightGBM/LSTM inference、model registry 和后台 worker 构成的业务能力。Research Agent 通过 `get_prediction`、`get_quantitative_snapshot` 等只读 tool 获取单股量化结果；Strategy Agent 通过 `submit_scan`、`get_scan_status` 和 `list_scan_results` tool 管理批量扫描。量化 training、model publish 和 drift handling 由 application/worker/HITL workflow 执行，不向普通对话 Agent 暴露自由调用入口。

因此首版只有 Research、Strategy 和 Planning 三类业务子智能体，Supervisor 仅负责任务路由与结果汇总。不采用无约束的单一 ReAct loop，也不为确定性计算虚构 Agent 身份，因为这两种方式都会增加授权、重试、来源关系和完成条件的复杂度。

### 3. 使用 SQLite，并分离 graph checkpoint、领域记录和事件流

首版以 SQLite 作为单机持久化 source of truth。LangGraph SQLite checkpointer 按用户隔离的 `thread_id` 保存 graph checkpoint；SQLAlchemy repository 保存领域 aggregate；outbox 风格的 run event 表保存可重连的 SSE 事件。所有数据使用同一个受配置管理的数据库文件与 migration 版本，但各类表保持职责分离。成为 evidence 或 workflow input 的 artifact payload 必须不可变或具有版本。

SQLite 启用 WAL、foreign key、`busy_timeout` 和显式 transaction policy。API 与 worker 使用短写事务；provider 请求、feature 计算和 model inference 不得持有数据库 transaction。首版限定单机部署和单个任务 worker，worker 内可以并发执行只读或外部计算，但通过受控写入路径串行提交结果。scan unit claim 使用原子条件更新，避免依赖 SQLite 不具备的 row-level lock。

checkpoint state 不能作为领域数据库：恢复 checkpoint 不得回滚已发布策略、已提交扫描或已激活计划。每个 command 携带 idempotency key，由 `run_id`、node/action 身份和规范化 payload hash 生成。repository port 与 migration 保持数据库实现边界，以便未来在写入吞吐或多节点成为真实需求时单独迁移 PostgreSQL；本次不维护 SQLite/PostgreSQL 双栈。内存持久化仅用于 unit test fake。

### 4. 使用独立 HITL 模块执行澄清、复核、审批与修订

HITL module 定义持久化 `HumanInteraction` aggregate，包含 `interaction_id`、`type`、owner、`thread_id`、`run_id`、subject reference/version、normalized payload/hash、允许的 response schema、状态、创建与截止时间、最终 actor、response 和 resolution。交互类型至少覆盖 `clarification`、`approval`、`review`、`correction` 和 `exception_resolution`；状态支持 `pending`、`resolved`、`expired` 和 `cancelled`。

每个 command 声明 action class、owner scope、risk level 和幂等行为。服务端 policy 使用已认证 actor 和规范化 command 进行评估。证券歧义、低置信度或冲突 evidence、out-of-distribution prediction 可以产生澄清或复核交互；发布 model/strategy、批量持久化 watchlist、激活交易计划、启用提醒，以及将复盘反馈应用为样例必须产生审批交互。只读分析和保存未完成草稿可以直接执行。

graph 先通过 HITL service 创建交互，再将 `interaction_id` 写入 LangGraph interrupt。API、CLI 或未来客户端均通过同一 HITL command 查询和响应待办。恢复时使用 compare-and-set 校验 owner、交互状态、subject version 和 payload hash，保证同一交互只解决一次；用户修订会生成新的规范化 payload/version，不会悄然修改原审批对象。超时不得视为批准，必须按 policy 取消、继续降级路径或保留为待处理异常。

不使用仅由 prompt 驱动的确认，也不把完整 HITL 状态只存入 checkpoint，因为 model 文本无法强制授权，checkpoint replay 也不能替代可查询、可审计的人机协作记录。

### 5. 使用版本化 Card Protocol 连接 HITL、领域 Artifact 与 Web

卡片不是 domain entity，也不是 LLM 输出格式。系统分别维护三个 source of truth：`HumanInteraction` 保存用户输入与决策的生命周期；各 capability domain object 保存业务事实；job/run 保存后台执行状态。确定性 presenter 将这些对象投影为统一 `CardEnvelope`：

```text
CardEnvelope
├── protocol_version              # card.v1
├── card_id                       # 稳定标识，用于原位更新
├── kind                          # 白名单语义类型
├── schema_version                # 具体 kind 的版本
├── revision                      # 单调递增
├── source                        # interaction/artifact/job id + version
├── state                         # pending/resolved/superseded/expired/cancelled/failed
├── data                          # 通过 catalog schema 校验的数据
├── actions                       # 服务端允许的语义 action
├── payload_hash                  # 防止显示内容与提交内容不一致
├── expires_at
└── text_fallback                 # 客户端不支持该 kind 时仍可安全展示
```

CardCatalog 是 kind 与 schema 的单一真相源。首版支持：

- Interaction Card：`interaction.form.v1`、`interaction.choice.v1`、`interaction.approval.v1`、`interaction.review.v1`、`interaction.correction.v1`。
- Artifact Card：`artifact.research.v1`、`artifact.quantitative_snapshot.v1`、`artifact.scan_result.v1`、`artifact.trade_plan.v1`、`artifact.reminder.v1`。
- Progress Card：`progress.research.v1`、`progress.scan.v1`。
- Notice Card：`notice.unsupported.v1`、`notice.data_gap.v1`、`notice.failure.v1`。

Form Card 的 field 只能来自 capability 定义的 schema，包含稳定 `key`、data type、control type、required/read-only、当前值、约束、枚举选项、字段级 error、provenance 和条件可见性。Card action 使用 `continue`、`confirm`、`edit`、`cancel`、`retry` 等语义 id，不允许后端发送脚本、HTML、组件名或任意跳转地址。显示文案使用 i18n key 与安全 fallback；客户端负责视觉组件，不解释业务规则。

LLM 只能输出 intent、候选 slot 和解释文本。capability validator 决定缺失字段，HITL policy 决定 interaction type，presenter 决定 card kind、field、action 和约束。选择这一边界而不使用任意生成式 UI，是为了避免 prompt injection 生成危险按钮、前后端 schema 漂移和未校验字段写入。

卡片 lifecycle 通过持久化 SSE event 传播：`card.created`、`card.updated`、`card.resolved`、`card.superseded` 和 `card.failed`。事件携带 run sequence、`card_id` 和 `revision`；Web reducer 以二者去重并原位更新卡片。刷新或跨设备进入会话时，客户端通过 pending HITL 与 artifact query 重建当前卡片，不依赖浏览器内存。客户端本地可以显示 `submitting`，但服务端状态只以持久化 interaction/artifact/job 为准。

HITL 响应统一提交到 `/api/hitl/{interaction_id}/responses`，携带 action、values、interaction version、payload hash 和 idempotency key。服务端执行 owner、状态、版本、hash 和 field schema 校验；校验失败返回字段级错误并产生新 card revision。已解决、过期、取消或 superseded 的卡片拒绝重复提交。

“我要买 NVDA”在当前无 broker execution 的边界内按以下流程处理：

```text
识别 symbol=NVDA、direction=buy
  -> Choice Card：说明不能下单，询问是否创建交易计划
  -> Form Card：一次收集 horizon、entry、invalidation/stop、target、risk/size
  -> capability validation
  -> Approval Card：区分用户输入、模型建议、source 和缺失项
  -> confirm/edit/cancel
  -> 幂等创建 TradingPlan
  -> Artifact Card：只读展示计划，并可继续提议 Reminder Card
```

系统不机械地每次只问一个字段：单一歧义使用 Choice Card，多个相关缺失字段合并为一张 Form Card，高风险写操作始终单独使用 Approval Card。“新增一个交易”如果无法区分交易计划、手工记录已发生交易或真实下单，则先创建 Choice Card；首版只支持创建交易计划，其他意图返回明确的 Notice Card，不伪造 transaction 或 order。

Web 首版采用 registry-driven renderer：只渲染 CardCatalog 导出的 `kind + schema_version`，未知类型显示 `text_fallback`；表单包含键盘操作、焦点恢复、错误关联、提交禁用、重复点击保护和移动端布局。页面只维护交互 UI 状态，所有业务状态从后端重新获取。

### 6. 在 request graph 之外运行扫描与提醒评估

对话 graph 通过 `ScanService` 提交扫描，并立即返回持久化 job artifact。数据库 worker 使用 lease claim 扫描单元，对每个冻结的 `(scan_id, security_id)` 只评估一次，写入结构化结果并发送进度事件。取消操作会停止新的 claim，但保留已完成结果。每个扫描单元持久化重试预算与最终失败状态。

提醒评估同样由定时 worker 基于活跃规则和新鲜 observation 执行。crossing 语义、cooldown、去重和状态迁移由确定性 service 实现。LangGraph 可以创建或检查这些对象，但不会保持运行以轮询市场。不在 graph node 中执行大规模扫描，因为 request 生命周期、checkpoint replay 和单证券重试具有不同的运行语义。

### 7. 使用冻结的 evidence snapshot 与 provider capability port

provider port 首版仅覆盖美国交易所上市证券的证券查询、报价、K 线、公司行动、SEC/基本面、新闻/搜索和通知交付。adapter 返回规范化的类型化 observation，并包含 provider、source URI 或 record reference、观测/发布时间、获取时间、授权 metadata 和 raw payload hash。provider 优先级、fallback、美国交易所覆盖和时效阈值放在配置中，不写入 prompt。

研究 service 组装不可变 evidence snapshot。model node 接收有边界的 snapshot projection，并且返回的 claim 必须引用 evidence 标识符。validator 拒绝未知 citation 并标记无支持 claim；冲突与缺失数据保持可见。不允许 model 任意调用远程 tool 或合成缺失数据，因为这会破坏审计性和安全性。

### 8. 使用独立量化 ML pipeline 负责预测与扫描评分

LLM 不参与股价、收益、方向概率、扫描分数或排序计算。strategy version 除用户自然语言描述外，还保存结构化条件、target、预测周期、候选 universe policy 和 ranking policy。LLM 可以把对话整理为待确认的策略草稿，也可以解释量化结果，但最终数值只能来自确定性计算或 model registry 中已批准的专用量化 model。

量化 pipeline 分为 point-in-time 数据集、版本化 feature definition、训练、walk-forward evaluation、model registry、batch/online inference 和 drift monitoring。每个 prediction 保存 `model_version`、`feature_snapshot_id`、`as_of`、target 定义、预测周期、概率/分位数或区间、不确定性及适用范围。训练与 inference 必须复用同一 feature 实现，且校验错误复权、发布时间和未来数据泄漏。

首个生产基线采用确定性规则加 LightGBM。原因是交易扫描通常以表格化的价量、波动率、基本面和横截面 feature 为主，LightGBM 对中等数据规模、缺失值、非线性和 feature attribution 更实用。LSTM 作为序列候选 model，仅当它在相同时间切分、相同 target 和相同成本假设下，持续改善样本外预测质量、校准度、稳定性及交易成本后指标，并满足 inference 延迟要求时才能发布。模型选择由证据门禁决定，不预设深度模型一定更优。

扫描先执行确定性的适用性、流动性、数据可用性和策略硬规则，再调用已批准 model 批量 inference。版本化 ranking function 只使用持久化 model output 和确定性字段。LLM 最后生成面向用户的 evidence 摘要、风险解释和计划草稿，但 validator 会拒绝其新增或篡改数值。历史 strategy version、universe snapshot、model version、prediction 和 result 均保持不可变；复盘与真实标签仅进入新的训练数据或策略草稿，不重写历史。

### 9. 将 API、CLI、Web 与流式协议作为 application adapter

FastAPI 提供经过认证的 conversation/run、HITL、card、artifact、strategy、scan、watchlist、plan、reminder 和 review endpoint。Server-Sent Events 是 run、card 和 job event 的默认有序流；普通 resource endpoint 在流式连接不可用时支持恢复。CLI 使用同一 application layer 并以文本方式呈现同一 interaction/artifact；最小 Web chat 使用 CardCatalog renderer 呈现卡片并提交 HITL response。

本地初始认证可使用可配置的开发身份，但所有 application command 仍必须接收 `UserContext`；production mode 在没有经过验证的认证时必须 fail closed。不优先采用 WebSocket，因为核心交互是服务端到客户端的事件流加普通 command，SSE 更易恢复与运维。

### 10. 将安全与可观测性定义为横切契约

本次变更不定义 broker provider port、order aggregate 或 execution tool。prompt 与 renderer 使用决策辅助表述，policy validator 拒绝成交或账户变更声明。所有重要研究输出都包含 source metadata、时效、缺口、置信度、风险和失效条件。

结构化 telemetry 记录关联标识符、graph/node 状态迁移、command 结果、provider 调用、model/prompt 标识符、延迟、token 用量、重试和审批。日志、trace 及可选第三方导出前必须经过 redaction layer。外部 observability provider 关闭时，本地结构化 trace 仍然可用。

### 11. 通过确定性边界进行测试

unit test 覆盖领域状态迁移、policy、HITL 状态机、CardCatalog/presenter、幂等性、排序、citation 校验、证券规范化、提醒语义、point-in-time feature 和 LLM 数值隔离。量化测试覆盖数据泄漏、训练/inference feature parity、walk-forward 切分、model artifact 可复现性、校准、ranking 和 drift policy。graph test 使用 fake LLM/provider port 和真实 SQLite test checkpointer 编译并执行 graph。repository/worker integration test 使用临时 SQLite 数据库并启用与生产一致的 pragma。contract test 覆盖 API/CLI/Web、HITL 查询/响应、card lifecycle 和可重连事件流。live provider test 必须显式启用，且不属于默认 test suite 的通过条件。

端到端验收覆盖：包含 citation 与数据缺口的美股研究；HITL Form/Choice/Approval Card 的创建、修订、超时、刷新恢复和准确响应；进程重启但不产生重复写入；冻结 watchlist 扫描输入；Progress Card 更新；计划激活与 Artifact Card；提醒去重；跨用户拒绝；拒绝非美股和 broker 执行。

### 12. 使用 LiteLLM adapter 统一模型调用

`core/llm` 定义供应商无关的 `LLMClient` port，以及 `LLMRequest`、`LLMResponse`、`ModelRoute`、结构化输出约束、usage、finish reason 和统一错误。LangGraph node、Supervisor 与业务子智能体只接收注入的 `LLMClient`，不得导入 LiteLLM 或 OpenAI、Anthropic、Google 等模型厂商 SDK。模型输出仍须经过本地 schema、citation、tool policy 和业务 validator；LiteLLM 成功返回不等于业务结果有效。

首版在 `adapters/llm/litellm` 使用进程内 LiteLLM Python SDK，并通过异步 completion 接口实现 `LLMClient`。选择 SDK 模式是为了保持首版单机部署简单，不额外引入 LiteLLM Proxy 进程；如果以后需要集中密钥、跨服务配额或统一网关，只替换 adapter/config，不改变 graph、agent 或 capability 契约。

应用按工作负载使用稳定的逻辑 route，例如 `intent_classifier`、`research_summarizer`、`strategy_drafter` 和 `planning_drafter`，而不是在 prompt 或 Agent 中写厂商 model name。类型化配置将逻辑 route 映射为 LiteLLM model identifier、允许的 provider、参数、timeout、最大 token、并发/预算以及显式 fallback chain。API key、base URL 和其他凭据只通过 secret reference 或环境注入；配置、日志、checkpoint 和领域记录不得保存明文密钥。

adapter 统一处理流式与非流式响应、JSON/结构化输出、token usage、成本估算、provider request id、取消、超时、限流和临时故障。只对明确可重试的错误执行有界重试；fallback 必须在配置中逐条允许，并遵守数据驻留、模型能力、结构化输出和预算约束，不得因为失败静默切换到未批准 provider。认证错误、无效请求、上下文超限和内容安全拒绝直接映射为稳定错误，不进行盲目重试。

LiteLLM 不承担业务 tool 的执行。模型返回的 tool call 或结构化意图必须先转换为内部 schema，再由 `ToolGateway` 白名单、policy 与 HITL 检查决定是否执行。量化预测同样不经过 LiteLLM；LightGBM/LSTM 继续由 `capabilities/quantitative` 与 `adapters/model_runtime` 负责。

测试使用 `FakeLLMClient` 验证 graph，不依赖真实模型。LiteLLM adapter contract test 通过 mock transport 覆盖 route 映射、结构化输出、stream chunk 合并、usage、timeout、retry、fallback、取消、错误归一化和脱敏；live model smoke test 必须显式启用。telemetry 记录逻辑 route、批准后的 provider/model 标识、prompt version、token、估算成本、延迟、重试和结果，不默认记录完整 prompt/response。

### 13. 分两阶段实施并设置架构确认门禁

第一次实施只交付架构骨架。允许创建 package/module 目录、公开 protocol 与类型占位、Agent/capability manifest、composition root、fake/in-memory adapter、最小 LangGraph 拓扑、空 API/CLI/worker/Web 入口、依赖方向测试和架构说明；实现应能够安装、导入、启动并运行骨架测试，但不得开始 SQLite schema、真实 provider、LiteLLM 网络调用、量化训练/inference、完整业务状态机、Card 细节或端到端 workflow。

第一阶段结束时必须输出可评审的模块树、依赖方向、公开接口清单、composition root 装配图、最小 graph 拓扑、仍为 stub/fake 的列表和下一阶段切片建议。随后停止实施，等待用户明确确认架构。没有该确认，不得勾选或执行第二阶段任务；即使第一阶段测试全部通过，也不能把技术可运行视为架构已批准。

架构确认后的第二阶段再按 vertical slice 落地具体实现，优先顺序为持久化与横切契约、证据研究、量化能力、watchlist/strategy、扫描、计划/HITL/Card/Web。若评审改变模块边界，先更新 OpenSpec 工件与骨架，再进入业务代码，避免在未经确认的结构上积累实现细节。

## 风险与取舍

- [初始范围横跨多个领域 module] -> 按依赖顺序交付 vertical slice，并将每项能力置于稳定 application command 之后；延后完整客户端和高级分析。
- [LangGraph checkpoint replay 可能重复外部副作用] -> 所有副作用通过幂等 command 执行，单独持久化 action result，并在 command 完成后只将其 reference 写入 checkpoint。
- [provider 数据可能过期、冲突、不可用或受授权限制] -> 携带时间戳与授权信息，执行可配置 freshness/trust policy，保留冲突并降级为明确的部分结果。
- [LLM 输出可能无证据支持或结构无效] -> 使用有边界的 schema、evidence 标识符、citation 校验和修复重试；校验仍失败时返回 `unavailable`。
- [金融时间序列容易产生未来数据泄漏和过拟合] -> 强制 point-in-time dataset、按时间切分、purge/embargo policy、walk-forward evaluation、锁定 test window，并在发布前与简单基线比较。
- [专用 model 会因市场状态变化而失效] -> 监控 data/feature/prediction drift、校准和标签回流表现；超过阈值时停止 model 或降级到已批准基线，绝不降级为 LLM 预测。
- [SQLite 写入锁会限制并发扫描、HITL 和事件吞吐] -> 首版限定单机单 worker，启用 WAL、短事务、`busy_timeout` 与串行写入协调；用指标监控 lock wait，在达到明确阈值后再提出 PostgreSQL migration。
- [SQLite 文件损坏或宿主机丢失会影响全部持久化状态] -> 使用受控数据库路径、定期在线 backup、启动完整性检查和经过演练的恢复流程。
- [HITL 待办可能长期悬挂或基于过期对象操作] -> 设置 deadline 与状态机，响应时校验对象版本和 payload hash；超时永不自动批准。
- [SSE 客户端重连后可能收到重复事件] -> 事件使用每个 run 内单调递增的 sequence number，并要求客户端通过 cursor 确认和去重。
- [参考项目范围可能引发不必要的功能对齐] -> 只将其 PRD 作为业务输入，并严格执行本次变更明确的非目标与 capability spec。

## 迁移计划

1. 第一阶段建立完整 module/package 骨架、公开 contract、fake adapter、composition root、最小 graph 与空应用入口，以架构测试验证依赖方向，不实现业务细节。
2. 输出架构评审材料并停止实施；只有用户明确确认后才进入以下步骤。
3. 第二阶段增加类型化配置、用户上下文、SQLite/migration、repository、幂等 command、provider fake、LiteLLM adapter 和可观测性基础设施。
4. 建立 point-in-time dataset、feature pipeline、LightGBM baseline、walk-forward evaluation、model registry 与离线 inference；LSTM 仅作为通过相同门禁的后续候选 model。
5. 完成 LangGraph supervisor、SQLite checkpoint、HITL module、Card Protocol、API/CLI adapter 和持久化事件流。
6. 按顺序交付 vertical capability：有来源研究、watchlist、版本化策略与量化扫描、交易计划/提醒/复盘，并为需要呈现的交互、artifact 与 job 增加确定性 card presenter。
7. 实现最小 Web chat shell、CardCatalog renderer、SSE reducer 与 HITL response client，不扩展为完整交易工作台。
8. 在启用非 fake provider 前，完成 unit、量化数据与 model、graph、SQLite integration、backup/restore、HITL、LiteLLM、API/CLI/Web contract 及端到端安全测试。
9. 按 capability 逐一通过配置和 provider contract test 启用真实 provider。

当前仓库没有生产实现或数据，因此无需迁移旧数据。回滚前先停止写入并备份 SQLite 文件，再停用受影响 route/worker、回退新服务部署和数据库 migration；外部来源的 raw record 可根据配置的数据保留策略继续保存。

## 待确认问题

- 首个生产 target 和预测周期是什么，例如 5/20 个交易日方向概率、收益分位数或波动率？它将决定 label、feature 和评测门槛。
- 美股行情、公司行动和基本面分别使用哪些已获授权的 provider？
- 生产环境允许使用哪个 LLM provider 与 observability backend？适用哪些数据驻留和脱敏约束？
- 首个版本需要哪些 notification channel：仅应用内、email、webhook 还是移动端 push？
- 生产环境默认 freshness threshold 和 scan universe 上限是多少？这些属于配置决策，不改变 capability 边界。
