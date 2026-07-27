## ADDED Requirements

### Requirement: LLM 与量化预测职责隔离
系统的证券评分、收益或价格预测、候选项筛选阈值和结果排序 MUST 来自确定性规则或已注册的专用量化 model，不得由 LLM 生成或修改。LLM 只能将用户意图整理为待确认的策略草稿，并对已有的量化结果、evidence、风险和计划进行总结。

#### Scenario: LLM 总结扫描结果
- **WHEN** 扫描完成后需要生成面向用户的解释
- **THEN** LLM 只能引用持久化的 model output 与 evidence，不得新增、覆盖或重新计算分数、预测值和排序

#### Scenario: 没有可用量化 model
- **WHEN** 请求的市场、时间周期或 target 没有已批准的专用量化 model
- **THEN** 系统返回预测能力不可用，不得改用 LLM 估算数值

### Requirement: 时间点一致的数据集与特征
系统 MUST 使用 point-in-time correct 的美股行情、公司行动、基本面和可选替代数据构建训练与 inference feature，记录 data snapshot、feature definition version、目标定义和美国交易日历。任何 feature 不得使用预测时点之后才可获得的信息。

#### Scenario: 构建训练样本
- **WHEN** 系统为美股、预测周期和 target 构建训练集
- **THEN** 每个样本只包含该决策时点可获得的数据，并记录可复现的数据与 feature 版本

#### Scenario: 发现未来数据泄漏
- **WHEN** feature validation 发现时间穿越、错误复权或训练与 inference 计算不一致
- **THEN** 系统阻止训练或发布，并记录具体违规 feature 和时间范围

### Requirement: 版本化专用量化 model
系统 MUST 通过可复现训练 pipeline 产生不可变 model version，记录算法、hyperparameter、训练区间、feature set、target、预测周期、适用的美国交易所证券范围、代码版本、随机种子、data snapshot 和 artifact hash。首个生产基线必须包含确定性规则或 LightGBM；LSTM 等序列 model 只有在相同样本外协议下证明增益后才能获准发布。

#### Scenario: 训练 LightGBM 基线
- **WHEN** 训练任务具有通过校验的数据集、feature set、target 和时间切分
- **THEN** 系统生成带完整 lineage 和评测结果的候选 model version，但不会自动将其设为生产版本

#### Scenario: 比较 LSTM 候选模型
- **WHEN** LSTM 候选 model 与当前基线完成相同 walk-forward 评测
- **THEN** 系统只有在预先声明的样本外质量、稳定性、延迟及计入交易成本后的门槛全部满足时才允许其进入审批发布

### Requirement: 避免泄漏的样本外评测与发布门禁
系统 MUST 使用按时间顺序的 train/validation/test 切分和 walk-forward evaluation，并根据 target 记录分类或回归指标、校准度、覆盖率、稳定性、换手率、交易成本后表现以及 benchmark 对比。未经审批或未达到预设门槛的 model version 不得用于生产预测。

#### Scenario: 候选模型未超过基线
- **WHEN** 候选 model 在样本外关键指标或交易成本后指标未达到预设门槛
- **THEN** 系统将其保留为未批准实验，不得路由生产 inference

#### Scenario: 批准生产模型
- **WHEN** 候选 model 通过数据、评测、风险和可复现性检查并由授权用户批准
- **THEN** model registry 将该不可变版本标记为美股、target 与预测周期的可用生产版本

### Requirement: 概率化且有不确定性的预测
系统 MUST 针对明确的 target 与预测周期输出由已注册 model 产生的概率、分位数或区间预测，并包含 prediction time、as-of time、model version、feature snapshot、适用范围和不确定性。系统不得将单一点估计表述为确定结果。

#### Scenario: 生成收益方向概率
- **WHEN** 已批准 model 对具备完整 feature 的证券执行 inference
- **THEN** 系统返回指定周期的 target 定义、方向概率或预测分布、校准信息和完整 model/data lineage

