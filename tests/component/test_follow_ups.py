"""Follow-ups of a task (DN002 §5.7): recorded by the worker or the orchestrator, turned into a
task or dismissed, put off until a recheck date, and kept out of the archive while open.

The user's words come from the real ``UserPromptSubmit`` hook; the worker only cites them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.guard import Guard
from tsw.machine.base import Caller
from tsw.tasks import task_row

ITEMS = [
    {"title": "周期对账", "detail": "目标→源清扫与源→目标补齐"},
    {
        "title": "会话 key 文件可被沙箱用户读取",
        "detail": "~/.claude 的 ACL 让 CodexSandboxUsers 可读",
        "kind": "out_of_scope",
        "severity": "high",
    },
]


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks run on the machine that holds the lease (the harness's)."""
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


def reason_of(call: Callable[[], Any]) -> str:
    with pytest.raises(TswError) as exc:
        call()
    return str(exc.value.details.get("reason"))


def say(h: Harness, worker: Caller, prompt: str) -> str:
    """The user types in the worker's session: the real hook records it."""
    env = {"CLAUDE_CODE_HOST_SESSION_ID": str(worker.session_id)}
    payload = {"cwd": str(h.home.root), "prompt": prompt, "hook_event_name": "UserPromptSubmit"}
    Guard(h.home, env, payload).run("UserPromptSubmit")
    rows = h.query("SELECT message_id FROM session_messages ORDER BY sequence DESC LIMIT 1")
    return str(rows[0]["message_id"])


def follow_ups(h: Harness, task_id: str) -> list[dict[str, Any]]:
    with h.ledger.view() as conn:
        return list(task_row(conn, task_id)["follow_ups"])


def attention(h: Harness, now: str) -> list[dict[str, Any]]:
    with h.ledger.view() as conn:
        return inbox_mod.attention(conn, h.ledger.storage, h.config, now=now)["follow_ups"]


def working(h: Harness, task_id: str = "T001") -> Caller:
    h.register(task_id)
    h.plan(task_id)
    return h.dispatch(task_id)


@pytest.mark.use_case("UC-FOLLOWUP-001")
def test_worker_records_follow_ups_without_changing_the_task(home: Home) -> None:
    h = Harness(home)
    worker = working(h)
    before = h.task("T001")
    out = h.run("follow_up_record", worker, task_id="T001", items=ITEMS)
    event = out["events"][0]
    assert event["type"] == "follow_up.recorded"
    assert [i["follow_up_id"] for i in event["data"]["items"]] == ["F-T001-1", "F-T001-2"]
    assert event["data"]["by"] == {
        "role": "worker",
        "attempt": "a1",
        "session_ref": worker.session_id,
    }
    assert "source" not in event["data"]

    after = h.task("T001")
    assert after["status"] == before["status"] == "in_progress"
    assert after["substate"] == before["substate"]
    assert after["revision"] == before["revision"]
    assert after["version"] == before["version"] + 1
    # A follow-up is a record, not business: the archive clock does not restart.
    assert after["business_changed_at"] == before["business_changed_at"]

    rows = follow_ups(h, "T001")
    assert [(r["follow_up_id"], r["kind"], r["severity"], r["status"]) for r in rows] == [
        ("F-T001-1", "remaining", None, "open"),
        ("F-T001-2", "out_of_scope", "high", "open"),
    ]
    assert rows[0]["recorded_by"]["role"] == "worker"
    # Numbers go on after the task's own follow-ups.
    again = h.run("follow_up_record", ORCH, task_id="T001", items=[ITEMS[0]])
    assert again["events"][0]["data"]["items"][0]["follow_up_id"] == "F-T001-3"


@pytest.mark.use_case("UC-FOLLOWUP-001")
def test_follow_up_input_is_checked(home: Home) -> None:
    h = Harness(home)
    worker = working(h)

    def record(**inp: Any) -> Callable[[], Any]:
        return lambda: h.run("follow_up_record", worker, task_id="T001", **inp)

    assert reason_of(record(items=[])) == "invalid_input"
    assert reason_of(record(items=[{"title": "只有标题"}])) == "invalid_input"
    assert reason_of(record(items=[{"title": "两\n行", "detail": "说明"}])) == "invalid_input"
    assert reason_of(record(items=[{**ITEMS[0], "kind": "bug"}])) == "invalid_input"
    assert reason_of(record(items=[{**ITEMS[0], "severity": "urgent"}])) == "invalid_input"
    # An out-of-scope finding says how serious it is (T264 A12).
    assert reason_of(record(items=[{**ITEMS[0], "kind": "out_of_scope"}])) == "invalid_input"
    # Another session is not the task's worker.
    other = Caller(role="worker", session_id="w-other", machine=MACHINE)
    assert (
        reason_of(lambda: h.run("follow_up_record", other, task_id="T001", items=ITEMS))
        == "not_bound_worker"
    )
    assert follow_ups(h, "T001") == []


