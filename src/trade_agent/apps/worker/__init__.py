"""单机 worker 入口。

当前 worker 进程主要承担两类后台职责：

- 量化扫描任务；
- 提醒与超时检查。

第一版这里只提供健康信息与组合根装配示例，真正的长运行循环仍待补充。
"""

import json
from dataclasses import dataclass

from trade_agent.apps.container import build_application_container
from trade_agent.core.config import AppSettings


@dataclass(frozen=True, slots=True)
class WorkerRegistration:
    """描述一个可由 worker 进程启动的后台处理器。

    Attributes:
        worker_id: 配置和健康检查共享的稳定标识。
        description: 面向运维的职责说明。
    """

    worker_id: str
    description: str

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or not self.description.strip():
            raise ValueError("worker registration 字段不能为空")


class WorkerRegistry:
    """按稳定 ID 注册后台处理器，并拒绝未知部署配置。"""

    def __init__(self, registrations: tuple[WorkerRegistration, ...]) -> None:
        entries = {item.worker_id: item for item in registrations}
        if len(entries) != len(registrations):
            raise ValueError("worker registration 不能重复")
        self._entries = entries

    def resolve(self, worker_ids: tuple[str, ...]) -> tuple[WorkerRegistration, ...]:
        """按配置顺序解析 worker，未知 ID 必须使启动失败。"""

        unknown = tuple(worker_id for worker_id in worker_ids if worker_id not in self._entries)
        if unknown:
            raise ValueError(f"未注册的 worker: {', '.join(unknown)}")
        return tuple(self._entries[worker_id] for worker_id in worker_ids)


def default_worker_registry() -> WorkerRegistry:
    """在 composition root 注册首版可用 worker；运行时本身不枚举业务分支。"""

    return WorkerRegistry(
        (
            WorkerRegistration("scan-worker", "执行冻结的量化扫描单元"),
            WorkerRegistration("reminder-worker", "评估提醒并投递通知"),
        )
    )


def health(
    settings: AppSettings | None = None,
    *,
    registry: WorkerRegistry | None = None,
) -> dict[str, object]:
    """返回 worker 进程的最小健康摘要。"""

    resolved_settings = settings or AppSettings()
    resolved_registry = registry or default_worker_registry()
    registrations = resolved_registry.resolve(resolved_settings.worker.worker_ids)
    container = build_application_container(resolved_settings)
    database = container.database
    return {
        "ready": database is not None and database.is_ready(),
        "workers": [item.worker_id for item in registrations],
        "process_count": resolved_settings.worker.process_count,
    }


def main() -> None:
    """打印 JSON 健康信息，便于命令行或进程管理器读取。"""

    print(json.dumps(health(), ensure_ascii=False))


__all__ = [
    "WorkerRegistration",
    "WorkerRegistry",
    "default_worker_registry",
    "health",
    "main",
]
