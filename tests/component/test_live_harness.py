"""The live harness without the live parts: the GitHub stand-in and the host-e2e checks.

The live scenarios themselves call the real app and reviewers and never run here
(docs/development/testing-layers.md); these tests cover the pieces a live run relies on.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools import live

STUB = Path(live.LIVE / "gh_stub.py")


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *live.GIT_ID, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def make_run(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    git(run, "init", "-q", "--bare", "--initial-branch=main", "origin.git")
    git(run, "init", "-q", "--initial-branch=main", "w")
    work = run / "w"
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "remote", "add", "origin", str(run / "origin.git"))
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "feature")
    (work / "b.txt").write_text("b\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "feature")
    git(work, "push", "-q", "origin", "feature")
    (run / "gh-state.json").write_text(
        json.dumps({"repository": "live/live-repo", "pulls": []}), encoding="utf-8"
    )
    return run


def gh(run: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TSW_LIVE_RUN": str(run)}
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STUB), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_gh_stub_squash_merges_into_the_bare_origin(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    head = git(run / "w", "rev-parse", "HEAD")
    created = gh(run, "pr", "create", "-R", "live/live-repo", "--head", "feature", "-t", "Feature")
    assert created.returncode == 0, created.stderr
    assert created.stdout.strip() == "https://github.com/live/live-repo/pull/1"
    unnamed = gh(run, "pr", "merge", "1", "--squash")
    assert unnamed.returncode != 0
    assert "-R" in unnamed.stderr
    other = gh(run, "pr", "merge", "1", "-R", "someone/else", "--squash")
    assert other.returncode != 0
    merged = gh(run, "pr", "merge", "1", "-R", "github.com/live/live-repo", "--squash")
    assert merged.returncode == 0, merged.stderr
    state: dict[str, Any] = json.loads((run / "gh-state.json").read_text(encoding="utf-8"))
    pr = state["pulls"][0]
    assert pr["state"] == "MERGED"
    assert pr["merged_head"] == head
    main = git(run / "origin.git", "log", "--format=%H", "main").split()
    assert pr["merge_commit"] == main[0]
    assert git(run / "origin.git", "show", "main:b.txt") == "b"
    view = gh(run, "pr", "view", "https://github.com/live/live-repo/pull/1", "--json", "state")
    assert json.loads(view.stdout)["mergeCommit"]["oid"] == pr["merge_commit"]
    again = gh(run, "pr", "merge", "1", "-R", "live/live-repo", "--squash")
    assert again.returncode != 0
    log = (run / "gh-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 6


def ev(seq: int, type_: str, task: str, actor: str | None = None, **data: Any) -> dict[str, Any]:
    return {
        "sequence": seq,
        "type": type_,
        "task_id": task,
        "attempt": "a1",
        "actor": {"role": "worker", "session_id": actor} if actor else {"role": "orchestrator"},
        "data": data,
        "time": "2026-11-07T00:00:00Z",
    }


def test_b1_requires_the_hook_message_from_the_asking_session() -> None:
    asked = ev(
        1, "input.escalated", "T001", "w1", kind="in_session", request_id="Q1", session_ref="w1"
    )
    message = ev(
        2, "input.session_message", "T001", request_id="Q1", session_ref="w1", message_id="M-1"
    )
    source = {"channel": "in-session", "message_ref": "M-1"}
    answer = ev(
        3, "input.resolved", "T001", "w1", request_id="Q1", source_kind="user", source=source
    )
    assert live.b1([asked, message, answer])["status"] == "passed"
    forged = ev(
        3,
        "input.resolved",
        "T001",
        "w1",
        request_id="Q1",
        source_kind="user",
        source={"channel": "in-session", "message_ref": "M-9"},
    )
    assert live.b1([asked, message, forged])["status"] == "failed"
    assert live.b1([asked, answer])["status"] == "failed"


def test_b2_requires_a_handoff_and_the_orchestrator_handling_it() -> None:
    asked = ev(
        1, "input.escalated", "T002", "w2", kind="in_session", request_id="Q2", session_ref="w2"
    )
    handed = ev(2, "input.handed_off", "T002", request_id="Q2", by="worker")
    decision = ev(3, "orchestrator.decision", "T002")
    resolved = ev(4, "input.resolved", "T002", request_id="Q2", source_kind="orchestrator")
    assert live.b2([asked, handed, decision, resolved])["status"] == "passed"
    assert live.b2([asked, decision, resolved])["status"] == "failed"
    assert live.b2([asked, handed])["status"] == "failed"


def test_scrub_removes_machine_paths(tmp_path: Path) -> None:
    run = live.Run("live-x")
    text = f"{run.dir}\\home and {live.ROOT} and {Path.home()}"
    out = live.scrub(text, run)
    assert str(run.dir) not in out and str(live.ROOT) not in out and str(Path.home()) not in out


def test_scenario_definitions_are_complete() -> None:
    for name in ("host-e2e", "reviewer"):
        spec = live.scenario(name)
        assert spec["id"] == name
        assert spec["useCases"] and spec["checks"]
    host = live.scenario("host-e2e")
    assert {t["task_id"] for t in host["tasks"]} == {"T001", "T002", "T003"}


def test_a_reviewer_that_cannot_run_is_reported_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSW_LIVE_RUNS", str(tmp_path / "runs"))
    monkeypatch.delenv("TSW_HOME", raising=False)
    run = live.Run.create("reviewer")
    checks = [
        live.check("X1", "全量评审", True, "ok"),
        {
            **live.check("C0", "claude-headless 可运行", False, "未实现"),
            "status": live.NOT_VERIFIED,
        },
    ]
    assert live.write_results(run, "reviewer", checks, [])
    evidence = (run.dir / "evidence.md").read_text(encoding="utf-8")
    assert "未验证（NOT VERIFIED）" in evidence and "另有未验证项" in evidence
    checks.append(live.check("X2", "增量评审", False, "失败"))
    assert not live.write_results(run, "reviewer", checks, [])


def test_without_a_subset_every_scenario_task_is_used() -> None:
    spec = live.scenario("host-e2e")
    every = [t["task_id"] for t in spec["tasks"]]
    assert [t["task_id"] for t in live.selected(spec, None)] == every
    assert [t["task_id"] for t in live.selected(spec, [""])] == every
    assert [t["task_id"] for t in live.selected(spec, ["T001", " T002"])] == ["T001", "T002"]
    with pytest.raises(SystemExit):
        live.selected(spec, ["T999"])


def test_concurrent_pull_requests_keep_every_record(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    work = run / "w"
    for branch in ("b1", "b2", "b3", "b4"):
        git(work, "checkout", "-q", "-b", branch, "main")
        (work / f"{branch}.txt").write_text(branch, encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", branch)
        git(work, "push", "-q", "origin", branch)
    env = {**os.environ, "TSW_LIVE_RUN": str(run)}
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                str(STUB),
                "pr",
                "create",
                "-R",
                "live/live-repo",
                "--head",
                branch,
                "-t",
                branch,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for branch in ("b1", "b2", "b3", "b4")
    ]
    urls = sorted(p.communicate(timeout=120)[0].strip() for p in procs)
    assert len(set(urls)) == 4
    state = json.loads((run / "gh-state.json").read_text(encoding="utf-8"))
    assert sorted(p["head"] for p in state["pulls"]) == ["b1", "b2", "b3", "b4"]
    assert not (run / "gh-state.lock").exists()


def test_unknown_sessions_block_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TSW_LIVE_RUNS", str(tmp_path / "runs"))
    monkeypatch.delenv("TSW_HOME", raising=False)
    run = live.Run.create("host-e2e")
    run.record("claude-session", "main")
    events = [
        ev(
            1,
            "attempt.start_requested",
            "T001",
            attempt="a1",
            generation=1,
            session_name="t001-a1-g1",
        ),
        ev(
            2,
            "attempt.start_requested",
            "T002",
            attempt="a1",
            generation=1,
            session_name="t002-a1-g1",
        ),
        ev(3, "attempt.not_created", "T002", attempt="a1", generation=1),
    ]
    live.reconcile_sessions(run, events)
    names = {e["name"]: e["state"] for e in live.unresolved(run)}
    # The orchestrator's session and T001's are unknown; T002's was never created.
    assert names == {"main": "maybe_created", "t001-a1-g1": "maybe_created"}
    bound = ev(4, "attempt.bound", "T001", attempt="a1", generation=1, session_ref="local_1")
    live.reconcile_sessions(run, [*events, bound])
    entry = next(e for e in run.manifest()["entries"] if e["name"] == "t001-a1-g1")
    assert (entry["state"], entry["ref"]) == ("created", "local_1")
    with pytest.raises(SystemExit):
        live.discard(argparse.Namespace(run_id=run.id))


def test_a_new_main_session_never_inherits_an_archived_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSW_LIVE_RUNS", str(tmp_path / "runs"))
    monkeypatch.delenv("TSW_HOME", raising=False)
    run = live.Run.create("host-e2e")
    run.record("claude-session", "main")
    run.bind_main("local_z")
    run.record("claude-session", "main", state="archived")
    # A later verify finds another main session that sorts first.
    for sid in ("local_a", "local_z"):
        run.bind_main(sid)
    states = {e["ref"]: e["state"] for e in run.manifest()["entries"]}
    assert states == {"local_z": "archived", "local_a": "created"}
    assert [e["ref"] for e in live.unresolved(run)] == ["local_a"]
    # Recording a different session under an archived name does not keep the archive mark.
    run.record("claude-session", "main", state="created", ref="local_b")
    assert next(e for e in run.manifest()["entries"] if e["name"] == "main")["state"] == "created"
