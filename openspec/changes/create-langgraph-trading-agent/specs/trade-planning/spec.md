## ADDED Requirements

### Requirement: 关联证据的交易计划草稿
系统 MUST 仅为美国交易所上市的规范证券以 `draft` 状态创建交易计划，内容包含方向或逻辑、时间周期、入场条件、失效或止损条件、目标、仓位备注、风险和 source reference。缺失的风险关键字段必须明确显示为未完成，不得由 model 猜测。

#### Scenario: 从研究结果创建计划草稿
- **WHEN** 用户要求将研究或扫描 artifact 转为交易计划
- **THEN** 系统预填可追溯字段、标识缺失字段，并且仅保存与 source artifact 关联的草稿

#### Scenario: 拒绝非美股交易计划
- **WHEN** 用户直接请求为非美国交易所上市证券创建计划
- **THEN** 系统返回 `unsupported_market` 且不创建草稿

### Requirement: 渐进式交易计划交互
系统 MUST 通过 HITL Choice、Form 和 Approval Card 补齐并确认交易计划信息。系统只能询问真实歧义或 capability validator 判定缺失的字段；多个相关缺失字段必须尽量合并到一张 Form Card，高风险最终写入必须使用独立 Approval Card。

#### Scenario: 用户表达新增一个交易
- **WHEN** 系统无法判断“新增一个交易”表示创建交易计划、记录已经发生的交易还是请求真实下单
- **THEN** 系统创建 Choice Card 澄清意图；首版仅允许继续创建交易计划，手工成交记录和真实下单返回明确的 unsupported Notice Card

#### Scenario: 用户表达买入股票但没有代码
- **WHEN** 用户表示想买股票但未提供可唯一解析的美股证券
- **THEN** 系统先创建 symbol Choice/Form Card，不创建计划，也不猜测证券

#### Scenario: 为明确证券补齐计划字段
- **WHEN** 用户表示想买入 NVDA，但缺少 horizon、entry、invalidation/stop、target 或 risk/size
- **THEN** 系统说明不能执行 broker 下单，并创建一张包含全部相关缺失字段的 Form Card，已确认字段预填且只读或可明确编辑

#### Scenario: 预览并确认计划
- **WHEN** Form Card 响应通过 capability validation
- **THEN** 系统生成 Approval Card，区分用户输入、确定性或模型建议、source、风险和缺失项，并只提供 confirm、edit 和 cancel 语义 action

#### Scenario: 修改审批卡片
- **WHEN** 用户在 Approval Card 选择 edit 并修改计划字段
- **THEN** 系统将旧卡片标记为 superseded，创建新 draft/version 并返回新的 Form 或 Approval Card，不执行旧 payload

#### Scenario: 确认后返回计划卡片
- **WHEN** owner 确认最新且完整的 Approval Card
- **THEN** 系统幂等创建或激活对应 TradingPlan，解决 HITL interaction，并返回只读 `artifact.trade_plan` Card，可继续提出创建 reminder 的独立交互

### Requirement: 明确批准后激活计划
系统 MUST 在用户批准规范化 plan payload 后，才允许计划从 `draft` 转为 `active`。激活动作必须幂等，并记录为 audit event。

#### Scenario: 激活完整草稿
- **WHEN** owner 批准一个完整的交易计划草稿
- **THEN** 计划只转换一次到 `active`，并记录批准者及其批准的 payload 版本

#### Scenario: 阻止激活不完整草稿
- **WHEN** 在必需的失效条件或时间周期字段缺失时尝试批准
- **THEN** 系统保持计划为 `draft` 并报告缺失字段

### Requirement: 经过确认的提醒规则
系统 MUST 支持与交易计划关联的价格阈值、定时复核和计划失效提醒规则。规则在用户批准其规范化 condition 和 notification channel 前必须保持 `draft` 或 `disabled`。

#### Scenario: 触发价格提醒
- **WHEN** 新鲜的 provider observation 按照 crossing 和 cooldown 语义满足活跃价格规则
- **THEN** 系统记录一个 trigger event 并尝试通知，且不声称交易已经发生

#### Scenario: 避免重复通知
- **WHEN** 在配置的 cooldown 内，连续 observation 始终处于同一个已触发条件
- **THEN** 系统不创建重复 trigger event 或通知

### Requirement: 受控的计划生命周期
系统 MUST 通过经过校验的状态迁移支持 `draft`、`active`、`triggered`、`cancelled`、`expired` 和 `reviewed` 状态。每次迁移必须记录 actor、时间、原因和原状态。

#### Scenario: 拒绝无效状态迁移
- **WHEN** 操作请求从当前状态执行不允许的迁移
- **THEN** 系统拒绝请求、不修改计划，并且不记录虚假的成功结果

### Requirement: 复盘反馈
系统 MUST 允许用户将计划或关联扫描结果复盘为有用、误报、漏报、已执行、已忽略或其他带注释结果，并保留与确切 strategy 和 evidence 版本的关联。

#### Scenario: 将复盘反馈用于策略改进
- **WHEN** 用户记录一个由扫描结果创建的计划反馈
- **THEN** 该复盘可作为未来策略草稿的输入，且不修改历史策略版本

### Requirement: 不提供 broker 执行
本次变更不得暴露下单、撤单、成交、账户余额或 broker 同步的 tool 或状态迁移。系统 MUST 清晰区分计划与提醒状态和 broker 或市场执行状态。

#### Scenario: 用户将已触发计划理解为成交
- **WHEN** 计划或提醒条件被触发
- **THEN** 系统只报告观察到的条件，并明确避免将其表述为已执行或已成交订单
