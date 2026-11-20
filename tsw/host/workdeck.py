"""The workdeck plugin manifest of the Dashboard (DN006 §8.10, CC006 §13; T309).

workdeck mounts any local web app that describes itself in a manifest (workdeck DN005 §11,
decision 0030). The Dashboard's manifest depends on this installation (the interpreter, the
product checkout and the data directory), so tsw generates it rather than shipping a file; the
Dashboard serves it at :data:`WELL_KNOWN_PATH`, ``tsw dashboard workdeck-manifest`` prints it, and
``tsw setup`` hands it to ``workdeck plugin add``.

The manifest makes workdeck start its own Dashboard (``spawn``) and sign in with the one-time token
in the address it prints (``query-token``): a manifest never carries a credential, and the hosted
service's tokens exist only as digests (DN006 §8.10, "常驻服务"), so there is nothing to give it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from tsw.errors import rejected
from tsw.host.session_labels import LABELS_PATH

WELL_KNOWN_PATH = "/.well-known/workdeck-plugin.json"
PROTOCOL = 1
DEFAULT_ID = "tsw"
NAME = "TaskSteward Dashboard"
DESCRIPTION = "TaskSteward 的任务看板：任务、attempt 与会话，验收、退回与取消"
LABELS_REFRESH_SECONDS = 10
# workdeck's rule for a plugin id (DN005 §11.2): the mount name /plugins/<id>/.
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def console_python(executable: str = sys.executable) -> str:
    """An interpreter that writes to its standard output: ``pythonw.exe`` (the hosted service's)
    has none, and workdeck reads the address from that output, so its ``python.exe`` sibling."""
    path = Path(executable)
    if path.name.lower() == "pythonw.exe":
        sibling = path.with_name("python.exe")
        if sibling.is_file():
            return str(sibling)
    return str(path)


def check_id(plugin_id: str) -> str:
    if not ID_PATTERN.fullmatch(plugin_id):
        raise rejected(
            "workdeck 插件 id 只能是小写字母、数字与连字符（1–32 个字符，以字母或数字开头）",
            reason="invalid_plugin_id",
            id=plugin_id,
        )
    return plugin_id


def manifest(
    home_root: Path,
    *,
    python: str,
    checkout: Path,
    plugin_id: str = DEFAULT_ID,
) -> dict[str, Any]:
    """The manifest (workdeck plugin protocol 1) for the Dashboard of ``home_root``."""
    return {
        "workdeckPlugin": PROTOCOL,
        "id": check_id(plugin_id),
        "name": NAME,
        "description": DESCRIPTION,
        "spawn": {
            "command": console_python(python),
            "args": [
                "-X",
                "utf8",
                "-m",
                "tsw",
                "status",
                "serve",
                "--no-open",
                "--home",
                str(home_root.resolve()),
            ],
            "cwd": str(checkout.resolve()),
        },
        "auth": {"type": "query-token"},
        "labels": {"path": LABELS_PATH, "refreshSeconds": LABELS_REFRESH_SECONDS},
    }


def to_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def digest(value: dict[str, Any]) -> str:
    """A stable digest of a manifest: what ``tsw setup`` records to tell whether it changed."""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
