"""Live scenarios: the real Claude app and real reviewers, on this machine only.

    python tools/live.py reviewer [--reviewer codex-app-server]   run the reviewer scenario
    python tools/live.py host-e2e prepare                          set up a run for the Claude app
    python tools/live.py host-e2e verify RUN_ID [--trust-prompted yes|no] [--blocked yes|no]
    python tools/live.py routine-e2e prepare | verify RUN_ID [...]  the routine session, Teams
                                                                  check the run's ledger
    python tools/live.py handoff-e2e prepare | verify RUN_ID [--blocked yes|no]
                                                    one orchestrator hands over to another
    python tools/live.py title-e2e prepare | verify RUN_ID [--observed yes|no]
                                                    worker session titles (DN002 §5.8)
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
import re
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
from tsw.machine.ask_user import user_words  # noqa: E402
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
    "user.email=live@example.com",
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


def installed_runtime_claims(env: Mapping[str, str] | None = None) -> Path:
    """The runtime the installed plugin uses must know claiming data directories.

    A worker's folder carries no settings, so its session gets the user's environment:
    ``TSW_HOME`` names the real data directory. The cached plugin's launcher picks its runtime
    in this order (``plugin/bin/tsw_launch.py``): ``TSW_REPO``; the checkout the plugin
    directory sits in, which a cached copy never has; the ``product_repo`` recorded in the data
    directory ``TSW_HOME`` names. Only a runtime that knows ``claim_sessions`` turns to the run's
    data directory; an older one would work in the real data directory, so the run is refused,
    and so is a run whose runtime cannot be determined. Returns the runtime the workers use.
    """
    environ = os.environ if env is None else env
    candidates: list[tuple[Path, str]] = []
    if environ.get("TSW_REPO"):
        candidates.append((Path(environ["TSW_REPO"]).expanduser(), "TSW_REPO"))
    real = environ.get("TSW_HOME")
    marker = Path(real).expanduser() / "tsw-home.json" if real else None
    if marker is not None and marker.is_file():
        repo = json.loads(marker.read_text(encoding="utf-8")).get("product_repo")
        if repo:
            candidates.append((Path(str(repo)), f"数据目录 {real} 的 product_repo"))
    # The launcher skips a candidate without the package and takes the next one.
    usable = [(p, s) for p, s in candidates if (p / "tsw" / "__init__.py").is_file()]
    chosen, source = usable[0] if usable else (None, "")
    home_py = chosen / "tsw" / "home.py" if chosen is not None else None
    if chosen is None or home_py is None or not home_py.is_file():
        raise SystemExit(
            "live: 无法确定已安装插件在 worker 会话中使用的运行时（没有 TSW_REPO，TSW_HOME 的"
            " tsw-home.json 也没有 product_repo）；先核实再准备运行"
        )
    if "claim_sessions" not in home_py.read_text(encoding="utf-8"):
        raise SystemExit(
            f"live: 已安装的插件经 {source} 找到的运行时（{chosen}）还不认识 claim_sessions，"
            "worker 会话会落到真实数据目录；先把该检出更新到含 claim_sessions 的 main，再准备运行"
        )
    return chosen


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
                same = fields.get("ref") in (None, entry.get("ref"))
                if entry.get("state") == "archived" and fields.get("state") == "created" and same:
                    fields = {k: v for k, v in fields.items() if k != "state"}  # never undo cleanup
                entry.update(fields)
                break
        else:
            entry = {"kind": kind, "name": name, "state": "maybe_created", **fields}
            manifest["entries"].append(entry)
        self.save(manifest)
        return dict(entry)

    def bind_main(self, session: str) -> None:
        """Give a main session found in the ledger its own entry, keyed by its ID: the first
        one fills the ``main`` placeholder written at prepare, later ones get ``main:<ID>``."""
        manifest = self.manifest()
        entries = [e for e in manifest["entries"] if e["kind"] == "claude-session"]
        if any(e.get("ref") == session for e in entries):
            return
        placeholder = next((e for e in entries if e["name"] == "main" and not e.get("ref")), None)
        if placeholder is not None:
            placeholder.update(state="created", ref=session)
        else:
            manifest["entries"].append(
                {
                    "kind": "claude-session",
                    "name": f"main:{session}",
                    "state": "created",
                    "ref": session,
                }
            )
        self.save(manifest)

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


def probe_marks(run: Run) -> dict[str, str]:
    """Check W's marks: each is only in the file it proves was read or the skill it proves was
    used, never in a task's goal or acceptance."""
    suffix = run.id.rsplit("-", 1)[-1].upper()
    return {
        "agents": f"AGENTS-{suffix}",
        "repo_skill": f"REPO-SKILL-{suffix}",
        "home_skill": f"HOME-SKILL-{suffix}",
    }


def probe_skill(name: str, where: str, mark: str) -> str:
    return (
        f"---\nname: {name}\ndescription: {where}的探针 skill。任务要求使用它时使用。\n---\n"
        f"把下面这个标记原样写进报告：{mark}\n"
    )


def host_repo_files(run: Run) -> dict[str, str]:
    """live-repo for host-e2e: its AGENTS.md and its own skill carry check W's marks."""
    marks = probe_marks(run)
    return {
        **REPO_FILES,
        "AGENTS.md": REPO_FILES["AGENTS.md"] + f"- 规则标记（写进报告）：{marks['agents']}\n",
        ".claude/skills/live-repo-probe/SKILL.md": probe_skill(
            "live-repo-probe", "live-repo ", marks["repo_skill"]
        ),
    }


