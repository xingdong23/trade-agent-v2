"""证券解析、证券研究与主题研究的薄只读 tool adapters。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolManifest, ToolRequest, ToolResult


class MarketResearchToolApplication(Protocol):
    async def resolve_security(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...

    async def research_security(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...

    async def research_theme(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...


_OUTPUT: dict[str, JsonValue] = {"type": "object", "additionalProperties": True}


@dataclass(frozen=True, slots=True)
class ResolveSecurityTool:
    application: MarketResearchToolApplication

    manifest = ToolManifest(
        "market_research.resolve_security",
        "将用户输入解析为唯一的美国上市规范证券",
        True,
        False,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["owner_id", "query"],
            "properties": {
                "owner_id": {"type": "string", "minLength": 1},
                "query": {"type": "string", "minLength": 1},
                "market_hint": {"type": ["string", "null"]},
            },
        },
        _OUTPUT,
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        _validate(request, self.manifest)
        return ToolResult("resolved", await self.application.resolve_security(request.arguments))


@dataclass(frozen=True, slots=True)
class ResearchSecurityTool:
    application: MarketResearchToolApplication

    manifest = ToolManifest(
        "market_research.research_security",
        "读取有 citation、时效与数据缺口的美股研究 artifact",
        True,
        False,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["owner_id", "security_id", "as_of"],
            "properties": {
                "owner_id": {"type": "string", "minLength": 1},
                "security_id": {"type": "string", "minLength": 1},
                "as_of": {"type": "string", "format": "date-time"},
            },
        },
        _OUTPUT,
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        _validate(request, self.manifest)
        return ToolResult("available", await self.application.research_security(request.arguments))


@dataclass(frozen=True, slots=True)
class ResearchThemeTool:
    application: MarketResearchToolApplication

    manifest = ToolManifest(
        "market_research.research_theme",
        "读取行业、主题或产业链角色与美股候选研究 artifact",
        True,
        False,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["owner_id", "theme", "as_of"],
            "properties": {
                "owner_id": {"type": "string", "minLength": 1},
                "theme": {"type": "string", "minLength": 1},
                "as_of": {"type": "string", "format": "date-time"},
            },
        },
        _OUTPUT,
        side_effect="none",
        risk="low",
        idempotent=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        _validate(request, self.manifest)
        return ToolResult("available", await self.application.research_theme(request.arguments))


def _validate(request: ToolRequest, manifest: ToolManifest) -> None:
    if request.tool_id != manifest.tool_id:
        raise ValueError("tool id 与 handler 不匹配")
    required = manifest.input_schema.get("required")
    if isinstance(required, list):
        missing = [key for key in required if isinstance(key, str) and key not in request.arguments]
        if missing:
            raise ValueError(f"tool 参数缺少字段: {', '.join(missing)}")


__all__ = [
    "MarketResearchToolApplication",
    "ResearchSecurityTool",
    "ResearchThemeTool",
    "ResolveSecurityTool",
]
