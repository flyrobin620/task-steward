"""``tsw reviewer models`` / ``set``, ``tsw setup models`` and the ``reviewer_models`` MCP tool as
separate processes (DN004 §3.4). The reviewer command lines are the model-list stand-in
(``tests/fixtures/fake_model_lists.py``, first on ``PATH``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import child_env, run_json, run_tsw
from tests.surface.test_host_surface import INIT, LIST, NOTE, call, mcp, payload

pytestmark = pytest.mark.use_case("UC-REVIEW-004")


@pytest.fixture
def home() -> Path:
    code, out = run_json("init")
    assert code == 0
    return Path(out["home"])


def config_file(home: Path) -> dict[str, Any]:
    return json.loads((home / "config" / "task-steward.json").read_text(encoding="utf-8"))


def test_the_models_are_listed_with_the_configuration(home: Path) -> None:
    code, out = run_json("--home", str(home), "reviewer", "models")
    assert code == 0 and out["reviewer"] == "codex-app-server" and out["available"]
    assert out["current"]["model"] == "gpt-6-astra"
    assert [m["id"] for m in out["models"] if m["current"]] == ["gpt-6-astra"]
    code, out = run_json("--home", str(home), "reviewer", "models", "--reviewer", "copilot-cli")
    assert code == 0 and "auto" not in [m["id"] for m in out["models"]]
    assert out["preselected"] == {"model": "gpt-6.1-sol", "effort": "medium"}
    text = run_tsw("--home", str(home), "reviewer", "models")
    assert text.returncode == 0
    assert " * gpt-6-astra  GPT-6-Astra  推理强度：low / medium / high / xhigh / max" in text.stdout


def test_the_user_changes_the_reviewer_in_a_terminal(home: Path) -> None:
    args = ["--home", str(home), "reviewer", "set", "--reviewer", "copilot-cli"]
    code, out = run_json(*args, "--model", "gpt-6.2-sol", "--effort", "high", "--yes")
    assert code == 0 and out["changed"] and out["change_id"] == "C001"
    reviewer = config_file(home)["roles"]["reviewer"]
    assert reviewer == {
        "session": "copilot-cli",
        "fallback_session": None,
        "model": "gpt-6.2-sol",
        "effort": "high",
    }
    # Not in the list: refused, nothing written.
    code, err = run_json(*args, "--model", "gpt-9", "--effort", "high", "--yes")
    assert code == 3 and err["details"]["reason"] == "model_not_listed"
    # Not interactive and not --yes: nobody confirms.
    code, err = run_json(*args, "--model", "gpt-6.2-astra", "--effort", "high")
    assert code == 3 and err["details"]["reason"] == "not_confirmed"
    # Not interactive and nothing to pick with.
    code, err = run_json(*args, "--yes")
    assert code == 3 and err["details"]["reason"] == "invalid_input"
    assert config_file(home)["roles"]["reviewer"]["model"] == "gpt-6.2-sol"


def test_a_session_cannot_change_the_reviewer(home: Path) -> None:
    env = child_env(CLAUDE_CODE_HOST_SESSION_ID="any-session")
    code, err = run_json(
        "--home",
        str(home),
        "reviewer",
        "set",
        "--model",
        "gpt-5.5",
        "--effort",
        "high",
        "--yes",
        env=env,
    )
    assert code == 3 and err["details"]["reason"] == "host_session_cli"
    assert not (home / "config" / "task-steward.json").is_file()
    # Listing only reads: a session may.
    code, out = run_json("--home", str(home), "reviewer", "models", env=env)
    assert code == 0 and out["available"]


def test_setup_lists_a_reviewer_for_the_session_entry() -> None:
    code, out = run_json("setup", "models", "--reviewer", "codex-app-server")
    assert code == 0 and out["available"] and out["host_default"] == "gpt-6.1-sol"
    assert out["preselected"] == {"model": "gpt-6-astra", "effort": "medium"}
    efforts = {m["id"]: m["efforts"] for m in out["models"]}
    assert efforts["gpt-5.5"] == ["low", "medium", "high", "xhigh"]


def test_the_main_session_lists_and_writes_with_the_users_words(home: Path) -> None:
    first = {
        r["id"]: r
        for r in mcp(
            home,
            home,
            "orch-9",
            INIT,
            NOTE,
            LIST,
            call(2, "lease_acquire"),
            call(3, "reviewer_models", reviewer="copilot-cli"),
        )
    }
    assert "reviewer_models" in {t["name"] for t in first[1]["result"]["tools"]}
    offered = payload(first[3])
    assert offered["available"] and offered["reviewer"] == "copilot-cli"
    assert offered["example_ops"][0] == {
        "op": "set",
        "path": "roles.reviewer.session",
        "value": "copilot-cli",
    }
    words = {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": "orch-9:msg-1",
        "text": "评审换成 Copilot 的 gpt-6.2-astra，推理强度 high",
        "target": ["config", offered["file_sha256"], "authorize"],
    }
    ops = [
        *offered["example_ops"][:2],
        {"op": "set", "path": "roles.reviewer.model", "value": "gpt-6.2-astra"},
        {"op": "set", "path": "roles.reviewer.effort", "value": "high"},
    ]
    replies = {
        r["id"]: r
        for r in mcp(
            home,
            home,
            "orch-9",
            INIT,
            NOTE,
            call(1, "config_change", ops=ops, reason="用户选了评审模型", user_source=words),
        )
    }
    assert replies[1]["result"].get("isError") is not True, replies[1]
    reviewer = config_file(home)["roles"]["reviewer"]
    assert (reviewer["model"], reviewer["effort"]) == ("gpt-6.2-astra", "high")
