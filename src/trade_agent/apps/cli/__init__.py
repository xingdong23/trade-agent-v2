"""本地命令行适配器。

CLI 与 HTTP API 共享同一个 application container 和会话运行时，所以它不是另一套
业务实现。CLI 主要用于本地运维、诊断以及直接观察 JSON、HITL 和资源变化。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from uuid import uuid4

from trade_agent.adapters.sqlite import SQLiteAggregateRepository
from trade_agent.apps.container import ApplicationContainer, build_application_container
from trade_agent.core.config import AppSettings
from trade_agent.core.llm.contracts import JsonValue


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trade-agent")
    parser.add_argument("--user", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--thread", required=True)
    run.add_argument("message")
    hitl = sub.add_parser("hitl")
    hitl_sub = hitl.add_subparsers(dest="hitl_command", required=True)
    hitl_sub.add_parser("list")
    respond = hitl_sub.add_parser("respond")
    respond.add_argument("interaction_id")
    respond.add_argument("--version", type=int, required=True)
    respond.add_argument("--subject-version", type=int, required=True)
    respond.add_argument("--payload-hash", required=True)
    respond.add_argument("--action", required=True)
    respond.add_argument("--values", default="{}")
    cancel = hitl_sub.add_parser("cancel")
    cancel.add_argument("interaction_id")
    cancel.add_argument("--version", type=int, required=True)
    resource = sub.add_parser("resource")
    resource.add_argument("kind")
    resource.add_argument("resource_id")
    return parser


def execute(
    argv: Sequence[str],
    *,
    container: ApplicationContainer,
    settings: AppSettings,
) -> MappingResult:
    """解析一条 CLI 命令并委托给对应 application service。"""

    args = _parser().parse_args(list(argv))
    owner_id = args.user or settings.authentication.development_user_id
    if not owner_id:
        raise PermissionError("CLI 必须指定 --user")
    if args.command == "run":
        run_result = _required(container.conversation_runtime).start_run(
            owner_id=owner_id,
            thread_id=args.thread,
            message=args.message,
            correlation_id=str(uuid4()),
        )
        response: MappingResult = {
            "thread_id": run_result.thread_id,
            "run_id": run_result.run_id,
            "status": run_result.status,
        }
        if run_result.pending_interaction_id is not None:
            response["pending_interaction_id"] = run_result.pending_interaction_id
        if run_result.card is not None:
            response["card"] = run_result.card.to_mapping()
        return response
    if args.command == "hitl" and args.hitl_command == "list":
        interactions = _required(container.hitl_service).list_pending(owner_id)
        return {
            "pending": [
                {
                    "interaction_id": item.interaction_id,
                    "type": item.interaction_type.value,
                    "version": item.version,
                }
                for item in interactions
            ]
        }
    if args.command == "hitl" and args.hitl_command == "respond":
        raw = json.loads(args.values)
        if not isinstance(raw, dict):
            raise ValueError("--values 必须是 JSON object")
        values: dict[str, JsonValue] = raw
        interaction = _required(container.hitl_service).respond(
            owner_id=owner_id,
            interaction_id=args.interaction_id,
            expected_version=args.version,
            subject_version=args.subject_version,
            payload_hash=args.payload_hash,
            actor_id=owner_id,
            response=values,
            resolution=args.action,
        )
        hitl_response: MappingResult = {
            "interaction_id": interaction.interaction_id,
            "status": interaction.status.value,
        }
        card = _required(container.conversation_runtime).handle_resolved_interaction(interaction)
        if card is not None:
            hitl_response["card"] = card.to_mapping()
        return hitl_response
    if args.command == "hitl" and args.hitl_command == "cancel":
        interaction = _required(container.hitl_service).cancel(
            owner_id=owner_id,
            interaction_id=args.interaction_id,
            expected_version=args.version,
            actor_id=owner_id,
        )
        return {"interaction_id": interaction.interaction_id, "status": interaction.status.value}
    if args.command == "resource":
        value = SQLiteAggregateRepository(_required(container.database), args.kind).get(
            owner_id, args.resource_id
        )
        if value is None:
            raise LookupError("资源不存在或不属于当前用户")
        return {
            "resource_id": value.reference_id,
            "version": value.version,
            "payload": dict(value.payload),
        }
    raise ValueError("不支持的 CLI command")


type MappingResult = dict[str, JsonValue]


def _required[T](value: T | None) -> T:
    if value is None:
        raise RuntimeError("CLI container 缺少依赖")
    return value


def main() -> None:
    import sys

    settings = AppSettings()
    result = execute(
        sys.argv[1:], container=build_application_container(settings), settings=settings
    )
    print(json.dumps(result, ensure_ascii=False))


__all__ = ["execute", "main"]
