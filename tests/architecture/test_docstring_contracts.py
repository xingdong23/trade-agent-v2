"""公共模型实体和 Protocol 必须遵守结构化中文 Docstring 契约。"""

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "trade_agent"
MODEL_BASE_NAMES = frozenset(
    {
        "BaseModel",
        "BaseSettings",
        "StrictSettingsModel",
        "TypedDict",
        "Enum",
        "IntEnum",
        "StrEnum",
    }
)


def _source_files() -> tuple[Path, ...]:
    """返回产品源码树中的全部 Python 模块。"""

    return tuple(sorted(SOURCE_ROOT.rglob("*.py")))


def _classes(path: Path) -> tuple[ast.ClassDef, ...]:
    """解析一个模块的顶层公共类。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    )


def _expression_name(expression: ast.expr) -> str | None:
    """读取装饰器或基类表达式最末端的名称。"""

    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    if isinstance(expression, ast.Subscript):
        return _expression_name(expression.value)
    return None


def _decorator_name(decorator: ast.expr) -> str | None:
    """读取带参数或无参数装饰器的名称。"""

    if isinstance(decorator, ast.Call):
        return _expression_name(decorator.func)
    return _expression_name(decorator)


def _base_names(node: ast.ClassDef) -> frozenset[str]:
    """返回类声明中可静态识别的直接基类名称。"""

    return frozenset(name for base in node.bases if (name := _expression_name(base)) is not None)


def _is_protocol(node: ast.ClassDef) -> bool:
    """判断类是否声明为公共 Protocol。"""

    return "Protocol" in _base_names(node)


def _is_model(node: ast.ClassDef) -> bool:
    """判断类是否属于必须解释字段语义的公共数据模型。"""

    decorators = frozenset(
        name
        for decorator in node.decorator_list
        if (name := _decorator_name(decorator)) is not None
    )
    return "dataclass" in decorators or bool(_base_names(node) & MODEL_BASE_NAMES)


def _is_chinese(docstring: str) -> bool:
    """判断 Docstring 是否至少包含一个中文汉字。"""

    return any("\u4e00" <= character <= "\u9fff" for character in docstring)


def _location(path: Path, node: ast.ClassDef, section: str) -> str:
    """生成便于 IDE 定位的失败说明。"""

    relative = path.relative_to(SOURCE_ROOT)
    return f"{relative}:{node.lineno}:{node.name}:{section}"


def test_all_public_models_use_strict_chinese_docstrings() -> None:
    """保证全仓公共模型解释全部字段，而不是只覆盖人工挑选的模块。"""

    missing: list[str] = []
    for path in _source_files():
        for node in _classes(path):
            if not _is_model(node):
                continue
            docstring = ast.get_docstring(node) or ""
            if not _is_chinese(docstring):
                missing.append(_location(path, node, "中文摘要"))
            if "Attributes:" not in docstring:
                missing.append(_location(path, node, "Attributes"))
    assert missing == [], "\n".join(missing)


def test_all_public_protocols_explain_contract_and_implementations() -> None:
    """保证每个公共 Protocol 都说明契约与可导航的实现位置。"""

    missing: list[str] = []
    for path in _source_files():
        for node in _classes(path):
            if not _is_protocol(node):
                continue
            docstring = ast.get_docstring(node) or ""
            if not _is_chinese(docstring):
                missing.append(_location(path, node, "中文摘要"))
            for section in ("Contract:", "Implemented by:"):
                if section not in docstring:
                    missing.append(_location(path, node, section.rstrip(":")))
    assert missing == [], "\n".join(missing)


def test_conversation_runtime_is_business_agnostic() -> None:
    """保证会话入口只依赖注册协议，不认识任何具体业务流程。"""

    path = SOURCE_ROOT / "apps" / "conversation_runtime.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for forbidden in (
        "PlanningService",
        "ResearchWorkflowBackend",
        "PlanDraftRequest",
        "PlanningCardPresenter",
        "Intent.PLANNING",
        "Intent.RESEARCH",
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

    workflow_or_subject_value_branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr in {"workflow_id", "subject_type"}
        and any(
            not (isinstance(item, ast.Constant) and item.value is None) for item in node.comparators
        )
    ]
    assert workflow_or_subject_value_branches == []


def test_source_tree_does_not_use_removed_journey_term() -> None:
    """保证被 Workflow 取代的旧 Journey 术语不会重新进入产品源码。"""

    violations: list[str] = []
    for path in _source_files():
        source = path.read_text(encoding="utf-8")
        if any(term in source for term in ("Journey", "journey", "旅程", "JOURNEY_")):
            violations.append(str(path.relative_to(SOURCE_ROOT)))
    assert violations == []
