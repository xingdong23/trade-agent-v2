"""证券解析、证券研究与主题研究的薄只读 tool adapters。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolManifest, ToolProtocol, ToolRequest, ToolResult
from trade_agent.core.tools.identity import bind_trusted_identity, identity_fields_for_manifest


class MarketResearchToolApplication(Protocol):
    """研究 Tool 调用的应用层门面协议。

    Contract:
        - 所有结果必须是通过 capability 校验的结构化 JSON，Tool 不补业务字段。
        - 实现方负责 owner 隔离、provider 调用和 evidence 来源校验。

    Implemented by:
        组合根注册的市场研究应用门面与 ``FakeResearchApplication`` 测试实现。
    """

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
class ResolveSecurityTool(ToolProtocol):
    """证券解析 Tool 的只读适配器。

    Attributes:
        application: 注入的研究应用服务，用于执行证券解析用例。

    Invariants:
        - Tool 自身不承载业务规则，只做身份绑定、参数校验和委托调用。
    """

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
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _validate(request, self.manifest)
        return ToolResult("resolved", await self.application.resolve_security(request.arguments))


@dataclass(frozen=True, slots=True)
class ResearchSecurityTool(ToolProtocol):
    """证券研究 Tool 的只读适配器。

    Attributes:
        application: 注入的研究应用服务，用于执行单证券研究用例。

    Invariants:
        - Tool 自身不承载业务规则，只做身份绑定、参数校验和委托调用。
    """

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
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        _validate(request, self.manifest)
        return ToolResult("available", await self.application.research_security(request.arguments))


@dataclass(frozen=True, slots=True)
class ResearchThemeTool(ToolProtocol):
    """主题研究 Tool 的只读适配器。

    Attributes:
        application: 注入的研究应用服务，用于执行主题研究用例。

    Invariants:
        - Tool 自身不承载业务规则，只做身份绑定、参数校验和委托调用。
    """

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
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
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
