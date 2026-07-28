# Trade Agent 课程源码导读

本文面向第一次阅读项目的学生，目标是回答三个问题：系统从哪里启动、各包负责
什么、一次用户请求如何走完整条链路。它描述的是当前代码，不把尚未完成的生产
装配说成已经实现。

## 1. 先建立整体视图

系统遵循“外层负责连接，内层负责规则”的分层方式：

```text
Web / CLI
    |
    v
apps/api 或 apps/cli          传输协议、认证、参数校验
    |
    v
apps/conversation_runtime     一次会话的流程编排与暂停/恢复
    |
    +--> agents               判断由谁处理，不保存业务事实
    |
    +--> core/tools           检查 Agent 是否有权调用 Tool
    |
    +--> capabilities         研究、量化、计划等确定性业务规则
              |
              v
           ports              业务层声明需要的外部接口
              |
              v
           adapters           SQLite、LiteLLM、行情和模型的具体实现
    |
    v
Card + Event + HITL           返回 Web，并在需要人类决定时暂停
```

这条依赖方向很重要。领域模型不知道 FastAPI、LangGraph、SQLite 或 LiteLLM；外部
技术可以替换，业务规则不需要跟着重写。

## 2. 五个顶层包分别做什么

| 包 | 责任 | 不应该做什么 | 推荐入口 |
|---|---|---|---|
| `core` | 定义所有模块共享的 Agent、Tool、LLM、HITL、Card、安全和事件契约 | 不包含股票研究等具体业务 | `core/runtime/contracts.py` |
| `agents` | Supervisor 路由和 Research、Strategy、Planning Agent 清单 | 不直接访问数据库、行情 SDK 或量化模型 | `agents/supervisor/graph.py` |
| `capabilities` | 实现确定性的业务能力和领域规则 | 不依赖 HTTP、LangGraph 和具体 provider | `capabilities/planning/application/__init__.py` |
| `adapters` | 实现 core/capability 声明的端口 | 不决定业务流程和审批规则 | `adapters/sqlite/database.py` |
| `apps` | 启动进程、连接依赖、编排跨 capability 工作流 | 不复制 capability 内的业务规则 | `apps/container.py` |

### `core`：框架公共语言

- `runtime`：Agent 运行状态、意图、manifest 和子图执行约定。
- `tools`：Tool manifest、注册表、权限策略和统一 Gateway。
- `llm`：与模型供应商无关的请求/响应协议及结构化输出校验。
- `hitl`：人工交互聚合、状态机和一次性响应规则。
- `presentation`：前后端共享的 Card envelope 和投影器。
- `events`：一次 run 内按顺序输出的事件契约。
- `security`：认证上下文、访问边界和日志脱敏。

`core` 可以理解为“搭建 Agent 系统所需的积木”，它不知道 NVDA 或交易计划是什么。

### `agents`：决策角色，不是业务能力

当前只有三个业务 Agent：Research、Strategy、Planning。每个 Agent 通过 manifest 声明
自己允许调用哪些 Tool，Supervisor 根据 `Intent` 选择 Agent。Agent 不应自行查询
SQLite 或调用 LightGBM，否则权限、审计与测试都会绕过 ToolGateway。

Quantitative 不属于 Agent。它是 Research 或 Strategy 可以通过 Tool 调用的专用能力：
模型产生预测和评分，LLM 只解释已经持久化的结果。

### `capabilities`：业务事实的所有者

每个 capability 使用相同的内部结构：

| 子包 | 作用 | 例子 |
|---|---|---|
| `domain` | 纯业务对象、不变量和状态迁移 | 计划只有草稿可以激活 |
| `application` | 编排一次业务用例 | 创建计划、运行扫描 |
| `ports` | 声明所需仓储或 provider 接口 | `MarketDataProvider` |
| `tools` | 把 application 用例暴露给 Agent | `planning.create_plan_draft` |
| `cards` | 把业务结果投影为 Web Card | `artifact.trade_plan` |
| `contracts.py` | 供其他模块引用的稳定公开类型 | `TradingPlan` |

