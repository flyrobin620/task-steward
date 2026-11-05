"""``tsw review run``: one recorded self-review round against an injected reviewer."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw.errors import ErrorCode, TswError
from tsw.home import Home
from tsw.host.identity import SESSION_ENV
from tsw.host.mcp import Server
from tsw.locks import FileLock
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
    # Any existing program will do: the reviewer itself is injected.
    return Harness(home, {"roles": {"reviewer": {"executable": sys.executable}}})


def live_worker(h: Harness, tmp_path: Path, workdir: Path | None = None) -> Path:
    h.register("T001", kind="code" if workdir else "research")
    h.plan("T001")
    if workdir is None:
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
        result("draft-1", HIGH, verdict="changes_requested"), result("draft-2", verdict="pass")
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
    out = review(h, workdir, head="draft-2")
    assert out["reviewed_head"] == "draft-2"
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


def git_repo(path: Path) -> tuple[str, str]:
    """A repository whose main has one commit and whose task branch adds one: (base, head)."""
    path.mkdir(parents=True)

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    run("init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "t"),
        ("user.email", ""),  # git accepts an empty address; the repo is throwaway
        ("core.autocrlf", "false"),
        ("core.safecrlf", "false"),
    ):
        run("config", key, value)
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-q", "-m", "base")
    base = run("rev-parse", "HEAD")
    run("checkout", "-q", "-b", "task")
    (path / "b.txt").write_text("b\n", encoding="utf-8")
    run("add", "b.txt")
    run("commit", "-q", "-m", "work")
    return base, run("rev-parse", "HEAD")


def events(h: Harness) -> list[str]:
    return [e.type for e in h.ledger.storage.read()]


def test_git_scope_is_checked_before_the_round_opens(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "repo"
    base, head = git_repo(workdir)
    live_worker(h, tmp_path, workdir)
    fake = FakeReviewer(result(head[:7]))
    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", fake)
    for bad, reason in (
        ({"base": f"--output={tmp_path / 'outside.txt'}"}, "invalid_base"),
        ({"base": "no-such-ref"}, "invalid_base"),
        ({"head": "-p"}, "invalid_head"),
    ):
        with pytest.raises(TswError) as refused:
            review(h, workdir, **bad)
        assert refused.value.details["reason"] == reason
    assert not (tmp_path / "outside.txt").exists()
    assert "review.started" not in events(h)
    out = review(h, workdir)  # base defaults to where the task branch left main
    assert out["reviewed_head"] == head  # the abbreviation resolves to this round's commit
    meta = h.home.root / "runs" / "T001" / "a1" / "review" / "r1-run.json"
    assert json.loads(meta.read_text(encoding="utf-8"))["base"] == base
    assert f"git diff {base}..{head}" in fake.calls[0]["prompt"]


def test_a_failed_round_is_retried_on_its_recorded_scope(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "repo"
    base, head = git_repo(workdir)
    live_worker(h, tmp_path, workdir)
    fake = FakeReviewer(FileNotFoundError("codex.exe"), result(head))
    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", fake)
    first = review(h, workdir, base=base)
    assert first["outcome"] == "review.failed" and "FileNotFoundError" in first["error"]
    with pytest.raises(TswError) as moved:
        review(h, workdir, base=head)  # not the base this round was opened with
    assert moved.value.details["reason"] == "retry_scope_mismatch"
    out = review(h, workdir)  # no options: the recorded base and head are used
    assert out["round"] == 1 and out["verdict"] == "pass"
    assert f"git diff {base}..{head}" in fake.calls[1]["prompt"]


def test_a_reviewer_that_names_another_head_fails_the_round(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = live_worker(h, tmp_path)
    fake = FakeReviewer(result("draft-1"))
    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", fake)
    out = review(h, workdir, head="draft-10")
    assert out["outcome"] == "review.failed" and "draft-1" in out["error"]


def test_one_round_at_a_time_and_a_missing_reviewer_opens_none(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = live_worker(h, tmp_path)
    monkeypatch.setitem(review_run.REVIEWERS, "codex-app-server", FakeReviewer(result("d1")))
    lock = FileLock(h.home.locks_dir / "review-T001-a1.lock", timeout=0)
    with lock, pytest.raises(TswError) as busy:
        review(h, workdir, head="d1")
    assert busy.value.details["reason"] == "review_in_progress"
    Harness(h.home, {"roles": {"reviewer": {"executable": str(tmp_path / "nope.exe")}}})
    with pytest.raises(TswError) as missing:
        review(h, workdir, head="d1")
    assert missing.value.details["reason"] == "reviewer_unavailable"
    assert "review.started" not in events(h)


def test_every_request_carries_the_current_requirements(h: Harness, tmp_path: Path) -> None:
    live_worker(h, tmp_path)
    task = {**h.task("T001"), "revision": 2, "goal": "NEW_GOAL", "acceptance": '["NEW_ACC"]'}
    previous = {"round": 1, "reviewed_head": "d1", "cycle": 1, "findings": []}
    incremental = review_run.render_request(
        h.home,
        h.config,
        task,
        workdir=tmp_path,
        is_git=False,
        round_no=2,
        head="d2",
        base=None,
        notes=None,
        previous=previous,
    )
    assert "增量评审" in incremental and "NEW_GOAL" in incremental and "NEW_ACC" in incremental
    fresh = review_run.render_request(
        h.home,
        h.config,
        task,
        workdir=tmp_path,
        is_git=False,
        round_no=3,
        head="d3",
        base=None,
        notes=None,
        previous=None,
        superseded=True,
    )
    assert "需求已变更为 revision 2" in fresh and "新线程" in fresh
