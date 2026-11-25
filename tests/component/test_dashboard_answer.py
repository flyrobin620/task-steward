"""Answering a worker's question in the Dashboard (DN006 §8.10 "在 Dashboard 中回答问题", T357).

The app delivers the words the user confirmed to the worker's session inbox, the way the workdeck
daemon does; the real UserPromptSubmit hook then records them and the worker registers the answer.
The session's inbox is a fake one in the test's own sessions folder (tests/fixtures/fake_inbox.py);
the hook, the ledger and the app are the real ones.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_inbox import TOKEN, FakeInbox
from tests.machine_harness import MACHINE, Harness
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import session_inbox
from tsw.host.guard import Guard
from tsw.host.session_inbox import key_file
from tsw.host.status import AppView, render
from tsw.host.status_app import StatusApp
from tsw.machine.base import Caller

pytestmark = pytest.mark.use_case("UC-DIRECT-013")
COMMAND = "dashboard-answer-0001"


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks run on the machine that holds the lease (the harness's)."""
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


@pytest.fixture
def sessions(tmp_path: Path) -> Path:
    return tmp_path / "claude" / "sessions"


@pytest.fixture
def worker(h: Harness) -> Caller:
    h.register("T001")
    h.plan("T001")
    return h.dispatch("T001")


@pytest.fixture
def inbox(sessions: Path, worker: Caller) -> Iterator[FakeInbox]:
    found = FakeInbox(sessions, str(worker.session_id))
    yield found
    found.close()


@pytest.fixture
def app(h: Harness, sessions: Path) -> StatusApp:
    served = StatusApp(h.home, token="t" * 32)
    served.sessions_dir = sessions
    return served


def ask(h: Harness, worker: Caller, **extra: Any) -> str:
    out = h.run("ask_user", worker, task_id="T001", question="用方案 A 还是 B？", **extra)
    return str(out["events"][0]["data"]["request_id"])


def say(h: Harness, worker: Caller, prompt: str) -> None:
    """What the host does with the delivered message: the real UserPromptSubmit hook runs."""
    env = {"CLAUDE_CODE_HOST_SESSION_ID": str(worker.session_id)}
    payload = {"cwd": str(h.home.root), "prompt": prompt, "hook_event_name": "UserPromptSubmit"}
    Guard(h.home, env, payload).run("UserPromptSubmit")


def last_message(h: Harness) -> dict[str, Any]:
    return h.query("SELECT * FROM session_messages ORDER BY sequence DESC LIMIT 1")[0]


