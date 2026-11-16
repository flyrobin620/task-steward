"""The Dashboard's standing-preferences page and its record, replace and revoke on a real data
directory, ledger and SQLite (UC-DIRECT-007, DN006 §8.10, DN007 §6.2, T290)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tsw import lease, preferences
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.status_app import StatusApp

mark = pytest.mark


@pytest.fixture
def h(home: Home) -> Harness:
    harness = Harness(home)
    # The harness's orchestrator, on another machine, hands the lease back: the user may write.
    harness.ledger.commit(lease.release("orch-1", machine=MACHINE, actor=ORCH.actor()))
    return harness


@pytest.fixture
def app(h: Harness) -> StatusApp:
    return StatusApp(h.home, token="tok")


def sources(h: Harness, kind: str) -> list[dict[str, Any]]:
    return [e.data["source"] for e in h.ledger.storage.read(since=0) if e.type == kind]


def reason(app: StatusApp, action: str, ref: str | None, **body: Any) -> str:
    with pytest.raises(TswError) as exc:
        app.preference(action, ref, body)
    return str(exc.value.details.get("reason") or exc.value.details.get("field"))


def state(h: Harness) -> dict[str, dict[str, Any]]:
    with h.ledger.view() as conn:
        return {str(i["ref"]): i for i in preferences.load(conn, None)}


@mark.use_case("UC-DIRECT-007")
def test_the_page_records_replaces_and_revokes_as_the_user(h: Harness, app: StatusApp) -> None:
    out = app.preference(
        "record", None, {"scope": "global", "topic": "画图", "text": "用 A\n再用 B"}
    )
    assert (out["ref"], out["event"], out["channel"]) == (
        "P-1",
        "user.preference_recorded",
        "status-app",
    )
    # The source is the user's own, on the page's channel, with the words typed there.
    first = sources(h, "user.preference_recorded")[-1]
    assert first["channel"] == "status-app" and first["message_ref"].startswith("status-app:")
    assert first["text"] == "用 A\n再用 B" and first["target"] == [
        "preference",
        "global",
        "standing",
    ]
    assert "@" in first["principal"]
    # Replace in the same scope: a new number, the old one replaced.
    out = app.preference(
        "record",
        None,
        {"scope": "global", "topic": "画图", "text": "只用 B", "supersedes": ["P-1"]},
    )
    assert out["ref"] == "P-2"
    # Replace a global one in a narrower scope: it holds there only.
    app.preference(
        "record",
        None,
        {"scope": " repo:demo ", "topic": "画图", "text": "demo 用 C", "supersedes": ["P-2"]},
    )
    now = state(h)
    assert now["P-1"]["superseded_by"] == "P-2" and not now["P-2"].get("superseded_by")
    assert now["P-2"]["overridden_in"] == [{"ref": "P-3", "scope": "repo:demo"}]
    assert sources(h, "user.preference_recorded")[-1]["target"] == [
        "preference",
        "repo:demo",
        "standing",
    ]
    # Revoke with the reason typed on the page.
    out = app.preference("revoke", "P-3", {"reason": "demo 不再特殊"})
    assert (out["ref"], out["event"]) == ("P-3", "user.preference_revoked")
    gone = sources(h, "user.preference_revoked")[-1]
    assert gone["channel"] == "status-app" and gone["text"] == "demo 不再特殊"
    assert gone["target"] == ["preference", "P-3", "revoke"]
    now = state(h)
    assert now["P-3"]["revoked"]["reason"] == "demo 不再特殊" and "overridden_in" not in now["P-2"]
    # Each request has its own message reference.
    refs = [s["message_ref"] for s in sources(h, "user.preference_recorded")]
    assert len(set(refs)) == 3
    # The page shows what the ledger holds now.
    page = app.preferences_page("all=1", "tok")
    assert re.findall(r'<article class="pref (\w+)" id="pref-(P-\d)"', page) == [
        ("replaced", "P-1"),
        ("current", "P-2"),
        ("revoked", "P-3"),
    ]
    assert (
        "撤销：Dashboard（status-app）" in page and '<meta name="tsw-token" content="tok">' in page
    )
    assert "现行 1 条 · 共 3 条" in app.preferences_page("", "tok")


@mark.use_case("UC-DIRECT-007")
def test_the_tools_rules_refuse_and_nothing_is_written(h: Harness, app: StatusApp) -> None:
    app.preference("record", None, {"scope": "repo:demo", "topic": "PR", "text": "draft"})
    app.preference(
        "record",
        None,
        {"scope": "repo:demo", "topic": "PR", "text": "ready", "supersedes": ["P-1"]},
    )
    before = h.ledger.storage.tail()
    # Missing or malformed fields, from the app itself.
    assert reason(app, "record", None, scope="global", topic="t") == "invalid_input"
    assert reason(app, "record", None, scope="global", topic="t", text="  ") == "invalid_input"
    assert reason(app, "record", None, scope="global", topic="t", text="x", supersedes="P-2") == (
        "invalid_input"
    )
    assert reason(app, "record", None, scope="global", topic="t", text="x", supersedes=[1]) == (
        "invalid_input"
    )
    assert reason(app, "revoke", "P-2") == "invalid_input"
    assert reason(app, "revoke", "P-2", reason=7) == "invalid_input"
    assert reason(app, "rename", "P-2", reason="x") == "invalid_input"
    # The tool's rules: a scope of the right form, only one in force may be replaced or revoked,
    # a replacement as narrow or narrower.
    assert reason(app, "record", None, scope="team:x", topic="t", text="x") == "invalid_input"
    for gone in ("P-1", "P-9"):
        assert reason(
            app, "record", None, scope="repo:demo", topic="t", text="x", supersedes=[gone]
        ) == ("invalid_input"), gone
        assert reason(app, "revoke", gone, reason="x") == "invalid_input", gone
    assert reason(app, "record", None, scope="global", topic="t", text="x", supersedes=["P-2"]) == (
        "invalid_input"
    )
    app.preference("revoke", "P-2", {"reason": "不要了"})
    after = h.ledger.storage.tail()
    assert reason(app, "revoke", "P-2", reason="again") == "invalid_input"
    assert reason(
        app, "record", None, scope="repo:demo", topic="t", text="x", supersedes=["P-2"]
    ) == ("invalid_input")
    assert h.ledger.storage.tail() == after
    assert after.sequence == before.sequence + 1


@mark.use_case("UC-DIRECT-007")
def test_a_retry_with_the_same_command_id_records_once(h: Harness, app: StatusApp) -> None:
    """A response lost after the commit, then the same submission again (PR #53 review)."""
    body = {
        "scope": "global",
        "topic": "PR",
        "text": "默认 ready",
        "command_id": "dashboard-pref-1",
    }
    first = app.preference("record", None, body)
    again = app.preference("record", None, dict(body))
    assert (again["ref"], again["sequence"]) == (first["ref"], first["sequence"])
    assert len(sources(h, "user.preference_recorded")) == 1
    # A narrower override of a global one, retried: still one.
    narrow = {
        "scope": "repo:demo",
        "topic": "PR",
        "text": "demo 先开 draft",
        "supersedes": ["P-1"],
        "command_id": "dashboard-pref-2",
    }
    assert app.preference("record", None, narrow)["ref"] == "P-2"
    assert app.preference("record", None, dict(narrow))["ref"] == "P-2"
    assert len(sources(h, "user.preference_recorded")) == 2
    revoke = {"reason": "不要了", "command_id": "dashboard-pref-3"}
    first = app.preference("revoke", "P-2", revoke)
    assert app.preference("revoke", "P-2", dict(revoke))["sequence"] == first["sequence"]
    # The same id with other content, or after the app restarted (a new message reference),
    # is refused rather than recorded twice.
    tail = h.ledger.storage.tail()
    other = {**body, "text": "改了"}
    assert reason(app, "record", None, **other) == "command_conflict"
    restarted = StatusApp(h.home, token="tok")
    assert reason(restarted, "record", None, **body) == "command_conflict"
    assert h.ledger.storage.tail() == tail
    # Without an id every submission stands alone; a malformed id is refused.
    plain = {"scope": "global", "topic": "邮件", "text": "只存草稿"}
    assert (
        app.preference("record", None, plain)["ref"] != app.preference("record", None, plain)["ref"]
    )
    for bad in ("short", "has space in it", 7, "x" * 81):
        assert reason(app, "record", None, **{**plain, "command_id": bad}) == "invalid_input", bad
