"""Security values required at application boundaries."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UserContext:
    user_id: str
    correlation_id: str
    roles: frozenset[str] = frozenset()


class AccessPolicy(Protocol):
    def authorize(self, actor: UserContext, resource_owner_id: str) -> None: ...