def answer(app: StatusApp, request_id: str, text: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    body = {"request_id": request_id, "text": text, "command_id": COMMAND, **extra}
    status, result = app.answer("T001", body)
    return int(status), result


@pytest.mark.transition("T11")
@pytest.mark.transition("T12")
def test_an_option_goes_to_the_worker_session_and_the_worker_registers_it(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp
) -> None:
    request_id = ask(h, worker, options=["方案 A", "方案 B"])
    assert h.query("SELECT options FROM input_options") == [{"options": '["方案 A", "方案 B"]'}]
    sequence = h.ledger.storage.tail().sequence

    status, result = answer(app, request_id, "方案 A")
    assert (status, result["state"]) == (HTTPStatus.OK, "sent")
    assert TOKEN not in str(result)
    # Delivered as the user's message, the way workdeck does: the words only, as confirmed.
    [sent] = inbox.wait(1)
    assert sent["from"] == session_inbox.SENDER and sent["priority"] == "next"
    assert sent["message"] == {"role": "user", "content": "方案 A"}
    assert sent["token"] == TOKEN
    # The app recorded nothing itself.
    assert h.ledger.storage.tail().sequence == sequence
    assert app.answer_status("T001", f"request_id={request_id}&command_id={COMMAND}")["state"] == (
        "sent"
    )

    say(h, worker, "方案 A")
    message = last_message(h)
    assert (message["request_id"], message["text"]) == (request_id, "方案 A")
    progress = app.answer_status("T001", f"request_id={request_id}&command_id={COMMAND}")
    assert (progress["state"], progress["message_id"]) == ("received", message["message_id"])

    h.run(
        "user_answer",
        worker,
        task_id="T001",
        request_id=request_id,
        message_id=message["message_id"],
        answer="用方案 A",
    )
    assert h.status("T001") == "in_progress"
    progress = app.answer_status("T001", f"request_id={request_id}&command_id={COMMAND}")
    assert progress["state"] == "registered"


def test_a_retry_is_not_delivered_twice(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp
) -> None:
    request_id = ask(h, worker)
    assert answer(app, request_id, "用 B，理由见设计")[0] == HTTPStatus.OK
    again = answer(app, request_id, "用 B，理由见设计")
    assert again[0] == HTTPStatus.OK and again[1]["duplicate"] is True
    assert len(inbox.wait(1)) == 1
    # Another command_id with the words already in the session (a retry after a restart).
    say(h, worker, "用 B，理由见设计")
    status, result = app.answer(
        "T001",
        {"request_id": request_id, "text": "用 B，理由见设计", "command_id": "dashboard-answer-02"},
    )
    assert (status, result["duplicate"], result["state"]) == (HTTPStatus.OK, True, "received")
    assert len(inbox.messages()) == 1
    # The page follows that answer by its own command_id too (review F5).
    progress = app.answer_status("T001", f"request_id={request_id}&command_id=dashboard-answer-02")
    assert progress["state"] == "received"
    # One command_id is one answer.
    status, result = answer(app, request_id, "改成 A")
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "command_reused")


def handed_off_and_answered(h: Harness, request_id: str) -> None:
    """The question times out, and the orchestrator answers it within its authority."""
    h.clock.now = "2026-11-05T00:10:00Z"
    h.run("sync")
    h.run(
        "handoff_resolve",
        task_id="T001",
        request_id=request_id,
        decision="answer",
        answer="用 A：与现有接口一致",
        rationale="任务书要求兼容现有接口",
        basis=["任务书 目标"],
        confidence="high",
    )


@pytest.mark.transition("T12")
def test_a_question_answered_elsewhere_is_not_sent_unless_the_user_insists(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp
) -> None:
    request_id = ask(h, worker)
    handed_off_and_answered(h, request_id)
    status, result = answer(app, request_id, "还是 B 吧")
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "answered")
    assert result["effective"] == {"by": "主会话代答", "text": "用 A：与现有接口一致"}
    assert inbox.messages() == []
    # 「仍发给 worker」: delivered, and the hook ties it to this very question, so the worker
    # records it as the late answer that prevails over the orchestrator's (DN006 §8.1c).
    status, result = answer(app, request_id, "还是 B 吧", late=True)
    assert (status, result["state"], result["late"]) == (HTTPStatus.OK, "sent", True)
    assert [m["message"]["content"] for m in inbox.wait(1)] == ["还是 B 吧"]
    say(h, worker, "还是 B 吧")
    message = last_message(h)
    assert message["request_id"] == request_id
    out = h.run(
        "user_answer",
        worker,
        task_id="T001",
        request_id=request_id,
        message_id=message["message_id"],
        answer="用 B",
    )
    assert out["events"][0]["type"] == "input.late_response"
    progress = app.answer_status("T001", f"request_id={request_id}&command_id={COMMAND}")
    assert (progress["state"], progress["late"]) == ("received", True)


