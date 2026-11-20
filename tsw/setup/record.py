"""``install.json`` and ``setup-answers.json`` in the per-user state directory (DN009 §3, §5).

The install record says what each step changed: the checkout, the interpreter and host command
lines, the shim directory, whether ``Path`` got the shim directory from us, the user-settings
keys and rules we added, the Dashboard task, backups. A step that changes the user's
environment writes its ownership (what was there before, what it will add) **before** changing
anything and its result after; a rerun keeps the ownership written first, so what an
interrupted run added is never taken for the user's own (DN009 §5, T305 r7).

The answers file holds the user's answers, never credentials: a rerun need not ask again.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from tsw.events import utc_now
from tsw.host.dashboard_service import state_dir
from tsw.storage.fs import atomic_write

RECORD_VERSION = 1
RECORD_FILE = "install.json"
ANSWERS_FILE = "setup-answers.json"
CHECKOUT_DIR = "task-steward"
SHIM_DIR = "bin"


def record_path(env: Mapping[str, str]) -> Path:
    return state_dir(env) / RECORD_FILE


def answers_path(env: Mapping[str, str]) -> Path:
    return state_dir(env) / ANSWERS_FILE


def shim_dir(env: Mapping[str, str]) -> Path:
    """Where the ``tsw`` shims go: ``%LOCALAPPDATA%\\TaskSteward\\bin`` (CC009 §8)."""
    return state_dir(env) / SHIM_DIR


def default_checkout(env: Mapping[str, str]) -> Path:
    """The fixed checkout location: ``%LOCALAPPDATA%\\TaskSteward\\task-steward`` (DN009 §6)."""
    local = env.get("LOCALAPPDATA")
    base = Path(local) / "TaskSteward" if local else state_dir(env)
    return base / CHECKOUT_DIR


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", raw) if isinstance(raw, dict) else None


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    text = json.dumps(dict(data), ensure_ascii=False, indent=2) + "\n"
    atomic_write(path, text.encode("utf-8"))


def norm(path: str | Path) -> str:
    """A path for comparing: absolute, normalized, case-folded on Windows."""
    return os.path.normcase(os.path.normpath(str(Path(path).absolute())))


class InstallRecord:
    """``install.json``: loaded once, saved after every change of a step."""

    def __init__(self, env: Mapping[str, str], data: dict[str, Any] | None = None) -> None:
        self.env = env
        self.path = record_path(env)
        self.data: dict[str, Any] = data if data is not None else {}

    @classmethod
    def load(cls, env: Mapping[str, str]) -> InstallRecord:
        return cls(env, _read_json(record_path(env)))

    @property
    def exists(self) -> bool:
        return bool(self.data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def steps(self) -> dict[str, Any]:
        found: Any = self.data.get("steps")
        if not isinstance(found, dict):
            found = {}
            self.data["steps"] = found
        return cast("dict[str, Any]", found)

    def step(self, name: str) -> dict[str, Any]:
        """A step's entry (empty when the step never ran)."""
        found: Any = self.steps().get(name)
        return cast("dict[str, Any]", found) if isinstance(found, dict) else {}

    def begin(self, name: str, ownership: Mapping[str, Any]) -> dict[str, Any]:
        """Record a step's ownership before it changes anything; keep what was recorded first.

        Keys already in the step's entry win over ``ownership``: a rerun after an interrupted
        run sees its own additions as the current state, not as the user's.
        """
        entry = dict(self.step(name))
        for key, value in ownership.items():
            entry.setdefault(key, value)
        entry["state"] = "started"
        entry["started_at"] = utc_now()
        self.steps()[name] = entry
        self.save()
        return entry

    def finish(self, name: str, **result: Any) -> None:
        entry = dict(self.step(name))
        entry.update(result)
        entry["state"] = "done"
        entry["done_at"] = utc_now()
        self.steps()[name] = entry
        self.save()

    def set(self, **values: Any) -> None:
        self.data.update(values)
        self.save()

    def save(self) -> None:
        self.data.setdefault("version", RECORD_VERSION)
        self.data["updated_at"] = utc_now()
        _write_json(self.path, self.data)


def load_answers(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    if data is None:
        from tsw.errors import rejected  # noqa: PLC0415  (only on the error path)

        raise rejected("读不到答案文件（须为 JSON 对象）", reason="invalid_input", path=str(path))
    return data


def save_answers(env: Mapping[str, str], answers: Mapping[str, Any]) -> Path:
    path = answers_path(env)
    _write_json(path, answers)
    return path


def recorded_claude(env: Mapping[str, str]) -> str | None:
    """The ``claude`` command line the install recorded, when it still exists (CC009 §3)."""
    data = _read_json(record_path(env)) or {}
    value = data.get("claude")
    return value if isinstance(value, str) and value and Path(value).is_file() else None
