"""The worker instructions have one source and every brief starts with them (DN006 §6.2)."""

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


def starts_with_instructions(text: str, task_id: str, attempt: str = "a1") -> bool:
    head = source_text().rstrip("\n") + "\n\n"
    return text.startswith(head) and text[len(head) :].startswith(f"# 任务书 {task_id} {attempt}\n")


def guard(h: Harness, session: str, cwd: Path) -> Guard:
    g = Guard(h.home, {SESSION_ENV: session}, {"cwd": str(cwd)})
    g.machine = "m1"
    g.ledger = Ledger(h.home, clock=h.clock, machine="m1")
    return g


def change(h: Harness, task_id: str, revision: int, goal: str) -> dict[str, Any]:
    return h.run(
        "task_update",
        task_id=task_id,
        goal=goal,
        acceptance=["新验收"],
        user_source=h.source(task_id, revision, "change"),
    )


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


def test_every_brief_starts_with_the_worker_instructions(h: Harness, tmp_path: Path) -> None:
    # Prestart writes the brief, a requirement change rewrites it, dispatch writes it again.
    h.register("T001")
    h.plan("T001")
    h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert starts_with_instructions(brief(h, "T001"), "T001")
    change(h, "T001", 1, "第二版目标")
    text = brief(h, "T001")
    assert starts_with_instructions(text, "T001") and "第二版目标" in text
    assert text.count("# TaskSteward worker") == 1
    # A task dispatched without a prestarted session gets one as well.
    h.register("T002")
    h.plan("T002")
    h.run("task_dispatch", task_id="T002", workdir=str(tmp_path / "w2"), user_turn=True)
    assert starts_with_instructions(brief(h, "T002"), "T002")


@pytest.mark.parametrize(
    ("state", "reason"),
    [("missing", "worker_instructions_missing"), ("stale", "worker_instructions_stale")],
)
def test_no_brief_without_current_instructions(
    h: Harness, tmp_path: Path, state: str, reason: str
) -> None:
    h.register("T001")
    h.plan("T001")
    h.register("T002")
    h.plan("T002")
    h.run("attempt_start", task_id="T002", purpose="prestart", user_turn=True)
    copy = worker_copy(h.home.root)
    if state == "missing":
        copy.unlink()
    else:
        copy.write_text(source_text() + "\n- 本地改过的一条\n", encoding="utf-8")
    before = h.query("SELECT task_id, status, revision FROM tasks ORDER BY task_id")
    # Prestart refuses before the execution directory is made.
    with pytest.raises(TswError) as prestart:
        h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    assert prestart.value.details["reason"] == reason
    assert "tsw host render" in str(prestart.value)
    assert not (h.home.root / "work" / "T001").exists()
    # Dispatch refuses and writes no brief.
    with pytest.raises(TswError) as dispatch:
        h.run("task_dispatch", task_id="T001", workdir=str(tmp_path / "w1"), user_turn=True)
    assert dispatch.value.details["reason"] == reason
    assert not (h.home.root / "runs" / "T001").exists()
    # A requirement change that would rewrite a brief leaves the task card as it was.
    card = h.home.root / "todo" / "T002.md"
    card_before = card.read_bytes()
    brief_before = brief(h, "T002")
    with pytest.raises(TswError) as changed:
        change(h, "T002", 1, "第二版目标")
    assert changed.value.details["reason"] == reason
    assert card.read_bytes() == card_before and brief(h, "T002") == brief_before
    assert h.query("SELECT task_id, status, revision FROM tasks ORDER BY task_id") == before
    # Rendering again is the fix.
    render_instructions(h.home)
    h.run("task_dispatch", task_id="T001", workdir=str(tmp_path / "w1"), user_turn=True)
    assert starts_with_instructions(brief(h, "T001"), "T001")


def test_a_worker_is_told_only_its_own_session_and_pointed_at_the_brief(h: Harness) -> None:
    h.register("T001")
    h.plan("T001")
    started = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)
    data = started["events"][0]["data"]
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=data["generation"],
        outcome="created",
        session_ref="w-1",
    )
    out = guard(h, "w-1", h.home.root / "work" / "T001" / "a1").run("SessionStart")
    assert out is not None
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "T001" in text and "attempt a1" in text and "runs/T001/a1/brief.md" in text
    assert "待命中" in text and "plugin/roles/worker.md" in text and "开头" in text
    # No second copy of the rules: each rule word is in the instructions, none in the hook text.
    instructions = source_text()
    words = text.replace(str(h.home.root), "")  # the test's own paths are not rules
    for word in RULE_WORDS:
        assert word in instructions, word
        assert word not in words, word
