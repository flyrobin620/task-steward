"""The ``tsw`` shims and the user-level ``Path`` on Windows (DN009 §7, CC009 §8).

Two shims in the shim directory run the checkout's launcher with the recorded interpreter:
``tsw.cmd`` for cmd and PowerShell, and ``tsw``, a sh script, for Git Bash (Claude's Bash tool):
with only the ``.cmd`` Git Bash finds no command (T305 E6).

The user-level ``Path`` lives in ``HKCU\\Environment``. It is read and written with ``winreg``,
unexpanded and keeping the value's type (``REG_EXPAND_SZ`` entries such as
``%USERPROFILE%\\...`` stay as they are, non-ASCII folders survive); a test points
:data:`REG_ENV` at a JSON file standing in for that registry value, as ``TSW_SCHTASKS`` does for
Task Scheduler. After a real change Windows is told the environment changed (``WM_SETTINGCHANGE``),
so programs started from Explorer afterwards, such as the restarted Claude app, get the new
``Path``. Only the user's ``Path`` is changed, never the system's, and only by one entry.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from tsw.errors import rejected
from tsw.setup.record import norm

REG_ENV = "TSW_REG"
SHIM_ENV = "TSW_SHIM"
DEFER_SECONDS = 5
_DETACHED: list[subprocess.Popen[bytes]] = []
ENV_KEY = r"HKCU\Environment"
EXPAND = 2  # winreg.REG_EXPAND_SZ


def shim_texts(python: str, checkout: Path) -> dict[str, str]:
    """The shims' contents for this interpreter and checkout (name -> text)."""
    launcher = checkout / "plugin" / "bin" / "tsw_launch.py"
    posix_python = python.replace("\\", "/")
    posix_launcher = str(launcher).replace("\\", "/")
    return {
        # TSW_SHIM tells an uninstall run through this file that cmd will read it again after
        # tsw exits: deleting it now would make cmd fail (T308 live).
        "tsw.cmd": f'@setlocal & set "{SHIM_ENV}=%~f0" & "{python}" -X utf8 "{launcher}" %*\r\n',
        "tsw": f'#!/bin/sh\nexec "{posix_python}" -X utf8 "{posix_launcher}" "$@"\n',
    }


def shim_state(directory: Path, python: str, checkout: Path) -> dict[str, str]:
    """Each shim's state: ``ok``, ``missing`` or ``differs``."""
    states: dict[str, str] = {}
    for name, text in shim_texts(python, checkout).items():
        path = directory / name
        try:
            current = path.read_bytes().decode("utf-8")
        except OSError:
            states[name] = "missing"
            continue
        states[name] = "ok" if current == text else "differs"
    return states


