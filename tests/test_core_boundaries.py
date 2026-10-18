from __future__ import annotations

import ast
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1] / "app" / "core"
FORBIDDEN_IMPORT_ROOTS = (
    "fastapi",
    "sqlalchemy",
    "app.api",
    "app.db",
    "app.models",
    "app.schemas",
    "app.services",
    "app.settings",
)


def test_core_does_not_import_web_or_persistence_layers() -> None:
    violations: list[str] = []

    for source_path in CORE_ROOT.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.append(node.module)

            for module in imported_modules:
                if any(
                    module == root or module.startswith(f"{root}.")
                    for root in FORBIDDEN_IMPORT_ROOTS
                ):
                    violations.append(f"{source_path.relative_to(CORE_ROOT)}: {module}")

    assert violations == []
