"""The ``claude-headless`` and ``copilot-cli`` reviewers (DN004 §3, CC004, CP004), and turning
self-review off (DN004 §9).

The ``claude`` and ``copilot`` command lines are replaced by ``tests/fixtures/fake_claude.py``
and ``tests/fixtures/fake_copilot.py`` (platforms outside this repository). Everything else is
real: the ledger, the request files, git and the reviewer process handling.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.component.test_review_run import HIGH, WORKER, git_repo, live_worker, result
from tests.machine_harness import Harness
from tests.support import REPO_ROOT
from tsw import config as config_module
from tsw.config import load_config
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.identity import SESSION_ENV
from tsw.plugins.catalog import SESSIONS
from tsw.review import claude, copilot
from tsw.review import run as review_run

pytestmark = pytest.mark.use_case("UC-REVIEW-002")

FAKE = REPO_ROOT / "tests" / "fixtures" / "fake_copilot.py"
FAKE_CLAUDE = REPO_ROOT / "tests" / "fixtures" / "fake_claude.py"
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "copilot" / "final-answer.jsonl"
CLAUDE_SAMPLE = REPO_ROOT / "tests" / "fixtures" / "claude" / "final-answer.jsonl"


def review(h: Harness, workdir: Path, **options: Any) -> dict[str, Any]:
    return review_run.run_round(h.home, {SESSION_ENV: "w-1"}, workdir, machine="m1", **options)


def review_dir(h: Harness) -> Path:
    return h.home.root / "runs" / "T001" / "a1" / "review"


def types(h: Harness) -> list[str]:
    return [e.type for e in h.ledger.storage.read()]


def commit(workdir: Path, name: str) -> None:
    (workdir / name).write_text(f"{name}\n", encoding="utf-8")
    git = ["git", "-C", str(workdir)]
    subprocess.run([*git, "add", name], check=True)
    subprocess.run([*git, "commit", "-q", "-m", name], check=True)


# ---------------------------------------------------------------------- claude-headless


def fake_claude(tmp_path: Path, answers: list[Any], monkeypatch: pytest.MonkeyPatch) -> Path:
    wrapper = tmp_path / "claude.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{FAKE_CLAUDE}" %*\r\n', encoding="utf-8")
    (tmp_path / "claude-answers.json").write_text(json.dumps(answers), encoding="utf-8")
    monkeypatch.setenv("FAKE_REVIEWER_ANSWERS", str(tmp_path / "claude-answers.json"))
    monkeypatch.setenv("FAKE_REVIEWER_LOG", str(tmp_path / "claude.log"))
    return wrapper


def claude_calls(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "claude.log").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def claude_config(wrapper: Path, model: str = "claude-sonnet-5-5") -> dict[str, Any]:
    reviewer = {"session": "claude-headless", "model": model, "effort": "high"}
    return {"roles": {"reviewer": {**reviewer, "executable": str(wrapper)}}}


def test_tsw_runs_claude_read_only_and_resumes_its_session(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = fake_claude(tmp_path, ["echo-pass", "echo-pass"], monkeypatch)
    # Variables a Claude Code host hands down (and tsw's own) never reach the reviewer.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-session")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TSW_REVIEW_PROBE", "1")
    h = Harness(home, claude_config(wrapper))
    workdir = tmp_path / "repo"
    base, head = git_repo(workdir)
    live_worker(h, tmp_path, workdir)
    out = review(h, workdir, notes="单元测试 12 个通过")
    assert (out["verdict"], out["reviewed_head"]) == ("pass", head)
    first = claude_calls(tmp_path)[0]
    argv = first["argv"]
    assert Path(first["cwd"]).resolve() == workdir.resolve()
    assert first["inherited"] == []
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert {"Bash", "PowerShell", "Edit", "Write", "Agent", "Skill", "Workflow"} <= set(denied)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert {"--safe-mode", "--strict-mcp-config", "--verbose"} <= set(argv)
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == (
        "claude-sonnet-5-5",
        "high",
    )
    added = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert str(review_dir(h)) in added
    # The prompt only points at the request; the diff is written for a reviewer without a shell.
    request = review_dir(h) / "r1-request.md"
    assert str(request) in argv[argv.index("-p") + 1]
    text = request.read_text(encoding="utf-8")
    assert str(review_dir(h) / "r1-diff.patch") in text and "单元测试 12 个通过" in text
    assert "b.txt" in (review_dir(h) / "r1-diff.patch").read_text(encoding="utf-8")
    session = argv[argv.index("--session-id") + 1]
    meta = json.loads((review_dir(h) / "r1-run.json").read_text(encoding="utf-8"))
    assert (meta["session"], meta["thread_id"], meta["base"]) == ("claude-headless", session, base)
    assert (meta["model"], meta["model_used"]) == ("claude-sonnet-5-5", "claude-sonnet-5-5")
    assert types(h)[-1] == "review.completed"

    h.run("review_triage", WORKER, task_id="T001", round=1, items=[])
    h.run("review_decide", WORKER, task_id="T001", round=1, decision="finalize", head=head)
    h.run("self_check_failed", WORKER, task_id="T001", reason="补审定稿后的改动")
    commit(workdir, "c.txt")
    second = review(h, workdir)
    assert second["verdict"] == "pass"
    argv = claude_calls(tmp_path)[1]["argv"]
    assert argv[argv.index("--resume") + 1] == session and "--session-id" not in argv
    assert "增量评审" in (review_dir(h) / "r2-request.md").read_text(encoding="utf-8")
    since = (review_dir(h) / "r2-diff-since.patch").read_text(encoding="utf-8")
    assert "c.txt" in since and "b.txt" not in since


def test_an_alias_passes_for_any_model_of_its_family(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = fake_claude(tmp_path, ["echo-pass"], monkeypatch)
    h = Harness(home, claude_config(wrapper, "sonnet"))
    workdir = live_worker(h, tmp_path)
    assert review(h, workdir, head="draft-1")["verdict"] == "pass"
    meta = json.loads((review_dir(h) / "r1-run.json").read_text(encoding="utf-8"))
    assert (meta["model"], meta["model_used"]) == ("sonnet", "claude-sonnet-5-5")


@pytest.mark.parametrize(
    ("model", "answer", "expected"),
    [
        ("claude-sonnet-5-5", {"model": "claude-opus-5.5"}, "与配置的 claude-sonnet-5-5 不一致"),
        ("claude-sonnet-5-5", {"init_model": "claude-sonnet-5-4"}, "claude-sonnet-5-4"),
        ("sonnet", {"model": "claude-haiku-5.5"}, "与配置的 sonnet 不一致"),
        (
            "claude-sonnet-5-5",
            {"is_error": True, "content": "API Error: 400 Unknown model"},
            "Unknown model",
        ),
        ("claude-sonnet-5-5", {"content": "没问题"}, "不是 JSON"),
        ("claude-sonnet-5-5", {"session": "another"}, "会话 ID（another）"),
        ("claude-sonnet-5-5", {"exit": 3}, "退出码 3"),
    ],
)
def test_claude_answers_that_fail_the_round(
    home: Home,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str,
    answer: dict[str, Any],
    expected: str,
) -> None:
    wrapper = fake_claude(tmp_path, [answer], monkeypatch)
    h = Harness(home, claude_config(wrapper, model))
    workdir = live_worker(h, tmp_path)
    out = review(h, workdir, head="draft-1")
    assert out["outcome"] == "review.failed" and expected in out["error"]
    assert (review_dir(h) / "r1-raw.txt").is_file()


def test_the_recorded_claude_output_is_read_as_the_cli_wrote_it() -> None:
    lines = CLAUDE_SAMPLE.read_text(encoding="utf-8").splitlines()
    session = "a8a45b73-5e4e-43bb-a5c6-01c941fc896b"
    # init says claude-sonnet-5-5[1m], the messages claude-sonnet-5.5: one model.
    for model in ("claude-sonnet-5-5", "sonnet", "claude-sonnet-5.5"):
        run = claude.read_output(lines, session=session, model=model)
        assert (run.status, run.error, run.model) == ("completed", None, "claude-sonnet-5-5")
        assert run.final_message is not None
        assert json.loads(run.final_message)["verdict"] == "changes_requested"
    other = claude.read_output(lines, session=session, model="claude-opus-5-5")
    assert other.status == "failed" and "claude-sonnet-5-5" in str(other.error)
    lost = claude.read_output(lines[:-1], session=session, model="sonnet")
    assert lost.status == "failed" and "result" in str(lost.error)
    stranger = claude.read_output(lines, session="0", model="sonnet")
    assert stranger.status == "failed" and "会话 ID" in str(stranger.error)


def test_the_reviewer_environment_drops_the_hosts_variables() -> None:
    env = {
        "PATH": "p",
        "CLAUDE_CODE_ENTRYPOINT": "x",
        "ClaudeCode": "x",
        "ANTHROPIC_API_KEY": "x",
        "TSW_HOME": "x",
        "HOME": "h",
    }
    assert claude.child_env(env) == {"PATH": "p", "HOME": "h"}


def test_a_claude_run_past_its_time_is_ended(tmp_path: Path) -> None:
    wrapper = tmp_path / "claude.cmd"
    stubborn = REPO_ROOT / "tests" / "fixtures" / "stubborn_server.py"
    wrapper.write_text(
        f'@"{sys.executable}" "{stubborn}" "{tmp_path / "pid"}"\r\n', encoding="utf-8"
    )
    run = claude.run_review(
        "p", cwd=tmp_path, model="m", effort="high", timeout_seconds=3, executable=str(wrapper)
    )
    assert run.status == "timedOut" and "3 秒" in str(run.error)


def test_a_round_the_removed_subagent_opened_is_retried_by_the_configured_reviewer(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A round T325's claude-subagent opened and never got an answer for (DN004 §3.2).
    wrapper = fake_claude(tmp_path, ["echo-pass"], monkeypatch)
    with monkeypatch.context() as earlier:  # the version that still had it
        earlier.delitem(config_module.REMOVED_SESSIONS, "claude-subagent")
        earlier.setitem(SESSIONS, "claude-subagent", SESSIONS["claude-headless"])
        subagent = {"session": "claude-subagent", "model": "sonnet"}
        h = Harness(
            home,
            {
                "plugins": {
                    "sessions": [*config_module.DEFAULTS["plugins"]["sessions"], "claude-subagent"]
                },
                "roles": {"reviewer": subagent},
            },
        )
        workdir = live_worker(h, tmp_path)
        h.run("review_start", WORKER, task_id="T001", head="draft-1", reviewer=subagent)
    Harness(home, claude_config(wrapper))
    out = review(h, workdir)
    assert (out["round"], out["verdict"]) == (1, "pass")
    meta = json.loads((review_dir(h) / "r1-run.json").read_text(encoding="utf-8"))
    assert (meta["session"], meta["model"]) == ("claude-headless", "claude-sonnet-5-5")


def test_the_removed_subagent_cannot_be_configured(home: Home) -> None:
    Harness(home, {"roles": {"reviewer": {"executable": sys.executable}}})
    config = home.root / "config" / "task-steward.json"
    values = json.loads(config.read_text(encoding="utf-8"))
    values.setdefault("plugins", {})["sessions"] = ["claude-app", "claude-subagent"]
    values["roles"]["reviewer"] = {"session": "claude-subagent", "model": "sonnet"}
    config.write_text(json.dumps(values), encoding="utf-8")
    problems = load_config(home).problems
    assert any(
        p.startswith("roles.reviewer.session: 会话适配器 claude-subagent 已删除")
        and "claude-headless" in p
        for p in problems
    )
    assert any(p.startswith("plugins.sessions 中的 claude-subagent: 会话适配器") for p in problems)


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
    reviewer = {"session": "claude-headless", "model": "sonnet", "executable": sys.executable}
    h = Harness(home, {"review": {"self_review": "off"}, "roles": {"reviewer": reviewer}})
    workdir = live_worker(h, tmp_path)
    with pytest.raises(TswError) as refused:
        review(h, workdir, head="d1")
    assert refused.value.details["reason"] == "self_review_off"
    assert "review.started" not in types(h)
    brief = (h.home.root / "runs" / "T001" / "a1" / "brief.md").read_text(encoding="utf-8")
    assert "- 自评审：关闭（`review.self_review`）" in brief


def test_a_changed_reviewer_starts_afresh_with_the_last_triage(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review r1 F2: Claude's session is never handed to Copilot as a session to resume.
    workdir = tmp_path / "repo"
    _, head = git_repo(workdir)
    found = json.dumps(result(head, HIGH, verdict="changes_requested"))
    h = Harness(home, claude_config(fake_claude(tmp_path, [{"content": found}], monkeypatch)))
    live_worker(h, tmp_path, workdir)
    review(h, workdir)
    first = claude_calls(tmp_path)[0]["argv"]
    claude_session = first[first.index("--session-id") + 1]
    h.run(
        "review_triage",
        WORKER,
        task_id="T001",
        round=1,
        items=[{"finding": "F1", "action": "reject", "reason": "不成立"}],
    )
    h.run("review_decide", WORKER, task_id="T001", round=1, decision="finalize", head=head)
    h.run("self_check_failed", WORKER, task_id="T001", reason="改用 Copilot 再审")
    wrapper = fake_copilot(tmp_path, ["echo-pass"], monkeypatch)
    Harness(
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
    second = review(h, workdir)
    assert second["verdict"] == "pass"
    argv = calls(tmp_path)[0]["argv"]
    assert "--resume" not in argv and claude_session not in argv
    text = (review_dir(h) / "r2-request.md").read_text(encoding="utf-8")
    assert "没有上一轮的上下文" in text and "F1（high）：reject" in text
    assert str(review_dir(h) / "r1.json") in text


def test_a_new_reviewer_of_a_research_task_gets_the_last_triage(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review r1 F3: without git, a fresh reviewer still gets round 1's findings and triage.
    found = json.dumps(result("draft-1", HIGH, verdict="changes_requested"))
    h = Harness(home, claude_config(fake_claude(tmp_path, [{"content": found}], monkeypatch)))
    workdir = live_worker(h, tmp_path)
    review(h, workdir, head="draft-1")
    h.run(
        "review_triage",
        WORKER,
        task_id="T001",
        round=1,
        items=[{"finding": "F1", "action": "fix", "verification": "改了第 2 节"}],
    )
    h.run("review_decide", WORKER, task_id="T001", round=1, decision="iterate", head="draft-1")
    wrapper = fake_copilot(tmp_path, ["echo-pass"], monkeypatch)
    Harness(
        home,
        {
            "roles": {
                "reviewer": {"session": "copilot-cli", "model": "m", "executable": str(wrapper)}
            }
        },
    )
    review(h, workdir, head="draft-2")
    text = (review_dir(h) / "r2-request.md").read_text(encoding="utf-8")
    assert "没有上一轮的上下文" in text and "F1（high）：fix" in text and "改了第 2 节" in text
    assert str(review_dir(h) / "r1.json") in text and "draft-1" in text


@pytest.mark.use_case("UC-REVIEW-003")
def test_self_review_off_is_said_before_any_reviewer_check(home: Home, tmp_path: Path) -> None:
    # PR #74 review: no Codex on the machine still answers self_review_off, not unavailable.
    h = Harness(
        home,
        {
            "review": {"self_review": "off"},
            "roles": {"reviewer": {"executable": str(tmp_path / "no-codex.exe")}},
        },
    )
    workdir = live_worker(h, tmp_path)
    with pytest.raises(TswError) as refused:
        review(h, workdir, head="d1")
    assert refused.value.details["reason"] == "self_review_off"


def test_a_fresh_thread_still_gets_the_last_triage(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #74 review: review.incremental "fresh" keeps round 1's findings and triage.
    from tests.component.test_review_run import FakeReviewer  # noqa: PLC0415

    # Any existing program will do: the reviewer itself is injected (no codex on CI runners).
    h = Harness(
        home,
        {"review": {"incremental": "fresh"}, "roles": {"reviewer": {"executable": sys.executable}}},
    )
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
        items=[{"finding": "F1", "action": "fix", "verification": "补了测试"}],
    )
    h.run("review_decide", WORKER, task_id="T001", round=1, decision="iterate", head="draft-1")
    review(h, workdir, head="draft-2")
    assert fake.calls[1]["resume_thread"] is None
    prompt = fake.calls[1]["prompt"]
    assert "没有上一轮的上下文" in prompt and "F1（high）：fix" in prompt


def test_an_open_round_is_finished_by_the_reviewer_that_opened_it(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #74 review: after a failed Copilot round the user switches to Claude; the retry of the
    # open round still goes to Copilot, the next round to Claude.
    wrapper = fake_copilot(tmp_path, [{"error": "boom"}, "echo-pass"], monkeypatch)
    h, workdir, head = copilot_worker(home, tmp_path, wrapper)
    failed = review(h, workdir)
    assert failed["outcome"] == "review.failed"
    # The command line configured now is not Copilot's: the round keeps the one it ran with.
    other = tmp_path / "not-copilot.cmd"
    other.write_text("@exit 9\r\n", encoding="utf-8")
    claude_reviewer = {"session": "claude-headless", "model": "haiku", "executable": str(other)}
    Harness(home, {"roles": {"reviewer": claude_reviewer}})
    retried = review(h, workdir)
    assert (retried["round"], retried["verdict"]) == (1, "pass")
    assert len(calls(tmp_path)) == 2
    meta = json.loads((review_dir(h) / "r1-run.json").read_text(encoding="utf-8"))
    assert (meta["session"], meta["model"]) == ("copilot-cli", "gpt-6.1-sol")
    h.run("review_triage", WORKER, task_id="T001", round=1, items=[])
    h.run("review_decide", WORKER, task_id="T001", round=1, decision="finalize", head=head)
    h.run("self_check_failed", WORKER, task_id="T001", reason="再审")
    assert review(h, workdir)["outcome"] == "review.failed"  # "claude" here exits 9
    meta = json.loads((review_dir(h) / "r2-run.json").read_text(encoding="utf-8"))
    assert (meta["session"], meta["model"]) == ("claude-headless", "haiku")


def test_a_retried_round_keeps_its_model(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review r5 F4: the same reviewer with a new model still finishes the open round as opened.
    wrapper = fake_claude(tmp_path, [{"exit": 3}, "echo-pass"], monkeypatch)
    h = Harness(home, claude_config(wrapper, "sonnet"))
    workdir = live_worker(h, tmp_path)
    assert review(h, workdir, head="d1")["outcome"] == "review.failed"
    Harness(home, claude_config(wrapper, "haiku"))
    again = review(h, workdir)
    assert (again["round"], again["verdict"]) == (1, "pass")
    argv = claude_calls(tmp_path)[1]["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
