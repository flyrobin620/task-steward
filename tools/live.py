"""Live scenarios: the real Claude app and real reviewers, on this machine only.

    python tools/live.py reviewer [--reviewer codex-app-server]   run the reviewer scenario
    python tools/live.py host-e2e prepare                          set up a run for the Claude app
    python tools/live.py host-e2e verify RUN_ID                    check the run's ledger
    python tools/live.py cleanup RUN_ID [--archived SESSION ...]   finish a run's cleanup
    python tools/live.py list                                       runs and their state

Scenarios are defined in ``tools/live/scenarios/*.json`` (docs/development/testing-layers.md).
Every run gets ``.local/live/<runId>/`` (docs/development/test-environments.md): its own data
directory ``home/``, its own target repository ``repo/`` whose only remote is the bare
``origin.git`` next to it, ``manifest.json`` listing every session, thread and directory the run
creates (written before each is created), and ``result.json`` / ``evidence.md`` with the checks.

What is real and what is not: the Claude app, its hooks and MCP server, the tsw runtime of this
checkout and the reviewers are real. GitHub is not: ``origin.git`` and ``bin/gh`` (a stand-in
for the GitHub CLI, ``tools/live/gh_stub.py``) play it, so nothing leaves the machine; evidence
about pull requests and merges is simulated and reported as such.

The host-e2e scenario needs the user: only the user can open a session in a new folder of the
Claude app and speak in it. ``prepare`` writes the steps to ``OPERATOR.md``. Claude sessions
can only be archived from inside the app; ``cleanup`` records the sessions the operator archived
and confirmed with ``get_session`` and reports every other manifest entry as not cleaned up.
"""

# Ledger events, manifests and reviewer JSON are walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.checks.repo import utf8_stdio  # noqa: E402
from tsw.home import Home  # noqa: E402
from tsw.storage.fs import FsStorage  # noqa: E402

LIVE = ROOT / "tools" / "live"
SCENARIOS = LIVE / "scenarios"
RUNS = ROOT / ".local" / "live"  # --runs-dir or TSW_LIVE_RUNS put runs elsewhere
STRIP_PREFIXES = ("CLAUDE", "ANTHROPIC")
WORKER_SESSION = "live-worker-1"
ORCH_SESSION = "live-setup"
GIT_ID = (
    "-c",
    "user.name=live",
    "-c",
    "user.email=live@example.invalid",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.safecrlf=false",
)


# ---------------------------------------------------------------------------- run directory


def runs_dir() -> Path:
    return Path(os.environ.get("TSW_LIVE_RUNS") or RUNS)


def outside_real_home(path: Path) -> None:
    """A run never lives inside the data directory of the session that starts it.

    A session opened under a data directory would also read that directory's instructions
    (``CLAUDE.md`` is read from every parent folder), so a run inside it is not isolated.
    """
    real = os.environ.get("TSW_HOME")
    if not real:
        return
    target, home = path.resolve(), Path(real).expanduser().resolve()
    if target == home or home in target.parents:
        raise SystemExit(
            f"live: 运行目录 {path} 在当前会话的数据目录 {real} 之中；"
            "用 --runs-dir（或 TSW_LIVE_RUNS）把运行放到数据目录之外"
        )


def utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"live-{stamp}-{secrets.token_hex(3)}"


def scenario(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))
    return data


class Run:
    """A run directory and its manifest (written before anything is created)."""

    def __init__(self, run_id: str) -> None:
        self.id = run_id
        self.dir = runs_dir() / run_id
        self.home = self.dir / "home"
        self.repo = self.dir / "repo"
        self.origin = self.dir / "origin.git"
        self.bin = self.dir / "bin"
        self.manifest_path = self.dir / "manifest.json"

    def check_inside(self, path: Path) -> None:
        """Every path a run writes is in its own run directory, never anywhere else."""
        resolved, base = path.resolve(), self.dir.resolve()
        if resolved != base and base not in resolved.parents:
            raise SystemExit(f"live: {path} 不在本次运行目录 {self.dir} 中")

    def manifest(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return data

    def save(self, manifest: Mapping[str, Any]) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.manifest_path)

    def record(self, kind: str, name: str, **fields: Any) -> dict[str, Any]:
        """Add or update a manifest entry (``state`` starts as ``maybe_created``)."""
        manifest = self.manifest()
        for entry in manifest["entries"]:
            if entry["kind"] == kind and entry["name"] == name:
                entry.update(fields)
                break
        else:
            entry = {"kind": kind, "name": name, "state": "maybe_created", **fields}
            manifest["entries"].append(entry)
        self.save(manifest)
        return dict(entry)

    @classmethod
    def create(cls, scenario_id: str) -> Run:
        run = cls(new_run_id())
        outside_real_home(run.dir)
        run.dir.mkdir(parents=True)
        run.save(
            {
                "runId": run.id,
                "scenario": scenario_id,
                "created": utc(),
                "productHead": git_out(ROOT, "rev-parse", "HEAD"),
                "productDirty": bool(
                    git_out(ROOT, "status", "--porcelain", "--untracked-files=no")
                ),
                "entries": [],
            }
        )
        return run

    @classmethod
    def open(cls, run_id: str) -> Run:
        run = cls(run_id)
        if not run.manifest_path.is_file():
            raise SystemExit(f"live: 没有运行 {run_id}（{run.manifest_path} 不存在）")
        return run


