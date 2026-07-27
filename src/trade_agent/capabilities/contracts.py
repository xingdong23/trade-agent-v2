"""所有 capability 公共边界共享的命令、查询、结果和协议。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.tools import ToolManifest, ToolRequest, ToolResult


class ConcurrentWriteError(RuntimeError):
    """聚合版本与调用方预期不一致。"""


@dataclass(frozen=True, slots=True)
class CapabilityCommand:
    """提交给 capability application 的通用写命令。

    Attributes:
        command_id: 用于追踪和幂等的稳定命令标识。
        payload: 通过具体 capability schema 校验的参数。
    """

    command_id: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityQuery:
    """提交给 capability application 的通用只读查询。

    Attributes:
        query_id: 查询协议标识。
        parameters: 通过具体 capability schema 校验的参数。
    """

    query_id: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """Capability 跨层返回的版本化结果。

    Attributes:
        reference_id: 结果或聚合稳定标识。
        version: 返回对象的精确版本。
        payload: 可序列化、经过契约校验的数据。
    """

    reference_id: str
    version: int
    payload: Mapping[str, JsonValue] = field(default_factory=dict)


class CapabilityApplication(Protocol):
    """Capability 对 API、worker 与 Tool 暴露的统一应用协议。

    Contract:
        - 写命令必须执行领域不变量和幂等保护。
        - 查询不得改变业务状态。

    Implemented by:
        各 capability 的 ``application`` 子包。
    """

    async def execute(self, command: CapabilityCommand) -> CapabilityResult: ...

    async def query(self, query: CapabilityQuery) -> CapabilityResult: ...


class CapabilityRepository(Protocol):
    """版本化 capability 聚合的 owner-scoped repository 协议。

    Contract:
        - Save 必须原子校验 expected_version。
        - Get 必须按 owner_id 隔离，不得泄漏其他租户存在性。

    Implemented by:
        SQLite aggregate repository 和测试内存 repository。
    """

    def save(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
        expected_version: int,
        payload: Mapping[str, JsonValue],
        schema_version: int = 1,
    ) -> CapabilityResult: ...

    def get(self, owner_id: str, aggregate_id: str) -> CapabilityResult | None: ...


class CapabilityTool(Protocol):
    """把 capability application 暴露给 Agent 的薄 Tool 协议。

    Contract:
        - Tool 只转换协议并委托 application，不复制领域规则。

    Implemented by:
        各 capability 的 ``tools`` 子包。
    """

    manifest: ToolManifest

    async def handle(self, request: ToolRequest) -> ToolResult: ...


class CapabilityCardPresenter(Protocol):
    """把 capability 结果确定性投影为 Card 的协议。

    Contract:
        - 相同版本结果必须产生语义等价的 Card。

    Implemented by:
        各 capability 的 ``cards`` 子包。
    """

    def present(self, result: CapabilityResult) -> CardEnvelope: ...
