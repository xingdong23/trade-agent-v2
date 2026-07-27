# Python 中文 Docstring 规范

本项目作为课程源码，公共模型和协议的注释必须能独立解释契约，不能只重复类名。
代码仍以类型标注为事实来源，docstring 负责说明语义、边界和使用约束。

## 模型实体

`dataclass`、Pydantic model 和 `TypedDict` 统一使用以下格式：

```python
@dataclass(frozen=True, slots=True)
class ExampleModel:
    """一句话说明该模型代表的业务概念。

    Attributes:
        field_name: 字段的业务含义、单位，以及是否允许为空。

    Invariants:
        - 对象创建后始终成立的约束。
        - 字段之间的关系或版本规则。
    """
```

简单值对象可以省略 `Invariants`，但不能省略 `Attributes`。字段名称必须与代码一致。

## Protocol 协议

`Protocol` 是实现方必须遵守的行为合同，统一使用以下格式：

```python
class ExamplePort(Protocol):
    """一句话说明调用方为什么需要这个协议。

    Contract:
        - 实现方必须保证的行为。
        - 幂等、权限、顺序或失败语义。

    Implemented by:
        典型生产 adapter 与测试 fake 的位置。
    """

    def load(self, key: str) -> Value:
        """读取一个值。

        Args:
            key: 稳定业务标识，不是展示名称。

        Returns:
            找到的不可变值。

        Raises:
            LookupError: 标识不存在或调用方不可见。
        """
```

## 公共函数与服务方法

公共函数、application service 和有跨层语义的方法使用：

```text
一句话摘要。

Args:
    参数名: 业务语义。

Returns:
    返回值及状态含义。

Raises:
    异常: 触发条件。

Side Effects:
    写入哪些 repository、发布哪些事件或调用哪些 provider。
```

没有对应内容的章节可以省略，但摘要、参数语义和重要异常不能省略。

## 注释边界

- 解释“为什么这样设计”和“不这样做会破坏什么”，不逐行翻译代码。
- 自然语言分类、提示词和用户文案不得散落在 `if/elif` 中，应通过可替换协议或配置注入。
- 控制流不得解析异常消息或展示文案；错误分支必须依赖类型化异常和稳定错误码。
- 通用 runtime 不得枚举业务 journey 或 HITL `subject_type`；这些契约由 Journey 插件注册。
- 稳定协议 ID、Card kind、领域状态枚举不是“硬编码业务判断”，它们属于版本化契约。
- 中文标点已列入 Ruff `allowed-confusables`，仅用于注释和字符串；Python 标识符仍使用 ASCII。
