"""日志、trace 与第三方 export 共用的确定性脱敏层。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from trade_agent.core.llm.contracts import JsonValue

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
_API_KEY = re.compile(r"(?i)(?:sk|key)-[a-z0-9_-]{8,}")


class Redactor:
    def __init__(self, sensitive_fields: Sequence[str] = ()) -> None:
        self._sensitive_fields = _SECRET_KEYS | {field.casefold() for field in sensitive_fields}

    def redact(self, value: JsonValue | Mapping[str, JsonValue]) -> JsonValue:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return _API_KEY.sub("[REDACTED]", _BEARER.sub("Bearer [REDACTED]", value))
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return {
            str(key): (
                "[REDACTED]" if str(key).casefold() in self._sensitive_fields else self.redact(item)
            )
            for key, item in value.items()
        }


__all__ = ["Redactor"]
