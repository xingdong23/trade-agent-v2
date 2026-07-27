# 第一阶段架构评审材料

## 范围结论

本次交付严格停留在 OpenSpec `create-langgraph-trading-agent` 的第一阶段 1.1-1.7：

- 已建立 `src/trade_agent/{core,agents,capabilities,adapters,apps}` 模块树。
- 已建立每个 capability 的 `domain/application/ports/tools/cards` 统一结构。
- 已建立最小 `web/src/features/chat` Vue/TypeScript 骨架。
- 已建立最小 LangGraph supervisor graph、空 API/CLI/worker 入口、composition root、架构测试。
- 未实现 SQLite schema/migration、真实 provider、真实 LiteLLM 网络调用、量化训练/inference、完整业务 workflow、真实前端业务交互。

## 模块树

```text
src/trade_agent/
├── core/
│   ├── runtime/          # AgentState / Intent / ArtifactReference / AgentManifest
│   ├── llm/              # LLMClient / LLMRequest / LLMResponse / ModelRoute
│   ├── tools/            # ToolManifest / ToolRequest / ToolResult / ToolGateway
│   ├── hitl/             # HumanInteraction / HitlService
│   ├── events/           # RunEvent / EventPublisher
│   ├── presentation/     # CardEnvelope / CardPresenter
│   ├── security/         # UserContext / AccessPolicy
│   └── testing/          # FakeLLMClient / FakeToolGateway
├── agents/
│   ├── research/         # Research manifest
│   ├── strategy/         # Strategy manifest
│   ├── planning/         # Planning manifest
│   └── supervisor/
│       ├── __init__.py   # BUSINESS_AGENTS
│       └── graph.py      # supervisor graph skeleton
├── capabilities/
│   ├── market_research/
│   ├── quantitative/
│   ├── watchlist/
│   ├── strategy/
│   ├── planning/
│   └── reminder/
│       └── 每个 capability 下均有 domain/application/ports/tools/cards/contracts
├── adapters/
│   ├── llm/litellm/      # LiteLLM route config + disabled client scaffold
│   ├── sqlite/           # SQLite scaffold
│   ├── market_providers/ # market provider scaffold
│   ├── model_runtime/    # quantitative runtime scaffold
│   ├── notifications/    # notification scaffold
│   └── observability/    # telemetry scaffold
└── apps/
    ├── container.py      # composition root
    ├── status.py         # process-visible scaffold status
    ├── api/
    ├── cli/
    └── worker/

web/src/features/chat/
├── cards/
├── events/
└── hitl/
```

## 依赖方向

当前通过测试强制的方向：

```text
core -----------------> 不依赖 agents / capabilities / adapters / apps
agents ---------------> 仅依赖 core 与 agents 自身
capabilities/*/tools -> 仅依赖本 capability public contract + core.tools
capabilities/*/cards -> 仅依赖本 capability public contract + core.presentation
adapters -------------> 不依赖 agents / apps / capability implementation
apps -----------------> 通过 composition root 装配 graph、fake llm、fake tool gateway
```

额外结构结论：

- 不存在 `agents/quantitative`，量化能力没有被建模为 Agent。
- 当前 agent manifest 只声明允许的 `tool_id`，不直接引用 repository/provider。
- tool handler 与 card presenter 仍是 stub，不包含业务规则或外部调用。

## 公开接口清单

### Core contracts

- `trade_agent.core.runtime.AgentState`
- `trade_agent.core.runtime.Intent`
- `trade_agent.core.runtime.ArtifactReference`
- `trade_agent.core.runtime.AgentManifest`
- `trade_agent.core.llm.LLMClient`
- `trade_agent.core.llm.LLMRequest`
- `trade_agent.core.llm.LLMResponse`
- `trade_agent.core.llm.ModelRoute`
- `trade_agent.core.tools.ToolManifest`
- `trade_agent.core.tools.ToolRequest`
- `trade_agent.core.tools.ToolResult`
- `trade_agent.core.tools.ToolGateway`
- `trade_agent.core.hitl.HumanInteraction`
- `trade_agent.core.hitl.HitlService`
- `trade_agent.core.events.RunEvent`
- `trade_agent.core.presentation.CardEnvelope`
- `trade_agent.core.security.UserContext`

