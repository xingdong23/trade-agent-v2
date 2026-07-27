## ADDED Requirements

### Requirement: 持久化且用户隔离的会话
系统 MUST 将每个会话作为具有类型化状态、持久化 checkpoint 和已认证用户所有者的 LangGraph thread 执行。用户不得读取、恢复或修改其他用户拥有的 thread。

#### Scenario: 恢复中断的会话
- **WHEN** 已认证用户使用 thread 标识符恢复此前中断的会话
- **THEN** 系统恢复最近一次已提交的 checkpoint，并在不重复执行已完成副作用的前提下继续运行

#### Scenario: 拒绝跨用户访问
- **WHEN** 用户请求访问其他用户拥有的 thread
- **THEN** 系统拒绝请求，且不泄露 thread 状态或存在性细节

### Requirement: 类型化意图路由与工具契约
系统 MUST 通过具有输入输出校验 schema 的显式 graph node 和 tool 路由受支持的请求。对于不支持或存在歧义的请求，系统 MUST 发起澄清，不得虚构动作。

#### Scenario: 路由受支持的研究请求
- **WHEN** 用户请求分析一个可识别的证券
- **THEN** graph 使用规范化且通过 schema 校验的输入，将请求路由到市场研究 workflow

#### Scenario: 澄清有歧义的证券代码
- **WHEN** 证券解析返回多个合理候选项
- **THEN** graph 在获取或呈现特定证券结论前中断，并要求用户选择

### Requirement: 流式进度与结构化结果
系统 MUST 按顺序流式输出 graph 进度、assistant 文本、tool 状态、HITL 请求、结构化 artifact、完成和失败事件。客户端重连后必须能从先前已接收的 event cursor 继续。

#### Scenario: 重连正在运行的任务
- **WHEN** 客户端携带最后确认的 event cursor 重新连接
- **THEN** 系统按顺序发送后续持久化事件，且不重复发送已确认事件

### Requirement: 持久化 HITL 人机交互
系统 MUST 通过独立 HITL module 持久化歧义澄清、审批、人工复核、修订和异常处理交互。每个交互必须包含 owner、thread/run、交互类型、subject reference/version、规范化 payload/hash、允许的 response schema、状态、截止时间、响应 actor 和最终结果。

#### Scenario: 创建歧义澄清交互
- **WHEN** graph 无法唯一解析证券或关键业务字段
- **THEN** HITL module 创建 `clarification` 待办，graph 使用其 `interaction_id` 进入 interrupt，且不继续执行依赖该字段的 node

#### Scenario: 从其他客户端完成待办
- **WHEN** owner 在不同客户端查询并提交一个仍为 `pending` 的 HITL 待办响应
- **THEN** 系统持久化通过 schema 校验的响应、将待办转换为 `resolved`，并从对应 checkpoint 恢复 graph

#### Scenario: 拒绝过期对象上的响应
- **WHEN** HITL 响应引用的 subject version 或 payload hash 与当前待办不一致
- **THEN** 系统拒绝该响应且不恢复 graph，并要求基于最新对象重新创建交互

#### Scenario: HITL 待办超时
- **WHEN** 待办超过 deadline 仍未被处理
- **THEN** 系统将其转换为 `expired`，不得自动批准，并按照 policy 取消流程、采用显式降级路径或保留异常状态

### Requirement: 版本化前端卡片协议
系统 MUST 使用 CardCatalog 白名单中的 `kind + schema_version`，将 HITL interaction、领域 artifact 和后台 job 投影为版本化 `CardEnvelope`。每张卡片必须包含稳定 `card_id`、单调递增 revision、source id/version、服务端状态、通过 schema 校验的数据、允许的语义 action、payload hash、过期时间和 text fallback。LLM 不得直接决定 card kind、field、action、校验约束或生成可执行 UI。

#### Scenario: 推送 HITL 表单卡片
- **WHEN** capability validator 发现多个相关必填字段缺失并创建 HITL form interaction
- **THEN** 系统通过 `card.created` 事件发送由 CardCatalog 校验的 Form Card，字段和 action 与该 interaction 的 response schema 一致

#### Scenario: 原位更新卡片
- **WHEN** 字段校验失败或 subject version 变化导致同一交互产生新投影
- **THEN** 系统使用相同 `card_id` 和更高 revision 发送 `card.updated` 或 `card.superseded`，客户端不得并列展示两个仍可提交的版本

#### Scenario: 客户端刷新后恢复卡片
- **WHEN** Web 客户端刷新或携带 event cursor 重新连接
- **THEN** 系统通过 pending HITL、artifact/job query 和后续 SSE event 重建最新卡片状态，不依赖客户端原有内存

#### Scenario: 客户端不支持卡片版本
- **WHEN** 客户端收到 CardCatalog 中存在但本地 renderer 不支持的 `kind + schema_version`
- **THEN** 客户端安全显示 text fallback、禁用未知 action，并允许用户刷新或升级，不执行任意 payload

#### Scenario: LLM 建议任意前端组件
- **WHEN** LLM 输出未注册 card kind、HTML、脚本、组件名或 action
- **THEN** presenter 拒绝该输出，并只使用 capability 与 CardCatalog 定义的投影或 text fallback