@pytest.mark.use_case("UC-FOLLOWUP-001")
def test_worker_cites_the_users_words_from_its_own_session(home: Home) -> None:
    h = Harness(home)
    worker = working(h)
    other = working(h, "T002")
    message_id = say(h, worker, "把还没做的事情记录在这个 task 的 follow up 里面")
    elsewhere = say(h, other, "T002 里的话")

    out = h.run(
        "follow_up_record", worker, task_id="T001", items=[ITEMS[0]], message_ids=[message_id]
    )
    assert out["events"][0]["data"]["source"] == {
        "channel": "in-session",
        "principal": "session-owner",
        "message_ref": message_id,
        "text": "把还没做的事情记录在这个 task 的 follow up 里面",
        "request_id": None,
        "target": ["T001", 1, "follow_up"],
    }
    # Only what the host recorded for this task, in the worker's own session.
    for cited in (elsewhere, "M-T001-99"):
        assert (
            reason_of(
                lambda c=cited: h.run(
                    "follow_up_record", worker, task_id="T001", items=ITEMS, message_ids=[c]
                )
            )
            == "g_user"
        )
    # A worker cannot hand in the user's words as its own source.
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_record",
                worker,
                task_id="T001",
                items=ITEMS,
                user_source=h.source("T001", 1, "follow_up"),
            )
        )
        == "invalid_input"
    )


@pytest.mark.use_case("UC-FOLLOWUP-001")
def test_who_records_when(home: Home) -> None:
    h = Harness(home)
    worker = h.complete("T001")
    # Done but not yet accepted by the user: the worker can still record.
    h.run("follow_up_record", worker, task_id="T001", items=[ITEMS[0]])
    message_id = say(h, worker, "这个记成 follow-up")
    h.run("user_accept", task_id="T001", user_source=h.source("T001", 1, "accept"))
    assert h.status("T001") == "accepted"
    assert (
        reason_of(lambda: h.run("follow_up_record", worker, task_id="T001", items=[ITEMS[1]]))
        == "invalid_state"
    )
    # The orchestrator records on an accepted task, with the user's words in its own session or
    # a message the host recorded for the task in any session.
    with_words = h.run(
        "follow_up_record",
        task_id="T001",
        items=[ITEMS[1]],
        user_source=h.source("T001", 1, "follow_up", text="这个记成 follow-up"),
    )
    assert with_words["events"][0]["data"]["source"]["channel"] == "main-session"
    cited = h.run("follow_up_record", task_id="T001", items=[ITEMS[0]], message_ids=[message_id])
    assert cited["events"][0]["data"]["source"]["text"] == "这个记成 follow-up"
    wrong = h.source("T001", 1, "accept")
    assert (
        reason_of(lambda: h.run("follow_up_record", task_id="T001", items=ITEMS, user_source=wrong))
        == "g_user"
    )
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_record",
                task_id="T001",
                items=ITEMS,
                message_ids=[message_id],
                user_source=h.source("T001", 1, "follow_up"),
            )
        )
        == "invalid_input"
    )
    # The orchestrator's writes need the lease.
    stranger = Caller(role="orchestrator", session_id="orch-2", machine=MACHINE)
    assert (
        reason_of(lambda: h.run("follow_up_record", stranger, task_id="T001", items=ITEMS))
        == "lease_held"
    )
    assert [r["follow_up_id"] for r in follow_ups(h, "T001")] == [
        "F-T001-1",
        "F-T001-2",
        "F-T001-3",
    ]


