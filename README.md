# Trade Agent V2

本仓库实现基于 LangGraph 的美股研究与交易计划 Agent。系统使用 SQLite 持久化会话、HITL、领域对象和任务状态；LightGBM/LSTM adapter 负责量化预测，LiteLLM 只负责意图、总结、解释和计划草稿。首版不包含下单、成交、账户余额或 broker 同步能力。

## 源码阅读

第一次阅读项目时，先看 [`docs/course-guide.md`](docs/course-guide.md)。该导读从一次用户
请求出发，解释 `core`、`agents`、`capabilities`、`adapters`、`apps` 的职责，以及
Tool、Card、Event 和 HITL 如何共同工作。`docs/architecture-review.md` 是早期骨架阶段
的历史评审记录，不代表当前完成度。

新增 Python 模型或协议前，请遵守 [`docs/docstring-standard.md`](docs/docstring-standard.md)
中的中文 docstring 结构；关键契约由架构测试强制检查。

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

课程源码导读见 `docs/course-guide.md`，早期架构评审见 `docs/architecture-review.md`，
运行与故障恢复见 `docs/operations.md`，当前交付验证证据见 `docs/verification.md`。

## PyCharm 打开项目时出现红线 / 无法跳转怎么办

如果你看到 `from trade_agent...` 大片红线，但命令行 `uv run python` 可以正常导入，
通常不是代码坏了，而是 IDE 没有正确识别项目环境。

请按下面顺序检查：

1. 解释器要指向项目里的 `.venv`
   - `PyCharm -> Settings -> Python Interpreter`
   - 选择 `/Users/xingdong/workspace/trade-agent-v2/.venv/bin/python`
2. 把 `src/` 标记为 Sources Root
   - 右键 `src` 目录
   - 选择 `Mark Directory as -> Sources Root`
3. 如果还是不跳转，重新同步依赖

```bash
uv sync --all-groups --extra llm --extra quant
```

4. 然后在 PyCharm 执行
   - `File -> Invalidate Caches / Restart`

本仓库采用 `src/` 布局；如果 IDE 没把 `src` 当源码根，`trade_agent` 包就会被误判为
“找不到”，于是导入符号、跳转和自动补全都会一起失效。