def events(run: Run) -> list[dict[str, Any]]:
    storage = FsStorage(Home(run.home))
    return [
        {
            "sequence": e.sequence,
            "type": e.type,
            "task_id": e.task_id,
            "revision": e.revision,
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
    passed = all(
        c["status"] == "passed"
        for c in checks
        if not c.get("optional") and c["status"] != NOT_VERIFIED
    )
    result = {"runId": run.id, "time": utc(), "passed": passed, "checks": checks, "notes": notes}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    (run.dir / "result.json").write_text(text + "\n", encoding="utf-8")
    lines = [
        f"# live {title}：{run.id}",
        "",
        f"- 产品 HEAD：`{run.manifest()['productHead']}`"
        + ("（工作树有未提交改动）" if run.manifest().get("productDirty") else ""),
        f"- 结果：{'通过' if passed else '未通过'}"
        + ("（另有未验证项，见下表）" if any(c["status"] == NOT_VERIFIED for c in checks) else ""),
        "",
        "| 检查 | 结果 | 证据 |",
        "| --- | --- | --- |",
    ]
    for c in checks:
        mark = str(
            {"passed": "通过", "failed": "未通过", NOT_VERIFIED: "未验证（NOT VERIFIED）"}.get(
                c["status"], c["status"]
            )
        )
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
        return [
            {**check(f"{prefix}0", f"{reviewer} 可运行", False, reason), "status": NOT_VERIFIED}
        ]
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
    # attempt_start made the task's worktree of live-repo inside the execution directory (CC006
    # §8): the work and the review happen there.
    workdir = Path(str(data["worktree"]["path"]))
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


NOT_VERIFIED = "not_verified"  # could not run at all: reported, never counted as passed
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
    shutil.copyfile(LIVE / "capture_hook.py", run.bin / "capture_hook.py")
    (run.bin / "gh.cmd").write_text(f'@python -X utf8 "{gh}" %*\r\n', encoding="utf-8")
    (run.dir / "gh-state.json").write_text(
        json.dumps({"repository": None, "pulls": []}) + "\n", encoding="utf-8"
    )
    return gh


def bash_path(path: Path) -> str:
    """A Windows path as the sessions' Git Bash writes it (``C:/x`` becomes ``/c/x``)."""
    text = str(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        text = f"/{text[0].lower()}{text[2:]}"
    return text


def session_settings(run: Run, directory: Path) -> None:
    """Point sessions opened in ``directory`` at this run's data directory.

    The user's own settings give every Claude Code session ``TSW_HOME`` (their real data
    directory). Only the main session's folder, the run's data directory, gets this file: the
    operator trusts that folder anyway when opening the main session. Execution directories get
    none, as in a real data directory (decision 0003): a folder with its own settings asks for
    trust again, which is what the run checks. Workers find the run's data directory because it
    claims the sessions working inside it (``tsw init --claim-sessions``). The file carries no
    permission rules. The run's tasks call the GitHub stand-in by its full path (a session's
    shell does not take ``PATH`` from project settings); the hooks still recognise ``gh`` by its
    program name.
    """
    run.check_inside(directory)
    settings = directory / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    capture = run.bin / "capture_hook.py"
    hooks: dict[str, list[dict[str, Any]]] = {
        event: [{"hooks": [{"type": "command", "command": f'python -X utf8 "{capture}" {event}'}]}]
        for event in ("SessionStart", "UserPromptSubmit")
    }
    # How the main session sent the Teams card: the format is checked from the call (T256).
    hooks["PostToolUse"] = [
        {
            "matcher": CARD_TOOL,
            "hooks": [{"type": "command", "command": f'python -X utf8 "{capture}" PostToolUse'}],
        }
    ]
    # The installed plugin is a cached copy; until it is updated, its hooks do not see the
    # scheduling tools this checkout guards. Run this checkout's guard for them.
    launcher = ROOT / "plugin" / "bin" / "tsw_launch.py"
    hooks["PreToolUse"] = [
        {
            "matcher": "CronCreate|ScheduleWakeup",
            "hooks": [
                {"type": "command", "command": f'python -X utf8 "{launcher}" guard PreToolUse'}
            ],
        }
    ]
    env = {"TSW_HOME": str(run.home), "TSW_LIVE_RUN": str(run.dir)}
    data: dict[str, Any] = {"env": env, "hooks": hooks}
    settings.write_text(json.dumps(data, indent=2), encoding="utf-8")


def selected(spec: Mapping[str, Any], names: Iterable[str] | None) -> list[dict[str, Any]]:
    """The scenario's tasks a run uses: all of them, or the subset ``--tasks`` names."""
    tasks: list[dict[str, Any]] = list(spec["tasks"])
    wanted = {n.strip() for n in names or () if n.strip()}
    if not wanted:
        return tasks
    unknown = wanted - {str(t["task_id"]) for t in tasks}
    if unknown:
        raise SystemExit(f"live: 场景中没有任务 {sorted(unknown)}")
    return [t for t in tasks if str(t["task_id"]) in wanted]


def host_prepare(args: argparse.Namespace) -> int:
    return prepare("host-e2e", args)


def routine_prepare(args: argparse.Namespace) -> int:
    return prepare("routine-e2e", args)


def prepare(name: str, args: argparse.Namespace) -> int:
    spec = scenario(name)
    tasks = selected(spec, (getattr(args, "tasks", None) or "").split(","))
    installed_runtime_claims()
    run = Run.create(name)
    manifest = run.manifest()
    manifest["tasks"] = [str(t["task_id"]) for t in tasks]
    run.save(manifest)
    tsw(run, "init", "--claim-sessions")
    tsw(run, "host", "render")
    probe = run.home / ".claude" / "skills" / "live-home-probe" / "SKILL.md"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(
        probe_skill("live-home-probe", "数据目录", probe_marks(run)["home_skill"]),
        encoding="utf-8",
        newline="\n",
    )
    run.record("dir", "home", state="created", ref=str(run.home))
    base = init_repo(run, host_repo_files(run))
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
        "gh": bash_path(gh),
        "repository": repository,
        "timeout_plus": str(timeout + 1),
        "home": str(run.home),
        "run_id": run.id,
        "repo": str(run.repo),
        "interval": str((spec["config"].get("routine") or {}).get("interval_minutes", 10)),
        # The user's own terminal runs this checkout's tsw against the run's data directory.
        "tsw_cmd": f'cd "{ROOT}" && python -X utf8 -m tsw --home "{run.home}"',
    }
    session_settings(run, run.home)
    for task in tasks:
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
    code = "、".join(str(t["task_id"]) for t in tasks if t["kind"] != "research")
    life = any(t["task_id"] == "T003" for t in tasks)
    values["code_tasks"] = code
    values["prestart_step"] = "；T003 先预启动一个待命会话，等我说再派发" if life else ""
    values["life_step"] = (
        "在主会话中依次说：“派发 T003”，T003 的 worker 记录就绪后再说"
        "“暂停 T003”、“恢复 T003”、“取消 T003”。"
        if life
        else "（本次运行不含 T003，跳过暂停、恢复与取消。）"
    )
    steps = [fill(s, values) for s in spec["operator"]]
    operator = [
        f"# live {name} {run.id}",
        "",
        "开始前确认：",
        "",
        *[f"- {p}" for p in spec.get("prerequisites", [])],
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


def last(evs: list[dict[str, Any]], type_: str, task: str) -> dict[str, Any] | None:
    found = [e for e in evs if e["type"] == type_ and e["task_id"] == task]
    return found[-1] if found else None


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
        run.bind_main(sid)
    checks.append(
        check(
            "H1",
            "隔离：数据目录与产品仓库指向本运行，主会话在其中操作",
            Path(marker.get("product_repo", "")).resolve() == ROOT.resolve() and bool(orch),
            f"product_repo=<repo>; 主会话 {main_sessions}; 主会话事件 {len(orch)} 条",
        )
    )
    spec = scenario("host-e2e")
    chosen = selected(spec, run.manifest().get("tasks"))
    tasks = tuple(str(t["task_id"]) for t in chosen)
    code = tuple(str(t["task_id"]) for t in chosen if t["kind"] != "research")
    checks.append(permission_rules(run, chosen))
    reconcile_sessions(run, evs)
    prestarted = seqs(evs, "attempt.prestarted")
    delivered = {
        t: seqs(evs, "attempt.brief_delivered", t) + seqs(evs, "attempt.bound", t) for t in tasks
    }
    prestart = "T003" in tasks
    checks.append(
        check(
            "H2",
            "预启动与派发：任务书送达，每个任务都进入执行" + ("，并有预启动" if prestart else ""),
            (bool(prestarted) or not prestart) and all(delivered.values()),
            f"prestarted={prestarted}; delivered/bound={delivered}",
        )
    )
    reviews: dict[str, Any] = {}
    for t in code:
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
    submitted = {t: last(evs, "worker.submitted", t) for t in code}
    accepted = {t: seqs(evs, "task.accepted", t) for t in code}
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
    user_accepted = {t: seqs(evs, "task.user_accepted", t) for t in code}
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
    checks.append(worktrees(run, evs, submitted))
    checks.append(b1(evs))
    checks.append(b2(evs))
    checks.append(follow_ups(evs))
    checks.append(scheduled_prompts(run, evs))
    checks.append(merged(run, evs, submitted))
    checks.append(observed(args))
    # Only the checks the scenario lists (a check's id is the first word of its line).
    wanted = {str(c).split()[0] for c in spec["checks"]} - (set() if prestart else {"H5"})
    if "T002" not in tasks:
        wanted -= {"F"}
    checks = [c for c in checks if c["id"] in wanted or c["id"].rstrip("u") in wanted]
    notes.append(hook_summary(run))
    entries = [e for e in run.manifest()["entries"] if e["kind"] == "claude-session"]
    notes.append(
        "Claude 会话（清理时逐个归档并用 get_session 读回）："
        + ", ".join(f"{e['name']}={e.get('ref')}" for e in entries)
    )
    notes.append(
        "GitHub（push 目标、PR、合并）由运行目录内的 origin.git 与 gh 替身模拟，相关证据为模拟证据"
    )
    return 0 if write_results(run, "host-e2e", checks, notes) else 1


def routine_verify(args: argparse.Namespace) -> int:
    """The routine session, the handoff by the system rule and the Teams round trip."""
    run = Run.open(args.run_id)
    evs = events(run)
    marker = json.loads((run.home / "tsw-home.json").read_text(encoding="utf-8"))
    orch = [
        e for e in evs if e["actor"].get("role") == "orchestrator" and e["actor"].get("session_id")
    ]
    for sid in sorted({str(e["actor"]["session_id"]) for e in orch}):
        run.bind_main(sid)
    reconcile_sessions(run, evs)  # the workers, for cleanup
    checks = [
        check(
            "H1",
            "隔离：数据目录与产品仓库指向本运行，主会话在其中操作",
            Path(marker.get("product_repo", "")).resolve() == ROOT.resolve() and bool(orch),
            f"product_repo=<repo>; 主会话事件 {len(orch)} 条",
        ),
        routine_started(run, evs),
        routine_handoff(evs),
        teams_round_trip(evs),
        teams_card(run, evs, args.card_multiline),
        routine_wakes(run, evs),
        check(
            "O",
            "操作者观察：例行会话没有信任确认、没有被拦",
            args.trust_prompted == "no" and args.blocked == "no",
            {
                "trust_prompted": args.trust_prompted or "未记录",
                "blocked": args.blocked or "未记录",
                # Recorded only: whether the orchestrator was woken is check W, from the hooks
                # (an operator missed the wakes in the T243 run, T250).
                "woken_by_ticks": args.woken_by_ticks or "未记录（由检查 W 判定）",
                "evidence": "操作者在 app 中的观察",
            },
        ),
    ]
    notes = [
        hook_summary(run),
        "Teams 卡片与回复经用户真实的 Teams 自聊（真实证据）；GitHub 不参与本场景",
        "清空上下文（context_reset_hours，最短 1 小时）不在本场景的时长内，"
        "CC006 §6.5 的 R2 未在本次运行中验证",
    ]
    return 0 if write_results(run, "routine-e2e", checks, notes) else 1


def handoff_prepare(args: argparse.Namespace) -> int:
    return prepare("handoff-e2e", args)


def handoff_verify(args: argparse.Namespace) -> int:
    """One orchestrator hands the lease to another; the worker the first one started wakes the
    second (DN001 §6.3–§6.4, T254)."""
    run = Run.open(args.run_id)
    evs = events(run)
    marker = json.loads((run.home / "tsw-home.json").read_text(encoding="utf-8"))
    orch = [
        e for e in evs if e["actor"].get("role") == "orchestrator" and e["actor"].get("session_id")
    ]
    for sid in sorted({str(e["actor"]["session_id"]) for e in orch}):
        run.bind_main(sid)
    reconcile_sessions(run, evs)  # the worker, for cleanup
    handed, old, new = lease_handoff(evs)
    mains = {str(e["actor"]["session_id"]) for e in orch}
    checks = [
        check(
            "H1",
            "隔离：数据目录与产品仓库指向本运行，两个主会话都在其中操作",
            Path(marker.get("product_repo", "")).resolve() == ROOT.resolve() and len(mains) >= 2,
            f"product_repo=<repo>; 主会话 {len(mains)} 个，事件 {len(orch)} 条",
        ),
        handed,
        handoff_wake(run, evs, old, new),
        check(
            "O",
            "操作者观察：worker 与新主会话发消息时没有被拦",
            args.blocked == "no",
            {"blocked": args.blocked or "未记录", "evidence": "操作者在 app 中的观察"},
        ),
    ]
    notes = [
        hook_summary(run),
        "让出后的会话在会话开始时不自动领回由 component 与 surface 测试覆盖（SessionStart 只在"
        "会话恢复时运行，本场景不重启会话）",
        "GitHub 不参与本场景",
    ]
    return 0 if write_results(run, "handoff-e2e", checks, notes) else 1


def change_prepare(args: argparse.Namespace) -> int:
    return prepare("change-e2e", args)


def change_verify(args: argparse.Namespace) -> int:
    """The user adds to a task the orchestrator already accepted, in the worker's session; the
    worker records the change itself, delivers again and the orchestrator accepts revision 2
    (DN002 §5.1a, T262)."""
    run = Run.open(args.run_id)
    evs = events(run)
    marker = json.loads((run.home / "tsw-home.json").read_text(encoding="utf-8"))
    orch = [
        e for e in evs if e["actor"].get("role") == "orchestrator" and e["actor"].get("session_id")
    ]
    for sid in sorted({str(e["actor"]["session_id"]) for e in orch}):
        run.bind_main(sid)
    reconcile_sessions(run, evs)
    checks = [
        check(
            "H1",
            "隔离：数据目录与产品仓库指向本运行，主会话在其中操作",
            Path(marker.get("product_repo", "")).resolve() == ROOT.resolve() and bool(orch),
            f"product_repo=<repo>; 主会话事件 {len(orch)} 条",
        ),
        *requirement_change_checks(evs),
    ]
    final = last(evs, "worker.submitted", "T001")
    checks.append(merged(run, evs, {"T001": final}))
    checks.append(
        check(
            "O",
            "操作者观察：worker 与主会话发消息、worker 合并时没有被拦",
            args.blocked == "no",
            {"blocked": args.blocked or "未记录", "evidence": "操作者在 app 中的观察"},
        )
    )
    notes = [
        hook_summary(run),
        "GitHub（push 目标、PR、合并）由运行目录内的 origin.git 与 gh 替身模拟，相关证据为模拟证据",
    ]
    return 0 if write_results(run, "change-e2e", checks, notes) else 1


def title_prepare(args: argparse.Namespace) -> int:
    return prepare("title-e2e", args)


def title_verify(args: argparse.Namespace) -> int:
    """Worker session titles (DN002 §5.8, T272): prestarted with the standby mark, which the
    orchestrator drops on dispatch; accepted in the user's terminal, passed on by the routine
    session and prefixed by the orchestrator; renamed by the user, so left alone on cancel."""
    run = Run.open(args.run_id)
    evs = events(run)
    marker = json.loads((run.home / "tsw-home.json").read_text(encoding="utf-8"))
    orch = [
        e for e in evs if e["actor"].get("role") == "orchestrator" and e["actor"].get("session_id")
    ]
    for sid in sorted({str(e["actor"]["session_id"]) for e in orch}):
        run.bind_main(sid)
    reconcile_sessions(run, evs)
    checks = [
        check(
            "H1",
            "隔离：数据目录与产品仓库指向本运行，主会话在其中操作",
            Path(marker.get("product_repo", "")).resolve() == ROOT.resolve() and bool(orch),
            f"product_repo=<repo>; 主会话事件 {len(orch)} 条",
        ),
        *title_checks(run, evs),
        check(
            "O",
            "操作者观察：T001 改为“[已验收] …”，T002 保持用户的标题，没有出现改名确认",
            args.observed == "yes",
            {"observed": args.observed or "未记录", "evidence": "操作者在 app 中的观察"},
        ),
    ]
    notes = [
        hook_summary(run),
        "GitHub（push 目标、PR、合并）由运行目录内的 origin.git 与 gh 替身模拟，相关证据为模拟"
        "证据；会话标题由真实 app 的 set_session_title 修改，结果原文记在账本中",
    ]
    return 0 if write_results(run, "title-e2e", checks, notes) else 1


def title_checks(run: Run, evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Checks T, D, U, F, A and X of ``title-e2e``."""

    def updates(task: str, after: int = 0) -> list[dict[str, Any]]:
        return [
            e
            for e in evs
            if e["type"] == "attempt.session_updated"
            and e["task_id"] == task
            and e["sequence"] > after
        ]

    bases: dict[str, str] = {}
    started: list[dict[str, Any]] = []
    for task in ("T001", "T002"):
        found = first(evs, "attempt.start_requested", task, purpose="prestart")
        data = (found or {}).get("data") or {}
        bases[task] = str(data.get("title_base") or "")
        started.append(
            {
                "task": task,
                "title_base": data.get("title_base"),
                "session_title": data.get("session_title"),
            }
        )
    checks = [
        check(
            "T",
            "标题：预启动记下 title_base 与以“（待命）”结尾的 session_title",
            all(
                s["title_base"] and s["session_title"] == f"{s['title_base']}（待命）"
                for s in started
            ),
            started,
        )
    ]
    dispatched: list[dict[str, Any]] = []
    for task in ("T001", "T002"):
        delivered = first(evs, "attempt.brief_delivered", task)
        at = delivered["sequence"] if delivered else 0
        renamed = next(
            (
                e
                for e in updates(task, at)
                if e["data"].get("action") == "title"
                and e["data"].get("reason") == "dispatch"
                and e["data"].get("outcome") in ("renamed", "already")
            ),
            None,
        )
        dispatched.append(
            {
                "task": task,
                "delivered": delivered and delivered["sequence"],
                "updated": renamed and renamed["sequence"],
                "outcome": renamed and renamed["data"].get("outcome"),
                "title": renamed and renamed["data"].get("title"),
            }
        )
    checks.append(
        check(
            "D",
            "派发：投递任务书之后主会话去掉了“（待命）”",
            all(
                d["delivered"]
                and d["outcome"] in ("renamed", "already")
                and d["title"] == bases[str(d["task"])]
                for d in dispatched
            ),
            dispatched,
        )
    )
    accepted = first(evs, "task.user_accepted", "T001")
    source = ((accepted or {}).get("data") or {}).get("user_source") or {}
    checks.append(
        check(
            "U",
            "终端验收：T001 的 task.user_accepted 渠道为 user-cli",
            accepted is not None and source.get("channel") == "user-cli",
            {"accepted": accepted and accepted["sequence"], "channel": source.get("channel")},
        )
    )
    at = accepted["sequence"] if accepted else 0
    since = str((accepted or {}).get("time") or "")
    forwarded = [
        p["time"]
        for p in main_prompts(run)
        if since
        and p["time"] >= since
        and "TaskSteward 例行项 session-scan" in user_words(p["prompt"])
    ]
    checks.append(
        check(
            "F",
            "转告：终端验收之后主会话收到例行项 session-scan 的消息",
            bool(forwarded),
            {"since": since, "forwards": forwarded},
        )
    )
    prefixed = next(
        (
            e
            for e in updates("T001", at)
            if e["data"].get("action") == "title"
            and e["data"].get("reason") == "accepted"
            and e["data"].get("outcome") == "renamed"
        ),
        None,
    )
    wanted = f"[已验收] {bases['T001']}"
    data = (prefixed or {}).get("data") or {}
    checks.append(
        check(
            "A",
            "加前缀：主会话把 T001 的 worker 会话改为“[已验收] <title_base>”",
            prefixed is not None
            and prefixed["actor"].get("role") == "orchestrator"
            and data.get("outcome") == "renamed"
            and data.get("title") == wanted
            and wanted in str(data.get("result") or ""),
            {
                "updated": prefixed and prefixed["sequence"],
                "outcome": data.get("outcome"),
                "title": data.get("title"),
                "result": data.get("result"),
            },
        )
    )
    cancelled = first(evs, "task.cancelled", "T002")
    after_cancel = updates("T002", cancelled["sequence"] if cancelled else 0)
    left = next((e for e in after_cancel if e["data"].get("outcome") == "user_title"), None)
    renamed_after = [e["sequence"] for e in after_cancel if e["data"].get("outcome") == "renamed"]
    checks.append(
        check(
            "X",
            "用户改过名：T002 取消后记为 user_title，没有改名",
            cancelled is not None and left is not None and not renamed_after,
            {
                "cancelled": cancelled and cancelled["sequence"],
                "user_title": left and left["sequence"],
                "observed": left and left["data"].get("observed"),
                "renamed_after": renamed_after,
            },
        )
    )
    return checks


def requirement_change_checks(
    evs: list[dict[str, Any]], task: str = "T001"
) -> list[dict[str, Any]]:
    """Checks A, M, X, R and I of ``change-e2e``: delivered once, the user's added requirement
    recorded by the hook, registered by the worker (T28), delivered again and acknowledged."""

    def revision_of(event: dict[str, Any] | None) -> int | None:
        if event is None:
            return None
        submission = event["data"].get("submission") or {}
        return int(submission.get("revision") or event.get("revision") or 0)

    first_accept = first(evs, "task.accepted", task)
    first_submit = first(evs, "worker.submitted", task)
    checks = [
        check(
            "A",
            "第一次交付：revision 1 的提交与主会话技术验收",
            revision_of(first_submit) == 1
            and first_accept is not None
            and first_accept.get("revision") == 1,
            {
                "submitted": first_submit and first_submit["sequence"],
                "accepted": first_accept and first_accept["sequence"],
            },
        )
    ]
    after = first_accept["sequence"] if first_accept else 0
    change = next(
        (
            e
            for e in evs
            if e["type"] == "task.requirement_changed"
            and e["task_id"] == task
            and e["sequence"] > after
        ),
        None,
    )
    data = (change or {}).get("data") or {}
    source = data.get("user_source") or {}
    refs = str(source.get("message_ref") or "").split(",")
    recorded = [
        e
        for e in evs
        if e["type"] == "input.session_message"
        and e["task_id"] == task
        and e["sequence"] > after
        and e["data"].get("message_id") in refs
    ]
    attempt_session = next(
        (
            str(e["data"].get("session_ref"))
            for e in evs
            if e["task_id"] == task
            and e["type"] in ("attempt.bound", "attempt.brief_delivered", "attempt.prestarted")
            and e["data"].get("session_ref")
        ),
        None,
    )
    checks.append(
        check(
            "M",
            "记录：验收之后 hook 把用户在 worker 会话中追加的要求记为 input.session_message",
            bool(recorded)
            and all(
                not e["data"].get("request_id") and e["data"].get("session_ref") == attempt_session
                for e in recorded
            ),
            [
                {"seq": e["sequence"], "id": e["data"].get("message_id"), "text": e["data"]["text"]}
                for e in recorded
            ],
        )
    )
    texts = "\n\n".join(str(e["data"]["text"]) for e in recorded)
    by_orchestrator = [
        e["sequence"]
        for e in evs
        if e["type"] == "task.requirement_changed"
        and e["task_id"] == task
        and e["actor"].get("role") != "worker"
    ]
    checks.append(
        check(
            "X",
            "登记：worker 记录 T28、revision 2，来源是 hook 记录的原话；主会话没有 task_update",
            change is not None
            and change["actor"].get("role") == "worker"
            and data.get("transition") == "T28"
            and data.get("revision") == 2
            and source.get("channel") == "in-session"
            and bool(recorded)
            and source.get("text") == texts
            and not by_orchestrator,
            {
                "change": change and change["sequence"],
                "transition": data.get("transition"),
                "revision": data.get("revision"),
                "message_ref": source.get("message_ref"),
                "orchestrator_changes": by_orchestrator,
            },
        )
    )
    at = change["sequence"] if change else 0
    later = [e for e in evs if e["sequence"] > at and e["task_id"] == task]
    finalize = first(later, "review.decision", task, decision="finalize")
    second_submit = last(later, "worker.submitted", task)
    second_accept = last(later, "task.accepted", task)
    # The same attempt delivered both revisions: the change, the review that finalized it and
    # every later submission belong to the attempt of the first submission.
    original = (first_submit or {}).get("attempt")
    attempts = {
        e.get("attempt") or e["data"].get("attempt")
        for e in later
        if e["type"] in ("worker.submitted", "review.started", "review.decision")
    } | {data.get("attempt")}
    checks.append(
        check(
            "R",
            "重新交付：同一 attempt 的 revision 2 评审定稿、提交并经主会话再次技术验收",
            finalize is not None
            and revision_of(second_submit) == 2
            and second_accept is not None
            and second_accept.get("revision") == 2
            and original is not None
            and attempts == {original},
            {
                "original_attempt": original,
                "finalize": finalize and finalize["sequence"],
                "submitted": second_submit and second_submit["sequence"],
                "accepted": second_accept and second_accept["sequence"],
                "attempts": sorted(str(a) for a in attempts),
            },
        )
    )
    acked = [
        int(s)
        for e in evs
        if e["type"] == "orchestrator.inbox_acked"
        for s in e["data"].get("sequences") or []
    ]
    checks.append(
        check(
            "I",
            "通知：主会话确认了 worker 登记的需求变更",
            change is not None and change["sequence"] in acked,
            {"change": change and change["sequence"], "acked": acked},
        )
    )
    return checks


def lease_handoff(evs: list[dict[str, Any]]) -> tuple[dict[str, Any], str, str]:
    """L: the first orchestrator released the lease and stood down, another one took it without a
    takeover, and the first did not take it back. Returns the check and both session IDs."""
    title = "交接：A 让出租约，B 直接领取，A 没有再领取"
    released = next((e for e in evs if e["type"] == "controller.lease_released"), None)
    if released is None:
        return check("L", title, False, "没有 controller.lease_released"), "", ""
    old, seq = str(released["data"]["session_id"]), released["sequence"]
    stood = next(
        (
            e
            for e in evs
            if e["type"] == "controller.lease_stood_down"
            and e["data"].get("session_id") == old
            and e["data"].get("via") == "release"
            and e["sequence"] < seq
        ),
        None,
    )
    taken = next(
        (
            e
            for e in evs
            if e["type"] == "controller.lease_acquired"
            and e["sequence"] > seq
            and e["data"].get("session_id") != old
        ),
        None,
    )
    new = str(taken["data"]["session_id"]) if taken else ""
    back = [
        e["sequence"]
        for e in evs
        if e["type"] in ("controller.lease_acquired", "controller.lease_taken_over")
        and e["sequence"] > seq
        and e["data"].get("session_id") == old
    ]
    takeovers = seqs(evs, "controller.lease_taken_over")
    ok = (
        stood is not None
        and taken is not None
        and not taken["data"].get("renewed")
        and not back
        and not takeovers
    )
    evidence = {
        "released": seq,
        "stood_down": stood and stood["sequence"],
        "acquired_by_other": taken and taken["sequence"],
        "taken_back_by_old": back,
        "takeovers": takeovers,
    }
    return check("L", title, ok, evidence), old, new


def session_forwards(run: Run, session: str, sender: str, since: str) -> list[str]:
    """When ``session``'s hooks saw a message from ``sender`` (``<cross-session-message>``) at or
    after ``since``; times only, never the text."""
    path = run.dir / "hook-payloads.jsonl"
    if not session or not sender or not path.is_file():
        return []
    found: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        payload = record.get("payload")
        if record.get("event") != "UserPromptSubmit" or not isinstance(payload, dict):
            continue
        if str(record.get("session") or "") != session or str(record.get("time")) < since:
            continue
        words = user_words(str(payload.get("prompt") or ""))
        if words.startswith("<cross-session-message") and f'from="{sender}"' in words[:200]:
            found.append(str(record["time"]))
    return found


def handoff_wake(run: Run, evs: list[dict[str, Any]], old: str, new: str) -> dict[str, Any]:
    """K: after the handover the worker's question wakes the new orchestrator (its hooks saw the
    worker's message), not the one that started the worker; the new one answers and the worker
    hears it."""
    title = "唤醒：交接后 worker 的消息到达 B 而不是 A；B 回答，worker 收到"
    taken = next(
        (
            e
            for e in evs
            if e["type"] == "controller.lease_acquired" and e["data"].get("session_id") == new
        ),
        None,
    )
    if not new or taken is None:
        return check("K", title, False, "没有新主会话领取租约")
    question = next(
        (e for e in evs if e["type"] == "worker.question" and e["sequence"] > taken["sequence"]),
        None,
    )
    if question is None:
        return check("K", title, False, "B 领取之后没有 worker.question")
    worker = str(question["actor"].get("session_id") or "")
    released = next(e for e in evs if e["type"] == "controller.lease_released")
    to_new = session_forwards(run, new, worker, str(question["time"]))
    to_old = session_forwards(run, old, worker, str(released["time"]))
    decision = next(
        (
            e
            for e in evs
            if e["type"] == "orchestrator.decision"
            and e["task_id"] == question["task_id"]
            and e["sequence"] > question["sequence"]
            and e["actor"].get("session_id") == new
        ),
        None,
    )
    heard = (
        next(
            (
                e
                for e in evs
                if e["type"] == "worker.progress"
                and e["task_id"] == question["task_id"]
                and e["sequence"] > decision["sequence"]
                and e["actor"].get("session_id") == worker
            ),
            None,
        )
        if decision
        else None
    )
    ok = bool(to_new) and not to_old and decision is not None and heard is not None
    return check(
        "K",
        title,
        ok,
        {
            "question": question["sequence"],
            "messages_to_new": to_new,
            "messages_to_old_after_release": to_old,
            "decision_by_new": decision and decision["sequence"],
            "worker_heard": heard and heard["sequence"],
        },
    )


CARD_TOOL = "mcp__workiq__create_entity"
_CARD_TAGS = re.compile(r"<[^>]+>")


def card_calls(run: Run) -> list[dict[str, Any]]:
    """The main session's calls that sent a Teams message (its PostToolUse payloads): what it
    sent (``body``) and what came back (``response``, as text)."""
    path = run.dir / "hook-payloads.jsonl"
    if not path.is_file():
        return []
    home = os.path.normcase(os.path.realpath(run.home))
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        payload = record.get("payload")
        if record.get("event") != "PostToolUse" or not isinstance(payload, dict):
            continue
        if payload.get("tool_name") != CARD_TOOL:
            continue
        cwd = str(payload.get("cwd") or "")
        if not cwd or os.path.normcase(os.path.realpath(cwd)) != home:
            continue
        given = payload.get("tool_input")
        body: Any = given.get("jsonBody") if isinstance(given, dict) else None
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                body = None
        found = body.get("body") if isinstance(body, dict) else None
        out.append(
            {
                "time": str(record.get("time") or ""),
                "body": found if isinstance(found, dict) else {},
                "response": json.dumps(payload.get("tool_response"), ensure_ascii=False),
            }
        )
    return out


def teams_card(run: Run, evs: list[dict[str, Any]], seen_multiline: str | None) -> dict[str, Any]:
    """C (T256): the card of T002's question went out as rich text, from the main session's own
    call (its PostToolUse payload): ``contentType`` html, paragraphs, the worker's link filled
    in, and its text starting with the card's heading; the call created the message that
    ``channel_sent`` registered. That Teams shows it on several lines is the operator's
    observation (``--card-multiline``). Only facts are reported, never the card's text."""
    sent = first(evs, "channel.message_sent", "T002")
    message_id = str(sent["data"].get("message_id")) if sent else ""
    request_id = str(sent["data"].get("request_id")) if sent else ""
    calls = [c for c in card_calls(run) if message_id and message_id in c["response"]]
    call = calls[-1] if calls else None
    content = str((call or {}).get("body", {}).get("content") or "")
    words = re.sub(r"\s+", " ", _CARD_TAGS.sub(" ", content)).strip()
    content_type = str((call or {}).get("body", {}).get("contentType") or "")
    paragraphs = content.count("<p>")
    heading = bool(request_id) and words.startswith(f"[TaskSteward] T002 {request_id}")
    link_filled = bool(content) and "{worker_link}" not in content and "<a href=" in content
    facts = {
        "registered": bool(sent),
        "call_found": call is not None,
        "content_type": content_type or None,
        "paragraphs": paragraphs,
        "heading": heading,
        "link_filled": link_filled,
        "card_multiline": seen_multiline or "未记录",
    }
    ok = (
        call is not None
        and content_type.lower() == "html"
        and paragraphs >= 4
        and heading
        and link_filled
        and seen_multiline == "yes"
    )
    return check("C", "富文本卡片：经 WorkIQ 以 html 发出，Teams 中分行显示（T002）", ok, facts)


def main_prompts(run: Run) -> list[dict[str, Any]]:
    """The prompts the main session received (its hook payloads, cwd = the run's data dir)."""
    path = run.dir / "hook-payloads.jsonl"
    if not path.is_file():
        return []
    home = os.path.normcase(os.path.realpath(run.home))
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        payload = record.get("payload")
        if record.get("event") != "UserPromptSubmit" or not isinstance(payload, dict):
            continue
        cwd = str(payload.get("cwd") or "")
        if not cwd or os.path.normcase(os.path.realpath(cwd)) != home:
            continue
        out.append({"time": str(record.get("time") or ""), "prompt": str(payload.get("prompt"))})
    return out


def routine_wakes(run: Run, evs: list[dict[str, Any]]) -> dict[str, Any]:
    """W (T250): once the routine session in charge was detached, its runs no longer wake the
    orchestrator, and what it forwards still does. Read from the main session's hook payloads,
    not from what the operator saw; only counts and times are reported, never the text.

    All evidence belongs to the newest bound generation. An idle run is proven to have ended by
    the routine session's Stop hook (kept in ``cache/routine.json``), and at least one turn after
    the detach must have ended without forwarding anything."""
    bounds = [e for e in evs if e["type"] == "routine.bound"]
    bound = bounds[-1] if bounds else None
    generation = int(bound["data"]["generation"]) if bound else None
    session = str(bound["data"]["session_ref"]) if bound else ""
    detached = first(evs, "routine.detached", generation=generation) if bound is not None else None
    since = str(detached["time"]) if detached else ""
    notices: list[str] = []
    forwards: list[str] = []
    # The host's note when a session this one started ends a turn: 'The session "<title>" you
    # started (<id>) finished a turn'. Other notes list every linked session's ID too.
    started = re.compile(r"you started \(" + re.escape(session) + r"\)") if session else None
    for item in main_prompts(run) if started is not None else []:
        # The app may put its own reminders in front of a forwarded message; a notice can sit in
        # one of those reminders, so both are looked for, independently.
        words = user_words(item["prompt"])
        if words.startswith("<cross-session-message") and f'from="{session}"' in words[:200]:
            forwards.append(item["time"])
        if started is not None and started.search(item["prompt"]):
            notices.append(item["time"])
    after = [t for t in notices if since and t > since]
    forwarded = [t for t in forwards if since and t > since]
    cache_path = run.home / "cache" / "routine.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    same = generation is not None and cache.get("generation") == generation

    def times(key: str) -> list[str]:
        return sorted(str(t) for t in cache.get(key) or [] if same and since and str(t) > since)

    runs, ends = times("runs"), times("ends")
    # What the routine session forwards leaves events in the ledger (a handoff, a Teams reply).
    results = [
        str(e["time"])
        for e in evs
        if e["type"] in ("input.handed_off", "channel.reply_seen")
        and str((e.get("actor") or {}).get("session_id")) == session
    ]
    # A turn runs from a board read to the next recorded end (the routine session's Stop hook);
    # several reads before one end are one turn. An idle turn forwarded nothing.
    turns: list[tuple[str, str]] = []
    for start in runs:
        end = next((t for t in ends if t >= start), None)
        if end is not None and not (turns and turns[-1][1] == end):
            turns.append((start, end))
    idle = [
        end
        for start, end in turns
        if not any(start <= t <= end for t in results)
        and not any(start <= t <= end for t in forwarded)
    ]
    return check(
        "W",
        "例行会话分离后定时运行结束不再唤醒主会话，有转发时仍收到消息（主会话的 hook 记录）",
        bool(detached) and not after and bool(forwarded) and bool(idle),
        {
            "generation": generation,
            "detached": detached and {"seq": detached["sequence"], "time": since},
            "notices_before_detach": len(notices) - len(after),
            "notices_after_detach": after,
            "forwards_after_detach": forwarded,
            "turns_after_detach": len(turns),
            "idle_turns_ended": idle,
        },
    )


def routine_started(run: Run, evs: list[dict[str, Any]]) -> dict[str, Any]:
    requested = first(evs, "routine.start_requested")
    bound = first(evs, "routine.bound")
    armed = first(evs, "routine.armed")
    detached = first(evs, "routine.detached")
    # Every generation asked for gets an entry, so cleanup accounts for each of them.
    names = {
        int(e["data"]["generation"]): str(e["data"]["session_name"])
        for e in evs
        if e["type"] == "routine.start_requested"
    }
    for name in names.values():
        run.record("claude-session", name)
    for e in evs:
        if e["type"] == "routine.bound" and int(e["data"]["generation"]) in names:
            run.record(
                "claude-session",
                names[int(e["data"]["generation"])],
                state="created",
                ref=str(e["data"]["session_ref"]),
            )
    problems = [
        name
        for name, found in (
            ("start_requested", requested),
            ("bound", bound),
            ("detached", detached),
            ("armed", armed),
        )
        if found is None
    ]
    return check(
        "R0",
        "例行会话启动、绑定、从主会话名下分离并设定定时任务",
        not problems,
        {
            "start_requested": requested and requested["sequence"],
            "bound": bound and bound["sequence"],
            "detached": detached and detached["sequence"],
            "armed": armed and {"seq": armed["sequence"], "cron": armed["data"].get("cron")},
            "missing": problems,
        },
    )


def _after(evs: list[dict[str, Any]], kind: str, task: str, seq: int) -> dict[str, Any] | None:
    return next(
        (e for e in evs if e["type"] == kind and e["task_id"] == task and e["sequence"] > seq),
        None,
    )


def routine_handoff(evs: list[dict[str, Any]]) -> dict[str, Any]:
    asked = first(evs, "input.escalated", "T001", kind="in_session")
    if asked is None:
        return check("B2", "超时转交与主会话代答（T001）", False, "没有 in_session 的提问")
    request = asked["data"]["request_id"]
    handed = first(evs, "input.handed_off", "T001", request_id=request)
    decision = first(evs, "orchestrator.decision", "T001", request_id=request)
    resolved = first(evs, "input.resolved", "T001", request_id=request)
    problems: list[str] = []
    if handed is None:
        problems.append("没有 input.handed_off")
    elif handed["data"].get("by") != "system" or handed["actor"].get("role") != "routine":
        problems.append(f"不是例行会话按系统规则转交：{handed['data'].get('by')}")
    if decision is None or resolved is None:
        problems.append("主会话没有代答")
    elif resolved["data"].get("source_kind") != "orchestrator":
        problems.append(f"答复来源不是主会话：{resolved['data'].get('source_kind')}")
    note = resolved and _after(evs, "worker.progress", "T001", resolved["sequence"])
    if resolved is not None and note is None:
        problems.append("worker 没有在答复之后记录进展")
    return check(
        "B2",
        "超时转交（例行会话，by: system）与主会话代答（T001）",
        not problems,
        {
            "escalated": asked["sequence"],
            "handed_off": handed and {"seq": handed["sequence"], "by": handed["data"].get("by")},
            "decision": decision and decision["sequence"],
            "resolved": resolved and resolved["sequence"],
            "worker_progress": note and note["sequence"],
            "problems": problems,
        },
    )


def teams_round_trip(evs: list[dict[str, Any]]) -> dict[str, Any]:
    asked = first(evs, "input.escalated", "T002", kind="in_session")
    if asked is None:
        return check("T", "Teams 转达与登记（T002）", False, "没有 in_session 的提问")
    request = asked["data"]["request_id"]
    relayed = first(evs, "input.relayed", "T002", request_id=request, channel="teams-chat")
    sent = first(evs, "channel.message_sent", "T002", request_id=request)
    added = first(evs, "routine.job_added", job_id="teams-replies")
    seen = [e for e in evs if e["type"] == "channel.reply_seen"]
    resolved = first(evs, "input.resolved", "T002", request_id=request)
    problems: list[str] = []
    for name, found in (("relayed", relayed), ("sent", sent), ("job_added", added)):
        if found is None:
            problems.append(f"没有 {name}")
    # The run configures teams-chat only (T256): nothing goes to the orchestrator's session.
    elsewhere = [
        e["data"].get("channel")
        for e in evs
        if e["type"] == "input.relayed"
        and e["task_id"] == "T002"
        and e["data"].get("channel") != "teams-chat"
    ]
    if elsewhere:
        problems.append(f"转达到了未配置的渠道：{elsewhere}")
    if not seen:
        problems.append("例行表没有取出 Teams 回复")
    if any(not str(e["data"].get("text", "")).upper().startswith("T002") for e in seen):
        problems.append("取出了不以 T002 开头的消息")
    source = (resolved or {}).get("data", {}).get("source") or {}
    if resolved is None:
        problems.append("问题没有得到答复")
    elif (
        source.get("channel") != "teams-chat"
        or resolved["data"].get("source_kind", "user") != "user"
    ):
        problems.append(f"答复来源不是 Teams 中的用户：{source.get('channel')}")
    note = resolved and _after(evs, "worker.progress", "T002", resolved["sequence"])
    if resolved is not None and note is None:
        problems.append("worker 没有在答复之后记录进展")
    return check(
        "T",
        "经 Teams 转达、例行表取出回复、主会话登记（T002）",
        not problems,
        {
            "relayed": relayed and relayed["sequence"],
            "card_sent": sent and sent["sequence"],
            "job_added": added and added["sequence"],
            "replies_seen": [{"seq": e["sequence"], "text": e["data"].get("text")} for e in seen],
            "resolved": resolved
            and {"seq": resolved["sequence"], "channel": source.get("channel")},
            "worker_progress": note and note["sequence"],
            "problems": problems,
        },
    )


def worktrees(
    run: Run, evs: list[dict[str, Any]], submitted: Mapping[str, dict[str, Any] | None]
) -> dict[str, Any]:
    """Check W (CC006 §8): a delivering task worked in the worktree tsw made inside its
    execution directory (attempt_start recorded it, and the submitted head is that worktree's
    HEAD), and its report carries the marks that are only in the repository's AGENTS.md, in
    the repository's probe skill and in the data directory's probe skill (``probe_marks``)."""
    marks = probe_marks(run)
    details: dict[str, Any] = {}
    ok = bool(submitted)
    for task_id, sub in submitted.items():
        starts = [
            e for e in evs if e["type"] == "attempt.start_requested" and e["task_id"] == task_id
        ]
        made = [e["data"].get("worktree") or {} for e in starts]
        attempt_id = str(starts[-1]["data"].get("attempt")) if starts else "a1"
        expected = run.home / "work" / task_id / attempt_id / "live-repo"
        recorded = bool(made) and all(
            m.get("path") and Path(str(m["path"])).resolve() == expected.resolve() for m in made
        )
        submission = (
            ((sub or {}).get("data") or {}).get("submission") or (sub or {}).get("data") or {}
        )
        head = str(submission.get("head") or "")
        worktree_head = git_out(expected, "rev-parse", "HEAD") if expected.is_dir() else ""
        report = ""
        if sub is not None:
            path = run.home / str((submission.get("report") or {}).get("md_path") or "")
            report = path.read_text(encoding="utf-8") if path.is_file() else ""
        found = {name: mark in report for name, mark in marks.items()}
        details[task_id] = {
            "worktree": made,
            "submitted_head_is_worktree_head": same_commit(head, worktree_head),
            "marks_in_report": found,
        }
        ok = ok and recorded and same_commit(head, worktree_head) and all(found.values())
    return check(
        "W",
        "仓库 worktree：在 tsw 创建的 worktree 中工作；报告带有只在仓库 AGENTS.md、仓库 skill 与"
        "数据目录 skill 中出现的标记",
        ok,
        details,
    )


def observed(args: argparse.Namespace) -> dict[str, Any]:
    """What only the operator sees in the app (decision 0003): whether creating a worker session
    asked to trust its execution directory, and whether a worker's tsw tool, message to the
    main session or merge was held for a permission prompt or refused by the classifier."""
    trust, blocked = getattr(args, "trust_prompted", None), getattr(args, "blocked", None)
    return check(
        "O",
        "操作者观察：worker 会话创建时没有信任确认；worker 的 tsw 工具、发消息与合并没有被拦",
        trust == "no" and blocked == "no",
        {
            "trust_prompted": trust or "未记录",
            "blocked": blocked or "未记录",
            "evidence": "操作者在 app 中的观察（verify 的 --trust-prompted / --blocked）",
        },
    )


def permission_rules(run: Run, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """The permission rules of the run's sessions (CC008 §6): the main session's are generated
    in the data directory; execution directories carry none (decision 0003)."""

    def allow(path: Path) -> list[str]:
        data = read_json(path)
        return [str(r) for r in (data.get("permissions") or {}).get("allow") or []]

    main = allow(run.home / ".claude" / "settings.json")
    # An execution directory has no settings file at all (decision 0003).
    workers = {
        str(t["task_id"]): sorted(
            p.name for p in (run.home / "work" / str(t["task_id"]) / "a1" / ".claude").glob("*")
        )
        for t in tasks
    }
    ok = (
        "mcp__ccd_session__start_session" in main
        and "mcp__plugin_task-steward_tsw" in main
        and not any(workers.values())
    )
    return check(
        "P",
        "权限规则：主会话放行会话管理与 tsw 工具；worker 的执行目录没有任何设置文件",
        ok,
        {"main": main, **workers},
    )


def hook_summary(run: Run) -> str:
    """What the recorded hook payloads show (the prompts' leading tags, never their text)."""
    path = run.dir / "hook-payloads.jsonl"
    if not path.is_file():
        return "没有记录到 hook payload（capture_hook 未运行）"
    counts: dict[str, int] = {}
    leading: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        event = str(record.get("event"))
        counts[event] = counts.get(event, 0) + 1
        payload = record.get("payload")
        prompt = str(payload.get("prompt") or "") if isinstance(payload, dict) else ""
        if event == "UserPromptSubmit":
            text = prompt.lstrip()
            # The tag name only: attributes carry session titles.
            tag = "<" + re.split(r"[\s>]", text[1:], maxsplit=1)[0] if text.startswith("<") else ""
            tag = tag or "（用户文字）"
            leading[tag] = leading.get(tag, 0) + 1
    return f"hook payload：{counts}；UserPromptSubmit 的开头：{leading}"


def reconcile_sessions(run: Run, evs: list[dict[str, Any]]) -> None:
    """One manifest entry per worker session name (``t001-a1-g1``), as the orchestrator asked
    for it; the session it became, or ``not_created`` when the ledger says it was never made.
    An entry that stays ``maybe_created`` blocks cleanup until it is resolved."""
    names: dict[tuple[str, str, int], str] = {}
    for e in evs:
        data = e["data"]
        key = (str(e["task_id"]), str(data.get("attempt")), int(data.get("generation") or 0))
        if e["type"] == "attempt.start_requested" and data.get("session_name"):
            names[key] = str(data["session_name"])
            run.record("claude-session", names[key], task=e["task_id"])
        elif key in names and e["type"] == "attempt.not_created":
            run.record("claude-session", names[key], state="not_created")
        elif (
            key in names
            and e["type"] in ("attempt.bound", "attempt.prestarted", "attempt.late_session")
            and data.get("session_ref")
        ):
            run.record("claude-session", names[key], state="created", ref=str(data["session_ref"]))


def unresolved(run: Run) -> list[dict[str, Any]]:
    """Sessions and threads not confirmed archived (or confirmed never created)."""
    return [
        e
        for e in run.manifest()["entries"]
        if e["kind"] in ("claude-session", "codex-thread")
        and e["state"] not in ("archived", "not_created")
    ]


def follow_ups(evs: list[dict[str, Any]]) -> dict[str, Any]:
    """Check F (DN002 §5.7): T002's worker records a follow-up from its own session before it
    submits, and the orchestrator turns it into a new task that records where it came from."""
    title = "follow-up：T002 的 worker 提交前记录，主会话按用户要求转为新任务"
    recorded = [e for e in evs if e["type"] == "follow_up.recorded" and e["task_id"] == "T002"]
    by_worker = [e for e in recorded if e["actor"].get("role") == "worker"]
    submitted = last(evs, "worker.submitted", "T002")
    problems: list[str] = []
    evidence: dict[str, Any] = {"recorded": [e["sequence"] for e in recorded]}
    # Only what the session that made the last submission recorded before it counts: an earlier
    # attempt's records neither pass nor fail the check.
    before = [
        e
        for e in by_worker
        if submitted is not None
        and e["sequence"] < submitted["sequence"]
        and e["actor"].get("session_id") == submitted["actor"].get("session_id")
    ]
    if not by_worker:
        problems.append("T002 的 worker 没有记录 follow-up")
    elif submitted is None:
        problems.append("T002 没有提交")
    elif not before:
        problems.append("提交 T002 的 worker 会话没有在最后一次提交之前记录 follow-up")
    ids = [str(i["follow_up_id"]) for e in before for i in e["data"].get("items") or []]
    converted = next(
        (
            e
            for e in evs
            if e["type"] == "follow_up.converted"
            and e["task_id"] == "T002"
            and set(e["data"].get("follow_up_ids") or []) & set(ids)
        ),
        None,
    )
    if converted is None:
        problems.append("主会话没有把 worker 记录的 follow-up 转为任务")
    else:
        evidence["converted"] = converted["sequence"]
        target = converted["data"].get("to_task")
        made = first(evs, "task.registered", str(target))
        of = (made or {}).get("data", {}).get("follow_up_of") or []
        if converted["actor"].get("role") != "orchestrator":
            problems.append("转为任务的不是主会话")
        if made is None or not any(f.get("task_id") == "T002" for f in of):
            problems.append(f"新任务 {target} 没有记录 follow_up_of")
        else:
            evidence["task"] = target
    return check("F", title, not problems, problems or evidence)


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
    return check("B2", "超时转交与主会话处理（T002）", not problems, evidence, optional=True)


def scheduled_prompts(run: Run, evs: list[dict[str, Any]]) -> dict[str, Any]:
    """A worker's scheduled wake-up reaches the hook like a user's message; it must carry the
    ``<scheduled-task>`` tag and never be recorded as the user's words."""
    path = run.dir / "hook-payloads.jsonl"
    seen = 0
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line).get("payload")
            prompt = str(payload.get("prompt") or "") if isinstance(payload, dict) else ""
            seen += prompt.lstrip().startswith("<scheduled-task")
    recorded = [
        e["sequence"]
        for e in evs
        if e["type"] == "input.session_message"
        and "scheduled-task" in str(e["data"].get("text", ""))[:200]
    ]
    return check(
        "S",
        "定时唤醒提示带标记，且没有被记为用户消息",
        seen > 0 and not recorded,
        {"scheduled_prompts_seen": seen, "recorded_as_user": recorded},
    )


def same_commit(a: Any, b: Any) -> bool:
    """Two commit IDs name the same commit (a worker may record an abbreviated one)."""
    x, y = str(a or "").lower(), str(b or "").lower()
    return len(min(x, y, key=len)) >= 7 and (x.startswith(y) or y.startswith(x))


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
        submission = sub["data"].get("submission") or sub["data"]
        merge = submission.get("merge") or {}
        head = submission.get("head")
        if not merge:
            problems.append(f"{t} 提交没有 merge 记录")
            continue
        number = int(str(merge.get("pr", "0")).rstrip("/").rsplit("/", 1)[-1] or 0)
        pr = pulls.get(number)
        evidence[t] = {"pr": number, "merge_commit": merge.get("merge_commit"), "head": head}
        if not same_commit(merge.get("merged_head"), head):
            problems.append(f"{t} merged_head 不等于提交 HEAD")
        if pr is None or pr["state"] != "MERGED":
            problems.append(f"{t} 替身中 PR #{number} 未合并")
        elif not same_commit(pr["merge_commit"], merge.get("merge_commit")) or not same_commit(
            pr["merged_head"], head
        ):
            problems.append(f"{t} 替身记录的合并与提交不一致")
        if not any(same_commit(c, merge.get("merge_commit")) for c in main_log):
            problems.append(f"{t} 合并提交不在 origin 的 main 上")
        ci = merge.get("ci") or {}
        runs = {str(r["url"]): r for r in state.get("runs") or []}
        evidence[t]["ci"] = ci.get("url")
        evidence[t]["live"] = merge.get("live")
        recorded = runs.get(str(ci.get("url")))
        if recorded is None or not same_commit(recorded["headSha"], merge.get("merge_commit")):
            problems.append(f"{t} 提交所附的 CI 不是替身为合并提交记录的那次运行")
        if not str(merge.get("live") or "").strip():
            problems.append(f"{t} 提交没有 live 说明")
    return check(
        "C",
        "worker 自行合并、附合并提交的 CI 与 live 说明并通过 G-SUBMIT（GitHub 与 CI 为模拟）",
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
    left = unresolved(run)
    for e in left:
        ref = e.get("ref") or "会话 ID 未知，先运行 verify 或按名称对账"
        print(f"未清理：{e['kind']} {e['name']} {ref}（{e['state']}）")
    done = not left and threads["status"] == "passed"
    print("清理完成" if done else f"清理未完成：剩余 {len(left)} 项会话或线程")
    return 0 if done else 1


def discard(args: argparse.Namespace) -> int:
    """Delete a finished run's directory: only one with a manifest, only after its cleanup."""
    run = Run.open(args.run_id)
    left = unresolved(run)
    if left:
        names = ", ".join(f"{e['name']}={e.get('ref')}" for e in left)
        raise SystemExit(f"live: 还有未确认归档的对象（{names}），先完成 cleanup")

    def writable(func: Any, path: str, _exc: Any) -> None:
        Path(path).chmod(0o700)  # git marks its objects read-only on Windows
        func(path)

    shutil.rmtree(run.dir, onexc=writable) if sys.version_info >= (3, 12) else shutil.rmtree(
        run.dir, onerror=writable
    )
    print(f"已删除 {run.id}")
    return 0


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
    prep = steps.add_parser("prepare")
    prep.add_argument("--tasks", help="只用场景中的部分任务（逗号分隔，例如 T001,T002）")
    prep.set_defaults(handler=host_prepare)
    ver = steps.add_parser("verify")
    ver.add_argument("run_id")
    ver.add_argument(
        "--trust-prompted",
        choices=("yes", "no"),
        help="操作者观察：创建 worker 会话时 app 是否要求信任执行目录",
    )
    ver.add_argument(
        "--blocked",
        choices=("yes", "no"),
        help="操作者观察：worker 的 tsw 工具、发给主会话的消息或合并是否出现权限确认或分类器拒绝",
    )
    ver.set_defaults(handler=host_verify)
    routine = sub.add_parser("routine-e2e", help="例行会话与 Teams 渠道（需要用户在场）")
    routine_steps = routine.add_subparsers(dest="step", required=True)
    routine_steps.add_parser("prepare").set_defaults(handler=routine_prepare)
    routine_ver = routine_steps.add_parser("verify")
    routine_ver.add_argument("run_id")
    for flag, text in (
        ("--trust-prompted", "创建例行会话时 app 是否要求信任 routine/"),
        ("--blocked", "例行会话的定时任务或发消息是否出现权限确认或分类器拒绝"),
        # Kept for old command lines; check W reads the wakes from the hook payloads (T250).
        ("--woken-by-ticks", "例行会话每次定时运行结束时主会话是否被唤醒（只作记录）"),
        (
            "--card-multiline",
            "Teams 中的问题卡片是否分行、分段显示（标题、任务、问题、回复方式各自成行）",
        ),
    ):
        routine_ver.add_argument(flag, choices=("yes", "no"), help=f"操作者观察：{text}")
    routine_ver.set_defaults(handler=routine_verify)
    handoff = sub.add_parser("handoff-e2e", help="主会话交接与唤醒（需要用户在场）")
    handoff_steps = handoff.add_subparsers(dest="step", required=True)
    handoff_steps.add_parser("prepare").set_defaults(handler=handoff_prepare)
    handoff_ver = handoff_steps.add_parser("verify")
    handoff_ver.add_argument("run_id")
    handoff_ver.add_argument(
        "--blocked",
        choices=("yes", "no"),
        help="操作者观察：worker 或新主会话发消息时是否出现权限确认或分类器拒绝",
    )
    handoff_ver.set_defaults(handler=handoff_verify)
    changed = sub.add_parser(
        "change-e2e", help="用户在 worker 会话中追加要求，worker 登记并重新交付（需要用户在场）"
    )
    changed_steps = changed.add_subparsers(dest="step", required=True)
    changed_steps.add_parser("prepare").set_defaults(handler=change_prepare)
    changed_ver = changed_steps.add_parser("verify")
    changed_ver.add_argument("run_id")
    changed_ver.add_argument(
        "--blocked",
        choices=("yes", "no"),
        help="操作者观察：worker 或主会话发消息、worker 合并时是否出现权限确认或分类器拒绝",
    )
    changed_ver.set_defaults(handler=change_verify)
    titled = sub.add_parser(
        "title-e2e",
        help="worker 会话的标题：派发、终端验收后加前缀、用户改过名不动（需要用户在场）",
    )
    titled_steps = titled.add_subparsers(dest="step", required=True)
    titled_steps.add_parser("prepare").set_defaults(handler=title_prepare)
    titled_ver = titled_steps.add_parser("verify")
    titled_ver.add_argument("run_id")
    titled_ver.add_argument(
        "--observed",
        choices=("yes", "no"),
        help="操作者观察：T001 改为“[已验收] …”、T002 保持用户的标题、没有出现改名确认",
    )
    titled_ver.set_defaults(handler=title_verify)
    clean = sub.add_parser("cleanup", help="记录已归档的会话并报告剩余对象")
    clean.add_argument("run_id")
    clean.add_argument("--archived", action="append", help="已归档并用 get_session 读回的会话 ID")
    clean.set_defaults(handler=cleanup)
    drop = sub.add_parser("discard", help="删除已完成清理的运行目录")
    drop.add_argument("run_id")
    drop.set_defaults(handler=discard)
    sub.add_parser("list").set_defaults(handler=list_runs)
    args = parser.parse_args(argv)
    if args.runs_dir:
        os.environ["TSW_LIVE_RUNS"] = str(Path(args.runs_dir).expanduser().resolve())
    handler: Any = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
