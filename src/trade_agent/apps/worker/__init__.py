"""单机 worker 入口。

当前 worker 进程主要承担两类后台职责：

- 量化扫描任务；
- 提醒与超时检查。

第一版这里只提供健康信息与组合根装配示例，真正的长运行循环仍待补充。
"""

import json

from trade_agent.apps.container import build_application_container
from trade_agent.core.config import AppSettings


def health(settings: AppSettings | None = None) -> dict[str, object]:
    """返回 worker 进程的最小健康摘要。"""

    container = build_application_container(settings or AppSettings())
    database = container.database
    return {
        "ready": database is not None and database.is_ready(),
        "workers": list(container.worker_ids),
        "process_count": (settings or AppSettings()).worker.process_count,
    }


def main() -> None:
    """打印 JSON 健康信息，便于命令行或进程管理器读取。"""

    print(json.dumps(health(), ensure_ascii=False))


__all__ = ["health", "main"]
