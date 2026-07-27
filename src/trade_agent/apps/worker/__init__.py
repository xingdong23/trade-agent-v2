"""单机 scan/reminder worker composition 入口。"""

import json

from trade_agent.apps.container import build_application_container
from trade_agent.core.config import AppSettings


def health(settings: AppSettings | None = None) -> dict[str, object]:
    container = build_application_container(settings or AppSettings())
    database = container.database
    return {
        "ready": database is not None and database.is_ready(),
        "workers": list(container.worker_ids),
        "process_count": (settings or AppSettings()).worker.process_count,
    }


def main() -> None:
    print(json.dumps(health(), ensure_ascii=False))


__all__ = ["health", "main"]
