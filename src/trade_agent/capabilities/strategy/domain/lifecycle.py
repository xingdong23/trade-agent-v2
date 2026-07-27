"""策略草稿、不可变版本与发布审批。"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class StrategyDraft:
    strategy_id: str
    owner_id: str
    name: str
    logic: str
    target: str
    horizon: str
    entry_conditions: tuple[Mapping[str, JsonValue], ...]
    exclusion_conditions: tuple[Mapping[str, JsonValue], ...]
    required_inputs: tuple[str, ...]
    ranking_policy: Mapping[str, JsonValue]
    positive_examples: tuple[str, ...] = ()
    negative_examples: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entry_conditions",
            tuple(MappingProxyType(dict(condition)) for condition in self.entry_conditions),
        )
        object.__setattr__(
            self,
            "exclusion_conditions",
            tuple(MappingProxyType(dict(condition)) for condition in self.exclusion_conditions),
        )
        object.__setattr__(self, "ranking_policy", MappingProxyType(dict(self.ranking_policy)))

    @property
    def content_hash(self) -> str:
        payload = {
            "strategy_id": self.strategy_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "logic": self.logic,
            "target": self.target,
            "horizon": self.horizon,
            "entry_conditions": [dict(item) for item in self.entry_conditions],
            "exclusion_conditions": [dict(item) for item in self.exclusion_conditions],
            "required_inputs": list(self.required_inputs),
            "ranking_policy": dict(self.ranking_policy),
            "positive_examples": list(self.positive_examples),
            "negative_examples": list(self.negative_examples),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PublishedStrategy:
    strategy_id: str
    owner_id: str
    version: int
    draft: StrategyDraft
    approved_by: str
    source_draft_hash: str


class StrategyPublisher:
    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], list[PublishedStrategy]] = {}
        self._commands: dict[tuple[str, str], tuple[str, PublishedStrategy]] = {}
        self._lock = Lock()

    def publish(
        self,
        draft: StrategyDraft,
        *,
        actor_id: str,
        approved: bool,
        source_draft_hash: str,
        idempotency_key: str,
    ) -> PublishedStrategy:
        if not approved:
            raise PermissionError("策略发布必须经过明确审批")
        if actor_id != draft.owner_id:
            raise PermissionError("策略不属于当前 owner")
        if not source_draft_hash.strip() or not idempotency_key.strip():
            raise ValueError("策略发布必须包含 payload hash 与幂等键")
        if source_draft_hash != draft.content_hash:
            raise ValueError("审批 payload hash 与策略草稿内容不一致")
        self._validate(draft)
        command_key = (draft.owner_id, idempotency_key)
        with self._lock:
            previous = self._commands.get(command_key)
            if previous is not None:
                if previous[0] != source_draft_hash:
                    raise RuntimeError("幂等键对应的策略草稿已改变")
                return previous[1]
            key = (draft.owner_id, draft.strategy_id)
            versions = self._versions.setdefault(key, [])
            published = PublishedStrategy(
                draft.strategy_id,
                draft.owner_id,
                len(versions) + 1,
                draft,
                actor_id,
                source_draft_hash,
            )
            versions.append(published)
            self._commands[command_key] = (source_draft_hash, published)
            return published

    def get_version(self, owner_id: str, strategy_id: str, version: int) -> PublishedStrategy:
        if version < 1:
            raise LookupError("策略版本不存在或不属于当前 owner")
        try:
            return self._versions[(owner_id, strategy_id)][version - 1]
        except (KeyError, IndexError) as error:
            raise LookupError("策略版本不存在或不属于当前 owner") from error

    @staticmethod
    def _validate(draft: StrategyDraft) -> None:
        required_text = (draft.name, draft.logic, draft.target, draft.horizon)
        if not all(value.strip() for value in required_text):
            raise ValueError("策略名称、逻辑、target 与 horizon 不能为空")
        if not draft.entry_conditions or not draft.required_inputs or not draft.ranking_policy:
            raise ValueError("策略必须包含入场条件、所需输入和 ranking policy")