### Requirement: 卡片响应的并发与幂等控制
系统 MUST 通过 HITL response endpoint 校验 owner、interaction 状态与版本、subject version、payload hash、field schema 和 idempotency key。已 resolved、expired、cancelled 或 superseded 的卡片不得再次触发业务 command。

#### Scenario: 重复点击确认
- **WHEN** 客户端因重复点击或网络重试提交相同 idempotency key
- **THEN** 系统只解决 interaction 并执行一次 command，后续请求返回已提交结果

#### Scenario: 提交旧卡片
- **WHEN** 客户端提交低于当前 revision 或 payload hash 不匹配的卡片响应
- **THEN** 系统拒绝响应、返回最新 card revision，且不恢复 graph 或执行 command

### Requirement: 策略驱动的人工审批
系统 MUST 使用服务端 action policy 评估每个拟执行的 tool call。被归类为需要审批的动作必须通过 HITL module 创建 `approval` 交互并在 LangGraph interrupt 处暂停，并且仅在已认证用户批准完全一致的规范化 payload 后执行。

#### Scenario: 批准启用提醒
- **WHEN** graph 提议启用提醒，且用户批准所展示的规则
- **THEN** 系统从 interrupt 恢复，并且仅执行一次该规则的启用动作

#### Scenario: 拒绝或修改审批内容
- **WHEN** 用户拒绝或修改待审批动作
- **THEN** 系统不执行原始动作，并在使用修订后的上下文继续前记录用户决定

### Requirement: 幂等恢复与有界执行
系统 MUST 分配稳定的 run 和 action 标识符，执行 node 超时和重试策略，并确保有副作用的操作具备幂等性。重试耗尽后必须产生可检查的失败状态，不得静默重启 workflow。

#### Scenario: 瞬时 provider 故障后重试
- **WHEN** 可重试的 provider 调用暂时失败，随后在重试预算内成功
- **THEN** node 仅完成一次，且下游状态只包含一个逻辑结果

#### Scenario: 进程终止后恢复
- **WHEN** 进程在提交 action 后、推进 graph checkpoint 前终止
- **THEN** 恢复过程识别 action 标识符并复用已提交结果，而不是再次执行该 action

### Requirement: 可审计且脱敏的执行过程
系统 MUST 使用关联标识符记录 graph 状态迁移、model 与 tool 调用、延迟、token 用量、provider 引用、审批决定和最终结果。日志与 trace 必须对密钥及配置指定的敏感用户数据进行脱敏。

#### Scenario: 检查已完成的 run
- **WHEN** 获授权的运维人员检查某个 run 标识符
- **THEN** 系统提供有序执行 trace 和结果，且不暴露凭据或原始密钥值

### Requirement: 通过 LiteLLM 统一访问大模型
系统 MUST 通过供应商无关的 `LLMClient` port 与 LiteLLM adapter 调用大模型。Graph、Agent 与 capability 不得直接依赖模型厂商 SDK 或硬编码厂商 model name。逻辑 model route 必须通过类型化配置映射到获批准的 LiteLLM model、参数、timeout、预算和 fallback policy，并对响应、usage 与错误进行统一归一化。

#### Scenario: 使用逻辑模型路由
- **WHEN** Research Agent 请求 `research_summarizer` route 生成结构化摘要
- **THEN** 注入的 LiteLLM adapter 根据配置选择获批准模型，返回统一 `LLMResponse`，且 Agent 不感知厂商 SDK 或凭据

#### Scenario: 模型调用临时失败
- **WHEN** 当前 provider 返回允许重试的 timeout 或 rate limit 错误
- **THEN** adapter 只在配置的预算内重试或切换到明确批准且能力兼容的 fallback，并记录 route、重试与最终结果

#### Scenario: 不允许的隐式 fallback
- **WHEN** 当前模型失败且没有满足数据驻留、结构化输出或预算约束的已批准 fallback
- **THEN** 系统返回统一的模型不可用错误，不得静默调用其他 provider 或让 LLM 之外的组件伪造结果

#### Scenario: 模型返回 tool call
- **WHEN** LiteLLM 返回模型请求调用某个 tool
- **THEN** 系统先将其转换并校验为内部 tool schema，再交由 ToolGateway 白名单、policy 与 HITL 处理，LiteLLM adapter 不直接执行业务 tool

### Requirement: 架构确认前只实施骨架
系统实施 MUST 分为架构骨架和具体实现两个阶段。第一阶段只能建立模块树、公开 contract、fake adapter、composition root、最小 graph、空应用入口、架构测试与评审材料；在用户明确确认架构之前，不得实现第二阶段的数据库 schema、真实外部调用、量化 pipeline 或完整业务 workflow。

#### Scenario: 第一阶段骨架完成
- **WHEN** package 可安装导入、最小入口可启动、fake graph 可编译且架构依赖测试通过
- **THEN** 实施过程输出模块树、接口与装配关系、stub 清单和后续切片建议，然后停止并等待用户确认

#### Scenario: 尚未收到架构确认
- **WHEN** 第一阶段已完成但用户尚未明确批准模块边界
- **THEN** 第二阶段任务保持未开始，实施过程不得以骨架可运行为理由继续填充业务细节
