"""Contracts implemented by model adapters such as LiteLLM."""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class ModelRoute:
    name: str


class LLMErrorCode(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication_error"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LIMIT = "context_limit"
    CONTENT_POLICY = "content_policy"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROVIDER_NOT_ALLOWED = "provider_not_allowed"
    INVALID_RESPONSE = "invalid_response"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LLMError(RuntimeError):
    code: LLMErrorCode
    message: str
    retryable: bool = False
    route: str | None = None
    attempts: int = 1

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    route: ModelRoute
    messages: Sequence[LLMMessage]
    response_schema: Mapping[str, JsonValue] | None = None
    prompt_version: str = "unversioned"
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.route.name.strip() or not self.messages:
            raise ValueError("LLMRequest 必须包含 route 与 message")
        if self.prompt_version == "unversioned":
            raise ValueError("LLMRequest 必须声明 prompt_version")


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    structured: Mapping[str, JsonValue] | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    provider_request_id: str | None = None
    finish_reason: str | None = None


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    def stream(self, request: LLMRequest) -> AsyncIterator[str]: ...


class StructuredResponseValidator(Protocol):
    def validate(
        self, value: JsonValue | Mapping[str, JsonValue], schema: Mapping[str, JsonValue]
    ) -> None: ...