`tools` 与 `capabilities` 不是并列概念。Capability 是完整业务模块；Tool 只是 Agent
进入该模块的一扇受控门。HTTP API、worker 或测试也可以调用 application service，
不必假装成 Agent。

### `adapters`：把抽象接口接到真实技术

- `sqlite`：数据库、migration、仓储、事件、HITL、checkpoint 和后台任务。
- `llm/litellm`：把统一 `LLMClient` 转换为 LiteLLM 请求。
- `model_runtime`：LightGBM 基线和 LSTM 候选模型的训练/推理适配器。
- `market_providers`：美股行情和证券信息 provider；当前默认仍为空 fake。
- `authentication`：OIDC/JWT 验证。
- `notifications`：应用内提醒投递接口。
- `observability`：结构化且经过脱敏的运行轨迹。

### `apps`：把模块真正串起来

`apps/container.py` 是唯一组合根。它先初始化 SQLite，再创建业务 Service、ToolGateway
和会话运行时，最后交给 API、CLI 或 worker。测试可以在这里注入 fake，而无须修改
业务代码。

`apps/conversation_runtime.py` 是当前会话入口，只负责启动 run、调用 Supervisor Graph、
校验 Workflow 路由和委托 HITL 恢复。Card、事件、恢复上下文和恢复收据由
`apps/workflows/runtime.py` 统一持久化；具体业务状态机位于 Workflow 与 capability。

自然语言不会在这里用固定短语判断。组合根把三个部分连接起来：

```text
IntentClassifier
  -> IntentClassification(intent, workflow_id, entities)
  -> SupervisorGraphInvoker
  -> selected_agent_id
  -> WorkflowRegistry[workflow_id]
  -> ConversationWorkflow.start(context)
```

`IntentClassifier` 可以由结构化 LiteLLM、规则引擎或租户配置实现。运行时只接受
经过校验的 `workflow_id` 和实体；`WorkflowRegistry` 决定当前部署实际启用哪些工作流。
未知工作流、或 Graph 所选 Agent 与 Workflow 声明不一致时会安全拒绝，不会使用相似
关键词猜测。测试中的 `MappingIntentClassifier` 只用于声明测试 fixture，不属于生产
业务判断。

Workflow 是完整插件，不只是一个启动函数：它同时声明输入约束，并负责自己创建的
HITL 节点如何恢复。这样增加新业务只需新增插件并注册，不需要修改中台运行时。

这里要区分两类“固定值”：

- `planning.create_plan`、`interaction.form`、`card.created` 是版本稳定的协议 ID，类似
  API 路径或数据库字段，必须集中定义和校验。
- “新增一个交易”“我要买”“默认创建买入计划”是自然语言或业务默认值，只能位于
  分类 adapter、具体 Workflow、配置或测试 fixture 中，绝不能成为通用运行时的条件判断。

## 3. 一次“我要买 NVDA”如何运行

1. Web 调用 `POST /api/conversations/runs`，API 解析用户身份和请求。
2. `IntentClassifier` 输出 Planning 意图、`planning.create_plan` 工作流和 `NVDA` 实体。
3. `ConversationRunService.start_run()` 创建 `run_id`、绑定 thread，并写入
   `run.started` 事件。
4. Supervisor graph 接收 `AgentState`，把请求路由到 Planning。
5. `WorkflowRegistry` 同时校验 `workflow_id` 与 Graph 返回的 Agent ID，再找到组合根注册
   的计划 Workflow。系统明确返回“不支持真实下单”
   的 Card，但可以继续创建研究计划。
6. Planning capability 创建草稿；运行时创建表单型 HITL，并发布
   `interaction.form` Card。
7. Web 通过 SSE 收到 Card，渲染输入项。此时后端已经暂停，不会擅自批准。
8. 用户提交表单，API 校验 card revision、payload hash、subject version 和幂等键。
9. `handle_resolved_interaction()` 通过 `WorkflowRegistry` 按 `subject_type` 找到负责的
   Workflow，由该 Workflow 生成审批 Card。
10. 用户确认后 Planning domain 才允许草稿转为 active，并发布
   `artifact.trade_plan` Card。

这说明 HITL 不是一个弹窗组件，而是贯穿数据库、API、事件和前端的可恢复业务协议。

