"""Canonical JSON and hash helpers for persistence boundaries."""

import hashlib
import json
from collections.abc import Mapping

from trade_agent.core.llm.contracts import JsonValue


def dump_json(value: Mapping[str, JsonValue]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def load_json(value: str) -> dict[str, JsonValue]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("持久化 payload 必须是 JSON object")
    return parsed


def payload_hash(value: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(dump_json(value).encode()).hexdigest()