# ---------------------------------------------------------------------------- processes


def clean_env(run: Run, **extra: str) -> dict[str, str]:
    """A child environment that can only reach this run's data directory."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP_PREFIXES)}
    env.pop("TSW_REPO", None)
    env["TSW_HOME"] = str(run.home)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    env.update(extra)
    return env


def git_out(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_ID, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"live: git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def tsw(run: Run, *args: str, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Any:
    """Run this checkout's ``tsw`` against the run's data directory; returns its JSON output."""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "tsw", "--home", str(run.home), "--json", *args],
        cwd=cwd or ROOT,
        env=dict(env) if env is not None else clean_env(run),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out = proc.stdout.strip()
    if proc.returncode != 0:
        raise SystemExit(f"live: tsw {' '.join(args[:3])} 失败：{out}\n{proc.stderr.strip()}")
    return json.loads(out) if out else None


def tool(run: Run, name: str, role: str, session: str, **inp: Any) -> Any:
    request = run.dir / "requests" / f"{name}-{secrets.token_hex(3)}.json"
    request.parent.mkdir(exist_ok=True)
    request.write_text(json.dumps(inp, ensure_ascii=False), encoding="utf-8")
    return tsw(
        run, "tool", "run", name, "--input", f"@{request}", "--role", role, "--session", session
    )


def write_config(run: Run, overlay: Mapping[str, Any]) -> None:
    path = run.home / "config" / "task-steward.json"
    path.write_text(json.dumps({"version": 1, **overlay}, ensure_ascii=False, indent=2), "utf-8")


def init_repo(run: Run, files: Mapping[str, str]) -> str:
    """``repo/`` with one commit on ``main``, pushed to the bare ``origin.git``."""
    for path in (run.repo, run.origin):
        run.check_inside(path)
    git_out(run.dir, "init", "-q", "--bare", "--initial-branch=main", str(run.origin))
    git_out(run.dir, "init", "-q", "--initial-branch=main", str(run.repo))
    for rel, text in files.items():
        target = run.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    git_out(run.repo, "add", "-A")
    git_out(run.repo, "commit", "-q", "-m", "initial")
    git_out(run.repo, "remote", "add", "origin", str(run.origin))
    git_out(run.repo, "push", "-q", "origin", "main")
    return git_out(run.repo, "rev-parse", "HEAD")


CALC = '''"""A tiny calculator used by live scenarios."""


def subtract(a: int, b: int) -> int:
    return a - b


def divide(a: int, b: int) -> float:
    return a / b
'''
CALC_TEST = """import unittest

from calc import divide, subtract


class CalcTest(unittest.TestCase):
    def test_subtract(self) -> None:
        self.assertEqual(subtract(5, 3), 2)

    def test_divide(self) -> None:
        self.assertEqual(divide(6, 3), 2)


if __name__ == "__main__":
    unittest.main()
"""
REPO_FILES = {
    "README.md": "# live-repo\n",
    "AGENTS.md": "# live-repo\n\n- 测试：`python -m unittest`\n- PR 用 squash 合并。\n",
    "calc.py": CALC,
    "test_calc.py": CALC_TEST,
}


def events(run: Run) -> list[dict[str, Any]]:
    storage = FsStorage(Home(run.home))
    return [
        {
            "sequence": e.sequence,
            "type": e.type,
            "task_id": e.task_id,
            "attempt": e.attempt,
            "actor": e.actor,
            "data": e.data,
            "time": e.time,
        }
        for e in storage.read(since=0)
    ]


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def tree_digest(root: Path) -> dict[str, str]:
    """Every file under ``root`` (``.git`` included) and its SHA-256."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def scrub(text: str, run: Run) -> str:
    """Evidence goes into PRs: replace machine paths and the user name with placeholders."""
    for value, name in (
        (str(run.dir), "<run>"),
        (str(run.dir).replace("\\", "/"), "<run>"),
        (str(ROOT), "<repo>"),
        (str(ROOT).replace("\\", "/"), "<repo>"),
        (str(Path.home()), "<user-home>"),
        (str(Path.home()).replace("\\", "/"), "<user-home>"),
    ):
        text = text.replace(value, name)
        text = text.replace(value.replace("\\", "\\\\"), name)
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if user and len(user) > 2:
        text = text.replace(user, "<user>")
    return text


