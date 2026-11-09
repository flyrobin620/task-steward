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
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.machine_harness import MACHINE, Harness
from tools import live
from tsw import lease
from tsw.home import Home
from tsw.machine.base import Caller

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


def gh(run: Path, *args: str, encoding: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TSW_LIVE_RUN": str(run)}
    utf8 = ["-X", "utf8"] if encoding is None else []
    if encoding is not None:
        env["PYTHONIOENCODING"] = encoding
        env.pop("PYTHONUTF8", None)
    return subprocess.run(
        [sys.executable, *utf8, str(STUB), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding=encoding or "utf-8",
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
    # A worker's shell may write with a legacy code page (cp1252 in the live run): the stand-in
    # prints only ASCII, so a merge that happened is never reported as a failure.
    merged = gh(
        run, "pr", "merge", "1", "-R", "github.com/live/live-repo", "--squash", encoding="cp1252"
    )
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
    # The merge commit's CI on main, simulated as one passed run (decision 0003).
    listed = gh(run, "run", "list", "--commit", pr["merge_commit"][:10], "-R", "live/live-repo")
    assert listed.returncode == 0, listed.stderr
    (ci,) = json.loads(listed.stdout)
    assert (ci["headSha"], ci["conclusion"]) == (pr["merge_commit"], "success")
    assert ci["url"].endswith("/actions/runs/1")
    viewed = gh(run, "run", "view", "1", "-R", "github.com/live/live-repo")
    assert json.loads(viewed.stdout)["headSha"] == pr["merge_commit"]
    assert json.loads(gh(run, "run", "list", "-c", "0" * 40, "-R", "live/live-repo").stdout) == []
    log = (run / "gh-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 9


def runtime(root: Path, *, claims: bool) -> Path:
    (root / "tsw").mkdir(parents=True)
    (root / "tsw" / "__init__.py").write_text("", encoding="utf-8")
    text = "def claim_sessions(): ...\n" if claims else "def resolve_home(): ...\n"
    (root / "tsw" / "home.py").write_text(text, encoding="utf-8")
    return root


def test_prepare_checks_the_runtime_the_installed_plugin_will_use(tmp_path: Path) -> None:
    """The launcher's order: TSW_REPO first, then the recorded product_repo (a cached plugin is
    in no checkout). The run is refused when that runtime does not know claim_sessions, or
    when no runtime can be determined, so workers never fall back to the real data directory."""
    new, old = runtime(tmp_path / "new", claims=True), runtime(tmp_path / "old", claims=False)
    real = tmp_path / "real"
    real.mkdir()
    (real / "tsw-home.json").write_text(json.dumps({"product_repo": str(new)}), encoding="utf-8")
    assert live.installed_runtime_claims({"TSW_HOME": str(real)}) == new
    # The recorded runtime is up to date, but TSW_REPO wins and is not.
    with pytest.raises(SystemExit, match="TSW_REPO"):
        live.installed_runtime_claims({"TSW_HOME": str(real), "TSW_REPO": str(old)})
    assert live.installed_runtime_claims({"TSW_HOME": str(real), "TSW_REPO": str(new)}) == new
    # A TSW_REPO without the package is skipped by the launcher, which takes the recorded one.
    broken = tmp_path / "broken"
    (broken / "tsw").mkdir(parents=True)
    (broken / "tsw" / "home.py").write_text("def claim_sessions(): ...\n", encoding="utf-8")
    env = {"TSW_HOME": str(real), "TSW_REPO": str(broken)}
    assert live.installed_runtime_claims(env) == new
    (real / "tsw-home.json").write_text(json.dumps({"product_repo": str(old)}), encoding="utf-8")
    with pytest.raises(SystemExit, match="product_repo"):
        live.installed_runtime_claims({"TSW_HOME": str(real)})
    with pytest.raises(SystemExit, match="product_repo"):
        live.installed_runtime_claims({"TSW_HOME": str(real), "TSW_REPO": str(broken)})
    with pytest.raises(SystemExit, match="无法确定"):
        live.installed_runtime_claims({})


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


@pytest.mark.use_case("UC-CHANNEL-001")
def test_routine_e2e_teams_check_accepts_a_real_round_trip(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routine-e2e Teams check reads the events the real tools write (review r2 F1)."""
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)
    h = Harness(home, {"plugins": {"human_channels": ["in-session", "teams-chat"]}})
    out = h.run("routine_start", user_turn=True)
    h.run("routine_bind", generation=out["events"][0]["data"]["generation"], session_id="r")
    routine = Caller(role="routine", session_id="r", machine=MACHINE)
    h.register("T002")
    h.plan("T002")
    worker = h.dispatch("T002")
    asked = h.run("ask_user", worker, task_id="T002", question="红色还是蓝色？", reserved=True)
    request = asked["events"][0]["data"]["request_id"]
    h.clock.now = "2026-11-05T00:10:00Z"
    h.run("routine_run", routine, job_id="handoff-scan")
    h.run("input_relay", task_id="T002", request_id=request, channel="teams-chat")
    h.run("channel_sent", task_id="T002", request_id=request, chat_id="c", message_id="card")
    reply = {
        "id": "m1",
        "createdDateTime": "2026-11-05T00:12:00Z",
        "body": {"content": "T002 蓝色"},
    }
    h.run("routine_run", routine, job_id="teams-replies", chat_id="c", fetched=[reply])
    h.run(
        "input_resolve",
        task_id="T002",
        request_id=request,
        answer="蓝色",
        user_source={
            "channel": "teams-chat",
            "principal": "teams",
            "message_ref": "m1",
            "text": "T002 蓝色",
            "request_id": request,
            "target": ["T002", 1, "answer"],
        },
    )
    h.run("progress_note", worker, task_id="T002", text="收到答复：蓝色")
    evs = [
        {
            "sequence": e.sequence,
            "type": e.type,
            "task_id": e.task_id,
            "attempt": e.attempt,
            "actor": e.actor,
            "data": e.data,
            "time": e.time,
        }
        for e in h.ledger.storage.read(since=0)
    ]
    result = live.teams_round_trip(evs)
    assert result["status"] == "passed", result


@pytest.mark.use_case("UC-ROUTINE-001")
def test_routine_e2e_reads_the_orchestrator_wakes_from_the_hooks(tmp_path: Path) -> None:
    """Check W (T250): after the detach, a routine run's end no longer wakes the orchestrator;
    its forwards still do. Read from the main session's hook payloads, not the operator."""
    run_dir = tmp_path / "run"
    home = run_dir / "home"
    (home / "config").mkdir(parents=True)
    (home / "cache").mkdir()
    rid = "local_r"
    linked = f"(Linked sessions now: worker = local_w (idle); routine-g1 = {rid} (idle).)"

    def note(session: str) -> str:
        return (
            f'<system-reminder>\nThe session "x" you started ({session}) finished a turn. {linked}'
        )

    def forward(session: str) -> str:
        return f'<cross-session-message from="{session}" from-name="r">x</cross-session-message>'

    def write(*prompts: tuple[str, str, Path]) -> None:
        lines = [
            json.dumps(
                {"event": "UserPromptSubmit", "time": t, "payload": {"cwd": str(cwd), "prompt": p}}
            )
            for t, p, cwd in prompts
        ]
        (run_dir / "hook-payloads.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def cache(generation: int, *runs: str) -> None:
        (home / "cache" / "routine.json").write_text(
            json.dumps({"generation": generation, "runs": list(runs)}), encoding="utf-8"
        )

    def bound(seq: int, generation: int, session: str) -> dict[str, Any]:
        data = {"generation": generation, "session_ref": session}
        return {"sequence": seq, "type": "routine.bound", "data": data, "time": "t"}

    def detached(seq: int, generation: int, time: str) -> dict[str, Any]:
        data = {"generation": generation}
        return {"sequence": seq, "type": "routine.detached", "data": data, "time": time}

    evs = [bound(1, 1, rid), detached(2, 1, "2026-11-09T10:00:00Z")]
    run = cast(live.Run, SimpleNamespace(dir=run_dir, home=home))
    # Runs at 10:02 (forwards), 10:04 (idle, ended: the 10:06 run started) and 10:06.
    cache(
        1,
        "2026-11-09T09:58:00Z",
        "2026-11-09T10:02:00Z",
        "2026-11-09T10:04:00Z",
        "2026-11-09T10:06:00Z",
    )
    write(
        ("2026-11-09T09:58:30Z", note(rid), home),  # before the detach: counted apart
        ("2026-11-09T10:02:30Z", forward(rid), home),
        ("2026-11-09T10:03:00Z", note("local_w"), home),  # a worker; lists the routine's ID
        ("2026-11-09T10:05:00Z", note(rid), tmp_path),  # another session's prompt
    )
    passed = live.routine_wakes(run, evs)
    assert passed["status"] == "passed", passed
    assert passed["evidence"]["notices_before_detach"] == 1
    assert passed["evidence"]["idle_runs_ended"] == ["2026-11-09T10:04:00Z"]
    # A run that still wakes the orchestrator after the detach fails the check.
    write(("2026-11-09T10:02:30Z", forward(rid), home), ("2026-11-09T10:04:30Z", note(rid), home))
    assert live.routine_wakes(run, evs)["evidence"]["notices_after_detach"] == [
        "2026-11-09T10:04:30Z"
    ]
    assert live.routine_wakes(run, evs)["status"] == "failed"
    # No idle run known to have ended: only forwarding runs, or the idle one is the last.
    write(
        ("2026-11-09T10:02:30Z", forward(rid), home), ("2026-11-09T10:04:30Z", forward(rid), home)
    )
    assert live.routine_wakes(run, evs)["status"] == "failed"
    write(("2026-11-09T10:02:30Z", forward(rid), home))
    cache(1, "2026-11-09T10:02:00Z", "2026-11-09T10:04:00Z")
    assert live.routine_wakes(run, evs)["status"] == "failed"
    cache(1, "2026-11-09T10:02:00Z", "2026-11-09T10:04:00Z", "2026-11-09T10:06:00Z")
    assert live.routine_wakes(run, evs)["status"] == "passed"
    # No detach at all (the T243 run), or nothing forwarded after it.
    assert live.routine_wakes(run, evs[:1])["status"] == "failed"
    write(("2026-11-09T09:59:00Z", forward(rid), home))
    assert live.routine_wakes(run, evs)["status"] == "failed"
    # A newer generation needs its own detach and evidence; the old one's do not count.
    write(
        ("2026-11-09T10:02:30Z", forward(rid), home),
        ("2026-11-09T10:20:00Z", note("local_r2"), home),
    )
    newer = [*evs, bound(3, 2, "local_r2")]
    cache(2, "2026-11-09T10:12:00Z", "2026-11-09T10:14:00Z", "2026-11-09T10:16:00Z")
    assert live.routine_wakes(run, newer)["status"] == "failed"
    write(
        ("2026-11-09T10:12:30Z", forward("local_r2"), home),
        ("2026-11-09T10:20:00Z", note("local_r2"), home),
    )
    woken = live.routine_wakes(run, [*newer, detached(4, 2, "2026-11-09T10:10:00Z")])
    assert woken["status"] == "failed" and woken["evidence"]["generation"] == 2
    assert woken["evidence"]["notices_after_detach"] == ["2026-11-09T10:20:00Z"]
