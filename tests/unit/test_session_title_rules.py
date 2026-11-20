"""The rules of worker session titles (DN002 §5.8): titles handed out, the title a task's state
calls for, what a session still needs, and the routine job's message."""

from __future__ import annotations

from typing import Any

import pytest

from tsw.config import DEFAULTS, EffectiveConfig
from tsw.machine import routine as routine_mod
from tsw.machine import session_titles as titles

pytestmark = pytest.mark.use_case("UC-SESSION-001")


def config(**sessions: Any) -> EffectiveConfig:
    return EffectiveConfig(values={"sessions": {**DEFAULTS["sessions"], **sessions}}, sources={})


def row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "task_id": "T001",
        "attempt": "a1",
        "session_ref": "w-1",
        "status": "in_progress",
        "phase": "live",
        "base": "T001 短标题",
        "known": "T001 短标题",
        "user_title": None,
        "failed_title": None,
        "archive_failed": 0,
    }
    values.update(overrides)
    return values


def test_titles_handed_out() -> None:
    assert titles.base_title("T001", "任务", " T001 短标题 ") == "T001 短标题"
    assert titles.base_title("T001", "任务", None) == "T001 任务"
    long = titles.base_title("T001", "很" * 80, "")
    assert long.startswith("T001 ") and len(long) == titles.BASE_LIMIT
    # The status comes first, like the other prefixes (T302).
    assert titles.creation_title("T001 x", "prestart", config()) == "[待命] T001 x"
    assert titles.creation_title("T001 x", "dispatch", config()) == "T001 x"
    assert titles.creation_title("T001 x", "prestart", config(title_on_standby="")) == "T001 x"


@pytest.mark.parametrize(
    ("status", "phase", "wanted"),
    [
        ("ready", "standby", ("[待命] T001 x", "standby")),
        ("waiting_dependency", "standby", ("[待命] T001 x", "standby")),
        ("in_progress", "live", ("T001 x", "dispatch")),
        ("done", "live", ("T001 x", "dispatch")),
        ("accepted", "live", ("[已验收] T001 x", "accepted")),
        ("cancelled", "stopped", ("[已取消] T001 x", "cancelled")),
    ],
)
def test_the_title_a_state_calls_for(status: str, phase: str, wanted: tuple[str, str]) -> None:
    assert titles.wanted(status, phase, "T001 x", config()) == wanted


def test_prefixes_come_from_the_configuration() -> None:
    custom = config(title_on_accept="✅", title_on_cancel="", title_on_standby="⏸")
    assert titles.wanted("ready", "standby", "T001 x", custom) == ("⏸ T001 x", "standby")
    assert titles.wanted("accepted", "live", "T001 x", custom) == ("✅ T001 x", "accepted")
    assert titles.wanted("cancelled", "live", "T001 x", custom) == ("T001 x", "cancelled")


def test_what_a_session_still_needs() -> None:
    plain = config()
    assert titles._next(row(), plain) is None  # pyright: ignore[reportPrivateUsage]
    accepted = titles._next(row(status="accepted"), plain)  # pyright: ignore[reportPrivateUsage]
    assert accepted is not None
    assert (accepted["action"], accepted["title"], accepted["state"]) == (
        "title",
        "[已验收] T001 短标题",
        "due",
    )
    # A user's title is never touched; a failed step is listed but not due.
    assert titles._next(row(status="accepted", user_title="我的"), plain) is None  # pyright: ignore[reportPrivateUsage]
    failed = titles._next(  # pyright: ignore[reportPrivateUsage]
        row(status="accepted", failed_title="[已验收] T001 短标题"), plain
    )
    assert failed is not None and failed["state"] == "failed"
    # With the archive turned on, it comes after the title, whatever became of the title.
    archive = config(archive_on_accept=True)
    first = titles._next(row(status="accepted"), archive)  # pyright: ignore[reportPrivateUsage]
    assert first is not None and first["action"] == "title"
    for settled in (
        {"known": "[已验收] T001 短标题"},
        {"user_title": "我的"},
        {"failed_title": "[已验收] T001 短标题"},
    ):
        then = titles._next(row(status="accepted", **settled), archive)  # pyright: ignore[reportPrivateUsage]
        assert then is not None and (then["action"], then["state"]) == ("archive", "due")
    gave_up = titles._next(  # pyright: ignore[reportPrivateUsage]
        row(status="accepted", known="[已验收] T001 短标题", archive_failed=1), archive
    )
    assert gave_up is not None and gave_up["state"] == "failed"
    assert then["via"] == "sessions.archive_on_accept"
    # A cancelled task's session is not archived until the task is.
    assert titles._next(row(status="cancelled", known="[已取消] T001 短标题"), archive) is None  # pyright: ignore[reportPrivateUsage]


