"""Whether the Claude Code plugin a session runs is the product's current ``plugin/``.

Claude Code copies an installed plugin into its plugin cache and keeps that copy until the
plugin's computed version changes. A version string fixed in ``plugin.json`` therefore kept the
first copy forever: hooks, the launcher and files removed from the product stayed as they were
when the plugin was installed (T247). The manifest no longer fixes a version during development,
so an update replaces the copy. ``tsw host render`` runs that update (the user's decision: what
keeping the host current takes differs by host, so the render script does it), and this module
compares the copies before and after it. Apart from Claude Code's own update command it only
reads: files, and Claude Code's install records.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from tsw.host.claude_code import PLUGIN_DIR

PLUGIN_ID = "task-steward@task-steward"
UPDATE_COMMAND = f"claude plugin update {PLUGIN_ID}"
REINSTALL_COMMANDS = (f"claude plugin uninstall {PLUGIN_ID}", f"claude plugin install {PLUGIN_ID}")
UPDATE_TIMEOUT_SECONDS = 300
RESTART = "已在运行的 Claude 会话重启后才用上新的插件与 tsw 运行时（MCP server 在会话开始时载入）"
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


def _tail(text: str, lines: int = 10) -> list[str]:
    return [line for line in text.splitlines() if line.strip()][-lines:]


def update(env: Mapping[str, str]) -> dict[str, Any]:
    """Run Claude Code's plugin update for task-steward and report each install before and after.

    Idempotent: an install that is already current stays as it is. Nothing runs when Claude Code
    has no install record of the plugin (installing it is the user's step, ``install-hint``) or
    the ``claude`` command is not found; the report then says what to do.
    """
    before = installed_copies(env)
    report: dict[str, Any] = {"source": str(PLUGIN_DIR), "installed": before}
    if not before:
        report["note"] = (
            f"没有找到 {PLUGIN_ID} 的安装记录（未安装，或不在这台机器的插件目录中）；"
            "安装步骤见 tsw host install-hint"
        )
        return report
    executable = shutil.which("claude", path=env.get("PATH"))
    if executable is None:
        report["update"] = {"ran": False, "reason": "找不到 claude 命令", "command": UPDATE_COMMAND}
        return report
    try:
        proc = subprocess.run(
            [executable, "plugin", "update", PLUGIN_ID],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env),
            timeout=UPDATE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["update"] = {"ran": True, "command": UPDATE_COMMAND, "error": str(exc)}
    else:
        report["update"] = {
            "ran": True,
            "command": UPDATE_COMMAND,
            "returncode": proc.returncode,
            "output": _tail(f"{proc.stdout}\n{proc.stderr}"),
        }
    after = installed_copies(env)
    report["installed"] = after
    if all(c["current"] for c in after):
        if not all(c["current"] for c in before):
            report["next"] = RESTART
    else:
        report["next"] = (
            f"更新后副本仍与 plugin/ 不一致：在终端先 `{REINSTALL_COMMANDS[0]}` 再 "
            f"`{REINSTALL_COMMANDS[1]}`，然后重启 Claude 会话"
        )
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
        "`tsw host render`（它会更新插件并报告结果），再重启 Claude 会话。"
    )
