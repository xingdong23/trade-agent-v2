# 运行与运维手册

## 运行边界

- 首版仅支持美国交易所上市证券，单机部署、一个任务 worker、SQLite WAL。
- 量化预测、评分和排序只来自确定性规则或已批准的专用模型，不经过 LiteLLM。
- 系统只创建研究、策略、交易计划和提醒，不提供下单、撤单、成交、余额或 broker 同步。
- 默认测试不访问 live 市场数据或模型 provider；live smoke test 必须单独配置和启用。

## 安装与校验

```bash
uv sync --all-groups --extra llm --extra quant
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run pytest tests/architecture

cd web
npm ci
npm run lint
npm run typecheck
npm run build
```

默认 pytest 配置排除 `live` marker。真实 provider 或真实模型 smoke test 必须标记为
`live`，并在隔离环境中使用 `uv run pytest -o addopts= -m live` 显式执行。

## SQLite 初始化与 migration

默认数据库为 `.data/trade-agent.db`。production 必须通过 `TRADE_AGENT_DATABASE__PATH` 配置绝对路径。

```bash
uv run trade-agent-db init
uv run trade-agent-db health
```

初始化命令创建目录、执行前向 migration、启用 WAL、foreign key 和 `busy_timeout`，并将数据库文件权限设置为 `0600`。应用和 worker 启动前均应检查 health；schema version 落后时先执行 `init` 完成 migration。

## Backup 与 restore

```bash
uv run trade-agent-db backup /安全路径/trade-agent-20260727.db
uv run trade-agent-db restore /安全路径/trade-agent-20260727.db
uv run trade-agent-db health
```

restore 不覆盖已存在的目标文件。恢复后必须执行完整性检查和 migration smoke test，再启动 API/worker。保留原数据库及 WAL 文件，直到恢复结果经过验证。

## 本地进程

```bash
uv run trade-agent-api
uv run trade-agent-worker
uv run trade-agent run --thread local-thread "研究 NVDA"
```

API 默认监听 `127.0.0.1:8000`。开发模式可通过 `X-User-ID` 注入用户；production 必须配置 OIDC issuer 和 audience，不允许使用开发用户。

首版只运行一个 worker process。不要并行启动多个写 worker；SQLite 写入由短事务和进程内协调器控制，provider 请求、feature 计算和模型 inference 不得持有数据库事务。

## HITL 待办

```bash
uv run trade-agent hitl list
uv run trade-agent hitl respond <interaction-id> \
  --version 1 \
  --subject-version 1 \
  --payload-hash <hash> \
  --action confirm \
  --values '{"approved":true}'
uv run trade-agent hitl cancel <interaction-id> --version 1
```

响应会校验 owner、interaction/subject version、payload hash、字段 schema 与幂等键。过期、取消、已解决或旧 revision 不会恢复 graph 或重复执行 command；超时永不自动批准。

## 量化训练、评测与发布

训练 job 必须冻结代码版本、随机种子、data snapshot、feature set、target、时间区间和超参数。候选模型必须完成 point-in-time 泄漏检查、feature parity、purge/embargo、walk-forward、校准、稳定性、成本后 benchmark 与延迟评测。

LightGBM 是首版基线；LSTM 只有在样本外质量、校准、稳定性、成本后指标和延迟均通过门槛时才能申请发布。发布必须通过 model registry 审批 command；未批准或已停用版本不能提供 production inference。当前训练/发布由类型化 application service 和测试入口驱动，尚未提供面向普通用户的 CLI。

## 美股 Provider

provider 配置必须声明授权范围、timeout、重试和 freshness policy。生产环境不得使用 fake provider。行情、K 线、公司行动、SEC/基本面、新闻和搜索响应会保存 source reference、观测/发布时间、抓取时间、授权信息和 raw payload hash。

provider 不可用、冲突或数据过期时，研究结果必须显示 data gap 或失败；不得让 LiteLLM 补写事实、数值或量化预测。

## 故障恢复

1. API/worker 重启后使用 owner-scoped LangGraph checkpoint 恢复 thread。
2. 已提交 command 通过 idempotency store 复用结果，避免 checkpoint 尚未推进时重复副作用。
3. scan unit lease 到期后可由同一单 worker 重领；终态与单证券评估均幂等。
4. SSE 客户端携带最后 event cursor 重连，按 run sequence 恢复后续 card/job 事件。
5. HITL 从 SQLite 查询 pending 状态，跨客户端可继续处理，不依赖浏览器内存。
6. 数据库损坏时停止所有写进程，使用最近通过校验的 backup restore，再运行 health、migration 和非 live 测试。

## 已知非目标

- 非美股市场、多节点部署和 PostgreSQL 双栈。
- broker 下单、撤单、成交确认、账户或投资组合同步。
- 自动交易、收益承诺、完整回测与参数优化。
- 完整 Web 工作台、管理端和移动原生客户端。
- 默认测试中的 live provider、live LiteLLM 或真实通知发送。
