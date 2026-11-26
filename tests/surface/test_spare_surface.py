"""The spare-session pool (DN002 §5.9, T363) through the real MCP server, hook processes and the
Dashboard's HTTP interface, each a separate process. The sessions' registry and inboxes are fakes in
the test's own ``CLAUDE_CONFIG_DIR`` (tests/fixtures/fake_inbox.py)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_inbox import TOKEN, FakeInbox
from tests.machine_harness import Harness
from tests.surface.test_direct_surface import (  # pyright: ignore[reportPrivateUsage]
    App,
    _serve,
    handed_back,
)
from tests.surface.test_host_surface import INIT, LIST, NOTE, call, mcp, payload
from tests.surface.test_routine_surface import by_id, hook
from tsw.home import Home

# The Dashboard and the hooks read the real clock, the harness's is in the past.
YOUNG = {"scheduling": {"spare": {"enabled": True, "count": 2, "max_age_hours": 1_000_000}}}


@pytest.mark.use_case("UC-SPARE-001")
@pytest.mark.use_case("UC-SPARE-003")
def test_a_spare_session_over_stdio_and_hooks(home: Home) -> None:
    handed_back(Harness(home, YOUNG))  # the configuration; this machine's orch-1 takes the lease
    root = home.root
    started = by_id(
        mcp(
            root,
            root,
            "orch-1",
            INIT,
            NOTE,
            call(1, "lease_acquire"),
            call(2, "spare_start", user_turn=True),
        )
    )
    out = payload(started[2])
    cwd = Path(out["cwd"])
    assert cwd.is_dir() and cwd.name == "S1" and out["title"] == "[备用] S1"
    # Before the reconcile the new session sees only whoami, and calls wait.
    pending = by_id(
        mcp(root, cwd, "s-1", INIT, NOTE, LIST, call(2, "spare_moved", outcome="moved"))
    )
    assert {t["name"] for t in pending[1]["result"]["tools"]} == {"whoami"}
    reconciled = by_id(
        mcp(
            root,
            root,
            "orch-1",
            INIT,
            NOTE,
            call(1, "spare_reconcile", spare_id="S1", outcome="created", session_ref="s-1"),
            call(2, "attention"),
        )
    )
    assert "error" not in reconciled[1]
    assert payload(reconciled[2])["spares"][0]["available"] == []  # not running: to retire
    me = by_id(mcp(root, cwd, "s-1", INIT, NOTE, call(1, "whoami")))
    assert payload(me[1])["role"] == "spare" and payload(me[1])["spare_id"] == "S1"
    # The hooks: told to wait, nothing done before a claim; the user's words claim it.
    start = hook(root, "s-1", "SessionStart", cwd, hook_event_name="SessionStart")
    assert "备用会话 S1" in start["hookSpecificOutput"]["additionalContext"]
    denied = hook(
        root, "s-1", "PreToolUse", cwd, tool_name="Write", tool_input={"file_path": "x.txt"}
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    claimed = hook(root, "s-1", "UserPromptSubmit", cwd, prompt="你好，帮我看个问题")
    assert "set_session_title" in claimed["hookSpecificOutput"]["additionalContext"]
    after = by_id(mcp(root, cwd, "s-1", INIT, NOTE, call(1, "whoami")))
    assert payload(after[1])["claimed_as"] == "chat" and payload(after[1])["role"] == "none"


@pytest.fixture
def app(home: Home) -> Iterator[App]:
    Harness(home, YOUNG)
    yield from _serve("status", "serve")


def signed_in(app: App) -> None:
    status, headers, _ = app.request("GET", f"/?token={app.token}")
    assert status == 303
    app.cookie = headers["Set-Cookie"].split(";")[0]


def post(app: App, body: dict[str, Any], token: bool = True) -> tuple[int, Any]:
    headers = {"Origin": f"http://127.0.0.1:{app.port}"}
    if token:
        headers["X-TSW-Token"] = app.token
    status, _, raw = app.request("POST", "/api/sessions/new", body, headers)
    return status, json.loads(raw)


@pytest.mark.use_case("UC-SPARE-003")
def test_dashboard_new_session_over_http(home: Home, app: App) -> None:
    h = Harness(home, YOUNG)
    signed_in(app)
    body = {"text": "看看 workdeck 的日志", "command_id": "new-session-s1"}
    status, result = post(app, body)
    assert (status, result["code"]) == (409, "no_spare")
    h.run("spare_start", user_turn=True)
    h.run("spare_reconcile", spare_id="S1", outcome="created", session_ref="s-1")
    handed_back(h)  # the Dashboard writes on this machine
    inbox = FakeInbox(Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions", "s-1")
    try:
        status, _, page = app.request("GET", "/")
        assert status == 200 and 'id="new-session"' in page.decode("utf-8")
        status, _, raw = app.request("GET", "/api/spares")
        pool = json.loads(raw)
        assert status == 200 and [a["spare_id"] for a in pool["available"]] == ["S1"]
        # The same admission as the other buttons: the page's token in the header.
        assert post(app, body, token=False)[0] == 403
        status, result = post(app, body)
        assert status == 200, result
        assert (result["state"], result["spare_id"]) == ("sent", "S1")
        assert TOKEN not in json.dumps(result)
        [sent] = inbox.wait(1)
        assert (sent["from"], sent["message"]["content"]) == ("tsw-dashboard", body["text"])
        status, result = post(app, body)
        assert (status, result["duplicate"]) == (200, True)
        assert len(inbox.wait(2, timeout=1)) == 1
    finally:
        inbox.close()
    rows = h.query("SELECT status, claimed_as, claim_via FROM spares")
    assert rows == [{"status": "claimed", "claimed_as": "chat", "claim_via": "status-app"}]


@pytest.mark.use_case("UC-SPARE-002")
def test_a_task_claims_a_spare_over_stdio(home: Home) -> None:
    h = Harness(home, YOUNG)  # the harness's repository has no checkout: a plain folder
    h.register("T001")
    h.plan("T001")
    h.run("spare_start", user_turn=True)
    h.run("spare_reconcile", spare_id="S1", outcome="created", session_ref="s-1")
    handed_back(h)
    root = home.root
    orch = by_id(
        mcp(
            root,
            root,
            "orch-1",
            INIT,
            NOTE,
            call(1, "lease_acquire"),
            call(2, "task_dispatch", task_id="T001", user_turn=False, spare=True),
            call(
                3,
                "attempt_start",
                task_id="T001",
                purpose="dispatch",
                user_turn=False,
                spare="S1",
            ),
        )
    )
    started = payload(orch[3])
    assert started["session_id"] == "s-1" and "[TaskSteward 派发]" in started["claim_message"]
    target = Path(started["target"])
    bound = by_id(
        mcp(
            root,
            root,
            "orch-1",
            INIT,
            NOTE,
            call(
                1,
                "attempt_reconcile",
                task_id="T001",
                attempt="a1",
                generation=started["generation"],
                outcome="created",
                session_ref="s-1",
                brief_delivered=True,
            ),
        )
    )
    assert "error" not in bound[1]
    # The session makes its folder (the claim message's step 1) and records the move.
    target.mkdir(parents=True)
    spare_dir = root / "spares" / "S1"
    moved = by_id(
        mcp(
            root,
            spare_dir,
            "s-1",
            INIT,
            NOTE,
            call(1, "whoami"),
            call(2, "spare_moved", outcome="moved"),
        )
    )
    assert payload(moved[1])["role"] == "worker" and payload(moved[1])["cwd_consistent"] is True
    out = payload(moved[2])
    assert out["events"][0]["type"] == "spare.moved"
    assert out["wake"]["session_id"] == "orch-1" and "[TaskSteward 开始]" in out["wake"]["message"]
