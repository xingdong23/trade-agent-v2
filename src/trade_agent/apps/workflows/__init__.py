"""应用层会话工作流插件集合。

这个包对外暴露统一的 Workflow 协议与具体业务实现。协议位于独立 ``contracts``
模块，因此插件和通用 runtime 之间没有反向导入。
"""

from .contracts import (
    ConversationRunResult,
    ConversationRuntime,
    ConversationWorkflow,
    WorkflowRuntime,
    WorkflowStartContext,
)
from .planning import (
    PlanningConversationWorkflow,
    PlanningOperationSpec,
    PlanningWorkflowConfig,
    default_planning_workflow_config,
    planning_presenter_config_from_settings,
    planning_workflow_config_from_settings,
)
from .registry import WorkflowRegistry
from .research_to_plan import (
    ResearchToPlanWorkflow,
    ResearchWorkflowBackend,
    ResearchWorkflowResult,
    SecurityCandidate,
    research_to_plan_workflow_config_from_settings,
)
from .runtime import DefaultWorkflowRuntime, UnsupportedNoticeConfig

__all__ = [
    "ConversationRunResult",
    "ConversationRuntime",
    "ConversationWorkflow",
    "DefaultWorkflowRuntime",
    "PlanningConversationWorkflow",
    "PlanningOperationSpec",
    "PlanningWorkflowConfig",
    "ResearchToPlanWorkflow",
    "ResearchWorkflowBackend",
    "ResearchWorkflowResult",
    "SecurityCandidate",
    "UnsupportedNoticeConfig",
    "WorkflowRegistry",
    "WorkflowRuntime",
    "WorkflowStartContext",
    "default_planning_workflow_config",
    "planning_presenter_config_from_settings",
    "planning_workflow_config_from_settings",
    "research_to_plan_workflow_config_from_settings",
]
