"""Worker 注册目录必须在启动前拒绝未知处理器。"""

import pytest

from trade_agent.apps.worker import WorkerRegistration, WorkerRegistry


def test_worker_registry_resolves_configured_workers_without_runtime_branches() -> None:
    registry = WorkerRegistry(
        (
            WorkerRegistration("custom-worker", "自定义后台处理器"),
            WorkerRegistration("scan-worker", "扫描处理器"),
        )
    )

    resolved = registry.resolve(("custom-worker", "scan-worker"))

    assert tuple(item.worker_id for item in resolved) == ("custom-worker", "scan-worker")


def test_worker_registry_rejects_unknown_or_duplicate_registration() -> None:
    with pytest.raises(ValueError, match="未注册的 worker"):
        WorkerRegistry((WorkerRegistration("known", "已注册"),)).resolve(("unknown",))
    with pytest.raises(ValueError, match="不能重复"):
        WorkerRegistry(
            (
                WorkerRegistration("duplicate", "第一项"),
                WorkerRegistration("duplicate", "第二项"),
            )
        )