### Agent / graph boundary

- `trade_agent.agents.research.MANIFEST`
- `trade_agent.agents.strategy.MANIFEST`
- `trade_agent.agents.planning.MANIFEST`
- `trade_agent.agents.supervisor.BUSINESS_AGENTS`
- `trade_agent.agents.supervisor.graph.build_supervisor_graph()`

### Composition root / process entry

- `trade_agent.apps.container.ApplicationContainer`
- `trade_agent.apps.container.build_scaffold_container()`
- `trade_agent.apps.status.scaffold_status()`
- `trade_agent.apps.api.main()`
- `trade_agent.apps.cli.main()`
- `trade_agent.apps.worker.main()`

### Adapter boundary

- `trade_agent.adapters.llm.litellm.LiteLLMRouteConfig`
- `trade_agent.adapters.llm.litellm.LiteLLMClientScaffold`
- `trade_agent.adapters.sqlite.SQLiteAdapterScaffold`
- `trade_agent.adapters.market_providers.MarketProviderScaffold`
- `trade_agent.adapters.model_runtime.QuantitativeModelRuntimeScaffold`
- `trade_agent.adapters.notifications.NotificationAdapterScaffold`
- `trade_agent.adapters.observability.ObservabilityAdapterScaffold`

## Composition Root 装配关系

`src/trade_agent/apps/container.py` 是当前唯一 composition root：

1. 构建 `build_supervisor_graph()`
2. 注入 `BUSINESS_AGENTS`
3. 注入 `FakeLLMClient`
4. 注入 `FakeToolGateway`
5. 输出 `ApplicationContainer`
6. `api/cli/worker` 三个入口都只读取这个 container 并打印状态

这保证了：

- graph 装配点唯一
- 进程入口不分叉依赖树
- 第二阶段替换 fake adapter 时不必改 agent 模块

## 最小 Graph 拓扑

当前 graph 结构：

```text
START
  -> ingest
  -> classify
  -> research | strategy | planning | clarification
  -> policy_gate
  -> execute_command
  -> render
  -> END
```

说明：

- 这是最小 supervisor skeleton，不承载真实业务状态机。
- `classify` 默认使用 `Intent` 显式路由；测试只证明 graph 可编译和可调用。
- `policy_gate`、`execute_command`、`render` 目前均为 no-op scaffold node。

## Stub / Fake 清单

### Fake

- `trade_agent.core.testing.FakeLLMClient`
- `trade_agent.core.testing.FakeToolGateway`

### Stub / NotImplemented

- `LiteLLMClientScaffold.complete()/stream()`
- `SQLiteAdapterScaffold.connect()`
- `MarketProviderScaffold.fetch()`
- `QuantitativeModelRuntimeScaffold.predict()`
- `NotificationAdapterScaffold.send()`
- `ObservabilityAdapterScaffold.emit()`
- 所有 capability `application.execute/query()`
- 所有 capability `ports.get()`
- 所有 capability `tools.handle()`
- 所有 capability `cards.present()`

## 第二阶段 vertical slice 建议

建议顺序：

1. `core + sqlite + app bootstrap`
   - 配置、SQLite schema、checkpoint、event、idempotency、owner scope
2. `market_research`
   - 证券解析、evidence snapshot、citation、research artifact
3. `watchlist + strategy`
   - watchlist import、universe snapshot、strategy version、审批草稿
4. `quantitative`
   - model registry、prediction、scan job、progress event
5. `planning + reminder`
   - trade plan draft、approval、reminder draft
6. `api/cli/web`
   - SSE、HITL response、card renderer、恢复逻辑

## 当前已知结构风险

- `supervisor.graph` 目前只验证 compile 和最小路由，不代表最终 node 边界已经冻结。
- capability 的 public contract 仍偏 generic，第二阶段可能需要为关键 artifact 升级成更强类型模型。
- Web 目前只有 feature shell 和类型占位，尚未体现 card catalog / reducer / renderer 的最终约束。
- 目前没有真实 `ToolGateway` registry/policy/HITL gate，只是 fake 占位。
