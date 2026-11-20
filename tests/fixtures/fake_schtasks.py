"""A stand-in for Windows ``schtasks`` (``TSW_SCHTASKS``): Task Scheduler is outside this repo.

It keeps the registered task names in ``fake-schtasks.json`` under ``TSW_STATE_DIR`` and appends
every call's arguments to ``calls``, so tests can assert what tsw asked for. ``/Create`` records
the task (and copies the XML it was given), ``/Delete`` removes it, ``/Query`` succeeds only for a
registered task; ``/Run`` and ``/End`` succeed for a registered task. Nothing is started.
"""

import json
import os
import sys
from pathlib import Path

state = Path(os.environ["TSW_STATE_DIR"]) / "fake-schtasks.json"
data = json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {}
tasks = data.setdefault("tasks", {})
calls = data.setdefault("calls", [])
args = sys.argv[1:]
calls.append(args)
verb = args[0] if args else ""
name = args[args.index("/TN") + 1] if "/TN" in args else ""
code = 0
if verb == "/Create":
    xml = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
    tasks[name] = {"xml": xml}
elif verb == "/Delete":
    code = 0 if tasks.pop(name, None) is not None else 1
elif verb in ("/Query", "/Run", "/End"):
    code = 0 if name in tasks else 1
state.parent.mkdir(parents=True, exist_ok=True)
state.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
print("SUCCESS" if code == 0 else "ERROR: The system cannot find the file specified.")
sys.exit(code)
