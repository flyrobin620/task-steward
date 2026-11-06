"""Configuration and maintenance tools over the real MCP stdio server and hook entry point."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support import child_env, run_json
from tests.surface.test_host_surface import INIT, LIST, NOTE, PLUGIN, call, mcp, payload


@pytest.fixture
def home() -> Path:
    code, out = run_json("init")
    assert code == 0
    root = Path(out["home"])
    (root / "config" / "task-steward.json").write_text(
        json.dumps({"version": 1, "repositories": {"demo": {"path": "~/src/demo"}}}),
        encoding="utf-8",
    )
    return root


def by_id(replies: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    return {r["id"]: r for r in replies}


@pytest.mark.use_case("UC-CONFIG-002")
def test_the_orchestrator_changes_and_reverts_the_configuration(home: Path) -> None:
    first = by_id(
        mcp(
            home, home, "orch-9", INIT, NOTE, LIST, call(2, "lease_acquire"), call(3, "config_show")
        )
    )
    names = {t["name"] for t in first[1]["result"]["tools"]}
    assert {"config_change", "config_revert", "config_show", "config_history"} <= names
    sha = payload(first[3])["file_sha256"]
    delivery = {"op": "set", "path": "repositories.demo.delivery", "value": {"push": True}}
    words = {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": "orch-9:msg-1",
        "text": "同意 demo 推送",
        "target": ["config", sha, "authorize"],
    }
    replies = by_id(
        mcp(
            home,
            home,
            "orch-9",
            INIT,
            NOTE,
            call(1, "config_change", ops=[delivery], reason="没有原话"),
            call(2, "config_change", ops=[delivery], reason="用户同意", user_source=words),
            call(
                3,
                "config_change",
                ops=[{"op": "set", "path": "scheduling.max_workers", "value": 0}],
                reason="无效",
            ),
            call(4, "config_history"),
            call(
                5,
                "config_revert",
                to_change="C001",
                reason="撤销",
                user_source={**words, "target": ["config", "x", "authorize"]},
            ),
            call(6, "config_history", change_id="C001"),
        )
    )
    assert replies[1]["result"]["isError"] is True
    assert payload(replies[1])["details"]["reason"] == "g_user"
    assert payload(replies[2])["events"][0]["data"]["change_id"] == "C001"
    assert payload(replies[3])["details"]["reason"] == "config_invalid"
    history = payload(replies[4])
    assert [(c["change_id"], c["user_words"]) for c in history] == [("C001", "同意 demo 推送")]
    assert payload(replies[5])["details"]["reason"] == "g_user"
    detail = payload(replies[6])
    assert json.loads(detail["after"])["repositories"]["demo"]["delivery"] == {"push": True}
    written = json.loads((home / "config" / "task-steward.json").read_text(encoding="utf-8"))
    assert written["repositories"]["demo"]["delivery"] == {"push": True}


@pytest.mark.use_case("UC-CONFIG-002")
def test_other_sessions_cannot_use_the_configuration_tools(home: Path, tmp_path: Path) -> None:
    replies = by_id(
        mcp(home, tmp_path, "someone", INIT, NOTE, call(1, "config_change", ops=[], reason="r"))
    )
    assert payload(replies[1])["details"]["reason"] == "role_denied"
    env = child_env(CLAUDE_CODE_HOST_SESSION_ID="orch-9")
    event = {
        "cwd": str(home),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(home / "config" / "task-steward.json")},
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(PLUGIN / "bin" / "tsw_launch.py"),
            "guard",
            "PreToolUse",
        ],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=False,
    )
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "config_change" in out["permissionDecisionReason"]


@pytest.mark.use_case("UC-STORE-003")
def test_maintenance_tools_over_stdio(home: Path, tmp_path: Path) -> None:
    replies = by_id(
        mcp(
            home,
            home,
            "orch-9",
            INIT,
            NOTE,
            LIST,
            call(2, "lease_acquire"),
            call(3, "home_backup", dest=str(tmp_path / "bak")),
            call(4, "ledger_audit"),
            call(5, "ledger_rebuild"),
            call(6, "import_orchestrator_agent", snapshot=str(tmp_path / "none")),
        )
    )
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {"home_backup", "ledger_audit", "ledger_rebuild", "import_orchestrator_agent"} <= names
    assert not {"restore", "quiesce", "host_render"} & names
    assert payload(replies[3])["kind"] == "backup"
    assert (tmp_path / "bak" / "backup-manifest.json").is_file()
    assert payload(replies[4])["ok"] is True
    assert payload(replies[5])["kind"] == "ledger_rebuild"
    assert payload(replies[6])["details"]["reason"] == "g_user"


@pytest.mark.use_case("UC-CONFIG-002")
def test_a_broken_configuration_is_repaired_over_mcp_and_the_cli(home: Path) -> None:
    config_file = home / "config" / "task-steward.json"
    config_file.write_text("{ broken", encoding="utf-8")
    good = {"version": 1, "repositories": {"demo": {"path": "~/src/demo"}}}
    words = {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": "orch-9:msg-fix",
        "text": "按原样修好",
        "target": ["config", hashlib.sha256(b"{ broken").hexdigest(), "authorize"],
    }
    replies = by_id(
        mcp(
            home,
            home,
            "orch-9",
            INIT,
            NOTE,
            call(1, "lease_acquire"),
            call(2, "task_list"),
            call(
                3,
                "config_change",
                ops=[{"op": "set", "path": "", "value": good}],
                reason="修复",
                user_source=words,
            ),
            call(4, "config_show"),
        )
    )
    assert "expires_at" in payload(replies[1])
    assert payload(replies[3])["events"][0]["data"]["change_id"] == "C001"
    assert payload(replies[4])["valid"] is True

    # The command line (outside a host session) repairs the same way.
    config_file.write_text("{ broken again", encoding="utf-8")
    words = {
        **words,
        "message_ref": "cli:msg-fix",
        "target": ["config", hashlib.sha256(b"{ broken again").hexdigest(), "authorize"],
    }
    inp = {
        "ops": [{"op": "set", "path": "", "value": good}],
        "reason": "修复",
        "user_source": words,
    }
    code, out = run_json(
        "tool",
        "run",
        "config_change",
        "--input",
        json.dumps(inp, ensure_ascii=False),
        "--role",
        "orchestrator",
        "--session",
        "orch-9",
        "--machine",
        socket.gethostname(),
    )
    assert code == 0, out
    assert out["events"][0]["data"]["change_id"] == "C002"
    assert json.loads(config_file.read_text(encoding="utf-8")) == good