def write_results(run: Run, title: str, checks: list[dict[str, Any]], notes: list[str]) -> bool:
    passed = all(c["status"] == "passed" for c in checks if not c.get("optional"))
    result = {"runId": run.id, "time": utc(), "passed": passed, "checks": checks, "notes": notes}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    (run.dir / "result.json").write_text(text + "\n", encoding="utf-8")
    lines = [
        f"# live {title}：{run.id}",
        "",
        f"- 产品 HEAD：`{run.manifest()['productHead']}`"
        + ("（工作树有未提交改动）" if run.manifest().get("productDirty") else ""),
        f"- 结果：{'通过' if passed else '未通过'}",
        "",
        "| 检查 | 结果 | 证据 |",
        "| --- | --- | --- |",
    ]
    for c in checks:
        mark = str({"passed": "通过", "failed": "未通过"}.get(c["status"], c["status"]))
        if c.get("optional"):
            mark += "（可选）"
        evidence = str(c.get("evidence", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {c['id']} {c['title']} | {mark} | {evidence} |")
    if notes:
        lines += ["", "说明：", "", *[f"- {n}" for n in notes]]
    (run.dir / "evidence.md").write_text(scrub("\n".join(lines), run) + "\n", encoding="utf-8")
    print(scrub("\n".join(lines), run))
    return passed


def check(
    cid: str, title: str, ok: bool, evidence: Any, *, optional: bool = False
) -> dict[str, Any]:
    return {
        "id": cid,
        "title": title,
        "status": "passed" if ok else "failed",
        "evidence": evidence,
        **({"optional": True} if optional else {}),
    }


# ---------------------------------------------------------------------------- reviewer


def reviewer_scenario(args: argparse.Namespace) -> int:
    spec = scenario("reviewer")
    budget = int(spec["modelCalls"]["perReviewer"])
    run = Run.create("reviewer")
    print(f"run {run.id}: {run.dir}")
    checks: list[dict[str, Any]] = []
    notes: list[str] = []
    for reviewer in args.reviewer or spec["reviewers"]:
        checks += review_with(run, reviewer, budget, notes)
    checks.append(cleanup_threads(run))
    return 0 if write_results(run, "reviewer", checks, notes) else 1


def review_with(run: Run, reviewer: str, budget: int, notes: list[str]) -> list[dict[str, Any]]:
    from tsw.review.run import REVIEWERS  # noqa: PLC0415 - only this scenario needs it

    prefix = {"codex-app-server": "X", "claude-headless": "C"}.get(reviewer, "?")
    if reviewer not in REVIEWERS:
        reason = f"本版本没有 {reviewer} 评审适配器（tsw.review.run.REVIEWERS），无法运行"
        notes.append(f"{reviewer}：未验证（NOT VERIFIED）——{reason}")
        return [check(f"{prefix}0", f"{reviewer} 可运行", False, reason)]
    tsw(run, "init")
    base = init_repo(run, REPO_FILES)
    write_config(
        run,
        {
            "repositories": {"live-repo": {"path": str(run.repo)}},
            "roles": {"reviewer": {"session": reviewer}},
        },
    )
    tsw(run, "lease", "acquire", "--session", ORCH_SESSION)
    tsw(
        run,
        "task",
        "register",
        "--task-id",
        "T001",
        "--title",
        "live reviewer sample",
        "--repo",
        "live-repo",
        "--kind",
        "code",
        "--goal",
        "在 calc.py 中新增求和与平均值函数并补单元测试。",
        "--acceptance",
        "函数正确并有测试",
    )
    tool(
        run,
        "task_plan",
        "orchestrator",
        ORCH_SESSION,
        task_id="T001",
        dependencies=[],
        resources=[],
        reason="live",
    )
    workdir = run.home / "work" / "T001" / "a1"
    tool(
        run,
        "task_dispatch",
        "orchestrator",
        ORCH_SESSION,
        task_id="T001",
        workdir=str(workdir),
        user_turn=True,
    )
    started = tool(
        run,
        "attempt_start",
        "orchestrator",
        ORCH_SESSION,
        task_id="T001",
        purpose="dispatch",
        user_turn=True,
    )
    data = started["events"][0]["data"]
    tool(
        run,
        "attempt_reconcile",
        "orchestrator",
        ORCH_SESSION,
        task_id="T001",
        attempt=data["attempt"],
        generation=data["generation"],
        outcome="created",
        session_ref=WORKER_SESSION,
        brief_delivered=True,
    )
    git_out(run.dir, "clone", "-q", str(run.origin), str(workdir))
    git_out(workdir, "checkout", "-q", "-b", "feature")
    (workdir / "calc.py").write_text(
        CALC
        + "\n\ndef add(a: int, b: int) -> int:\n    return a + b\n"
        + "\n\ndef mean(values: list[int]) -> float:\n"
        + "    # Average of the values.\n    return sum(values) / (len(values) - 1)\n",
        encoding="utf-8",
        newline="\n",
    )
    git_out(workdir, "commit", "-q", "-am", "add add() and mean()")
    env = clean_env(run, CLAUDE_CODE_HOST_SESSION_ID=WORKER_SESSION)
    results: list[dict[str, Any]] = []
    calls = 0
    rounds: list[dict[str, Any]] = []
    for round_no in (1, 2):
        if calls >= budget:
            notes.append(f"{reviewer}：模型调用达到上限 {budget}，停止")
            break
        before = tree_digest(workdir)
        run.record("codex-thread", f"T001-a1-r{round_no}", reviewer=reviewer)
        calls += 1
        out = tsw(
            run, "review", "run", "--notes", f"live reviewer round {round_no}", env=env, cwd=workdir
        )
        after = tree_digest(workdir)
        review_dir = run.home / "runs" / "T001" / "a1" / "review"
        meta = read_json(review_dir / f"r{round_no}-run.json")
        result = read_json(review_dir / f"r{round_no}.json")
        thread = meta.get("thread_id")
        run.record("codex-thread", f"T001-a1-r{round_no}", state="created", ref=thread)
        rounds.append({"out": out, "result": result, "meta": meta, "unchanged": before == after})
        if round_no == 1:
            items = [
                {"finding": f["id"], "action": "fix", "verification": "round 2 re-review"}
                for f in result.get("findings") or []
                if isinstance(f, dict) and "id" in f
            ]
            tool(
                run, "review_triage", "worker", WORKER_SESSION, task_id="T001", round=1, items=items
            )
            head1 = git_out(workdir, "rev-parse", "HEAD")
            tool(
                run,
                "review_decide",
                "worker",
                WORKER_SESSION,
                task_id="T001",
                round=1,
                decision="iterate",
                head=head1,
            )
            text = (workdir / "calc.py").read_text(encoding="utf-8")
            text = text.replace("(len(values) - 1)", "len(values)")
            (workdir / "calc.py").write_text(text, encoding="utf-8", newline="\n")
            git_out(workdir, "commit", "-q", "-am", "fix mean()")
    (run.dir / "review-output.json").write_text(
        json.dumps(rounds, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    results.append(round_check(prefix, 1, reviewer, rounds, base, incremental=False))
    results.append(round_check(prefix, 2, reviewer, rounds, base, incremental=True))
    results.append(
        check(
            f"{prefix}3",
            f"{reviewer} 只读：评审前后执行目录（含 .git）不变",
            len(rounds) == 2 and all(r["unchanged"] for r in rounds),
            ", ".join(f"r{i + 1} unchanged={r['unchanged']}" for i, r in enumerate(rounds)),
        )
    )
    notes.append(f"{reviewer}：模型调用 {calls} 次（上限 {budget}）")
    return results


RESULT_KEYS = {"verdict", "summary", "reviewed_head", "findings"}


def round_check(
    prefix: str,
    round_no: int,
    reviewer: str,
    rounds: list[dict[str, Any]],
    base: str,
    *,
    incremental: bool,
) -> dict[str, Any]:
    title = f"{reviewer} 第 {round_no} 轮{'增量' if incremental else '全量'}评审完成且满足输出契约"
    if len(rounds) < round_no:
        return check(f"{prefix}{round_no}", title, False, "没有运行")
    r = rounds[round_no - 1]
    result: Mapping[str, Any] = r["result"]
    meta: Mapping[str, Any] = r["meta"]
    problems: list[str] = []
    if not set(result) >= RESULT_KEYS:
        problems.append(f"结果缺字段 {sorted(RESULT_KEYS - set(result))}")
    if meta.get("error"):
        problems.append(f"error={meta.get('error')}")
    if meta.get("head") and result.get("reviewed_head") not in (None, meta.get("head")):
        problems.append("reviewed_head 与评审 HEAD 不一致")
    if incremental:
        first = rounds[0]["meta"].get("thread_id")
        if not meta.get("resumed") or meta.get("resumed") != first:
            problems.append(
                f"没有续用第 1 轮线程（resumed={meta.get('resumed')}，第 1 轮={first}）"
            )
    elif meta.get("base") != base:
        problems.append(f"范围起点不是任务开始前的提交（base={meta.get('base')}）")
    evidence = (
        f"verdict={result.get('verdict')}, findings={len(result.get('findings') or [])}, "
        f"thread={meta.get('thread_id')}, resumed={meta.get('resumed')}"
    )
    return check(f"{prefix}{round_no}", title, not problems, "; ".join([evidence, *problems]))


def cleanup_threads(run: Run) -> dict[str, Any]:
    """Archive the run's Codex threads (tsw archives after each round) and read them back."""
    from tsw.review.codex import AppServer, resolve_executable  # noqa: PLC0415

    entries = [e for e in run.manifest()["entries"] if e["kind"] == "codex-thread"]
    left: list[str] = []
    if entries:
        server = AppServer(resolve_executable(None))
        try:
            server.call(
                "initialize",
                {"clientInfo": {"name": "tsw-live", "version": "0"}, "capabilities": {}},
            )
            server.notify("initialized")
            archived = archived_threads(server)
            for entry in entries:
                ref = entry.get("ref") or thread_of(run, str(entry["name"]))
                if ref and not entry.get("ref"):
                    run.record("codex-thread", entry["name"], ref=ref)
                if not ref:
                    left.append(f"{entry['name']}（线程 ID 未知）")
                    continue
                if ref not in archived:
                    try:
                        server.call("thread/archive", {"threadId": ref}, timeout=30)
                    except Exception as exc:  # report, never retry blindly
                        left.append(f"{ref}: {exc}")
                        continue
                    archived = archived_threads(server)
                if ref in archived:
                    run.record("codex-thread", entry["name"], state="archived", confirmed=utc())
                else:
                    left.append(f"{ref}（读回未见归档）")
        finally:
            server.close()
    return check(
        "R4",
        "清理：本次创建的评审线程已归档并读回确认",
        not left,
        f"{len(entries) - len(left)}/{len(entries)} 已确认" + (f"；剩余 {left}" if left else ""),
    )


def thread_of(run: Run, name: str) -> str | None:
    """The thread a round used, from tsw's own record (``T001-a1-r1`` → ``r1-run.json``)."""
    task_id, attempt, round_name = name.split("-")
    meta = read_json(run.home / "runs" / task_id / attempt / "review" / f"{round_name}-run.json")
    thread = meta.get("thread_id")
    return str(thread) if thread else None


def archived_threads(server: Any) -> set[str]:
    found: set[str] = set()
    cursor: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {"archived": True, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        page = server.call("thread/list", params, timeout=30) or {}
        for item in page.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                found.add(str(item["id"]))
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return found


# ---------------------------------------------------------------------------- host-e2e


def fill(text: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def install_gh(run: Run) -> Path:
    run.bin.mkdir()
    gh = run.bin / "gh"
    shutil.copyfile(LIVE / "gh_stub.py", gh)
    (run.bin / "gh.cmd").write_text(f'@python -X utf8 "{gh}" %*\r\n', encoding="utf-8")
    (run.dir / "gh-state.json").write_text(
        json.dumps({"repository": None, "pulls": []}) + "\n", encoding="utf-8"
    )
    return gh


def session_settings(run: Run, directory: Path) -> None:
    """Point sessions opened in ``directory`` at this run's data directory.

    The user's own settings give every Claude Code session ``TSW_HOME`` (their real data
    directory). Project settings of the folder a session is opened in override it, so the run's
    main session and workers resolve this run's data directory, never the real one.
    """
    run.check_inside(directory)
    settings = directory / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"env": {"TSW_HOME": str(run.home), "TSW_LIVE_RUN": str(run.dir)}}, indent=2),
        encoding="utf-8",
    )


def host_prepare(args: argparse.Namespace) -> int:
    spec = scenario("host-e2e")
    run = Run.create("host-e2e")
    tsw(run, "init")
    tsw(run, "host", "render")
    run.record("dir", "home", state="created", ref=str(run.home))
    base = init_repo(run, REPO_FILES)
    run.record("dir", "repo", state="created", ref=str(run.repo), base=base)
    gh = install_gh(run)
    repository = str(spec["repository"])
    state = json.loads((run.dir / "gh-state.json").read_text(encoding="utf-8"))
    state["repository"] = repository
    (run.dir / "gh-state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    config = dict(spec["config"])
    config["repositories"] = {
        "live-repo": {
            "path": str(run.repo),
            "repository": repository,
            "delivery": {"push": True, "create_pr": True, "merge": True},
        }
    }
    write_config(run, config)
    timeout = int(spec["config"]["input"]["in_session_timeout_minutes"])
    values = {
        "origin": run.origin.as_posix(),
        "gh": gh.as_posix(),
        "repository": repository,
        "timeout_plus": str(timeout + 1),
        "home": str(run.home),
        "run_id": run.id,
    }
    session_settings(run, run.home)
    for task in spec["tasks"]:
        tsw(
            run,
            "task",
            "register",
            "--task-id",
            task["task_id"],
            "--title",
            task["title"],
            "--repo",
            "live-repo",
            "--kind",
            task["kind"],
            "--goal",
            fill(task["goal"], values),
            *[a for item in task["acceptance"] for a in ("--acceptance", item)],
        )
        for attempt in ("a1", "a2"):
            session_settings(run, run.home / "work" / task["task_id"] / attempt)
    steps = [fill(s, values) for s in spec["operator"]]
    operator = [
        f"# live host-e2e {run.id}",
        "",
        "按顺序操作（每一步都在 Claude 桌面 app 中，不在终端）：",
        "",
        *[f"{i}. {s}" for i, s in enumerate(steps, 1)],
        "",
    ]
    (run.dir / "OPERATOR.md").write_text("\n".join(operator), encoding="utf-8")
    run.record("claude-session", "main", note="用户在 app 中于 home/ 新建；verify 时补记会话 ID")
    print("\n".join(operator))
    print(f"run {run.id}: {run.dir}")
    return 0


def first(
    evs: Iterable[dict[str, Any]], type_: str, task: str | None = None, **data: Any
) -> dict[str, Any] | None:
    for e in evs:
        if e["type"] != type_ or (task is not None and e["task_id"] != task):
            continue
        if all(e["data"].get(k) == v for k, v in data.items()):
            return e
    return None


def seqs(evs: Iterable[dict[str, Any]], kind: str, task: str | None = None) -> list[int]:
    return [
        e["sequence"] for e in evs if e["type"] == kind and (task is None or e["task_id"] == task)
    ]


def host_verify(args: argparse.Namespace) -> int:
    run = Run.open(args.run_id)
    evs = events(run)
    notes: list[str] = []
    checks: list[dict[str, Any]] = []
    marker = json.loads((run.home / "tsw-home.json").read_text(encoding="utf-8"))
    orch = [
        e for e in evs if e["actor"].get("role") == "orchestrator" and e["actor"].get("session_id")
    ]
    main_sessions = sorted({str(e["actor"]["session_id"]) for e in orch})
    for sid in main_sessions:
        run.record("claude-session", "main", state="created", ref=sid)
    checks.append(
        check(
            "H1",
            "隔离：数据目录与产品仓库指向本运行，主会话在其中操作",
            Path(marker.get("product_repo", "")).resolve() == ROOT.resolve() and bool(orch),
            f"product_repo=<repo>; 主会话 {main_sessions}; 主会话事件 {len(orch)} 条",
        )
    )
    tasks = ("T001", "T002", "T003")
    workers: dict[str, list[str]] = {}
    for e in evs:
        ref = e["data"].get("session_ref")
        if e["type"] in ("attempt.bound", "attempt.prestarted", "attempt.continued") and ref:
            workers.setdefault(str(e["task_id"]), []).append(str(ref))
        name = e["data"].get("session_name")
        if name and e["type"] == "attempt.start_requested":
            run.record("claude-session", str(name), task=e["task_id"])
    for task_id, refs in workers.items():
        for ref in refs:
            run.record("claude-session", f"{task_id}:{ref}", state="created", ref=ref, task=task_id)
    prestarted = seqs(evs, "attempt.prestarted")
    delivered = {
        t: seqs(evs, "attempt.brief_delivered", t) + seqs(evs, "attempt.bound", t) for t in tasks
    }
    checks.append(
        check(
            "H2",
            "预启动与派发：预启动、任务书送达，三个任务都进入执行",
            bool(prestarted) and all(delivered.values()),
            f"prestarted={prestarted}; delivered/bound={delivered}",
        )
    )
    reviews: dict[str, Any] = {}
    for t in ("T001", "T002"):
        done = [e for e in evs if e["type"] == "review.completed" and e["task_id"] == t]
        started = [e for e in evs if e["type"] == "review.started" and e["task_id"] == t]
        finalize = first(evs, "review.decision", t, decision="finalize")
        reviewers = sorted({str((e["data"].get("reviewer") or {}).get("session")) for e in started})
        reviews[t] = {
            "rounds": [e["sequence"] for e in done],
            "reviewers": reviewers,
            "finalize": finalize and finalize["sequence"],
        }
    checks.append(
        check(
            "H3",
            "自评审：T001、T002 经 codex-app-server 评审并 finalize",
            all(
                r["rounds"] and r["finalize"] and r["reviewers"] == ["codex-app-server"]
                for r in reviews.values()
            ),
            reviews,
        )
    )
    submitted = {t: first(evs, "worker.submitted", t) for t in ("T001", "T002")}
    accepted = {t: seqs(evs, "task.accepted", t) for t in ("T001", "T002")}
    checks.append(
        check(
            "H4",
            "提交与技术验收：T001、T002 的 worker.submitted 与 task.accepted",
            all(submitted.values()) and all(accepted.values()),
            {
                t: {"submitted": s and s["sequence"], "accepted": accepted[t]}
                for t, s in submitted.items()
            },
        )
    )
    user_accepted = {t: seqs(evs, "task.user_accepted", t) for t in ("T001", "T002")}
    checks.append(
        check(
            "H4u",
            "用户验收：task.user_accepted",
            all(user_accepted.values()),
            user_accepted,
            optional=True,
        )
    )
    life = {k: seqs(evs, k, "T003") for k in ("task.paused", "task.resumed", "task.cancelled")}
    checks.append(check("H5", "取消与恢复：T003 暂停、恢复、取消", all(life.values()), life))
    checks.append(b1(evs))
    checks.append(b2(evs))
    checks.append(merged(run, evs, submitted))
    entries = [e for e in run.manifest()["entries"] if e["kind"] == "claude-session"]
    notes.append(
        "Claude 会话（清理时逐个归档并用 get_session 读回）："
        + ", ".join(f"{e['name']}={e.get('ref')}" for e in entries)
    )
    notes.append(
        "GitHub（push 目标、PR、合并）由运行目录内的 origin.git 与 gh 替身模拟，相关证据为模拟证据"
    )
    return 0 if write_results(run, "host-e2e", checks, notes) else 1


def b1(evs: list[dict[str, Any]]) -> dict[str, Any]:
    asked = first(evs, "input.escalated", "T001", kind="in_session")
    problems: list[str] = []
    evidence: dict[str, Any] = {}
    if asked is None:
        return check(
            "B1",
            "会话内提问与 hook 记录的答复（T001）",
            False,
            "没有 in_session 的 input.escalated",
        )
    request, session = asked["data"]["request_id"], asked["data"]["session_ref"]
    evidence["escalated"] = asked["sequence"]
    message = first(evs, "input.session_message", "T001", request_id=request)
    if message is None:
        problems.append("hook 没有记录 input.session_message")
    else:
        evidence["session_message"] = message["sequence"]
        if message["data"].get("session_ref") != session:
            problems.append("记录的消息不来自提问的 worker 会话")
    resolved = first(evs, "input.resolved", "T001", request_id=request)
    if resolved is None:
        problems.append("没有 input.resolved")
    else:
        evidence["resolved"] = resolved["sequence"]
        source = resolved["data"].get("source") or {}
        if resolved["data"].get("source_kind") != "user" or source.get("channel") != "in-session":
            problems.append(f"答复来源不是会话内用户：{resolved['data'].get('source_kind')}")
        if message is not None and source.get("message_ref") != message["data"].get("message_id"):
            problems.append("答复没有引用 hook 记录的消息编号")
        if resolved["actor"].get("session_id") != session:
            problems.append("登记答复的不是提问的 worker 会话")
    return check(
        "B1",
        "会话内提问与 hook 记录的答复（T001）",
        not problems,
        {**evidence, "problems": problems},
    )


def b2(evs: list[dict[str, Any]]) -> dict[str, Any]:
    asked = first(evs, "input.escalated", "T002", kind="in_session")
    if asked is None:
        return check(
            "B2", "超时转交与主会话处理（T002）", False, "没有 in_session 的 input.escalated"
        )
    request = asked["data"]["request_id"]
    handed = first(evs, "input.handed_off", "T002", request_id=request)
    resolved = first(evs, "input.resolved", "T002", request_id=request)
    relayed = first(evs, "input.relayed", "T002")
    decision = first(evs, "orchestrator.decision", "T002")
    problems: list[str] = []
    if handed is None:
        problems.append("没有 input.handed_off")
    if resolved is None:
        problems.append("问题没有得到答复")
    elif resolved["data"].get("source_kind") == "orchestrator" and decision is None:
        problems.append("主会话代答没有 orchestrator.decision")
    elif resolved["data"].get("source_kind") == "user" and relayed is None:
        problems.append("用户答复不是经转达得到的（应在转交后由主会话处理）")
    evidence = {
        "escalated": asked["sequence"],
        "handed_off": handed and {"seq": handed["sequence"], "by": handed["data"].get("by")},
        "decision": decision and decision["sequence"],
        "relayed": relayed and relayed["sequence"],
        "resolved": resolved
        and {"seq": resolved["sequence"], "source_kind": resolved["data"].get("source_kind")},
        "problems": problems,
    }
    return check("B2", "超时转交与主会话处理（T002）", not problems, evidence)


def merged(
    run: Run, evs: list[dict[str, Any]], submitted: Mapping[str, dict[str, Any] | None]
) -> dict[str, Any]:
    state = json.loads((run.dir / "gh-state.json").read_text(encoding="utf-8"))
    pulls = {int(p["number"]): p for p in state["pulls"]}
    main_log = git_out(run.origin, "log", "--format=%H", "main").split()
    problems: list[str] = []
    evidence: dict[str, Any] = {}
    for t, sub in submitted.items():
        if sub is None:
            problems.append(f"{t} 没有提交")
            continue
        merge = sub["data"].get("merge") or {}
        head = sub["data"].get("head")
        if not merge:
            problems.append(f"{t} 提交没有 merge 记录")
            continue
        number = int(str(merge.get("pr", "0")).rstrip("/").rsplit("/", 1)[-1] or 0)
        pr = pulls.get(number)
        evidence[t] = {"pr": number, "merge_commit": merge.get("merge_commit"), "head": head}
        if merge.get("merged_head") != head:
            problems.append(f"{t} merged_head 不等于提交 HEAD")
        if pr is None or pr["state"] != "MERGED":
            problems.append(f"{t} 替身中 PR #{number} 未合并")
        elif pr["merge_commit"] != merge.get("merge_commit") or pr["merged_head"] != head:
            problems.append(f"{t} 替身记录的合并与提交不一致")
        if merge.get("merge_commit") not in main_log:
            problems.append(f"{t} 合并提交不在 origin 的 main 上")
    return check(
        "C",
        "worker 自行合并并通过 G-SUBMIT（GitHub 为模拟）",
        not problems,
        {**evidence, "problems": problems},
    )


# ---------------------------------------------------------------------------- cleanup / list


def cleanup(args: argparse.Namespace) -> int:
    run = Run.open(args.run_id)
    threads = cleanup_threads(run)
    print(f"{threads['id']} {threads['title']}：{threads['evidence']}")
    for sid in args.archived or []:
        manifest = run.manifest()
        matched = False
        for entry in manifest["entries"]:
            if entry["kind"] == "claude-session" and entry.get("ref") == sid:
                entry["state"] = "archived"
                entry["confirmed"] = f"{utc()} get_session（操作者读回）"
                matched = True
        if not matched:
            raise SystemExit(f"live: 会话 {sid} 不在本次运行的清单中，拒绝记录")
        run.save(manifest)
    left = [
        e
        for e in run.manifest()["entries"]
        if e["kind"] in ("claude-session", "codex-thread")
        and e["state"] not in ("archived",)
        and e.get("ref")
    ]
    for e in left:
        print(f"未清理：{e['kind']} {e['name']} {e.get('ref')}（{e['state']}）")
    done = not left and threads["status"] == "passed"
    print("清理完成" if done else f"清理未完成：剩余 {len(left)} 项会话或线程")
    return 0 if done else 1


def list_runs(args: argparse.Namespace) -> int:
    if not runs_dir().is_dir():
        return 0
    for path in sorted(runs_dir().iterdir()):
        result = path / "result.json"
        status = "未检查"
        if result.is_file():
            status = (
                "通过" if json.loads(result.read_text(encoding="utf-8"))["passed"] else "未通过"
            )
        print(f"{path.name}\t{status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(prog="live.py", description="live 场景（只在本机）")
    parser.add_argument("--runs-dir", help="运行目录的父目录（默认 .local/live 或 TSW_LIVE_RUNS）")
    sub = parser.add_subparsers(dest="scenario", required=True)
    rev = sub.add_parser("reviewer", help="评审器场景（无人值守）")
    rev.add_argument("--reviewer", action="append", help="只运行指定评审器（可重复）")
    rev.set_defaults(handler=reviewer_scenario)
    host = sub.add_parser("host-e2e", help="Claude app 全流程（需要用户在场）")
    steps = host.add_subparsers(dest="step", required=True)
    steps.add_parser("prepare").set_defaults(handler=host_prepare)
    ver = steps.add_parser("verify")
    ver.add_argument("run_id")
    ver.set_defaults(handler=host_verify)
    clean = sub.add_parser("cleanup", help="记录已归档的会话并报告剩余对象")
    clean.add_argument("run_id")
    clean.add_argument("--archived", action="append", help="已归档并用 get_session 读回的会话 ID")
    clean.set_defaults(handler=cleanup)
    sub.add_parser("list").set_defaults(handler=list_runs)
    args = parser.parse_args(argv)
    if args.runs_dir:
        os.environ["TSW_LIVE_RUNS"] = str(Path(args.runs_dir).expanduser().resolve())
    handler: Any = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
