# 交付验证记录

## 验证范围

本记录对应 OpenSpec 变更 `create-langgraph-trading-agent`。默认验证只运行非 live
测试，覆盖模块依赖、SQLite、HITL/Card/SSE、量化模型、API/CLI/Web 和安全边界。

## 2026-07-28 验证结果

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| Python 格式 | `uv run ruff format --check .` | 225 个文件格式正确 |
| Python lint | `uv run ruff check .` | 通过 |
| 严格类型检查 | `uv run mypy` | 203 个源文件无错误 |
| 完整非 live 测试 | `uv run pytest` | 250 项通过，1 个第三方弃用警告 |
| Web lint | `npm run lint` | 通过 |
| Web 类型检查 | `npm run typecheck` | 通过 |
| Web 生产构建 | `npm run build` | 通过，41 个模块完成转换 |
| OpenSpec 校验 | `openspec validate --all --strict --no-interactive` | 1 项通过，0 个问题 |

测试覆盖的关键门禁包括：

- SQLite repository owner 隔离、乐观锁、WAL、foreign key、短事务、写入串行、
  lock wait、checkpoint 恢复、原子 job claim、backup/restore 和并发幂等。
- point-in-time 数据泄漏、训练与 inference feature parity、LightGBM 可复现、
  walk-forward、model registry 审批、inference lineage、drift policy 和 LLM 隔离。
- thread/资源跨用户拒绝、OIDC 身份边界、HITL payload/revision 防篡改、重复响应、
  超时与取消、日志脱敏、非美股拒绝、provider 故障降级和 broker 能力结构性缺失。
- “新增一个交易”和“我要买 NVDA”的真实 conversation -> HITL -> Card -> TradingPlan
  纵切面，包括字段错误、edit/supersede、幂等 confirm、刷新恢复和 SSE 生命周期。
- 单一 conversation run 驱动“美股歧义澄清 -> citation 研究 -> 冻结 watchlist ->
  专用模型扫描 -> Progress/Review Card -> LLM 总结持久化结果 -> 计划/提醒审批 ->
  Artifact Card -> 复盘”，并通过 API、CLI 待办和 SSE 验证连续恢复。
- 类型化部署配置覆盖 checkpoint namespace、OIDC/JWKS、LiteLLM endpoint、worker、
  Agent Tool allowlist、Planning/Research-to-plan 字段与文案、提醒渠道和复盘目录；
  未注册 Agent/Worker/Workflow 均 fail closed。
- Web 使用动态 thread ID、单一 API base URL 和 owner 隔离的 conversation snapshot
  恢复消息、Card、pending HITL 与资源，不依赖固定 thread 或猜测式 endpoint。
- 会话入口只消费 Supervisor Graph 的 `selected_agent_id`，并与注册 Workflow 的
  `workflow_id + agent_id` 联合校验；Graph 之后不存在第二套业务路由。
- 全仓公共 dataclass、TypedDict、Pydantic model、枚举与 Protocol 通过严格中文
  Docstring 门禁；核心 Interface/Implementation 保留显式继承供 IDE 导航。

## Migration smoke test

使用独立临时目录顺序执行：

```bash
uv run trade-agent-db --database <temp>/source.db init
uv run trade-agent-db --database <temp>/source.db backup <temp>/backup.db
uv run trade-agent-db --database <temp>/restored.db restore <temp>/backup.db
uv run trade-agent-db --database <temp>/restored.db health
```

源库与恢复库均返回 `integrity=ok`、`journal_mode=wal`、`foreign_keys=True`、
`schema_version=3`。恢复库 health 的 lock wait 为 0 秒。

## 尚未覆盖的 live 风险

- 尚未连接真实美股行情、公司行动、SEC/基本面、新闻和通知 provider；fake provider
  的错误映射不能替代真实供应商授权、限流、数据时效和网络抖动验证。
- LiteLLM adapter 已完成 mock contract test，但未验证真实 provider 的费用、限流、
  fallback、内容安全和数据驻留行为。
- OIDC 已验证本地 token verifier contract，未覆盖真实 discovery/JWKS 轮换和网络故障。
- 当前 SQLite 边界是单机、单 worker；未验证多进程写入吞吐，也不承诺多节点运行。