@pytest.mark.use_case("UC-FOLLOWUP-001")
def test_update_open_follow_ups_and_put_one_off(home: Home) -> None:
    h = Harness(home)
    worker = working(h)
    h.run("follow_up_record", worker, task_id="T001", items=[ITEMS[0]])
    h.run("follow_up_record", task_id="T001", items=[ITEMS[1]])

    out = h.run(
        "follow_up_update",
        worker,
        follow_up_id="F-T001-1",
        detail="目标→源清扫；源→目标补齐；与初始全量一起做",
        reason="补充做法",
    )
    assert out["events"][0]["data"]["changes"] == {
        "detail": "目标→源清扫；源→目标补齐；与初始全量一起做"
    }
    # The worker changes only what workers recorded, and never puts anything off.
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_update", worker, follow_up_id="F-T001-2", title="改", reason="r"
            )
        )
        == "role_denied"
    )
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_update",
                worker,
                follow_up_id="F-T001-1",
                recheck_after="2026-12-01",
                reason="r",
            )
        )
        == "role_denied"
    )
    assert (
        reason_of(lambda: h.run("follow_up_update", follow_up_id="F-T001-1", reason="r"))
        == "invalid_input"
    )
    # Only YYYY-MM-DD: attention compares the date as text with today.
    for odd in ("12/01", "20261201", "2026-W49-2", "2026-12-1"):
        assert (
            reason_of(
                lambda d=odd: h.run(
                    "follow_up_update", follow_up_id="F-T001-1", recheck_after=d, reason="r"
                )
            )
            == "invalid_input"
        )
    # Made an out-of-scope finding, it needs a severity; one that has it can change kind freely.
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_update", worker, follow_up_id="F-T001-1", kind="out_of_scope", reason="r"
            )
        )
        == "invalid_input"
    )
    h.run("follow_up_update", follow_up_id="F-T001-2", kind="out_of_scope", reason="仍是范围外")
    # Putting it off is the user's decision.
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_update",
                follow_up_id="F-T001-2",
                recheck_after="2026-12-01",
                reason="暂不处理",
            )
        )
        == "g_user"
    )
    h.run(
        "follow_up_update",
        follow_up_id="F-T001-2",
        recheck_after="2026-12-01",
        severity="medium",
        reason="用户：暂不处理，12 月再看",
        user_source=h.source("T001", 1, "follow_up", text="暂不处理"),
    )
    rows = {r["follow_up_id"]: r for r in follow_ups(h, "T001")}
    assert rows["F-T001-1"]["detail"].endswith("与初始全量一起做")
    assert rows["F-T001-2"]["recheck_after"] == "2026-12-01"
    assert rows["F-T001-2"]["severity"] == "medium"
    assert rows["F-T001-2"]["status"] == "open"

    # attention lists what waits for a decision: not one put off until its date comes.
    assert [f["follow_up_id"] for f in attention(h, "2026-11-30T23:00:00Z")] == ["F-T001-1"]
    assert [f["follow_up_id"] for f in attention(h, "2026-12-01T00:00:00Z")] == [
        "F-T001-1",
        "F-T001-2",
    ]


@pytest.mark.use_case("UC-FOLLOWUP-002")
def test_convert_follow_ups_into_a_new_task(home: Home) -> None:
    h = Harness(home)
    h.complete("T001")
    h.run("follow_up_record", task_id="T001", items=ITEMS)
    # Several follow-ups make one task only with a title of its own.
    assert (
        reason_of(
            lambda: h.run("follow_up_convert", follow_up_ids=["F-T001-1", "F-T001-2"], task={})
        )
        == "invalid_input"
    )
    out = h.run(
        "follow_up_convert",
        follow_up_ids=["F-T001-1", "F-T001-2"],
        task={"title": "T001 的后续"},
        user_source=h.source("T001", 1, "follow_up", text="两项都转成任务"),
    )
    registered, converted = out["events"]
    assert registered["type"] == "task.registered"
    new = registered["task_id"]
    assert new == "T002"
    assert registered["data"]["follow_up_of"] == [
        {"task_id": "T001", "follow_up_id": "F-T001-1"},
        {"task_id": "T001", "follow_up_id": "F-T001-2"},
    ]
    assert registered["data"]["repo"] == "demo" and registered["data"]["kind"] == "code"
    assert registered["data"]["goal"].startswith("来自 T001 的 follow-up：")
    assert "F-T001-2 会话 key 文件可被沙箱用户读取" in registered["data"]["goal"]
    assert registered["data"]["acceptance"] == [
        "完成 F-T001-1：周期对账",
        "完成 F-T001-2：会话 key 文件可被沙箱用户读取",
    ]
    assert converted["type"] == "follow_up.converted"
    assert converted["task_id"] == "T001"
    assert converted["data"]["to_task"] == "T002" and converted["data"]["mode"] == "new"
    assert converted["data"]["source"]["text"] == "两项都转成任务"

    assert h.status("T002") == "intake"
    spec = (home.root / "todo" / "T002.md").read_text(encoding="utf-8")
    assert "F-T001-1 周期对账" in spec
    with h.ledger.view() as conn:
        assert task_row(conn, "T002")["follow_up_of"] == [
            {"task_id": "T001", "follow_up_id": "F-T001-1", "title": "周期对账"},
            {
                "task_id": "T001",
                "follow_up_id": "F-T001-2",
                "title": "会话 key 文件可被沙箱用户读取",
            },
        ]
    rows = follow_ups(h, "T001")
    assert {(r["status"], r["converted_to"]) for r in rows} == {("converted", "T002")}
    assert rows[0]["resolution"]["mode"] == "new"
    # A follow-up is converted once.
    assert (
        reason_of(lambda: h.run("follow_up_convert", follow_up_ids=["F-T001-1"], task={}))
        == "follow_up_closed"
    )
    assert (
        reason_of(lambda: h.run("follow_up_convert", follow_up_ids=["F-T001-9"], task={}))
        == "follow_up_not_found"
    )
    # Only the orchestrator turns follow-ups into tasks.
    worker = Caller(role="worker", session_id="w-T001-1", machine=MACHINE)
    assert (
        reason_of(lambda: h.run("follow_up_convert", worker, follow_up_ids=["F-T001-1"], task={}))
        == "role_denied"
    )