def test_a_late_answer_never_lands_on_another_question(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp
) -> None:
    # Answered in the worker's session already: a message there no longer belongs to it.
    first = ask(h, worker)
    say(h, worker, "A")
    h.run(
        "user_answer",
        worker,
        task_id="T001",
        request_id=first,
        message_id=last_message(h)["message_id"],
        answer="A",
    )
    status, result = answer(app, first, "B", late=True)
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "changed")
    # Answered by the orchestrator, then the worker asked again: the words for the first
    # question would land on the second, so they do not go out (review F1).
    second = ask(h, worker)
    handed_off_and_answered(h, second)
    third = ask(h, worker)
    status, result = app.answer(
        "T001",
        {"request_id": second, "text": "B", "command_id": "dashboard-answer-03", "late": True},
    )
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "changed")
    assert inbox.messages() == []
    assert h.query("SELECT status FROM input_requests WHERE request_id = ?", third) == [
        {"status": "pending"}
    ]


def test_no_running_session_means_nothing_is_sent(
    h: Harness, worker: Caller, app: StatusApp, sessions: Path
) -> None:
    request_id = ask(h, worker)
    status, result = answer(app, request_id, "A")
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "not_running")
    # A registry entry whose pipe is gone (the app quit, the entry stayed) is no better.
    sessions.mkdir(parents=True)
    pipe = r"\\.\pipe\tsw-test-gone" if os.name == "nt" else str(sessions / "gone.sock")
    entry = {"pid": 7, "hostSessionId": worker.session_id, "messagingSocketPath": pipe}
    (sessions / "7.json").write_text(json.dumps(entry), encoding="utf-8")
    key_file(sessions, 7, pipe).write_text(json.dumps({"peerToken": "t"}), encoding="utf-8")
    status, result = answer(app, request_id, "A")
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "not_running")
    # Without its key file nothing is written either.
    key_file(sessions, 7, pipe).unlink()
    status, result = answer(app, request_id, "A")
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "failed")


def test_a_question_that_changed_is_refused(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp
) -> None:
    request_id = ask(h, worker)
    status, result = answer(app, "Q-T001-9", "A")
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "changed")
    h.run(
        "task_cancel",
        task_id="T001",
        user_source=h.source("T001", 1, "cancel"),
        worker_stopped=True,
    )
    status, result = answer(app, request_id, "A")
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "changed")
    assert inbox.messages() == []


def test_the_request_is_checked(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp
) -> None:
    request_id = ask(h, worker)
    assert answer(app, request_id, "   ")[0] == HTTPStatus.BAD_REQUEST
    assert answer(app, request_id, "x" * 4001)[0] == HTTPStatus.BAD_REQUEST
    status, _ = app.answer("T001", {"request_id": request_id, "text": "A", "command_id": "short"})
    assert status == HTTPStatus.BAD_REQUEST
    assert answer(app, request_id, "A", late="yes")[0] == HTTPStatus.BAD_REQUEST
    assert inbox.messages() == []