## 4. 研究与量化链路

目标链路如下：

```text
Research Agent
  -> market_research Tool
  -> 行情与基本面 evidence
  -> quantitative Tool
  -> LightGBM/LSTM inference
  -> 持久化 scan result + model lineage
  -> 人工复核
  -> LiteLLM 总结已有结果
  -> Planning 计划草稿
```

量化模型负责特征、概率、评分和排序。LiteLLM 不接触训练标签，也不能修改扫描结果；
它只在人工复核之后总结持久化结果并生成可编辑文字。这是系统最重要的安全边界之一。

## 5. Card、Event 与 HITL 的关系

- Card 是前端显示协议，例如表单、审批、研究报告和扫描进度。
- Event 是 Card 的传输和重放协议，例如 `card.created`、`card.resolved`。
- HITL 是后端持久化的人工决策状态，包含版本、响应 schema、过期时间和结果。

前端不能把“按钮被点击”直接当成业务成功。它提交 HITL 响应，后端完成版本与权限
校验、推进领域状态并发布新 Event，前端再根据 Event 更新 Card。

## 6. 当前实现边界

已经接通的主线包括 SQLite、HITL、Card、SSE、API、CLI、Web，以及“新增一个交易”
和“我要买 NVDA”的 Planning 工作流。研究、量化、watchlist、策略和提醒具备领域服务、
Tool、适配器与测试。

仍需注意以下默认装配限制：

- `build_application_container()` 默认使用 `FakeLLMClient`，真实 LiteLLM 需要配置注入。
- 默认行情 provider 是空的 `FakeMarketProvider`。
- 默认 ToolRegistry 当前只装配 Planning Tool。
- Supervisor 的执行与渲染节点仍是骨架，主要业务由确定性会话状态机推进。
- 完整 Research 工作流依赖注入 `ResearchWorkflowBackend`；生产 façade 与 worker 仍待装配。
- 默认未配置生产 `IntentClassifier` 时会进入安全澄清；不会在中台代码中内置自然语言
  关键词。真实 LiteLLM 分类 adapter 仍需在生产组合根装配。

因此本项目现在适合讲解架构边界和已实现的纵切面，但不应宣称已经是可连接真实行情
并自动完成全部研究流程的生产系统。

## 7. 推荐课堂阅读顺序

1. `src/trade_agent/apps/container.py`：理解对象如何组装。
2. `src/trade_agent/apps/api/__init__.py`：理解请求如何进入系统。
3. `src/trade_agent/apps/conversation_runtime.py`：跟踪一次暂停与恢复。
4. `src/trade_agent/core/runtime/intent.py`：理解分类协议与安全降级。
5. `src/trade_agent/core/hitl/contracts.py` 与 `service.py`：理解人工审批状态机。
6. `src/trade_agent/core/presentation/contracts.py`：理解统一 Card 协议。
7. `src/trade_agent/capabilities/planning/domain/models.py`：理解领域不变量。
8. `src/trade_agent/capabilities/planning/application/__init__.py`：理解用例编排。
9. `src/trade_agent/core/tools/gateway.py`：理解 Agent 如何受控调用能力。
10. `src/trade_agent/adapters/sqlite/repositories.py`：理解持久化和 owner 隔离。
11. `tests/integration/test_conversation_runtime.py`：用可执行场景反向验证理解。

每读一层都可以问三个问题：它拥有什么状态、它依赖哪个抽象、失败时由谁负责。回答
清楚这三个问题，就能理解模块边界，而不是只记住目录名称。

## 8. 课堂环境建议

如果学生使用 PyCharm，建议在开课前统一完成两项配置：

1. 解释器使用项目根目录下的 `.venv`；
2. 将 `src/` 标记为 `Sources Root`。

原因很简单：本项目使用标准 `src` 布局。如果 IDE 没识别这一点，`trade_agent`
顶级包会被误判为不可导入，表现就是：

- `from trade_agent...` 出现大面积红线；
- Ctrl/Cmd + 点击无法跳转；
- 自动补全和类型提示明显异常。

这类问题通常不是代码本身错误，而是 IDE 的 Python 路径没有配对好。
