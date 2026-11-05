"""``tsw review run``: one recorded self-review round against an injected reviewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw.errors import ErrorCode, TswError
from tsw.home import Home
from tsw.host.identity import SESSION_ENV
from tsw.host.mcp import Server
from tsw.machine.base import Caller
from tsw.review import run as review_run
from tsw.review.codex import ReviewRun

pytestmark = pytest.mark.use_case("UC-REVIEW-002")

WORKER = Caller("worker", "w-1", "m1")


class FakeReviewer:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, prompt: str, **options: Any) -> ReviewRun:
        self.calls.append({"prompt": prompt, **options})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return ReviewRun(
            thread_id=f"thread-{len(self.calls)}"
            if not options["resume_thread"]
            else options["resume_thread"],
            status="completed",
            final_message=text,
            model=options["model"],
        )


def result(head: str, *findings: dict[str, Any], verdict: str = "pass") -> dict[str, Any]:
    return {"verdict": verdict, "summary": "s", "reviewed_head": head, "findings": list(findings)}


HIGH = {"id": "F1", "severity": "high", "category": "correctness", "claim": "c", "evidence": "e"}


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def live_worker(h: Harness, tmp_path: Path) -> Path:
    h.register("T001", kind="research")
    h.plan("T001")
    workdir = h.home.root / "work" / "T001" / "a1"
    workdir.mkdir(parents=True)
    h.run("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    return workdir


def review(h: Harness, workdir: Path, **options: Any) -> dict[str, Any]:
    return review_run.run_round(h.home, {SESSION_ENV: "w-1"}, workdir, machine="m1", **options)


def test_a_round_is_recorded_with_its_request_and_result(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = live_worker(h, tmp_path)
    fake = FakeReviewer(result("draft-1", HIGH, verdict="changes_requested"))
    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", fake)
    out = review(h, workdir, head="draft-1", notes="已运行 demo 校验")
    assert (out["round"], out["verdict"], out["blocking"]) == (1, "changes_requested", ["F1"])
    folder = h.home.root / "runs" / "T001" / "a1" / "review"
    request = (folder / "r1-request.md").read_text(encoding="utf-8")
    assert "第 1 轮评审（新线程）" in request and "已运行 demo 校验" in request
    assert '"reviewed_head":"draft-1"' in request
    assert json.loads((folder / "r1.json").read_text(encoding="utf-8"))["findings"][0]["id"] == "F1"
    meta = json.loads((folder / "r1-run.json").read_text(encoding="utf-8"))
    assert (meta["thread_id"], meta["model"], meta["resumed"]) == ("thread-1", "gpt-6-astra", None)
    assert fake.calls[0]["cwd"] == workdir and fake.calls[0]["effort"] == "medium"
    types = [e.type for e in h.ledger.storage.read()]
    assert types[-2:] == ["review.started", "review.completed"]
    assert h.task("T001")["substate"] == "triaging"


def test_the_next_round_resumes_the_thread_with_the_triage(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = live_worker(h, tmp_path)
    fake = FakeReviewer(
        result("draft-1", HIGH, verdict="changes_requested"), result("draft-2abc", verdict="pass")
    )
    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", fake)
    review(h, workdir, head="draft-1")
    h.run(
        "review_triage",
        WORKER,
        task_id="T001",
        round=1,
        items=[{"finding": "F1", "action": "fix"}],
    )
    h.run("review_decide", WORKER, task_id="T001", round=1, decision="iterate", head="draft-1")
    out = review(h, workdir, head="draft-2abcdef")
    assert out["reviewed_head"] == "draft-2abcdef"  # the short form was expanded
    assert fake.calls[1]["resume_thread"] == "thread-1"
    prompt = fake.calls[1]["prompt"]
    assert "第 2 轮增量评审" in prompt and "F1（high）：fix" in prompt


def test_unusable_answers_are_recorded_as_failed_rounds(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = live_worker(h, tmp_path)
    fake = FakeReviewer(
        "我觉得没问题",
        {"verdict": "fine"},
        TswError(ErrorCode.UNAVAILABLE, "codex app-server 无响应", reason="reviewer_timeout"),
    )
    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", fake)
    first = review(h, workdir, head="d1")
    assert (first["ok"], first["outcome"]) == (False, "review.failed")
    raw = h.home.root / "runs" / "T001" / "a1" / "review" / "r1-raw.txt"
    assert raw.read_text(encoding="utf-8") == "我觉得没问题"
    second = review(h, workdir, head="d1")
    assert "契约" in second["error"]
    third = review(h, workdir, head="d1")
    assert third["outcome"] == "review.unavailable"  # retry_limit (2) used up
    assert "reviewer_timeout" in third["error"]


def test_only_the_bound_worker_in_its_directory_may_review(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = live_worker(h, tmp_path)
    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", FakeReviewer())
    for env, cwd, reason in (
        ({SESSION_ENV: "orch-1"}, h.home.root, "role_denied"),
        ({SESSION_ENV: "w-1"}, tmp_path, "cwd_mismatch"),
    ):
        with pytest.raises(TswError) as denied:
            review_run.run_round(h.home, env, cwd, head="d1", machine="m1")
        assert denied.value.details["reason"] == reason
    monkeypatch.delitem(review_run.REVIEWERS, "codex-app-server")
    with pytest.raises(TswError) as missing:
        review(h, workdir, head="d1")
    assert missing.value.code is ErrorCode.UNSUPPORTED
    assert [e.type for e in h.ledger.storage.read()][-1] != "review.started"


@pytest.mark.use_case("UC-HOST-001")
def test_dispatching_a_research_task_creates_its_work_directory(h: Harness) -> None:
    h.register("T001", kind="research")
    h.plan("T001")
    server = Server(h.home, {SESSION_ENV: "orch-1"}, h.home.root)
    server.machine = "m1"
    workdir = h.home.root / "work" / "T001" / "a1"
    server.call("task_dispatch", {"task_id": "T001", "workdir": str(workdir), "user_turn": True})
    assert workdir.is_dir()
