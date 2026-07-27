# Trade Agent V2

本仓库实现基于 LangGraph 的美股研究与交易计划 Agent。系统使用 SQLite 持久化会话、HITL、领域对象和任务状态；LightGBM/LSTM adapter 负责量化预测，LiteLLM 只负责意图、总结、解释和计划草稿。首版不包含下单、成交、账户余额或 broker 同步能力。

## 开发命令

```bash
uv sync --all-groups --extra llm --extra quant
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pytest tests/architecture

cd web
npm ci
npm run lint
npm run typecheck
npm run build
```

初始化数据库并运行本地入口：

```bash
uv run trade-agent-db init
uv run trade-agent-api
uv run trade-agent run --thread local-thread "研究 NVDA"
uv run trade-agent-worker
```

架构评审见 `docs/architecture-review.md`，运行与故障恢复见 `docs/operations.md`，
当前交付验证证据见 `docs/verification.md`。
