"""从结构上阻止交易执行能力进入首版系统。"""

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "trade_agent"
FORBIDDEN_CAPABILITY_TOKENS = {
    "place_order",
    "cancel_order",
    "submit_order",
    "broker_sync",
    "sync_balance",
    "get_balance",
    "record_fill",
    "order.fill",
    "broker.order",
}


def _public_identifiers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            values.add(node.name.casefold())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.casefold()
            if any(marker in text for marker in ("tool_id=", "/api/", "side_effect=")):
                values.add(text)
    return values


def test_repository_tool_graph_and_api_expose_no_broker_execution() -> None:
    checked_roots = (
        PACKAGE_ROOT / "capabilities",
        PACKAGE_ROOT / "adapters" / "sqlite",
        PACKAGE_ROOT / "agents",
        PACKAGE_ROOT / "apps" / "api",
    )
    violations: list[str] = []
    for root in checked_roots:
        for path in root.rglob("*.py"):
            for value in _public_identifiers(path):
                for token in FORBIDDEN_CAPABILITY_TOKENS:
                    if token in value:
                        violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {token}")
    assert not violations, "首版暴露了 broker 执行能力:\n" + "\n".join(sorted(violations))


def test_no_broker_or_account_domain_module_exists() -> None:
    forbidden_directories = {
        PACKAGE_ROOT / "capabilities" / "broker",
        PACKAGE_ROOT / "capabilities" / "orders",
        PACKAGE_ROOT / "capabilities" / "account",
        PACKAGE_ROOT / "agents" / "broker",
    }
    assert not any(path.exists() for path in forbidden_directories)
