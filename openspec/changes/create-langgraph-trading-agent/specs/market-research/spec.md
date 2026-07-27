## ADDED Requirements

### Requirement: 首版仅支持美股
系统 MUST 只对配置允许的美国交易所上市证券提供解析、研究、watchlist 候选和下游计划能力。非美国交易所证券必须返回明确的 `unsupported_market` 结果，不得使用近似 ticker 或同名公司替代。

#### Scenario: 分析美国交易所上市证券
- **WHEN** 用户请求分析一个可由 provider 识别的美国交易所上市证券
- **THEN** 系统使用美股交易日历、市场时段、公司行动和规范证券标识符继续研究

#### Scenario: 拒绝非美股标的
- **WHEN** 用户请求分析中国内地、香港或其他非美国交易所上市证券
- **THEN** 系统返回 `unsupported_market`，说明首版仅支持美股，且不创建研究或交易计划 artifact

### Requirement: 规范化证券解析
系统 MUST 在执行证券专项分析前，将公司名称或类似 ticker 的输入解析为包含美国市场、交易所、代码和展示名称的规范证券标识符。

#### Scenario: 解析唯一证券代码
- **WHEN** 用户请求分析一个能够唯一识别的上市证券
- **THEN** 研究记录在所有 provider 请求和已存储 evidence 中使用该证券的规范标识符

#### Scenario: 不猜测无法解析的证券
- **WHEN** 系统无法可靠解析出规范证券
- **THEN** 系统报告解析失败并请求澄清，不得编造市场数据

### Requirement: 有来源且包含时效性的证据
系统 MUST 为每条行情、基本面、公告、新闻和网页事实记录 provider 身份、source reference、观测或发布时间、获取时间及时效状态。对于互相冲突的 evidence，系统 MUST 保留冲突，不得静默选择缺乏依据的值。

#### Scenario: 展示时效敏感的行情证据
- **WHEN** 报告使用市场报价
- **THEN** 报告展示报价时间戳、provider、市场时段上下文，以及是否满足配置的时效阈值

#### Scenario: 发现 provider 数值冲突
- **WHEN** 可信 provider 对同一事实和期间返回存在实质差异的数值
- **THEN** 报告标识冲突，并降低受影响结论的置信度或不输出该结论

### Requirement: 证据支持的证券分析
系统 MUST 输出结构化证券分析，涵盖可获得的价量背景、技术关键位、业务或基本面背景、催化因素、风险、关键假设、信息缺口、置信度和失效条件。重要主张必须引用 supporting evidence 标识符。

#### Scenario: 完成证券分析
- **WHEN** 请求的证券与时间范围具有足够的新鲜 evidence
- **THEN** 系统返回分析 artifact，其中重要主张关联 evidence，行动导向的结论包含风险与失效条件

#### Scenario: evidence 不完整时继续分析
- **WHEN** 部分请求的 evidence 不可用，但部分分析仍有价值
- **THEN** 系统标识缺失输入、限制受影响结论，且不暗示系统已检查不可用数据

### Requirement: 行业与主题研究
系统 MUST 将用户指定的行业、主题或公司产业链研究为结构化角色、美国交易所上市候选证券、supporting source、护城河假设和风险。未经单独且明确的用户动作，不得将候选证券加入 watchlist。

#### Scenario: 生成产业链 artifact
- **WHEN** 用户询问某个主题中的上市公司参与者
- **THEN** 系统按角色分组返回带来源和不确定性的候选公司，并支持提出将选中候选项导入 watchlist 的建议

### Requirement: 研究安全边界
系统 MUST 将研究呈现为决策辅助，不得承诺收益，也不得声称已完成下单、账户变更或 broker 同步。provider 故障必须显示为数据不可用，不得以 model 生成的事实替代。

#### Scenario: 用户请求下单
- **WHEN** 用户要求研究 agent 下单、撤单或修改交易
- **THEN** 系统说明执行能力不可用，并可改为建议创建交易计划草稿

#### Scenario: 必需 provider 不可用
- **WHEN** 必需的数据 provider 无法返回有效响应
- **THEN** 系统记录故障，并在结论中排除该数据
