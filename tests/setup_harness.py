"""Helpers for the ``tsw setup`` tests (DN009, CC009).

Every test gets its own product checkout: a git repository in the test's temporary directory with
this checkout's runtime, plugin and marketplace, committed once. ``tsw setup`` writes into the
checkout it runs from (``.local/tsw-home``), so it must never be this repository. Claude Code's
plugin commands are the stand-in ``tests/fixtures/fake_claude_plugins.py`` behind a ``claude.cmd``;
the user ``Path`` is the JSON file ``TSW_REG`` names, Task Scheduler the ``TSW_SCHTASKS``
stand-in (both set by ``conftest.isolated_env``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.support import REPO_ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "tsw-test",
    "GIT_AUTHOR_EMAIL": "tsw-test@example.com",
    "GIT_COMMITTER_NAME": "tsw-test",
    "GIT_COMMITTER_EMAIL": "tsw-test@example.com",
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **GIT_ENV},
        check=True,
    )
    return proc.stdout.strip()


def make_product(tmp_path: Path, name: str = "product") -> Path:
    """A committed product checkout with this repository's runtime and plugin."""
    repo = tmp_path / name
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REPO_ROOT / "tsw", repo / "tsw", ignore=ignore)
    shutil.copytree(REPO_ROOT / "plugin", repo / "plugin", ignore=ignore)
    shutil.copytree(REPO_ROOT / ".claude-plugin", repo / ".claude-plugin")
    shutil.copyfile(REPO_ROOT / "setup.ps1", repo / "setup.ps1")
    (repo / ".gitignore").write_text(".local/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    for key, value in (("core.autocrlf", "false"), ("core.safecrlf", "false")):
        git(repo, "config", key, value)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "product")
    return repo


def fake_claude(tmp_path: Path) -> Path:
    """A ``claude.cmd`` running the plugin-command stand-in; calls go to ``claude.log``."""
    bin_dir = tmp_path / "claude-bin"
    bin_dir.mkdir(exist_ok=True)
    script = FIXTURES / "fake_claude_plugins.py"
    command = bin_dir / "claude.cmd"
    command.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    return command


def claude_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "claude.log"
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def setup_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """The environment ``tsw setup`` runs in: the test's isolated one, without ``TSW_HOME`` (the
    install chooses the data directory) and with the stand-in's log."""
    env = {k: v for k, v in os.environ.items() if k not in ("TSW_HOME", "PYTHONPATH")}
    env["FAKE_CLAUDE_LOG"] = str(tmp_path / "claude.log")
    env["PYTHONUTF8"] = "1"
    env.update(extra)
    return env


def answers(home: Path, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "home": str(home),
        "language": "zh-CN",
        "reviewer": "claude-headless",
        "human_channels": "in-session",
        "host": "claude-code",
        "dashboard": False,
        "dashboard_port": 47801,
        "user_settings": True,
    }
    values.update(overrides)
    return values


def write_answers(tmp_path: Path, home: Path, **overrides: Any) -> Path:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(answers(home, **overrides)), encoding="utf-8")
    return path


def run_setup(
    product: Path,
    *args: str,
    env: dict[str, str],
    stdin: str = "",
    cwd: Path | None = None,
) -> tuple[int, dict[str, Any], str]:
    """``tsw setup <args> --json`` through the product's launcher, as the entries run it."""
    launcher = product / "plugin" / "bin" / "tsw_launch.py"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(launcher), "setup", *args, "--json"],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(cwd or product.parent),
        timeout=300,
        check=False,
    )
    # A result with "ok": false goes to stdout with exit code 3; an error goes to stderr.
    stream = proc.stdout if proc.stdout.strip() else proc.stderr
    # An interactive run prints its prompts first: the result is the last JSON object.
    lines = [line[line.index('{"') :] for line in stream.strip().splitlines() if '{"' in line]
    data: dict[str, Any] = json.loads(lines[-1]) if lines else {}
    return proc.returncode, data, proc.stdout + proc.stderr


def user_path(env: dict[str, str]) -> str | None:
    store = Path(env["TSW_REG"])
    if not store.is_file():
        return None
    value: Any = json.loads(store.read_text(encoding="utf-8")).get("Path")
    return None if value is None else str(value)


def set_user_path(env: dict[str, str], value: str) -> None:
    store = Path(env["TSW_REG"])
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"Path": value}), encoding="utf-8")


def settings(env: dict[str, str]) -> dict[str, Any]:
    path = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def record(env: dict[str, str]) -> dict[str, Any]:
    return json.loads((Path(env["TSW_STATE_DIR"]) / "install.json").read_text(encoding="utf-8"))