@pytest.mark.use_case("UC-FOLLOWUP-002")
def test_one_follow_up_becomes_a_task_with_its_own_title(home: Home) -> None:
    h = Harness(home, {"repositories": {"demo": {"path": "~/d"}, "other": {"path": "~/o"}}})
    h.register("T001")
    h.register("T002", repo="other", kind="research")
    h.run("follow_up_record", task_id="T001", items=[ITEMS[0]])
    h.run("follow_up_record", task_id="T002", items=[ITEMS[1]])
    out = h.run("follow_up_convert", follow_up_ids=["F-T001-1"], task={"task_id": "T010"})
    assert out["events"][0]["data"]["title"] == "周期对账"
    assert h.status("T010") == "intake"
    # From tasks of different repositories the new task's repository must be named.
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_convert",
                follow_up_ids=["F-T001-1", "F-T002-1"],
                task={"title": "合并"},
            )
        )
        == "follow_up_closed"
    )
    h.run("follow_up_record", task_id="T001", items=[ITEMS[0]])
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_convert",
                follow_up_ids=["F-T001-2", "F-T002-1"],
                task={"title": "合并"},
            )
        )
        == "invalid_input"
    )
    both = h.run(
        "follow_up_convert",
        follow_up_ids=["F-T001-2", "F-T002-1"],
        task={"title": "合并", "repo": "demo", "kind": "code"},
    )
    # One converted event per source task.
    assert [(e["type"], e["task_id"]) for e in both["events"]] == [
        ("task.registered", "T011"),
        ("follow_up.converted", "T001"),
        ("follow_up.converted", "T002"),
    ]
    assert both["events"][0]["data"]["goal"].startswith("来自 T001、T002 的 follow-up：")


@pytest.mark.use_case("UC-FOLLOWUP-002")
def test_merge_follow_ups_into_a_task_already_registered(home: Home) -> None:
    h = Harness(home)
    h.complete("T001")
    h.register("T002")
    h.register("T003")
    h.run("task_cancel", task_id="T003", user_source=h.source("T003", 1, "cancel"))
    h.run("follow_up_record", task_id="T001", items=ITEMS)
    before = h.task("T002")
    assert (
        reason_of(lambda: h.run("follow_up_convert", follow_up_ids=["F-T001-1"])) == "invalid_input"
    )
    assert (
        reason_of(
            lambda: h.run(
                "follow_up_convert", follow_up_ids=["F-T001-1"], task={}, into_task="T002"
            )
        )
        == "invalid_input"
    )
    for into, reason in (("T003", "invalid_state"), ("T001", "invalid_input")):
        assert (
            reason_of(
                lambda i=into: h.run("follow_up_convert", follow_up_ids=["F-T001-1"], into_task=i)
            )
            == reason
        )
    out = h.run("follow_up_convert", follow_up_ids=["F-T001-1", "F-T001-2"], into_task="T002")
    assert [e["type"] for e in out["events"]] == ["follow_up.converted"]
    assert out["events"][0]["data"]["mode"] == "existing"
    # Only linked: the task's own requirement stays as it was.
    after = h.task("T002")
    assert (after["goal"], after["acceptance"], after["revision"]) == (
        before["goal"],
        before["acceptance"],
        before["revision"],
    )
    with h.ledger.view() as conn:
        assert [f["follow_up_id"] for f in task_row(conn, "T002")["follow_up_of"]] == [
            "F-T001-1",
            "F-T001-2",
        ]


