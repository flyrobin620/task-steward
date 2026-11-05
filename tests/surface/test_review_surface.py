"""``review_run`` over MCP and ``tsw review run`` against a stand-in ``codex app-server``."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import ORCH, Harness
from tests.support import REPO_ROOT, child_env, run_tsw
from tsw import lease
from tsw.home import Home
from tsw.review.codex import AppServer

pytestmark = pytest.mark.use_case("UC-REVIEW-002")

FAKE = REPO_ROOT / "tests" / "fixtures" / "fake_codex_app_server.py"


def fake_codex(tmp_path: Path, answers: list[Any]) -> tuple[Path, dict[str, str]]:
    wrapper = tmp_path / "codex.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    (tmp_path / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    return wrapper, {
        "FAKE_CODEX_ANSWERS": str(tmp_path / "answers.json"),
        "FAKE_CODEX_LOG": str(tmp_path / "codex.log"),
    }


def bound_worker(home: Home, codex: Path) -> Path:
    h = Harness(home, {"roles": {"reviewer": {"executable": str(codex)}}})
    h.register("T001", kind="research")
    h.plan("T001")
    workdir = home.root / "work" / "T001" / "a1"
    workdir.mkdir(parents=True)
    h.run("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    # The harness schedules as machine "m1"; the CLI below runs on this host.
    h.ledger.commit(lease.release("orch-1", machine="m1", actor=ORCH.actor()))
    return workdir


def test_a_failed_round_over_mcp_is_retried_from_the_command_line(
    home: Home, tmp_path: Path
) -> None:
    codex, fake_env = fake_codex(tmp_path, ["我觉得可以"])
    workdir = bound_worker(home, codex)
    env = child_env(CLAUDE_CODE_HOST_SESSION_ID="w-1", **fake_env)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "review_run", "arguments": {"head": "draft-1", "notes": "n"}},
    }
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(REPO_ROOT / "plugin" / "bin" / "tsw_launch.py"), "mcp"],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=workdir,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    failed = json.loads(json.loads(proc.stdout)["result"]["content"][0]["text"])
    assert (failed["ok"], failed["outcome"], failed["round"]) == (False, "review.failed", 1)

    retried = run_tsw("review", "run", "--json", env=env, cwd=workdir)
    assert retried.returncode == 0, retried.stderr
    out = json.loads(retried.stdout)
    assert (out["round"], out["verdict"], out["reviewed_head"]) == (1, "pass", "draft-1")

    calls = [json.loads(line) for line in (tmp_path / "codex.log").read_text().splitlines()]
    starts = [c["params"] for c in calls if c.get("method") == "thread/start"]
    turns = [c["params"] for c in calls if c.get("method") == "turn/start"]
    assert {(s["model"], s["sandbox"], s["approvalPolicy"]) for s in starts} == {
        ("gpt-6-astra", "read-only", "never")
    }
    assert {(t["effort"], t["sandboxPolicy"]["type"]) for t in turns} == {("medium", "readOnly")}
    review = home.root / "runs" / "T001" / "a1" / "review"
    assert json.loads((review / "r1.json").read_text(encoding="utf-8"))["verdict"] == "pass"
    assert (review / "r1-raw.txt").read_text(encoding="utf-8") == "我觉得可以"


def alive(pid: int) -> bool:
    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
    )
    return str(pid) in listing.stdout


def test_closing_the_server_ends_the_wrapper_and_its_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "child.pid"
    stubborn = REPO_ROOT / "tests" / "fixtures" / "stubborn_server.py"
    wrapper = tmp_path / "server.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{stubborn}" "{pid_file}"\r\n', encoding="utf-8")
    monkeypatch.setattr(AppServer, "close_wait", 0.5)
    server = AppServer(str(wrapper))
    deadline = time.monotonic() + 30
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    child = int(pid_file.read_text(encoding="utf-8"))
    assert alive(child)
    server.close()
    assert server.proc.poll() is not None
    assert not alive(child)


def test_a_child_left_holding_the_pipe_is_ended_without_waiting_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "child.pid"
    fixtures = REPO_ROOT / "tests" / "fixtures"
    wrapper = tmp_path / "server.cmd"
    wrapper.write_text(
        f'@"{sys.executable}" "{fixtures / "orphaning_server.py"}" '
        f'"{fixtures / "stubborn_server.py"}" "{pid_file}"\r\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(AppServer, "close_wait", 5.0)
    server = AppServer(str(wrapper))
    deadline = time.monotonic() + 30
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    child = int(pid_file.read_text(encoding="utf-8"))
    started = time.monotonic()
    server.close()  # the wrapper exits at end of input; its grandchild does not
    assert time.monotonic() - started < 20
    assert server.proc.poll() is not None
    assert not alive(child)
