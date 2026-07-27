"""应用层会话旅程插件集合。

这个包对外暴露统一的 Journey 协议与具体业务实现。协议位于独立 ``contracts``
模块，因此插件和通用 runtime 之间没有反向导入。
"""

from .contracts import (
    ConversationJourney,
    ConversationRunResult,
    ConversationRuntimePort,
    JourneyStartContext,
)
from .planning import (
    PlanningConversationJourney,
    PlanningJourneyConfig,
    PlanningOperationSpec,
    default_planning_journey_config,
    planning_journey_config_from_settings,
    planning_presenter_config_from_settings,
)
from .research_to_plan import (
    ResearchJourneyBackend,
    ResearchJourneyResult,
    ResearchToPlanJourney,
    SecurityCandidate,
    research_to_plan_journey_config_from_settings,
)

__all__ = [
    "ConversationJourney",
    "ConversationRunResult",
    "ConversationRuntimePort",
    "JourneyStartContext",
    "PlanningConversationJourney",
    "PlanningJourneyConfig",
    "PlanningOperationSpec",
    "ResearchJourneyBackend",
    "ResearchJourneyResult",
    "ResearchToPlanJourney",
    "SecurityCandidate",
    "default_planning_journey_config",
    "planning_journey_config_from_settings",
    "planning_presenter_config_from_settings",
    "research_to_plan_journey_config_from_settings",
]
