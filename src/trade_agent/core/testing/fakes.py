"""Small fakes used before concrete adapters exist."""

from collections.abc import AsyncIterator

from trade_agent.core.llm import LLMRequest, LLMResponse
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
