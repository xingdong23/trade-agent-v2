"""Executable import rules for the modular monolith boundary."""

import ast
from collections.abc import Iterator
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "trade_agent"
CAPABILITY_NAMES = {
    "market_research",
    "quantitative",
    "watchlist",
    "strategy",
    "planning",
    "reminder",
}


def _python_files(root: Path) -> Iterator[Path]:
    yield from root.rglob("*.py")


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    return imports


def _assert_no_import_prefix(root: Path, forbidden: tuple[str, ...]) -> None:
    violations: list[str] = []
    for path in _python_files(root):
        for imported in _absolute_imports(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {imported}")
    assert not violations, "发现非法依赖:\n" + "\n".join(sorted(violations))


def test_core_has_no_outward_dependencies() -> None:
    _assert_no_import_prefix(
        PACKAGE_ROOT / "core",
        (
            "trade_agent.agents",
            "trade_agent.capabilities",
            "trade_agent.adapters",
            "trade_agent.apps",
        ),
    )


def test_agents_only_depend_on_agents_and_core() -> None:
    _assert_no_import_prefix(
        PACKAGE_ROOT / "agents",
        ("trade_agent.capabilities", "trade_agent.adapters", "trade_agent.apps"),
    )


def test_quantitative_is_not_an_agent() -> None:
    assert not (PACKAGE_ROOT / "agents" / "quantitative").exists()


def test_capabilities_do_not_import_other_capability_internals() -> None:
    violations: list[str] = []
    capabilities_root = PACKAGE_ROOT / "capabilities"
    for capability in CAPABILITY_NAMES:
        for path in _python_files(capabilities_root / capability):
            for imported in _absolute_imports(path):
                prefix = "trade_agent.capabilities."
                if not imported.startswith(prefix):
                    continue
                imported_capability = imported.removeprefix(prefix).split(".", maxsplit=1)[0]
                if imported_capability in CAPABILITY_NAMES and imported_capability != capability:
                    violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {imported}")
    assert not violations, "发现跨 capability 内部依赖:\n" + "\n".join(sorted(violations))


def test_tool_modules_do_not_import_tools_or_infrastructure() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.glob("capabilities/*/tools/**/*.py"):
        for imported in _absolute_imports(path):
            is_other_tool = (
                imported.startswith("trade_agent.capabilities.") and ".tools" in imported
            )
            if is_other_tool or imported.startswith(("trade_agent.adapters", "trade_agent.apps")):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {imported}")
    assert not violations, "tool 边界违规:\n" + "\n".join(sorted(violations))


def test_card_modules_only_use_presentation_and_own_public_contracts() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.glob("capabilities/*/cards/**/*.py"):
        capability = path.parts[-3]
        allowed = (
            "trade_agent.core.presentation",
            f"trade_agent.capabilities.{capability}.contracts",
        )
        for imported in _absolute_imports(path):
            if imported.startswith("trade_agent") and not imported.startswith(allowed):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {imported}")
    assert not violations, "card presenter 边界违规:\n" + "\n".join(sorted(violations))


def test_adapters_do_not_import_agents_apps_or_capability_implementation() -> None:
    violations: list[str] = []
    for path in _python_files(PACKAGE_ROOT / "adapters"):
        for imported in _absolute_imports(path):
            if imported.startswith(("trade_agent.agents", "trade_agent.apps")):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {imported}")
            if imported.startswith("trade_agent.capabilities.") and not (
                imported.endswith(".ports") or imported.endswith(".contracts")
            ):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {imported}")
    assert not violations, "adapter 边界违规:\n" + "\n".join(sorted(violations))
