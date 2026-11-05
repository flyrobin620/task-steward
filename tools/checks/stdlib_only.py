"""Check that the runtime packages import only the Python standard library.

Runtime code must also run on Python 3.10, so modules added to the standard library later are
refused even when the checking interpreter has them.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RUNTIME_DIRS = ("tsw", "plugin")
FIRST_PARTY = {"tsw"}
NEWER_THAN_310 = {"tomllib", "_tomllib"}


def check(root: Path) -> list[str]:
    allowed = set(sys.stdlib_module_names) - NEWER_THAN_310
    problems: list[str] = []
    for top in RUNTIME_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    module = name.split(".")[0]
                    if module in FIRST_PARTY or module == "__future__" or module in allowed:
                        continue
                    rel = path.relative_to(root).as_posix()
                    problems.append(f"{rel}:{node.lineno}: 运行时导入了非标准库模块 {name}")
    return problems
