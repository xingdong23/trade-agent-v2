"""课程源码中的模型实体和 Protocol 必须遵守结构化 docstring 契约。"""

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "trade_agent"
REQUIRED_MODEL_MODULES = (
    SOURCE_ROOT / "core" / "runtime" / "contracts.py",
    SOURCE_ROOT / "core" / "runtime" / "intent.py",
    SOURCE_ROOT / "core" / "llm" / "contracts.py",
    SOURCE_ROOT / "core" / "tools" / "contracts.py",
    SOURCE_ROOT / "core" / "hitl" / "contracts.py",
    SOURCE_ROOT / "apps" / "container.py",
    SOURCE_ROOT / "apps" / "conversation_runtime.py",
    SOURCE_ROOT / "apps" / "journeys" / "contracts.py",
    SOURCE_ROOT / "apps" / "journeys" / "planning.py",
    SOURCE_ROOT / "apps" / "journeys" / "research_to_plan.py",
    SOURCE_ROOT / "capabilities" / "market_research" / "domain" / "models.py",
    SOURCE_ROOT / "capabilities" / "market_research" / "domain" / "research.py",
    SOURCE_ROOT / "capabilities" / "market_research" / "domain" / "evidence.py",
    SOURCE_ROOT / "capabilities" / "quantitative" / "domain" / "models.py",
    SOURCE_ROOT / "capabilities" / "quantitative" / "domain" / "data_contracts.py",
    SOURCE_ROOT / "capabilities" / "quantitative" / "domain" / "model_lifecycle.py",
    SOURCE_ROOT / "capabilities" / "quantitative" / "domain" / "scanning.py",
    SOURCE_ROOT / "capabilities" / "quantitative" / "domain" / "monitoring.py",
    SOURCE_ROOT / "capabilities" / "quantitative" / "application" / "training.py",
    SOURCE_ROOT / "capabilities" / "planning" / "application" / "__init__.py",
    SOURCE_ROOT / "capabilities" / "planning" / "domain" / "models.py",
    SOURCE_ROOT / "capabilities" / "watchlist" / "domain" / "models.py",
    SOURCE_ROOT / "capabilities" / "reminder" / "domain" / "models.py",
    SOURCE_ROOT / "capabilities" / "strategy" / "domain" / "models.py",
    SOURCE_ROOT / "capabilities" / "strategy" / "domain" / "lifecycle.py",
    SOURCE_ROOT / "adapters" / "sqlite" / "database.py",
    SOURCE_ROOT / "adapters" / "model_runtime" / "lightgbm.py",
    SOURCE_ROOT / "adapters" / "model_runtime" / "lstm.py",
)


def _classes(path: Path) -> tuple[ast.ClassDef, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(node for node in tree.body if isinstance(node, ast.ClassDef))


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Protocol")
        or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
        for base in node.bases
    )


def _is_model(node: ast.ClassDef) -> bool:
    decorators = {
        decorator.func.id
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
    }
    typed_dict = any(isinstance(base, ast.Name) and base.id == "TypedDict" for base in node.bases)
    return "dataclass" in decorators or typed_dict


def test_key_models_use_attributes_and_invariants_sections() -> None:
    missing: list[str] = []
    for path in REQUIRED_MODEL_MODULES:
        for node in _classes(path):
            if not _is_model(node):
                continue
            docstring = ast.get_docstring(node) or ""
            if "Attributes:" not in docstring:
                missing.append(f"{path.relative_to(SOURCE_ROOT)}:{node.name}:Attributes")
    assert missing == []


def test_key_protocols_explain_contract_and_implementations() -> None:
    missing: list[str] = []
    for path in REQUIRED_MODEL_MODULES:
        for node in _classes(path):
            if not _is_protocol(node):
                continue
            docstring = ast.get_docstring(node) or ""
            for section in ("Contract:", "Implemented by:"):
                if section not in docstring:
                    missing.append(f"{path.relative_to(SOURCE_ROOT)}:{node.name}:{section}")
    assert missing == []


def test_conversation_runtime_is_business_agnostic() -> None:
    path = SOURCE_ROOT / "apps" / "conversation_runtime.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for forbidden in (
        "PlanningService",
        "ResearchJourneyBackend",
        "PlanDraftRequest",
        "PlanningCardPresenter",
        '"新增一个交易"',
        '"买"',
        '"研究"',
        '"扫描"',
    ):
        assert forbidden not in source

    capability_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("trade_agent.capabilities")
    }
    assert capability_imports == set()

    subject_type_branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "subject_type"
    ]
    assert subject_type_branches == []
