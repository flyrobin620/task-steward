"""Helpers shared by tests."""

from __future__ import annotations

import json
import os
import re
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


def flow_layouts(page: str) -> dict[str, dict[str, Any]]:
    """The task flow's layouts in a Dashboard page by key (``live:dep,fu,ref`` and so on).

    Each has the ``cells`` (column, top) by task, the ``lanes`` (top, bottom) by repository,
    the number of ``cols``, the columns' ``xs`` (``xw`` with the references shown) and the
    ``lines``, ``peek`` and ``wide`` routes as (kind, source, target, points).
    """
    found = re.search(
        r'<script type="application/json" id="flow-layouts">(.*?)</script>', page, re.S
    )
    assert found, "no task flow layouts in the page"
    data = json.loads(found.group(1))
    ids, repos = data["ids"], data["repos"]
    kinds = ("dep", "fu", "ref")

    def routes(rows: list[list[float]]) -> list[tuple[str, str, str, list[tuple[float, float]]]]:
        out = []
        for row in rows:
            points = [(row[3], row[4])]
            for i, v in enumerate(row[5:], start=1):  # x, then y, in turn
                x, y = points[-1]
                points.append((v, y) if i % 2 else (x, v))
            out.append((kinds[int(row[0])], ids[int(row[1])], ids[int(row[2])], points))
        return out

    layouts: dict[str, dict[str, Any]] = {}
    for key, at in data["pick"].items():
        chosen = data["sets"][at]
        layouts[key] = {
            "set": at,
            "cols": chosen["cols"],
            "xs": chosen["xs"],
            "xw": chosen["xw"],
            "cells": {t: tuple(c) for t, c in zip(ids, chosen["cards"], strict=True) if c},
            "lanes": {
                r: tuple(rows) for r, rows in zip(repos, chosen["lanes"], strict=True) if rows
            },
            "lines": routes(chosen["lines"]),
            "peek": routes(chosen["peek"] or chosen["lines"]),
            "wide": routes(chosen["wide"] or chosen["lines"]),
        }
    return layouts
