import ast
from pathlib import Path


def test_domain_layer_has_no_framework_storage_or_network_dependencies() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "domain"
    forbidden_imports = {
        "fastapi",
        "httpx",
        "requests",
        "sqlalchemy",
        "sqlite3",
        "subprocess",
    }

    violations: list[str] = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".")[0]}
            else:
                continue
            if roots & forbidden_imports:
                violations.append(f"{path.name}:{node.lineno}")

    assert violations == []


def test_domain_layer_contains_no_dynamic_code_execution_calls() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "domain"
    forbidden_calls = {"eval", "exec", "compile", "__import__"}
    violations: list[str] = []

    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden_calls
            ):
                violations.append(f"{path.name}:{node.lineno}:{node.func.id}")

    assert violations == []