@pytest.mark.use_case("UC-FOLLOWUP-002")
def test_a_follow_up_cannot_go_into_its_own_task(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    h.run("follow_up_record", task_id="T001", items=[ITEMS[0]])
    assert (
        reason_of(lambda: h.run("follow_up_convert", follow_up_ids=["F-T001-1"], into_task="T001"))
        == "invalid_input"
    )


@pytest.mark.use_case("UC-FOLLOWUP-003")
@pytest.mark.transition("T24")
@pytest.mark.transition("T26")
def test_dismiss_and_open_follow_ups_keep_a_task_out_of_the_archive(home: Home) -> None:
    h = Harness(home)
    h.complete("T001")
    h.run("follow_up_record", task_id="T001", items=ITEMS)
    h.run("user_accept", task_id="T001", user_source=h.source("T001", 1, "accept"))
    h.clock.now = "2026-11-30T00:00:00Z"
    h.run("sync")
    assert h.status("T001") == "accepted"

    assert (
        reason_of(lambda: h.run("follow_up_dismiss", follow_up_ids=["F-T001-1"])) == "invalid_input"
    )
    out = h.run(
        "follow_up_dismiss",
        follow_up_ids=["F-T001-1"],
        reason="已在 T005 中完成",
        user_source=h.source("T001", 1, "follow_up", text="不用做了"),
    )
    data = out["events"][0]["data"]
    assert data["follow_up_ids"] == ["F-T001-1"] and data["reason"] == "已在 T005 中完成"
    h.run("sync")
    assert h.status("T001") == "accepted"  # F-T001-2 is still open
    h.run("follow_up_dismiss", follow_up_ids=["F-T001-2"], reason="用户决定不做")
    assert (
        reason_of(lambda: h.run("follow_up_dismiss", follow_up_ids=["F-T001-2"], reason="r"))
        == "follow_up_closed"
    )
    h.run("sync")
    assert h.status("T001") == "archived"
    rows = follow_ups(h, "T001")
    assert [r["status"] for r in rows] == ["dismissed", "dismissed"]
    assert rows[0]["resolution"]["source"]["text"] == "不用做了"
    # An archived task takes no more follow-ups.
    assert (
        reason_of(lambda: h.run("follow_up_record", task_id="T001", items=ITEMS)) == "invalid_state"
    )

    # A cancelled task is archived only once its follow-ups are decided.
    h.register("T002")
    h.run("task_cancel", task_id="T002", user_source=h.source("T002", 1, "cancel"))
    h.run("follow_up_record", task_id="T002", items=[ITEMS[0]])
    archive = h.source("T002", 1, "archive")
    assert (
        reason_of(lambda: h.run("task_archive", task_id="T002", user_source=archive))
        == "follow_ups_open"
    )
    h.run("follow_up_convert", follow_up_ids=["F-T002-1"], task={})
    h.run("task_archive", task_id="T002", user_source=archive)
    assert h.status("T002") == "archived"


@pytest.mark.use_case("UC-FOLLOWUP-004")
def test_rebuild_gives_the_same_follow_ups(home: Home) -> None:
    h = Harness(home)
    worker = working(h)
    h.run("follow_up_record", worker, task_id="T001", items=ITEMS)
    h.run("follow_up_update", worker, follow_up_id="F-T001-1", title="对账", reason="缩短")
    h.run("follow_up_convert", follow_up_ids=["F-T001-1"], task={})
    h.run("follow_up_dismiss", follow_up_ids=["F-T001-2"], reason="不做")
    query = "SELECT * FROM follow_ups ORDER BY follow_up_id"
    before = h.query(query)
    h.ledger.rebuild()
    assert h.query(query) == before
    assert [r["status"] for r in before] == ["converted", "dismissed"]
    assert before[0]["title"] == "对账"
