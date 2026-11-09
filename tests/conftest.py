"""Shared fixtures and the result recorder used by ``tools/gate.py``.

Every test runs with its own data directory and a scrubbed environment: no inherited
``CLAUDE*`` / ``ANTHROPIC*`` / ``CODEX*`` variables, and ``HOME`` / ``USERPROFILE`` /
``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` point into the test's temporary directory. So does
``CLAUDE_PROJECT_DIR``: tsw looks for a data directory that claims the session's directory
before ``TSW_HOME`` (``tsw.home.claiming_home``), and a checkout inside such a data directory
must not lead a test, or a process it starts, there.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from tsw.home import Home, init_home
from tsw.ledger import Ledger

SCRUBBED_PREFIXES = ("CLAUDE", "ANTHROPIC", "CODEX")
RESULTS_ENV = "TSW_RESULTS_FILE"
TIERS = ("unit", "component", "surface", "conformance")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in list(os.environ):
        if key.upper().startswith(SCRUBBED_PREFIXES) or key in ("TSW_TEST_FAULTS", "TSW_REPO"):
            monkeypatch.delenv(key, raising=False)
    user = tmp_path / "user"
    user.mkdir()
    for key in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_PROJECT_DIR"):
        monkeypatch.setenv(key, str(user))
    data_home = tmp_path / "tsw-home"
    monkeypatch.setenv("TSW_HOME", str(data_home))
    return data_home


@pytest.fixture
def home(isolated_env: Path, tmp_path: Path) -> Home:
    assert isolated_env.is_relative_to(tmp_path), "TSW_HOME must stay inside the test directory"
    created, _ = init_home(isolated_env)
    return created


@pytest.fixture
def ledger(home: Home) -> Ledger:
    return Ledger(home)


# ---------------------------------------------------------------------- result recorder


class _Recorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.time()
        self.collected = 0
        self.tests: dict[str, dict[str, Any]] = {}

    def item_meta(self, item: pytest.Item) -> dict[str, Any]:
        parts = Path(str(item.path)).parts
        tier = next((part for part in reversed(parts) if part in TIERS), "unknown")
        return {
            "nodeid": item.nodeid,
            "tier": tier,
            "use_cases": [str(m.args[0]) for m in item.iter_markers("use_case")],
            "transitions": [str(m.args[0]) for m in item.iter_markers("transition")],
            "outcome": "not_run",
        }


_RECORDER_KEY = pytest.StashKey[_Recorder]()


def pytest_configure(config: pytest.Config) -> None:
    target = os.environ.get(RESULTS_ENV)
    if target:
        config.stash[_RECORDER_KEY] = _Recorder(Path(target))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    recorder = config.stash.get(_RECORDER_KEY, None)
    if recorder is None:
        return
    recorder.collected = len(items)
    for item in items:
        recorder.tests[item.nodeid] = recorder.item_meta(item)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    config = _CONFIG.get("config")
    if config is None:
        return
    recorder = config.stash.get(_RECORDER_KEY, None)
    if recorder is None or report.nodeid not in recorder.tests:
        return
    entry = recorder.tests[report.nodeid]
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        entry["outcome"] = report.outcome
    elif report.when == "teardown" and report.outcome == "failed":
        entry["outcome"] = "failed"


_CONFIG: dict[str, pytest.Config] = {}


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    _CONFIG["config"] = session.config


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    recorder = session.config.stash.get(_RECORDER_KEY, None)
    if recorder is None:
        return
    recorder.path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "started": recorder.started,
        "finished": time.time(),
        "exitstatus": int(exitstatus),
        "collected": recorder.collected,
        "tests": list(recorder.tests.values()),
    }
    recorder.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
