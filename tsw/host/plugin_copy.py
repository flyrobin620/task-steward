"""Whether the Claude Code plugin a session runs is the product's current ``plugin/``.

Claude Code copies an installed plugin into its plugin cache and keeps that copy until the
plugin's computed version changes. A version string fixed in ``plugin.json`` therefore kept the
first copy forever: hooks, the launcher and files removed from the product stayed as they were
when the plugin was installed (T247). The manifest no longer fixes a version during development,
so an update replaces the copy; this module finds a copy that still differs and says how to
update it. It only compares files and reads Claude Code's install records; it never changes them.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from tsw.host.claude_code import PLUGIN_DIR

PLUGIN_ID = "task-steward@task-steward"
UPDATE_COMMAND = f"claude plugin update {PLUGIN_ID}"
REINSTALL_COMMANDS = (f"claude plugin uninstall {PLUGIN_ID}", f"claude plugin install {PLUGIN_ID}")
# Claude Code's own bookkeeping inside a cached copy, and Python's byte-code caches.
IGNORED = frozenset({".in_use", ".orphaned_at", "__pycache__"})


def _digests(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in IGNORED for part in rel.parts) or not path.is_file():
            continue
        found[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def differences(installed: Path, source: Path = PLUGIN_DIR) -> list[str]:
    """Files that are missing, extra or different in ``installed`` compared with ``source``."""
    have, want = _digests(installed), _digests(source)
    return sorted(name for name in have.keys() | want.keys() if have.get(name) != want.get(name))


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def plugins_root(env: Mapping[str, str]) -> Path:
    """Claude Code's plugins root: ``CLAUDE_CODE_PLUGIN_CACHE_DIR``, else ``<config>/plugins``."""
    explicit = env.get("CLAUDE_CODE_PLUGIN_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    config = env.get("CLAUDE_CONFIG_DIR")
    return (Path(config).expanduser() if config else Path.home() / ".claude") / "plugins"


def installed_copies(env: Mapping[str, str]) -> list[dict[str, Any]]:
    """The task-steward installs Claude Code recorded, each with whether it is current."""
    try:
        records: Any = json.loads(
            (plugins_root(env) / "installed_plugins.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    plugins: Any = (
        cast("dict[str, Any]", records).get("plugins") if isinstance(records, dict) else None
    )
    installs: Any = (
        cast("dict[str, Any]", plugins).get(PLUGIN_ID) if isinstance(plugins, dict) else None
    )
    result: list[dict[str, Any]] = []
    for install in cast("list[Any]", installs) if isinstance(installs, list) else []:
        if not isinstance(install, dict):
            continue
        record = cast("dict[str, Any]", install)
        path = record.get("installPath")
        if not isinstance(path, str) or not path:
            continue
        copy = Path(path)
        entry: dict[str, Any] = {
            "scope": record.get("scope"),
            "version": record.get("version"),
            "path": str(copy),
        }
        if not copy.is_dir():
            entry["current"] = False
            entry["differs"] = ["(missing)"]
        elif _same_dir(copy, PLUGIN_DIR):
            entry["current"] = True
        else:
            diff = differences(copy)
            entry["current"] = not diff
            if diff:
                entry["differs"] = diff
        result.append(entry)
    return result


def status(env: Mapping[str, str]) -> dict[str, Any]:
    """What ``tsw host render`` reports about the installed plugin."""
    copies = installed_copies(env)
    report: dict[str, Any] = {"source": str(PLUGIN_DIR), "installed": copies}
    if not copies:
        report["note"] = f"没有找到 {PLUGIN_ID} 的安装记录（未安装，或不在这台机器的插件目录中）"
    elif not all(c["current"] for c in copies):
        report["update"] = [UPDATE_COMMAND, "然后重启 Claude 会话"]
    return report


def session_hint(env: Mapping[str, str] = os.environ) -> str | None:
    """One line for the main session when the plugin it loaded differs from ``plugin/``.

    Claude Code gives hooks ``CLAUDE_PLUGIN_ROOT``, the directory the plugin was loaded from.
    Nothing is said when it is unset, is the product's own ``plugin/`` (loaded in place) or
    matches it.
    """
    root = env.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return None
    loaded = Path(root)
    if not loaded.is_dir() or _same_dir(loaded, PLUGIN_DIR):
        return None
    diff = differences(loaded)
    if not diff:
        return None
    shown = "、".join(diff[:5]) + ("等" if len(diff) > 5 else "")
    return (
        f"注意：本会话加载的 task-steward 插件副本（{loaded}）与产品仓库的 plugin/ 不一致"
        f"（{len(diff)} 个文件：{shown}），hooks 与启动器可能是旧的。告诉用户在终端运行 "
        f"`{UPDATE_COMMAND}`，再重启 Claude 会话；仍不一致时先 `{REINSTALL_COMMANDS[0]}` 再 "
        f"`{REINSTALL_COMMANDS[1]}`。"
    )
