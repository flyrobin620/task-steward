"""The self-update and the hosted Dashboard as separate processes (T283, DN006 §8.10, §9.8)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.support import child_env, run_json, run_tsw
from tests.surface.test_host_surface import INIT, call, mcp, payload
from tests.surface.test_self_update_surface import bound_worker_home

UPDATE = "UC-HOST-009"
DASHBOARD = "UC-DIRECT-006"


@pytest.fixture
def worker_home(tmp_path: Path) -> tuple[Path, Path]:
    return bound_worker_home(tmp_path)


def _json(proc: Any) -> dict[str, Any]:
    stream = proc.stdout if proc.returncode == 0 else proc.stderr
    return json.loads(stream.strip().splitlines()[-1])


@pytest.mark.use_case(UPDATE)
def test_update_plan_needs_a_start_until_render_records_one(
    worker_home: tuple[Path, Path],
) -> None:
    root, _ = worker_home
    code, out = run_json("host", "update-plan")
    assert code == 3 and out["details"]["reason"] == "from_unknown"
    code, out = run_json("host", "update-plan", "--from", "HEAD", "--to", "HEAD")
    assert code == 0, out
    assert out["from"] == out["to"] and out["changed"] == [] and out["categories"] == {}
    assert out["sessions"]["workers"][0]["session_ref"] == "w-1"
    assert out["state"]["dashboard"]["installed"] is False
    assert run_json("host", "render")[0] == 0
    code, out = run_json("host", "update-plan")
    assert code == 0, out
    assert [s["source"] for s in out["from_sources"]] == ["rendered"]
    assert out["state"]["render"]["commit"] == out["to"]
    # Read-only: a worker may run it too.
    proc = run_tsw(
        "host",
        "update-plan",
        "--json",
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID="w-1"),
        cwd=worker_home[1],
    )
    assert proc.returncode == 0, proc.stderr
    assert root.is_dir()


@pytest.mark.use_case(UPDATE)
def test_whoami_reports_the_runtime_and_update_record_is_logged(
    worker_home: tuple[Path, Path],
) -> None:
    root, _ = worker_home
    # The fixture took the lease for machine m1; this MCP server runs on this machine.
    assert run_json("lease", "release", "--session", "orch-1", "--machine", "m1")[0] == 0
    replies = mcp(
        root,
        root,
        "orch-2",
        INIT,
        call(2, "whoami"),
        call(3, "lease_acquire"),
        call(4, "update_record", **{"from": "aaa", "to": "bbb", "actions": ["render"]}),
    )
    me = payload(replies[1])
    assert me["role"] == "orchestrator"
    assert len(me["runtime"]["commit"]) == 40 and me["runtime"]["started_at"]
    recorded = payload(replies[3])
    assert recorded["kind"] == "host_update" and recorded["sequence"] > 0


@pytest.mark.use_case(DASHBOARD)
def test_the_users_dashboard_commands_run_only_outside_sessions(
    worker_home: tuple[Path, Path],
) -> None:
    root, workdir = worker_home
    session = child_env(CLAUDE_CODE_HOST_SESSION_ID="orch-1")
    for command in ("install", "uninstall", "open", "service"):
        proc = run_tsw("dashboard", command, "--json", env=session, cwd=root)
        assert proc.returncode == 3, command
        assert _json(proc)["details"]["reason"] == "host_session_cli", command
    # The worker may read the service's state but not restart it.
    worker = child_env(CLAUDE_CODE_HOST_SESSION_ID="w-1")
    assert run_tsw("dashboard", "status", "--json", env=worker, cwd=workdir).returncode == 0
    refused = run_tsw("dashboard", "restart", "--json", env=worker, cwd=workdir)
    assert refused.returncode == 3 and "只有主会话" in _json(refused)["message"]
    # The main session may restart it (here: nothing is installed yet).
    proc = run_tsw("dashboard", "restart", "--json", env=session, cwd=root)
    assert proc.returncode == 0, proc.stderr
    assert _json(proc)["installed"] is False


@pytest.mark.use_case(DASHBOARD)
def test_install_open_status_and_uninstall_in_a_terminal(
    worker_home: tuple[Path, Path],
) -> None:
    code, out = run_json("dashboard", "install")
    assert code == 0, out
    assert out["installed"] is True and out["url"] == "http://127.0.0.1:47800/"
    assert "token" not in json.dumps(out).replace("token_created", "")
    code, opened = run_json("dashboard", "open", "--no-browser")
    assert code == 0 and opened["url"] == "http://127.0.0.1:47800/" and opened["opened"] is False
    code, state = run_json("dashboard", "status")
    assert code == 0 and state["installed"] is True and state["port"] == 47800
    secret = Path(os.environ["TSW_STATE_DIR"]) / "tsw-dashboard-secret"
    assert len(list(secret.glob("*.json"))) == 1
    code, removed = run_json("dashboard", "uninstall")
    assert code == 0 and removed["uninstalled"] is True
    assert list(secret.glob("*.json")) == []
    assert run_json("dashboard", "open", "--no-browser")[0] == 3
