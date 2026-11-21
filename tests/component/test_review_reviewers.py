"""The ``claude-subagent`` and ``copilot-cli`` reviewers (DN004 §3, CC004, CP004), and turning
self-review off (DN004 §9).

The Copilot CLI is replaced by ``tests/fixtures/fake_copilot.py`` (a platform outside this
repository); the subagent's answer is what a worker would hand back. Everything else is real:
the ledger, the request files, git and the reviewer process handling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.component.test_review_run import HIGH, WORKER, git_repo, live_worker, result
from tests.machine_harness import Harness
from tests.support import REPO_ROOT
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.identity import SESSION_ENV
from tsw.review import copilot
from tsw.review import run as review_run

pytestmark = pytest.mark.use_case("UC-REVIEW-002")

FAKE = REPO_ROOT / "tests" / "fixtures" / "fake_copilot.py"
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "copilot" / "final-answer.jsonl"
SUBAGENT = {"session": "claude-subagent", "model": "sonnet", "effort": "high"}


def review(h: Harness, workdir: Path, **options: Any) -> dict[str, Any]:
    return review_run.run_round(h.home, {SESSION_ENV: "w-1"}, workdir, machine="m1", **options)


def review_dir(h: Harness) -> Path:
    return h.home.root / "runs" / "T001" / "a1" / "review"


def types(h: Harness) -> list[str]:
    return [e.type for e in h.ledger.storage.read()]


# ---------------------------------------------------------------------- claude-subagent


def test_the_subagent_round_opens_and_takes_the_answer_back(home: Home, tmp_path: Path) -> None:
    h = Harness(home, {"roles": {"reviewer": SUBAGENT}})
    workdir = tmp_path / "repo"
    base, head = git_repo(workdir)
    live_worker(h, tmp_path, workdir)
    opened = review(h, workdir, notes="单元测试 12 个通过")
    assert (opened["status"], opened["round"]) == ("awaiting_agent", 1)
    agent = opened["agent"]
    assert agent["subagent_type"] == "task-steward:reviewer"
    assert (agent["model"], agent["effort"]) == ("sonnet", "high")
    request = review_dir(h) / "r1-request.md"
    assert str(request) in agent["prompt"] and "round=1" in opened["next"]
    # No shell in the subagent: the diff is written for it and the request points at it.
    patch = (review_dir(h) / "r1-diff.patch").read_text(encoding="utf-8")
    assert "b.txt" in patch
    text = request.read_text(encoding="utf-8")
    assert str(review_dir(h) / "r1-diff.patch") in text and "单元测试 12 个通过" in text
    assert h.task("T001")["substate"] == "reviewing"
    assert types(h)[-1] == "review.started"

    answer = "评审完成。\n" + json.dumps(result(head[:8], HIGH, verdict="changes_requested"))
    done = review(h, workdir, round_no=1, result=answer, agent_id="agent-7")
    assert (done["verdict"], done["blocking"], done["reviewed_head"]) == (
        "changes_requested",
        ["F1"],
        head,
    )
    meta = json.loads((review_dir(h) / "r1-run.json").read_text(encoding="utf-8"))
    assert (meta["session"], meta["model"], meta["thread_id"], meta["base"]) == (
        "claude-subagent",
        "sonnet",
        "agent-7",
        base,
    )
    assert types(h)[-1] == "review.completed" and h.task("T001")["substate"] == "triaging"


def test_a_later_subagent_round_is_a_full_review_with_the_triage(
    home: Home, tmp_path: Path
) -> None:
    h = Harness(home, {"roles": {"reviewer": SUBAGENT}})
    workdir = tmp_path / "repo"
    base, head = git_repo(workdir)
    live_worker(h, tmp_path, workdir)
    review(h, workdir)
    review(
        h, workdir, round_no=1, result=json.dumps(result(head, HIGH, verdict="changes_requested"))
    )
    h.run(
        "review_triage",
        WORKER,
        task_id="T001",
        round=1,
        items=[{"finding": "F1", "action": "fix", "verification": "补了测试"}],
    )
    h.run("review_decide", WORKER, task_id="T001", round=1, decision="iterate", head=head)
    (workdir / "c.txt").write_text("c\n", encoding="utf-8")
    git = ["git", "-C", str(workdir)]
    import subprocess  # noqa: PLC0415

    subprocess.run([*git, "add", "c.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fix"], check=True)
    second = review(h, workdir)
    assert second["round"] == 2
    text = (review_dir(h) / "r2-request.md").read_text(encoding="utf-8")
    # A new subagent has no memory of round 1: the whole task, then round 1's triage.
    assert "没有上一轮的上下文" in text and "F1（high）：fix" in text and "增量评审" not in text
    assert "b.txt" in (review_dir(h) / "r2-diff.patch").read_text(encoding="utf-8")
    since = (review_dir(h) / "r2-diff-since.patch").read_text(encoding="utf-8")
    assert "c.txt" in since and "b.txt" not in since
    assert base in text


def test_a_failed_or_unusable_subagent_answer_fails_the_round(home: Home, tmp_path: Path) -> None:
    h = Harness(home, {"roles": {"reviewer": SUBAGENT}})
    workdir = live_worker(h, tmp_path)
    review(h, workdir, head="draft-1")
    failed = review(h, workdir, round_no=1, error="Agent type 'task-steward:reviewer' not found")
    assert failed["outcome"] == "review.failed" and "not found" in failed["error"]
    # Called again without an answer: the same round, the same instructions (E02).
    again = review(h, workdir)
    assert (again["status"], again["round"]) == ("awaiting_agent", 1)
    unusable = review(h, workdir, round_no=1, result="看起来不错")
    assert unusable["outcome"] == "review.failed" and "JSON" in unusable["error"]
    assert (review_dir(h) / "r1-raw.txt").read_text(encoding="utf-8") == "看起来不错"
    review(h, workdir)
    passed = review(h, workdir, round_no=1, result=json.dumps(result("draft-1")))
    assert passed["verdict"] == "pass"


def test_answers_only_for_the_open_round_of_the_subagent(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(home, {"roles": {"reviewer": SUBAGENT}})
    workdir = live_worker(h, tmp_path)
    with pytest.raises(TswError) as nothing_open:
        review(h, workdir, round_no=1, result=json.dumps(result("d1")))
    assert nothing_open.value.details["reason"] == "stale_round"
    review(h, workdir, head="d1")
    with pytest.raises(TswError) as wrong:
        review(h, workdir, round_no=2, result=json.dumps(result("d1")))
    assert wrong.value.details["reason"] == "stale_round"
    with pytest.raises(TswError) as no_round:
        review(h, workdir, result=json.dumps(result("d1")))
    assert no_round.value.details["reason"] == "round_missing"
    # A reviewer tsw runs itself never takes an answer from the worker.
    Harness(home, {"roles": {"reviewer": {"executable": sys.executable}}})
    with pytest.raises(TswError) as direct:
        review(h, workdir, round_no=1, result=json.dumps(result("d1")))
    assert direct.value.details["reason"] == "not_agent_reviewer"
    assert "review.completed" not in types(h) and "review.failed" not in types(h)


# ---------------------------------------------------------------------- copilot-cli


def fake_copilot(tmp_path: Path, answers: list[Any], monkeypatch: pytest.MonkeyPatch) -> Path:
    wrapper = tmp_path / "copilot.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    (tmp_path / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    monkeypatch.setenv("FAKE_COPILOT_ANSWERS", str(tmp_path / "answers.json"))
    monkeypatch.setenv("FAKE_COPILOT_LOG", str(tmp_path / "copilot.log"))
    return wrapper


def calls(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "copilot.log").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def copilot_worker(home: Home, tmp_path: Path, wrapper: Path) -> tuple[Harness, Path, str]:
    h = Harness(
        home,
        {
            "roles": {
                "reviewer": {
                    "session": "copilot-cli",
                    "model": "gpt-6.1-sol",
                    "executable": str(wrapper),
                }
            }
        },
    )
    workdir = tmp_path / "repo"
    _, head = git_repo(workdir)
    live_worker(h, tmp_path, workdir)
    return h, workdir, head


def test_copilot_reviews_read_only_and_resumes_its_session(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = fake_copilot(tmp_path, ["echo-pass", "echo-pass"], monkeypatch)
    h, workdir, head = copilot_worker(home, tmp_path, wrapper)
    out = review(h, workdir)
    assert (out["verdict"], out["reviewed_head"]) == ("pass", head)
    first = calls(tmp_path)[0]
    argv = first["argv"]
    assert Path(first["cwd"]).resolve() == workdir.resolve()
    tools = argv[argv.index("--available-tools") + 1 : argv.index("--available-tools") + 4]
    assert tools == ["view", "grep", "glob"]
    denied = [argv[i + 1] for i, a in enumerate(argv) if a == "--deny-tool"]
    assert denied == ["write", "shell", "memory", "url"]
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert argv[argv.index("--reasoning-effort") + 1] == "medium"
    assert str(review_dir(h)) in [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    session = argv[argv.index("--session-id") + 1]
    meta = json.loads((review_dir(h) / "r1-run.json").read_text(encoding="utf-8"))
    assert meta["thread_id"] == session and meta["session"] == "copilot-cli"
    assert (review_dir(h) / "r1-diff.patch").is_file()

    h.run("review_triage", WORKER, task_id="T001", round=1, items=[])
    h.run("review_decide", WORKER, task_id="T001", round=1, decision="finalize", head=head)
    h.run("self_check_failed", WORKER, task_id="T001", reason="补审定稿后的改动")
    second = review(h, workdir)
    assert second["verdict"] == "pass"
    argv = calls(tmp_path)[1]["argv"]
    assert argv[argv.index("--resume") + 1] == session and "--session-id" not in argv
    assert "增量评审" in (review_dir(h) / "r2-request.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"model": "gpt-6-other"}, "与配置的 gpt-6.1-sol 不一致"),
        ({"error": 'Model "gpt-6.1-sol" is not available.'}, "is not available"),
        ({"content": "没问题"}, "不是 JSON"),
    ],
)
def test_copilot_answers_that_fail_the_round(
    home: Home,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: dict[str, Any],
    expected: str,
) -> None:
    wrapper = fake_copilot(tmp_path, [answer], monkeypatch)
    h, workdir, _ = copilot_worker(home, tmp_path, wrapper)
    out = review(h, workdir)
    assert out["outcome"] == "review.failed" and expected in out["error"]


def test_the_recorded_copilot_output_is_read_as_the_cli_wrote_it() -> None:
    lines = SAMPLE.read_text(encoding="utf-8").splitlines()
    session = "11111111-2222-4333-8444-555555555555"
    run = copilot.read_output(lines, session=session, model="gpt-6.1-sol")
    assert run.status == "completed" and run.error is None
    assert run.final_message is not None and json.loads(run.final_message)["bug"]
    # The administrator's policy picked the model: a reviewer configured otherwise fails.
    other = copilot.read_output(lines, session=session, model="claude-sonnet-5-5")
    assert other.status == "failed" and "gpt-6.1-sol" in str(other.error)
    lost = copilot.read_output(lines[:-1], session=session, model="gpt-6.1-sol")
    assert lost.status == "failed" and "result" in str(lost.error)


def test_a_copilot_run_past_its_time_is_ended(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / "copilot.cmd"
    stubborn = REPO_ROOT / "tests" / "fixtures" / "stubborn_server.py"
    wrapper.write_text(
        f'@"{sys.executable}" "{stubborn}" "{tmp_path / "pid"}"\r\n', encoding="utf-8"
    )
    run = copilot.run_review(
        "p", cwd=tmp_path, model="m", effort="medium", timeout_seconds=3, executable=str(wrapper)
    )
    assert run.status == "timedOut" and "3 秒" in str(run.error)


# ---------------------------------------------------------------------- self-review off


@pytest.mark.use_case("UC-REVIEW-003")
def test_with_self_review_off_there_is_no_round(home: Home, tmp_path: Path) -> None:
    h = Harness(home, {"review": {"self_review": "off"}, "roles": {"reviewer": SUBAGENT}})
    workdir = live_worker(h, tmp_path)
    with pytest.raises(TswError) as refused:
        review(h, workdir, head="d1")
    assert refused.value.details["reason"] == "self_review_off"
    assert "review.started" not in types(h)
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "- 自评审：关闭（`review.self_review`）" in brief
