"""Shared shapes for capability public boundaries."""

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
    command_id: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityQuery:
    query_id: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    reference_id: str
    version: int
    payload: Mapping[str, JsonValue] = field(default_factory=dict)


class CapabilityApplication(Protocol):
    async def execute(self, command: CapabilityCommand) -> CapabilityResult: ...

    async def query(self, query: CapabilityQuery) -> CapabilityResult: ...


class CapabilityRepository(Protocol):
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
    manifest: ToolManifest

    async def handle(self, request: ToolRequest) -> ToolResult: ...


class CapabilityCardPresenter(Protocol):
    def present(self, result: CapabilityResult) -> CardEnvelope: ...
