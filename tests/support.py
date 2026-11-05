"""Helpers shared by tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tsw.ledger import Ledger
from tsw.tasks import TaskSpec, register

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ACTOR = {"role": "cli"}


def spec(**overrides: Any) -> TaskSpec:
    values: dict[str, Any] = {
        "title": "示例任务",
        "repo": "demo",
        "kind": "code",
        "goal": "实现示例功能",
        "acceptance": ["测试通过"],
    }
    values.update(overrides)
    return TaskSpec(**values)


def register_task(ledger: Ledger, **overrides: Any) -> str:
    result = ledger.commit(register(spec(**overrides), actor=TEST_ACTOR))
    task_id = result.events[0].task_id
    assert task_id is not None
    return task_id


def child_env(**extra: str) -> dict[str, str]:
    """The current (already scrubbed) environment plus ``PYTHONPATH`` for the repository."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUTF8"] = "1"
    env.update(extra)
    return env


def run_tsw(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> Any:
    """Run the CLI as a separate process; returns the completed process."""
    return subprocess.run(
        [sys.executable, "-m", "tsw", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env or child_env(),
        cwd=cwd,
        timeout=120,
        check=False,
    )


def run_json(*args: str, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    proc = run_tsw(*args, "--json", env=env)
    stream = proc.stdout if proc.returncode == 0 else proc.stderr
    return proc.returncode, json.loads(stream.strip().splitlines()[-1])
