## 背景与原因

当前仓库只有 Python 示例骨架，无法支持交易助手所需的“研究到计划”工作流。我们需要建立一套基于 LangGraph 的基础架构，将对话请求转化为可追踪、可恢复且有证据支持的交易研究与计划动作，同时严格防止编造数据及执行未经确认的交易相关动作。

## 变更内容

- 建立 Python 交易 Agent 服务，使用持久化 LangGraph graph 编排对话 workflow，并支持流式输出、checkpoint、重试和 HITL interrupt。
- 使用 LiteLLM 统一封装大模型调用：业务与 Agent 只依赖供应商无关的 `LLMClient` 契约，LiteLLM adapter 负责模型路由、结构化输出、超时、重试、预算、用量统计和错误归一化，不允许业务模块直接调用任何模型厂商 SDK。
- 增加独立 HITL 人机交互模块，统一承载歧义澄清、审批、人工复核、修订、超时、取消和恢复，并完整记录交互 payload、对象版本、操作者和结果。
- 增加版本化 Card Protocol 与最小 Web chat 交互层：HITL 使用 Form、Choice、Approval、Review 和 Correction 卡片收集输入，领域结果使用 Artifact Card 展示，后台任务使用 Progress Card 更新状态。
- 首版仅支持美国交易所上市证券，并增加有来源的美股研究能力，包括证券解析、行情与 K 线上下文、公司与产业研究、证据追踪、不确定性和风险披露。
- 增加版本化用户策略和专用量化模型 pipeline，并支持在明确的 watchlist 候选集合上执行异步扫描与指定周期预测；评分、预测和排序只由确定性规则或已注册的 LightGBM、LSTM 等量化模型产生，LLM 不参与数值预测。
- 增加 watchlist 导入、规范化、分组和来源追踪，使研究与扫描结果能够沉淀为可复用的候选集合。
- 增加“草稿优先”的交易计划、提醒与复盘反馈，并保持其与来源研究、策略版本及扫描结果的关联。
- 增加持久化领域存储、provider 边界、用户隔离、审计事件及运行可观测性，以支持 Agent run 的恢复与检查。
- 首版使用 SQLite 统一保存领域数据、LangGraph checkpoint、HITL 交互、任务状态和事件流，并以单机部署、WAL 和受限写入并发为运行边界。
- 本次变更不包含 broker 下单、账户修改、收益承诺、自动交易、复杂回测、完整 Web 工作台、iOS 或管理端客户端；Web 范围仅包括 chat shell、版本化卡片 renderer 和 HITL 响应。
- 实施分为两个阶段：第一阶段只搭建可启动、可检查的整体模块骨架、公开契约、composition root、fake adapter 和架构测试；必须在人工确认模块边界后，第二阶段才实现数据库、provider、量化模型、业务 workflow 和前端细节。

## 能力范围

### 新增能力

- `agent-workflow-runtime`：持久化 LangGraph 会话、类型化状态、tool 路由、流式输出、checkpoint、独立 HITL 人机交互、版本化 Card Protocol、最小 Web chat renderer、run 恢复和执行可观测性。
- `market-research`：面向美国交易所上市证券，使用规范证券标识符、明确数据 provider、时效 metadata、引用、不确定性和安全披露，提供有证据支持的证券与产业研究。
- `strategy-scanning`：版本化策略定义、时间点一致的特征数据、专用量化模型训练与评测、概率预测，以及在冻结候选集合上的可恢复异步扫描；LLM 仅总结量化结果，不生成评分或预测。
- `watchlist-management`：用户隔离的 watchlist 导入、证券规范化、去重、分组、来源追踪及扫描候选集合复用。
- `trade-planning`：“草稿优先”的交易计划、经过确认的提醒、生命周期跟踪，以及与生成该计划的 evidence 和 strategy 关联的复盘反馈。

### 修改能力

无。当前仓库不存在既有 capability spec。

## 影响

- 新增 Python package，并按 `core`、`agents`、`capabilities` 三个核心模块以及 `adapters`、`apps` 两个边界层组织；业务 tool 与所属 capability 共置，CLI/API、SQLite/provider adapter、后台 worker 和测试通过 composition root 装配。
- 新增 LangGraph、LiteLLM、类型化配置与 model、异步 HTTP/API 层、SQLite 数据库/checkpoint 存储，以及量化特征计算、LightGBM 基线训练、model registry 与 inference 相关依赖；LSTM 仅在样本外评测证明增益后作为候选序列模型启用。具体 package 与版本在实现阶段锁定，不继承参考项目的自定义 Agent loop。
- 建立 LLM、美股行情、SEC/公司基本面、新闻/搜索、通知和可选可观测性 provider 的外部集成边界。
- 新增最小 Vue/TypeScript Web chat surface、CardCatalog、HITL response client 和 SSE card lifecycle reducer；前端只能渲染白名单 `kind + schema_version`，不执行 LLM 生成的任意 UI。
- 将 `/Users/xingdong/workspace/trade-agent` 作为产品业务参考，同时将 `trade-agent-v2` 作为拥有独立契约与 migration 的全新实现。