def test_an_archived_tasks_session_is_archived_not_renamed() -> None:
    # T302: once the task is archived its sessions still open are archived
    # (sessions.archive_on_archive, on by default), whatever their title.
    plain = config()
    for kept in (
        {"phase": "live", "known": "[已验收] T001 短标题"},
        {"phase": "live", "known": "T001 短标题"},  # its prefix never came: no rename now
        {"phase": "stopped", "user_title": "我的"},
        {"phase": "standby", "known": "T001 短标题（待命）"},
    ):
        item = titles._next(row(status="archived", **kept), plain)  # pyright: ignore[reportPrivateUsage]
        assert item is not None
        assert (item["action"], item["reason"], item["via"], item["state"]) == (
            "archive",
            "archived",
            "sessions.archive_on_archive",
            "due",
        )
        assert "title" not in item
    failed = titles._next(row(status="archived", archive_failed=1), plain)  # pyright: ignore[reportPrivateUsage]
    assert failed is not None and failed["state"] == "failed"
    # Turned off, or disposed of already (closed): nothing.
    off = config(archive_on_archive=False)
    assert titles._next(row(status="archived"), off) is None  # pyright: ignore[reportPrivateUsage]
    assert titles._next(row(status="archived", phase="closed"), plain) is None  # pyright: ignore[reportPrivateUsage]
    # archive_on_accept alone does not archive an archived task's session.
    only_accept = config(archive_on_accept=True, archive_on_archive=False)
    assert titles._next(row(status="archived"), only_accept) is None  # pyright: ignore[reportPrivateUsage]


def test_a_standby_title_of_the_old_form_is_renamed() -> None:
    # A standby session from before T302 still shows the title tsw gave it, "<base>（待命）": it
    # is renamed to the prefixed form; a title the user set stays.
    old = titles._next(row(status="ready", phase="standby", known="T001 短标题（待命）"), config())  # pyright: ignore[reportPrivateUsage]
    assert old is not None
    assert (old["action"], old["title"], old["state"]) == ("title", "[待命] T001 短标题", "due")
    assert (
        titles._next(  # pyright: ignore[reportPrivateUsage]
            row(status="ready", phase="standby", known="T001 短标题（待命）", user_title="我的"),
            config(),
        )
        is None
    )


def test_what_get_session_returned() -> None:
    seen = {"sessionId": "w-1", "title": "x", "isArchived": False}
    assert titles._observed(seen) == seen  # pyright: ignore[reportPrivateUsage]
    assert titles._observed('{"sessionId": "w-1", "title": "x"}') == {  # pyright: ignore[reportPrivateUsage]
        "sessionId": "w-1",
        "title": "x",
    }
    for other in ("Session not found", '{"sessionId": "w-1"}', None, ["x"]):
        assert titles._observed(other) is None  # pyright: ignore[reportPrivateUsage]


def test_the_session_scan_message() -> None:
    assert routine_mod.session_scan_text([]) == ""
    text = routine_mod.session_scan_text(
        [
            {
                "task_id": "T001",
                "attempt": "a1",
                "session_id": "w-1",
                "action": "title",
                "title": "[已验收] T001 x",
                "reason": "accepted",
            },
            {
                "task_id": "T002",
                "attempt": "a2",
                "session_id": "w-2",
                "action": "archive",
                "reason": "accepted",
                "via": "sessions.archive_on_accept",
            },
            {
                "task_id": "T003",
                "attempt": "a1",
                "session_id": "w-3",
                "action": "archive",
                "reason": "archived",
                "via": "sessions.archive_on_archive",
            },
        ]
    )
    assert text.startswith("TaskSteward 例行项 session-scan")
    assert "T001 a1，worker 会话 w-1：改标题为“[已验收] T001 x”（任务已验收）" in text
    assert "T002 a2，worker 会话 w-2：按 sessions.archive_on_accept 归档（任务已验收）" in text
    assert "T003 a1，worker 会话 w-3：按 sessions.archive_on_archive 归档（任务已归档）" in text


def test_titles_that_count_as_tsws() -> None:
    # Only the title tsw last knew and the one it last asked for: a title that merely looks like
    # one tsw would make is the user's (T272 review r2).
    assert titles.tsw_titles(row(asked=None)) == {"T001 短标题"}
    assert titles.tsw_titles(row(asked="[已验收] T001 短标题")) == {
        "T001 短标题",
        "[已验收] T001 短标题",
    }
    assert "[已取消] T001 短标题" not in titles.tsw_titles(row(asked=None))