#### Scenario: 输入超出模型适用范围
- **WHEN** 证券、市场状态或 feature 缺失程度超出 model 的已批准适用范围
- **THEN** 系统返回 `unavailable` 或 out-of-distribution 警告，不输出误导性预测

### Requirement: 版本化策略定义
系统 MUST 允许用户创建策略草稿，内容包含名称、交易逻辑、入场与排除条件、时间周期、所需输入、输出评分规则以及可选正反例。发布策略必须在用户明确批准后创建不可变版本。

#### Scenario: 发布策略版本
- **WHEN** 用户批准一个完整的策略草稿
- **THEN** 系统创建不可变版本，同时保留可继续产生后续版本的策略身份

#### Scenario: 保留历史版本
- **WHEN** 用户编辑已发布策略
- **THEN** 既有扫描继续引用原版本，编辑内容保持草稿状态，直到发布为新版本

### Requirement: 冻结且可复现的扫描输入
系统 MUST 仅使用明确的已发布策略版本和只包含美国交易所上市证券的已解析候选集合启动扫描。系统 MUST 持久化冻结的 universe snapshot、请求周期、相关 data snapshot reference、model 与 prompt 标识符及扫描配置。

#### Scenario: 从 watchlist 启动扫描
- **WHEN** 用户使用已发布策略和 watchlist group 启动扫描
- **THEN** 系统在开始评估前冻结该 group 的规范证券代码并记录全部输入

#### Scenario: 拒绝无效扫描请求
- **WHEN** 扫描引用未发布策略、空候选集合或不支持的时间周期
- **THEN** 系统返回校验详情并拒绝请求，不创建后台任务

### Requirement: 可恢复的异步扫描
系统 MUST 将扫描作为持久化后台 job 执行，支持 `queued`、`running`、`completed`、`failed` 和 `cancelled` 状态、可观察进度、单证券有界重试及重启恢复。

#### Scenario: 恢复运行中的扫描
- **WHEN** worker 在扫描仍有未完成证券时重启
- **THEN** 其他 worker 可以 claim 剩余工作，且不重新评估已完成的证券与版本组合

#### Scenario: 取消运行中的扫描
- **WHEN** 用户取消自己拥有的活跃扫描
- **THEN** 系统停止 claim 新的证券任务，保留已完成结果，并将扫描转为 `cancelled`

### Requirement: 可解释的排序扫描结果
系统 MUST 为每个已评估候选项返回状态、由专用量化 model 产生的规范化分数或概率、命中条件、排除项检查、evidence reference、风险、置信度、model version 和数据缺口。排序必须由版本化 ranking function 根据持久化量化结果复现，不得依赖 LLM 文本。

#### Scenario: 检查命中候选项
- **WHEN** 候选项达到策略版本的阈值
- **THEN** 结果解释已命中和未命中的条件，关联评估使用的 evidence、feature snapshot 和 model version

#### Scenario: 记录未命中或评估失败
- **WHEN** 候选项不满足条件或无法可靠评估
- **THEN** 结果记录 `non-match` 或 `unavailable` 状态及原因，不得静默省略该候选项

### Requirement: 扫描来源与复盘关联
系统 MUST 保留每个结果与 scan job、strategy version、universe snapshot、evidence snapshot 及任何下游计划或用户复盘之间的关系。

#### Scenario: 标记误报结果
- **WHEN** 用户将扫描结果复盘为误报
- **THEN** 复盘关联到确切结果和策略版本，并可被提议为未来策略版本的反例

### Requirement: 生产模型监控与降级
系统 MUST 监控生产 model 的输入数据质量、feature drift、prediction drift、校准度、覆盖率、延迟和已有标签回流后的样本外表现。超过配置阈值时，系统 MUST 告警并支持停止路由或降级到已批准基线，不得自动让 LLM 接管预测。

#### Scenario: 检测到显著 feature drift
- **WHEN** 生产输入的 feature distribution 持续超过已批准阈值
- **THEN** 系统记录 drift event、告警并按 policy 停止该 model 或切换到已批准基线
