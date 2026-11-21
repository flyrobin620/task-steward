"""Run ``tsw setup install --interactive``'s arrow-key interface on a scripted console.

    python drive_setup_keys.py <product checkout> <script.json> <out.json> [setup install args…]

The runtime is the product checkout's (``tsw setup`` writes into the checkout it runs from); the
console is the scripted stand-in from ``tests/terminal_harness.py``, in colour. The script is a
JSON list of key names and ``["line", text]`` pairs, or ``{"columns": n, "keys": [...]}`` for a
console ``n`` columns wide (400 otherwise). Writes ``{"code", "result", "error",
"output", "screen"}`` to ``out.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

product, script_file, out_file, *rest = sys.argv[1:]
sys.path.insert(0, product)
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

from tests.terminal_harness import ScriptedConsole, ScriptOver  # noqa: E402
from tsw.cli import build_parser  # noqa: E402
from tsw.errors import TswError  # noqa: E402
from tsw.setup.interactive import run_keys  # noqa: E402
from tsw.setup.terminal import Mode, Style  # noqa: E402

loaded: Any = json.loads(Path(script_file).read_text(encoding="utf-8"))
raw: list[Any] = loaded["keys"] if isinstance(loaded, dict) else loaded
columns = int(loaded.get("columns", 400)) if isinstance(loaded, dict) else 400
console = ScriptedConsole((tuple(i) if isinstance(i, list) else i for i in raw), columns)
args = build_parser().parse_args(["setup", "install", "--interactive", *rest])
out: dict[str, Any] = {"code": 0, "result": None, "error": None}
try:
    out["result"] = run_keys(args, console, Style(Mode(keys=True, color=True, unicode=True)))
except TswError as exc:
    out["code"], out["error"] = 1, exc.to_dict()
except ScriptOver:
    out["code"], out["error"] = 2, "script over"
out["output"] = console.output()
out["screen"] = console.screen()
out["left"] = [list(i) if isinstance(i, tuple) else i for i in console.script]
Path(out_file).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