def write_shims(directory: Path, python: str, checkout: Path) -> list[str]:
    """Write the shims that are missing or differ; returns the names written."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, text in shim_texts(python, checkout).items():
        path = directory / name
        try:
            if path.read_bytes() == text.encode("utf-8"):
                continue
        except OSError:
            pass
        path.write_bytes(text.encode("utf-8"))
        if name == "tsw":
            path.chmod(0o755)
        written.append(name)
    return written


def remove_shims(directory: Path, env: Mapping[str, str]) -> dict[str, list[str]]:
    """Delete the shims and their folder when it is left empty.

    cmd reads a batch file again after each command, so the ``tsw.cmd`` this uninstall runs
    through (``TSW_SHIM``) is deleted a few seconds later by a detached cmd, after this process and
    its cmd have exited; deleting it at once would end the user's command with "The system cannot
    find the path specified" and exit code 1.
    """
    removed: list[str] = []
    deferred: list[str] = []
    running = env.get(SHIM_ENV)
    for name in ("tsw", "tsw.cmd"):
        path = directory / name
        if not path.is_file():
            continue
        if running and _same(Path(running), path) and sys.platform == "win32":
            _delete_later(path)
            deferred.append(name)
            continue
        path.unlink()
        removed.append(name)
    if not deferred:
        with contextlib.suppress(OSError):
            directory.rmdir()  # only when empty
    return {"removed": removed, "deferred": deferred}


def _same(a: Path, b: Path) -> bool:
    return norm(a) == norm(b)


def _delete_later(path: Path) -> None:
    """Delete ``path`` (and its folder when empty) from a detached cmd after a short wait."""
    script = (
        f"ping -n {DEFER_SECONDS + 1} 127.0.0.1 >nul & "
        f'del /f /q "{path}" & rmdir "{path.parent}" 2>nul'
    )
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Outside a job that kills its processes when the terminal or tool that ran tsw closes.
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    # Kept referenced: the cleanup outlives this call (and usually this process).
    for extra in (breakaway, 0):
        try:
            _DETACHED.append(
                subprocess.Popen(
                    # One command line as cmd reads it: /s keeps the inner quotes as written.
                    f'cmd /d /s /c "{script}"',
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags | extra,
                    close_fds=True,
                )
            )
        except OSError:
            continue  # the job does not allow breaking away
        return


# ---------------------------------------------------------------------- the user's Path


def _fake_store(env: Mapping[str, str]) -> Path | None:
    r"""The stand-in for ``HKCU\Environment`` a test names in :data:`REG_ENV` (a JSON file)."""
    override = env.get(REG_ENV)
    return Path(override) if override else None


def read_user_path(env: Mapping[str, str]) -> tuple[str | None, int]:
    """The persisted user ``Path`` (unexpanded) and its registry type; ``None`` when unset."""
    fake = _fake_store(env)
    if fake is not None:
        try:
            data: Any = json.loads(fake.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, EXPAND
        value: Any = cast("dict[str, Any]", data).get("Path") if isinstance(data, dict) else None
        return (str(value) if value is not None else None), EXPAND
    if sys.platform != "win32":
        raise rejected(
            "用户级 PATH 目前只在 Windows 上由安装脚本维护",
            reason="unsupported_platform",
            platform=sys.platform,
        )
    import winreg  # noqa: PLC0415  (Windows only)

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            found, kind = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        return None, EXPAND
    return str(found), int(kind)


def write_user_path(env: Mapping[str, str], value: str, kind: int) -> None:
    """Persist the user ``Path``, keeping its type, and announce the change."""
    fake = _fake_store(env)
    if fake is not None:
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(json.dumps({"Path": value}, ensure_ascii=False), encoding="utf-8")
        return
    if sys.platform != "win32":
        raise rejected(
            "用户级 PATH 目前只在 Windows 上由安装脚本维护", reason="unsupported_platform"
        )
    import winreg  # noqa: PLC0415  (Windows only)

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, value)
    broadcast_environment_change()


def entries(value: str | None) -> list[str]:
    return [part for part in (value or "").split(";") if part.strip()]


def has_entry(value: str | None, directory: Path) -> bool:
    wanted = norm(directory)
    return any(
        norm(part.strip().strip('"')) == wanted for part in entries(value) if "%" not in part
    )


def with_entry(value: str | None, directory: Path) -> str:
    """``value`` with ``directory`` appended (unchanged when it is already there)."""
    if has_entry(value, directory):
        return value or ""
    return ";".join([*entries(value), str(directory)])


def without_entry(value: str | None, directory: Path) -> str:
    wanted = norm(directory)
    kept = [
        part for part in entries(value) if "%" in part or norm(part.strip().strip('"')) != wanted
    ]
    return ";".join(kept)


def broadcast_environment_change() -> None:
    """Tell running programs (Explorer above all) that the user's environment changed."""
    if sys.platform != "win32":
        return
    import ctypes  # noqa: PLC0415  (Windows only)
    from ctypes import wintypes  # noqa: PLC0415

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return
    result = wintypes.DWORD(0)
    hwnd_broadcast, wm_settingchange, smto_abortifhung = 0xFFFF, 0x001A, 0x0002
    windll.user32.SendMessageTimeoutW(
        hwnd_broadcast,
        wm_settingchange,
        0,
        "Environment",
        smto_abortifhung,
        5000,
        ctypes.byref(result),
    )
