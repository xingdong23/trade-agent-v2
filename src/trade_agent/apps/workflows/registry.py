"""会话工作流的注册与路由校验。"""

from collections.abc import Iterable

from .contracts import ConversationWorkflow


class WorkflowRegistry:
    """保存当前部署允许启动和恢复的会话工作流。

    调用方只提供结构化 ``workflow_id``、Agent ID 或 HITL ``subject_type``；
    Registry 负责唯一性检查和映射，不解释自然语言，也不包含具体业务分支。
    """

    def __init__(self, workflows: Iterable[ConversationWorkflow] = ()) -> None:
        self._by_id: dict[str, ConversationWorkflow] = {}
        self._by_subject_type: dict[str, ConversationWorkflow] = {}
        for workflow in workflows:
            self.register(workflow)

    def register(self, workflow: ConversationWorkflow) -> None:
        """注册一个工作流及其所有启动和恢复标识。

        Args:
            workflow: 显式声明 Agent、启动 ID 和恢复 subject type 的工作流实现。

        Raises:
            ValueError: Agent ID、Workflow ID 或 subject type 为空或重复。
        """

        agent_id = workflow.agent_id.strip()
        if not agent_id:
            raise ValueError("workflow agent_id 不能为空")
        workflow_ids = _normalized_ids(workflow.workflow_ids, name="workflow_id")
        subject_types = _normalized_ids(workflow.subject_types, name="subject_type")
        duplicated_workflows = set(workflow_ids) & set(self._by_id)
        if duplicated_workflows:
            duplicated = ", ".join(sorted(duplicated_workflows))
            raise ValueError(f"workflow 已注册: {duplicated}")
        duplicated_subjects = set(subject_types) & set(self._by_subject_type)
        if duplicated_subjects:
            duplicated = ", ".join(sorted(duplicated_subjects))
            raise ValueError(f"subject_type 已注册: {duplicated}")
        self._by_id.update(dict.fromkeys(workflow_ids, workflow))
        self._by_subject_type.update(dict.fromkeys(subject_types, workflow))

    def resolve_start(
        self, workflow_id: str, *, selected_agent_id: str
    ) -> ConversationWorkflow | None:
        """返回同时匹配 Workflow ID 与 Graph 路由结果的工作流。

        不一致时返回 ``None``，使会话入口安全关闭，而不是绕过 Supervisor Graph。
        """

        workflow = self._by_id.get(workflow_id)
        if workflow is None or workflow.agent_id != selected_agent_id:
            return None
        return workflow

    def resolve_resume(self, subject_type: str) -> ConversationWorkflow | None:
        """按持久化 HITL subject type 返回负责恢复的工作流。"""

        return self._by_subject_type.get(subject_type)

    @property
    def workflow_ids(self) -> tuple[str, ...]:
        """返回当前部署已注册的稳定 Workflow ID。"""

        return tuple(self._by_id)


def _normalized_ids(values: tuple[str, ...], *, name: str) -> tuple[str, ...]:
    """规范化一组协议 ID，并拒绝空值及工作流内部重复。"""

    normalized = tuple(value.strip() for value in values)
    if not normalized or any(not value for value in normalized):
        raise ValueError(f"{name} 不能为空")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"同一 workflow 内的 {name} 不能重复")
    return normalized


__all__ = ["WorkflowRegistry"]
