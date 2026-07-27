"""可替换的自然语言意图分类协议。

中台运行时只消费结构化 ``IntentClassification``，不认识“买”“研究”等具体短语。
关键词、模型提示词和租户自定义规则属于协议实现，可在组合根中替换。
"""

from dataclasses import dataclass
from typing import Protocol

from .contracts import Intent, RouteIntent


@dataclass(frozen=True, slots=True)
class IntentClassification:
    """一次用户消息的结构化分类结果。

    Attributes:
        intent: Supervisor 使用的顶层 Agent 意图或扩展 Agent ID。
        journey_id: JourneyRegistry 中注册的业务旅程标识；为空表示需要澄清。
        confidence: 分类置信度，取值范围为 0 到 1。
        entities: 分类阶段提取的稳定实体，例如标准化美股代码。
        reason_code: 可审计原因码，不保存模型的自由文本推理过程。

    Invariants:
        - ``confidence`` 必须位于闭区间 ``[0, 1]``。
        - 只有已注册的 ``journey_id`` 才能在运行时执行。
    """

    intent: RouteIntent
    journey_id: str | None
    confidence: float
    reason_code: str
    entities: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("intent confidence 必须位于 0 到 1")
        if not self.reason_code.strip():
            raise ValueError("intent reason_code 不能为空")

    def entity(self, name: str) -> str | None:
        """读取分类器提取的单个实体。

        Args:
            name: 分类协议中的实体名称。

        Returns:
            对应字符串值；实体不存在时返回 ``None``。
        """

        return dict(self.entities).get(name)


class IntentClassifier(Protocol):
    """将自然语言转换为中台可执行的结构化路由决定。

    Contract:
        - 实现必须返回版本稳定的 ``journey_id``，不能返回任意 Python 调用目标。
        - 无法可靠分类时必须返回 ``journey_id=None``，由 HITL 负责澄清。
        - 分类器不得执行 Tool、写数据库或产生交易副作用。

    Implemented by:
        生产环境的结构化 LLM adapter、规则引擎或测试中的确定性 fake。
    """

    def classify(self, *, message: str, owner_id: str) -> IntentClassification:
        """分类一条用户消息。

        Args:
            message: 未经业务解释的用户原始文本。
            owner_id: 已认证用户标识，供租户级路由配置使用。

        Returns:
            通过本地 schema 校验的结构化分类结果。

        Raises:
            RuntimeError: 分类 provider 不可用且没有安全降级结果。
        """

        ...


class ClarificationIntentClassifier:
    """在没有配置分类 provider 时使用的安全降级分类器。

    Contract:
        - 永远不猜测用户意图，也不选择可执行业务旅程。
        - 调用方应把结果投影为澄清或 unsupported Card。

    Implemented by:
        组合根在没有真实分类 adapter 时直接实例化本类。
    """

    def classify(self, *, message: str, owner_id: str) -> IntentClassification:
        """返回低置信度 clarification 结果。

        Args:
            message: 用户原始消息；安全降级不会解释其内容。
            owner_id: 已认证用户；安全降级不会读取租户配置。

        Returns:
            ``journey_id=None`` 的 clarification 分类。
        """

        del message, owner_id
        return IntentClassification(
            Intent.CLARIFICATION,
            None,
            0.0,
            reason_code="classifier_not_configured",
        )


__all__ = ["ClarificationIntentClassifier", "IntentClassification", "IntentClassifier"]
