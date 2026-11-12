"""The worker instructions have one source and open every worker session's prompt (DN006 §6.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw.errors import TswError
from tsw.home import Home, init_home
from tsw.host.claude_code import render_instructions
from tsw.host.guard import Guard
from tsw.host.identity import SESSION_ENV
from tsw.host.mcp import Server
from tsw.ledger import Ledger
from tsw.roles import worker_copy, worker_source

pytestmark = pytest.mark.use_case("UC-HOST-007")

# What a worker is told to do with tsw: the rules live in the worker instructions only.
RULE_WORDS = (
    "review_run",
    "review_triage",
    "review_decide",
    "self_check_failed",
    "ask_user",
    "user_answer",
    "requirement_change",
    "follow_up_record",
    "question",
    "submit",
    "send_message",
    "wake",
    "gh pr merge",
    "wrap_up",
)


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def source_text() -> str:
    return worker_source().read_text(encoding="utf-8").replace("\r\n", "\n")


def brief(h: Harness, task_id: str, attempt: str = "a1") -> str:
    return (h.home.root / "runs" / task_id / attempt / "brief.md").read_text(encoding="utf-8")


def head_and_line(prompt: str) -> tuple[str, str]:
    """The prompt split into the instructions and the line after them."""
    head, sep, line = prompt.partition("\n\n---\n\n")
    assert sep, prompt[-200:]
    return head, line.strip()


def guard(h: Harness, session: str, cwd: Path, **payload: Any) -> Guard:
    g = Guard(h.home, {SESSION_ENV: session}, {"cwd": str(cwd), **payload})
    g.machine = "m1"
    g.ledger = Ledger(h.home, clock=h.clock, machine="m1")
    return g


def context_of(out: dict[str, Any] | None) -> str:
    return "" if out is None else str(out["hookSpecificOutput"]["additionalContext"])


def prestart(h: Harness, task_id: str, session: str) -> dict[str, Any]:
    started = h.run("attempt_start", task_id=task_id, purpose="prestart", user_turn=True)
    data = started["events"][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id=task_id,
        attempt=data["attempt"],
        generation=data["generation"],
        outcome="created",
        session_ref=session,
    )
    return started


def test_init_and_render_put_the_source_into_the_data_directory(home: Home, tmp_path: Path) -> None:
    copy = worker_copy(home.root)
    assert copy == home.root / "roles" / "worker.md"
    assert copy.read_bytes() == worker_source().read_bytes()
    # init only fills a data directory that has none; render brings it back to the source.
    copy.write_text("旧说明", encoding="utf-8")
    init_home(home.root)
    assert copy.read_text(encoding="utf-8") == "旧说明"
    assert copy in render_instructions(home)
    assert copy.read_bytes() == worker_source().read_bytes()
    copy.unlink()
    init_home(home.root)
    assert copy.read_bytes() == worker_source().read_bytes()
    fresh, created = init_home(tmp_path / "fresh")
    assert created and worker_copy(fresh.root).read_bytes() == worker_source().read_bytes()


def test_the_session_prompt_starts_with_the_worker_instructions(h: Harness, tmp_path: Path) -> None:
    # Prestart: the instructions, then the standby line.
    h.register("T001")
    h.plan("T001")
    started = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    head, line = head_and_line(started["prompt"])
    assert head == source_text().rstrip()
    assert line.startswith("你是 TaskSteward 任务 T001 的预启动待命会话（attempt a1）")
    assert "runs/T001/a1/brief.md" in line and "不要开始工作" in line
    digest = started["events"][0]["data"]["worker_instructions_sha256"]
    assert len(digest) == 64
    # The brief is the task alone.
    assert brief(h, "T001").startswith("# 任务书 T001 a1\n")
    assert "# TaskSteward worker" not in brief(h, "T001")
    # Dispatch: the instructions, then the line that sends the worker to its brief.
    h.register("T002")
    h.plan("T002")
    h.run("task_dispatch", task_id="T002", workdir=str(tmp_path / "w2"), user_turn=True)
    assert "# TaskSteward worker" not in brief(h, "T002")
    started = h.run("attempt_start", task_id="T002", purpose="dispatch", user_turn=True)
    head, line = head_and_line(started["prompt"])
    assert head == source_text().rstrip()
    assert line == (
        "你是 TaskSteward 任务 T002 的 worker（attempt a1）。先调用 whoami，"
        "再按任务书 runs/T002/a1/brief.md 执行。"
    )
    # The MCP server hands the prompt to the main session too, next to the cwd.
    h.register("T003")
    h.plan("T003")
    server = Server(h.home, {SESSION_ENV: "orch-1"}, h.home.root)
    server.machine = "m1"
    out = server.call(
        "attempt_start", {"task_id": "T003", "purpose": "prestart", "user_turn": True}
    )
    assert out["prompt"].startswith(source_text().rstrip()) and out["cwd"]


@pytest.mark.parametrize(
    ("state", "reason"),
    [("missing", "worker_instructions_missing"), ("stale", "worker_instructions_stale")],
)
def test_no_session_without_current_instructions(
    h: Harness, tmp_path: Path, state: str, reason: str
) -> None:
    h.register("T001")
    h.plan("T001")
    h.register("T002")
    h.plan("T002")
    # Dispatch itself does not create a session and goes ahead; starting the session does not.
    h.run("task_dispatch", task_id="T002", workdir=str(tmp_path / "w2"), user_turn=True)
    copy = worker_copy(h.home.root)
    if state == "missing":
        copy.unlink()
    else:
        copy.write_text(source_text() + "\n- 本地改过的一条\n", encoding="utf-8")
    before = h.query("SELECT task_id, attempt, phase, generation FROM attempts ORDER BY task_id")
    with pytest.raises(TswError) as early:
        h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert early.value.details["reason"] == reason
    assert "tsw host render" in str(early.value)
    assert not (h.home.root / "work" / "T001").exists()
    assert not (h.home.root / "runs" / "T001").exists()
    with pytest.raises(TswError) as late:
        h.run("attempt_start", task_id="T002", purpose="dispatch", user_turn=True)
    assert late.value.details["reason"] == reason
    assert not (tmp_path / "w2").exists()
    after = h.query("SELECT task_id, attempt, phase, generation FROM attempts ORDER BY task_id")
    assert after == before
    # Rendering again is the fix.
    render_instructions(h.home)
    started = h.run("attempt_start", task_id="T002", purpose="dispatch", user_turn=True)
    assert started["prompt"].startswith(source_text().rstrip())


def test_a_worker_is_told_only_its_own_session(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    prestart(h, "T001", "w-1")
    workdir = h.home.root / "work" / "T001" / "a1"
    text = context_of(guard(h, "w-1", workdir).run("SessionStart"))
    assert "T001" in text and "attempt a1" in text and "runs/T001/a1/brief.md" in text
    assert "待命中" in text and "提示语开头" in text
    assert str(worker_copy(h.home.root)) in text
    # No second copy of the rules: each rule word is in the instructions, none in the hook text.
    instructions = source_text()
    words = text.replace(str(h.home.root), "")  # the test's own paths are not rules
    for word in RULE_WORDS:
        assert word in instructions, word
        assert word not in words, word


def test_a_session_hears_once_of_newer_instructions(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    prestart(h, "T001", "w-1")
    workdir = h.home.root / "work" / "T001" / "a1"
    copy = worker_copy(h.home.root)
    # Created with the current instructions: nothing to say.
    assert "重读" not in context_of(guard(h, "w-1", workdir).run("SessionStart"))
    assert guard(h, "w-1", workdir, prompt="继续").run("UserPromptSubmit") is None
    # The instructions change (a product update and tsw host render): told once.
    copy.write_text(source_text() + "\n- 新的一条\n", encoding="utf-8")
    told = context_of(guard(h, "w-1", workdir, prompt="继续").run("UserPromptSubmit"))
    assert "有更新" in told and str(copy) in told
    assert guard(h, "w-1", workdir, prompt="再继续").run("UserPromptSubmit") is None
    assert "有更新" not in context_of(guard(h, "w-1", workdir).run("SessionStart"))
    # A session from before the prompt carried the instructions has no record: told too.
    (h.home.root / "cache" / "worker-instructions").joinpath(
        h.query("SELECT session_name FROM attempts")[0]["session_name"]
    ).unlink()
    first = context_of(guard(h, "w-1", workdir).run("SessionStart"))
    assert "还没有附上 worker 说明" in first and str(copy) in first
