## ADDED Requirements

### Requirement: 经过校验的 watchlist 导入
系统 MUST 接受粘贴或上传的证券代码输入，逐行校验、解析美国交易所上市的规范证券，并在需要审批的批量持久化之前，返回每行的 `accepted`、`ambiguous`、`duplicate`、`unsupported_market` 和 `rejected` 结果。

#### Scenario: 导入有效性混合的证券代码
- **WHEN** 用户提交同时包含有效美股、重复、有歧义、非美股和无效证券代码的列表
- **THEN** 系统分类展示结果，并且只持久化用户已批准的美国交易所上市规范证券

### Requirement: 规范化去重与来源保留
系统 MUST 确保同一 watchlist 内每个规范证券最多有一个 membership，同时保留合并后的标签、备注、source type、source reference 和导入时间戳。

#### Scenario: 再次导入已有证券
- **WHEN** 已批准的导入内容包含目标 watchlist 中已存在的证券
- **THEN** 系统只保留一个 membership，并仅合并通过校验的 metadata，且不丢失既有 provenance

### Requirement: 用户管理的分组与分类
系统 MUST 允许用户创建 group，并通过明确选择或接受分类建议来分配 membership。AI 生成的主题、行业、风险或 workflow 状态分类在用户接受或编辑前必须保持为建议。

#### Scenario: 接受分类建议
- **WHEN** 用户批准选中的分类建议
- **THEN** 系统更新 membership，并同时记录建议来源和用户决定

### Requirement: 稳定的扫描候选集合
系统 MUST 允许将 watchlist 或 group 解析为用于扫描的不可变 universe snapshot。后续 watchlist 修改不得改变既有 snapshot。

#### Scenario: 提交扫描后编辑分组
- **WHEN** 扫描冻结候选集合后 membership 发生变化
- **THEN** 活跃扫描保留原始规范证券集合，后续扫描使用更新后的集合

### Requirement: 租户隔离
系统 MUST 按已认证 owner 隔离 watchlist、group、membership、import 和 universe snapshot，并在 repository query 与 command 中强制执行所有权校验。

#### Scenario: 尝试跨用户访问 watchlist
- **WHEN** 用户引用其他用户的 watchlist 标识符
- **THEN** 系统拒绝操作，且不返回其内容
