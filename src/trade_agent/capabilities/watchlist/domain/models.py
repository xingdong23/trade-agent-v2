"""Watchlist、membership、分组和冻结 universe models。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class Watchlist:
    """表示一个 owner 隔离的关注列表聚合。

    Attributes:
        watchlist_id: 关注列表稳定标识。
        owner_id: 资源所有者。
        name: 关注列表展示名称。
        version: 当前聚合版本。
    """

    watchlist_id: str
    owner_id: str
    name: str
    version: int


class ImportStatus(StrEnum):
    """Watchlist 导入单行解析状态的稳定枚举。

    Attributes:
        ACCEPTED: 当前行已成功解析为唯一受支持证券。
        AMBIGUOUS: 当前行匹配到多个候选，需要人工澄清。
        DUPLICATE: 当前行对应的证券已在目标 watchlist 中存在。
        UNSUPPORTED_MARKET: 当前行解析到的证券不属于受支持市场。
        REJECTED: 当前行无法形成可接受的导入结果。

    Invariants:
        - 枚举值驱动导入审批、前端高亮与后续写入策略。
    """

    ACCEPTED = "accepted"
    AMBIGUOUS = "ambiguous"
    DUPLICATE = "duplicate"
    UNSUPPORTED_MARKET = "unsupported_market"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ImportRow:
    """表示一次导入中的单行解析结果。

    Attributes:
        row_number: 原始导入文件中的行号。
        raw_value: 用户提供的原始文本。
        status: 当前行的解析状态。
        security_id: 成功解析后的规范证券标识；失败时可为空。
        message: 该行的补充说明或错误信息。
        metadata: 与导入来源或解析过程相关的附加元数据。
    """

    row_number: int
    raw_value: str
    status: ImportStatus
    security_id: str | None = None
    message: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Provenance:
    """记录 membership 来源的不可变溯源信息。

    Attributes:
        source_type: 来源类型，例如 import 或 research。
        source_reference: 外部来源或上游产物标识。
        imported_at: 该来源进入 watchlist 的时间。
    """

    source_type: str
    source_reference: str
    imported_at: datetime


@dataclass(frozen=True, slots=True)
class Membership:
    """表示一只证券在 watchlist 中的归属关系。

    Attributes:
        security_id: 规范证券标识。
        tags: 用户或系统追加的标签集合。
        notes: 与该证券相关的备注信息。
        provenance: 该 membership 的全部来源链路。
    """

    security_id: str
    tags: frozenset[str]
    notes: tuple[str, ...]
    provenance: tuple[Provenance, ...]


@dataclass(frozen=True, slots=True)
class WatchlistGroup:
    """表示 watchlist 内的一个命名分组。

    Attributes:
        group_id: 分组稳定标识。
        name: 分组展示名称。
        security_ids: 当前分组包含的证券标识集合。
    """

    group_id: str
    name: str
    security_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ClassificationSuggestion:
    """表示 AI 或规则引擎给出的分组建议。

    Attributes:
        suggestion_id: 建议稳定标识。
        security_id: 被建议分组的证券标识。
        proposed_group_id: 系统建议的目标分组标识。
        source_reference: 建议来源引用。
        accepted: 该建议是否已被用户接受。
        decided_by: 做出最终决定的用户标识；未决时为空。
        accepted_group_id: 最终接受的分组标识；拒绝或未决时为空。
    """

    suggestion_id: str
    security_id: str
    proposed_group_id: str
    source_reference: str
    accepted: bool = False
    decided_by: str | None = None
    accepted_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    """冻结一次扫描所使用的候选证券集合。

    Attributes:
        snapshot_id: universe 快照稳定标识。
        owner_id: 快照归属用户。
        source_watchlist_id: 生成该快照的 watchlist 标识。
        security_ids: 快照内按冻结顺序保存的证券集合。
        created_at: 快照生成时间。
        source_group_id: 若仅冻结某个分组，则记录分组标识；否则为空。
    """

    snapshot_id: str
    owner_id: str
    source_watchlist_id: str
    security_ids: tuple[str, ...]
    created_at: datetime
    source_group_id: str | None = None