def test_an_error_while_writing_is_unknown_and_the_retry_checks_first(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = ask(h, worker)
    real = session_inbox._write  # pyright: ignore[reportPrivateUsage]

    def broken(conn: Any, payload: bytes) -> None:
        real(conn, payload)
        raise OSError("broken pipe after writing")

    monkeypatch.setattr(session_inbox, "_write", broken)
    status, result = answer(app, request_id, "A")
    assert (status, result["code"]) == (HTTPStatus.BAD_GATEWAY, "unknown")
    monkeypatch.setattr(session_inbox, "_write", real)
    # It did arrive, but the hook has not recorded it yet: the retry writes nothing (review F2).
    assert len(inbox.wait(1)) == 1
    status, result = answer(app, request_id, "A")
    assert (status, result["duplicate"], result["unconfirmed"]) == (HTTPStatus.OK, True, True)
    assert result["state"] == "sent"
    assert len(inbox.wait(2, timeout=0.5)) == 1
    # Once the hook ran, the same retry shows it received.
    say(h, worker, "A")
    status, result = answer(app, request_id, "A")
    assert (status, result["duplicate"], result["state"]) == (HTTPStatus.OK, True, "received")
    assert "unconfirmed" not in result
    assert len(inbox.messages()) == 1


def test_ask_user_options_are_checked(h: Harness, worker: Caller) -> None:
    for bad in (["A"] * 2, [f"o{i}" for i in range(7)], ["x" * 81], [""], "A"):
        with pytest.raises(TswError) as caught:
            ask(h, worker, options=bad)
        assert caught.value.details.get("reason") == "invalid_input"
    ask(h, worker)
    assert h.query("SELECT * FROM input_options") == []


def test_the_card_shows_the_question_options_and_outputs_and_the_static_page_no_controls(
    h: Harness, worker: Caller
) -> None:
    h.run(
        "output_link",
        worker,
        task_id="T001",
        output_id="design",
        type="files",
        ref="work/T001/a1/design.html",
        role="设计草稿",
        evidence=["e"],
    )
    request_id = ask(h, worker, options=["方案 A", "方案 B"], reserved=True)
    with h.ledger.view() as conn:
        page = render(conn, AppView(token="t", actions={}))
        static = render(conn)
    card = page[page.index('id="task-T001"') :]
    card = card[: card.index("</article>")]
    assert 'qa-card" id="task-T001"' in page
    assert f'data-request="{request_id}"' in card and "🔒 只能由你回答" in card
    assert card.count('class="qa-opt"') == 2 and "<textarea" in card
    assert "design" in card and "设计草稿" in card
    assert "其他产出" not in card  # listed in the panel, not again under 「详情」
    assert "等待用户（" not in card  # the old note is gone
    still = static[static.index('id="task-T001"') :]
    still = still[: still.index("</article>")]
    assert "<textarea" not in still and "在 Dashboard 应用或 worker 会话中回答" in still


def test_words_delivered_for_a_question_that_moved_on_answer_no_other(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp
) -> None:
    """The question moves on between the delivery and the hook (review r2 F1): answered
    elsewhere and a new one asked. The delivered words then answer no question at all."""
    first = ask(h, worker)
    assert answer(app, first, "A")[0] == HTTPStatus.OK
    inbox.wait(1)
    handed_off_and_answered(h, first)
    second = ask(h, worker)
    say(h, worker, "A")  # the session reads the message only now
    assert last_message(h)["request_id"] == ""
    assert h.query("SELECT status FROM input_requests WHERE request_id = ?", second) == [
        {"status": "pending"}
    ]
    # The note is used up: the user's own "A" typed later answers the open question as usual.
    say(h, worker, "A")
    assert last_message(h)["request_id"] == second


def test_a_delivery_that_did_not_go_out_leaves_the_users_own_words_alone(
    h: Harness, worker: Caller, app: StatusApp
) -> None:
    first = ask(h, worker)
    assert answer(app, first, "A")[1]["code"] == "not_running"
    say(h, worker, "A")
    assert last_message(h)["request_id"] == first


def test_a_retry_after_the_answer_was_registered_says_so(
    h: Harness, worker: Caller, inbox: FakeInbox, app: StatusApp
) -> None:
    request_id = ask(h, worker)
    assert answer(app, request_id, "A")[0] == HTTPStatus.OK
    inbox.wait(1)
    say(h, worker, "A")
    h.run(
        "user_answer",
        worker,
        task_id="T001",
        request_id=request_id,
        message_id=last_message(h)["message_id"],
        answer="A",
    )
    # The response was lost, the page retries the same answer (review r2 F6).
    status, result = answer(app, request_id, "A")
    assert (status, result["duplicate"], result["state"]) == (HTTPStatus.OK, True, "registered")
    # An older page still showing the question learns the answer, not "nothing sent".
    status, result = app.answer(
        "T001", {"request_id": request_id, "text": "B", "command_id": "dashboard-answer-04"}
    )
    assert (status, result["code"]) == (HTTPStatus.CONFLICT, "answered")
    assert result["effective"]["text"] == "A"
    assert len(inbox.messages()) == 1
