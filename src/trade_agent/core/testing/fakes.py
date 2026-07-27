"""测试专用的确定性 LLM、Tool 与意图分类替身。"""

from collections.abc import AsyncIterator

from trade_agent.core.llm import LLMRequest, LLMResponse
from trade_agent.core.runtime import Intent, IntentClassification
from trade_agent.core.tools import ToolRequest, ToolResult


class FakeLLMClient:
    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self._responses = list(responses or [LLMResponse(content="scaffold")])
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("FakeLLMClient 没有剩余响应")
        return self._responses.pop(0)

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        response = await self.complete(request)
        yield response.content


class FakeToolGateway:
    async def invoke(self, request: ToolRequest) -> ToolResult:
        return ToolResult(status="scaffold", payload={"tool_id": request.tool_id})


class MappingIntentClassifier:
    """用测试数据声明的精确消息映射模拟意图分类。

    Attributes:
        classifications: 消息文本到结构化分类结果的映射。
        fallback: 找不到精确消息时返回的安全分类结果。

    Invariants:
        - 该 fake 不做关键词包含判断，避免测试复制生产中的自然语言解析逻辑。
        - 未声明消息默认进入 clarification，不会猜测业务旅程。
    """

    def __init__(
        self,
        classifications: dict[str, IntentClassification] | None = None,
        *,
        fallback: IntentClassification | None = None,
    ) -> None:
        self._classifications = dict(classifications or {})
        self._fallback = fallback or IntentClassification(
            Intent.CLARIFICATION,
            None,
            0.0,
            reason_code="test_mapping_miss",
        )

    def classify(self, *, message: str, owner_id: str) -> IntentClassification:
        """返回测试预先声明的分类结果。

        Args:
            message: 用作映射键的完整消息。
            owner_id: 为满足生产协议保留；fake 不解释该值。

        Returns:
            精确匹配结果，或安全的 clarification fallback。
        """

        del owner_id
        return self._classifications.get(message.strip(), self._fallback)
