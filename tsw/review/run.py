"""One self-review round for the calling worker (DN004 §2–§4): ``tsw review run`` / ``review_run``.

The round is recorded before the reviewer is called (``review_start``), the request and the
result are written by tsw into ``runs/<task>/<attempt>/review/`` (workers never write ``runs/``),
and the round always ends in the ledger: ``review_complete`` with a result that satisfies the
output contract, or ``review_fail`` (E02 retry, or ``review.unavailable`` once retries are used
up). Triage and the iterate / finalize / escalate decision stay with the worker.

Only one round per attempt runs at a time (a file lock in ``locks/``). Everything that can be
checked before the round is opened is checked first; anything that goes wrong after it is
opened is recorded as a failed round.
"""

# Ledger rows and reviewer JSON are walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsw import lease
from tsw.config import SEVERITIES, EffectiveConfig, load_config, resolve_path
from tsw.errors import ErrorCode, TswError, rejected
from tsw.home import Home
from tsw.host.identity import Identity, resolve
from tsw.ledger import Ledger
from tsw.locks import FileLock
from tsw.machine.base import Caller
from tsw.machine.model import loads, row_dict
from tsw.machine.runner import run_tool
from tsw.machine.worker import CATEGORIES, VERDICTS, validate_result
from tsw.review.codex import ReviewRun, extract_json, resolve_executable, run_review
from tsw.storage.fs import FsStorage
from tsw.workspace import plan

Reviewer = Callable[..., ReviewRun]
# Session adapters that can review (DN006 §7.4) and are implemented in this version.
REVIEWERS: dict[str, Reviewer] = {"codex-app-server": run_review}
# Where a task's work usually starts, when the worker does not name a base.
BASE_CANDIDATES = ("origin/HEAD", "origin/main", "main", "origin/master", "master")


def ledger_for(home: Home, config: EffectiveConfig, machine: str) -> Ledger:
    storage = FsStorage(home, segment_max_bytes=int(config.get("ledger.segment_max_mb")) << 20)
    return Ledger(
        home, storage, checkpoint_every=int(config.get("ledger.checkpoint_every")), machine=machine
    )


def git(workdir: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def commit_id(workdir: Path, rev: str | None) -> str | None:
    """The full commit ID ``rev`` names in ``workdir``, or None (options are never passed on)."""
    if not rev or rev.startswith("-"):
        return None
    return git(workdir, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}")


@dataclass
class Context:
    home: Home
    config: EffectiveConfig
    ledger: Ledger
    who: Identity
    caller: Caller
    reviewer: Reviewer
    executable: str
    session: str
    model: str


def run_round(
    home: Home,
    env: Mapping[str, str],
    cwd: Path,
    *,
    head: str | None = None,
    base: str | None = None,
    notes: str | None = None,
    machine: str | None = None,
) -> dict[str, Any]:
    config = load_config(home).require_valid()
    machine = machine or lease.this_machine()
    ledger = ledger_for(home, config, machine)
    with ledger.view() as conn:
        who = resolve(conn, home, env, cwd)
    _require_worker(who)
    session = str(config.get("roles.reviewer.session"))
    reviewer = REVIEWERS.get(session)
    if reviewer is None:
        raise TswError(
            ErrorCode.UNSUPPORTED,
            f"本版本尚未实现评审会话适配器 {session}",
            reason="reviewer_not_implemented",
        )
    ctx = Context(
        home=home,
        config=config,
        ledger=ledger,
        who=who,
        caller=Caller(role="worker", session_id=who.session_id, machine=machine),
        reviewer=reviewer,
        executable=resolve_executable(config.get("roles.reviewer.executable")),
        session=session,
        model=str(config.get("roles.reviewer.model")),
    )
    lock = FileLock(home.locks_dir / f"review-{who.task_id}-{who.attempt}.lock", timeout=0)
    try:
        lock.acquire()
    except TswError as exc:
        raise rejected(
            "本 attempt 已有一轮评审在进行，等它结束后再调用", reason="review_in_progress"
        ) from exc
    try:
        return _run_locked(ctx, head=head, base=base, notes=notes)
    finally:
        lock.release()


def _run_locked(
    ctx: Context, *, head: str | None, base: str | None, notes: str | None
) -> dict[str, Any]:
    task_id, attempt_id = str(ctx.who.task_id), str(ctx.who.attempt)
    with ctx.ledger.view() as conn:  # read again under the lock
        row = row_dict(conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
        attempt = row_dict(
            conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? AND attempt = ?", (task_id, attempt_id)
            ).fetchone()
        )
        previous = _previous_round(conn, task_id, attempt_id)
        retry = None
        if row is not None and row["substate"] == "reviewing":  # E02: the same round again
            retry = row_dict(
                conn.execute(
                    "SELECT round, head FROM review_rounds WHERE task_id = ? AND attempt = ? "
                    "AND verdict IS NULL ORDER BY round DESC LIMIT 1",
                    (task_id, attempt_id),
                ).fetchone()
            )
    assert row is not None and attempt is not None
    workdir = Path(str(attempt["workdir"] or ctx.home.root / "work" / task_id / attempt_id))
    # A repository task is reviewed in the worktree tsw made inside its execution directory
    # (CC006 §8), where its commits are.
    ws = plan(ctx.home, ctx.config, row, attempt_id, str(attempt["workdir"] or workdir))
    if ws is not None and ws.worktree is not None and ws.worktree.is_dir():
        workdir = ws.worktree
    is_git = git(workdir, "rev-parse", "--is-inside-work-tree") == "true"
    if retry is not None:
        round_no = int(retry["round"])
        recorded_base = _started_base(ctx.ledger, task_id, attempt_id, round_no)
        for given, kept, field in (
            (head, retry["head"], "head"),
            (base, recorded_base, "base"),
        ):
            if given and (commit_id(workdir, given) if is_git else given) != kept:
                raise rejected(
                    "评审调用失败后的重试必须评审同一个范围",
                    reason="retry_scope_mismatch",
                    field=field,
                    given=given,
                    recorded=kept,
                )
        head, base = str(retry["head"]), recorded_base
        # The round may have been opened with review_start directly: the recorded scope gets
        # the same check as a new one, and only verified commit IDs ever reach git.
        if is_git and (
            commit_id(workdir, head) != head or (base and commit_id(workdir, base) != base)
        ):
            return _fail(
                ctx,
                round_no,
                f"本轮落账的评审范围不是执行目录中的完整提交（head={head}，base={base}），"
                "不调用评审器",
            )
    else:
        head, base = _scope(ctx, row, workdir, is_git, head=head, base=base)
        started = run_tool(
            ctx.ledger,
            ctx.config,
            ctx.caller,
            "review_start",
            {
                "task_id": task_id,
                "head": head,
                **({"base": base} if base else {}),
                "reviewer": {"session": ctx.session, "model": ctx.model},
            },
        )
        round_no = int(started["events"][0]["data"]["round"])
    # The round is open: from here on every failure is recorded as a failed round.
    try:
        return _review(
            ctx,
            row,
            workdir=workdir,
            is_git=is_git,
            round_no=round_no,
            head=head,
            base=base,
            notes=notes,
            previous=previous if previous and previous["cycle"] == row["cycle"] else None,
            superseded=previous is not None and previous["cycle"] != row["cycle"],
            retry=retry is not None,
        )
    except TswError as exc:
        if exc.code is ErrorCode.QUIESCED:
            raise  # the data directory refuses writes; the round is retried once it resumes
        return _fail(ctx, round_no, f"{exc.message} {json.dumps(exc.details, ensure_ascii=False)}")
    except Exception as exc:  # anything else after the round was opened ends it as failed
        return _fail(ctx, round_no, f"{type(exc).__name__}: {exc}")


def _scope(
    ctx: Context,
    row: Mapping[str, Any],
    workdir: Path,
    is_git: bool,
    *,
    head: str | None,
    base: str | None,
) -> tuple[str, str | None]:
    """The head and base of a new round, checked before the round is opened."""
    if not is_git:
        if not head:
            raise rejected(
                "执行目录不是 git 仓库：用 head 指定评审的成果标识", reason="head_missing"
            )
        return head, None
    full_head = commit_id(workdir, head or "HEAD")
    if full_head is None:
        raise rejected("head 不是执行目录中的提交", reason="invalid_head", head=head)
    if base:
        full_base = commit_id(workdir, base)
        if full_base is None:
            raise rejected("base 不是执行目录中的提交", reason="invalid_base", base=base)
        return full_head, full_base
    configured = ctx.config.get(f"repositories.{row['repo']}.base_ref")
    for candidate in (configured, *BASE_CANDIDATES):
        if not isinstance(candidate, str) or commit_id(workdir, candidate) is None:
            continue
        fork = git(workdir, "merge-base", candidate, full_head)
        if fork and fork != full_head:
            return full_head, fork
    raise rejected(
        "无法确定评审范围的起点：用 base 指定任务开始前的提交",
        reason="base_missing",
        head=full_head,
    )


def _started_base(ledger: Ledger, task_id: str, attempt_id: str, round_no: int) -> str | None:
    base: str | None = None
    for event in ledger.storage.read(since=0):
        if (
            event.type == "review.started"
            and event.task_id == task_id
            and event.data.get("attempt") == attempt_id
            and event.data.get("round") == round_no
        ):
            base = event.data.get("base")
    return base


def _review(
    ctx: Context,
    row: Mapping[str, Any],
    *,
    workdir: Path,
    is_git: bool,
    round_no: int,
    head: str,
    base: str | None,
    notes: str | None,
    previous: Mapping[str, Any] | None,
    superseded: bool,
    retry: bool,
) -> dict[str, Any]:
    task_id, attempt_id = str(ctx.who.task_id), str(ctx.who.attempt)
    review_dir = ctx.home.root / "runs" / task_id / attempt_id / "review"
    rel = f"runs/{task_id}/{attempt_id}/review"
    resume = None
    if previous is not None and ctx.config.get("review.incremental") == "resume_thread":
        resume = _thread_of(review_dir, int(previous["round"]))
    prompt = render_request(
        ctx.home,
        ctx.config,
        row,
        workdir=workdir,
        is_git=is_git,
        round_no=round_no,
        head=head,
        base=base,
        notes=notes,
        previous=previous if resume else None,
        superseded=superseded,
    )
    _write(ctx.home, review_dir / f"r{round_no}-request.md", prompt)
    run, error, result = _call(ctx, prompt, workdir=workdir, resume=resume)
    if result is not None:
        error = _check_head(result, head, workdir, is_git)
        if error is not None:
            result = None
    meta = {
        "round": round_no,
        "head": head,
        "base": base,
        "session": ctx.session,
        "model": ctx.model,
        "effort": ctx.config.get("roles.reviewer.effort"),
        "thread_id": run.thread_id if run else None,
        "resumed": resume,
        "retry": retry,
        "status": run.status if run else None,
        "error": error,
    }
    _write(ctx.home, review_dir / f"r{round_no}-run.json", _dumps(meta))
    if result is None:
        if run is not None and run.final_message:
            _write(ctx.home, review_dir / f"r{round_no}-raw.txt", run.final_message)
        return _fail(ctx, round_no, str(error))
    _write(ctx.home, review_dir / f"r{round_no}.json", _dumps(result))
    completed = run_tool(
        ctx.ledger,
        ctx.config,
        ctx.caller,
        "review_complete",
        {
            "task_id": task_id,
            "round": round_no,
            "result": result,
            "result_path": f"{rel}/r{round_no}.json",
        },
    )
    data = completed["events"][0]["data"]
    return {
        "round": round_no,
        "verdict": data["verdict"],
        "reviewed_head": data["reviewed_head"],
        "findings": result["findings"],
        "blocking": [f["id"] for f in data["findings"] if f["blocking"]],
        "result_path": data["result_path"],
        "thread_id": meta["thread_id"],
    }


def _fail(ctx: Context, round_no: int, error: str) -> dict[str, Any]:
    failed = run_tool(
        ctx.ledger,
        ctx.config,
        ctx.caller,
        "review_fail",
        {"task_id": ctx.who.task_id, "round": round_no, "error": error},
    )
    return {
        "ok": False,
        "round": round_no,
        "outcome": failed["events"][0]["type"],
        "error": error,
        "request_path": f"runs/{ctx.who.task_id}/{ctx.who.attempt}/review/r{round_no}-request.md",
    }


def _require_worker(who: Identity) -> None:
    if who.role != "worker":
        raise rejected(
            "只有绑定到任务的 worker 会话能运行自评审", reason="role_denied", role=who.role
        )
    if who.phase != "live":
        raise rejected("attempt 不在运行状态", reason="attempt_not_live", phase=who.phase)
    if not who.cwd_consistent:
        raise rejected("会话目录与 attempt 的执行目录不一致", reason="cwd_mismatch")


def _previous_round(conn: Any, task_id: str, attempt: str) -> dict[str, Any] | None:
    """The last completed round of this attempt, with the worker's triage of its findings."""
    row = row_dict(
        conn.execute(
            "SELECT * FROM review_rounds WHERE task_id = ? AND attempt = ? AND verdict IS NOT NULL "
            "ORDER BY round DESC LIMIT 1",
            (task_id, attempt),
        ).fetchone()
    )
    if row is None:
        return None
    row["findings"] = [
        row_dict(r)
        for r in conn.execute(
            "SELECT finding_id, severity, blocking, action, reason, verification, disposition "
            "FROM findings WHERE task_id = ? AND attempt = ? AND round = ? ORDER BY finding_id",
            (task_id, attempt, row["round"]),
        )
    ]
    return row


def _thread_of(review_dir: Path, round_no: int) -> str | None:
    try:
        meta = json.loads((review_dir / f"r{round_no}-run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    thread = meta.get("thread_id") if isinstance(meta, dict) else None
    return thread if isinstance(thread, str) and thread else None


def _call(
    ctx: Context, prompt: str, *, workdir: Path, resume: str | None
) -> tuple[ReviewRun | None, str | None, dict[str, Any] | None]:
    """Run the reviewer; returns (run, error, result) with exactly one of error / result set."""
    try:
        run = ctx.reviewer(
            prompt,
            cwd=workdir,
            model=ctx.model,
            effort=str(ctx.config.get("roles.reviewer.effort")),
            timeout_seconds=int(ctx.config.get("roles.reviewer.timeout_seconds")),
            executable=ctx.executable,
            resume_thread=resume,
            context_window=ctx.config.get("roles.reviewer.context_window"),
        )
    except TswError as exc:
        return None, f"{exc.message} {json.dumps(exc.details, ensure_ascii=False)}", None
    except OSError as exc:  # the reviewer process could not be started or talked to
        return None, f"{type(exc).__name__}: {exc}", None
    if run.status != "completed":
        return run, f"评审线程未正常完成（{run.status}）{run.error or ''}", None
    result = extract_json(run.final_message)
    if result is None:
        return run, "评审输出不是 JSON 对象", None
    try:
        validate_result(result)
    except TswError as exc:
        detail = json.dumps(exc.details, ensure_ascii=False)
        return run, f"评审输出不满足契约：{exc.message} {detail}", None
    return run, None, result


def _check_head(result: dict[str, Any], head: str, workdir: Path, is_git: bool) -> str | None:
    """The reviewer must name the head it was given; a Git abbreviation must resolve to it."""
    reported = str(result.get("reviewed_head") or "")
    if reported == head:
        return None
    if is_git and commit_id(workdir, reported) == head:
        result["reviewed_head"] = head
        return None
    return f"评审结果的 reviewed_head（{reported}）与本轮 HEAD（{head}）不一致"


def render_request(
    home: Home,
    config: EffectiveConfig,
    task: Mapping[str, Any],
    *,
    workdir: Path,
    is_git: bool,
    round_no: int,
    head: str,
    base: str | None,
    notes: str | None,
    previous: Mapping[str, Any] | None,
    superseded: bool = False,
) -> str:
    """The review package (DN004 §2 step 2) as one prompt."""
    model = config.get("roles.reviewer.model")
    effort = config.get("roles.reviewer.effort")
    kind = str(task["kind"])
    lines: list[str] = []
    if previous is not None:
        lines.append(
            f"第 {round_no} 轮增量评审（续用上一轮线程）。不要修改任何文件，不要运行会写入的命令。"
            f"上一轮（第 {previous['round']} 轮）评审的 HEAD 是 {previous['reviewed_head']}，"
            f"现在的 HEAD 是 {head}。"
        )
    else:
        lines.append(
            f"第 {round_no} 轮评审（新线程）。你是只读评审者（{model}，{effort}）。"
            f"当前目录是任务 {task['task_id']} 的执行目录。"
            "不要修改任何文件，不要运行会写入的命令。"
        )
    if superseded:
        lines.append(
            f"需求已变更为 revision {task['revision']}：此前各轮评审针对旧需求，"
            "本轮按当前需求完整评审。"
        )
    lines += [
        "",
        f"## 任务（revision {task['revision']}，以此为准）",
        "",
        f"- 标题：{task['title']}",
        f"- 类型：{kind}",
        f"- 目标：{task['goal']}",
        "- 验收要求：",
    ]
    for i, item in enumerate(loads(task["acceptance"], []), 1):
        lines.append(f"  {i}. {item}")
    lines += ["", "## 评审范围", ""]
    if previous is not None:
        lines += [
            "worker 对上一轮意见的处置：",
            *(_disposition(f) for f in previous["findings"]),
            "",
            (
                f"请核对 `git diff {previous['reviewed_head']}..{head}`"
                if is_git
                else "请核对报告草稿与证据的变化"
            )
            + "：上述处置是否真正解决了对应问题、拒绝的理由是否成立，"
            "以及这次改动有没有引入新的问题。新意见的 id 接着上一轮编号。",
        ]
    elif is_git and base:
        lines.append(f"本任务的改动：`git diff {base}..{head}`。")
        stat = git(workdir, "diff", "--stat", "--end-of-options", base, head, "--")
        if stat:
            lines += ["", "```", stat, "```"]
    else:
        lines.append(f"报告草稿与证据：`{workdir}`。成果标识为 {head}。")
    lines += [
        "",
        f"worker 的报告草稿位于 `{home.root / 'work' / str(task['task_id'])}` 下本 attempt 的"
        "目录（如有）。执行目录中的 AGENTS.md、CLAUDE.md 等仓库约定请自行只读查阅。",
    ]
    template = config.get(f"review.prompt_templates.{kind}")
    if isinstance(template, str):
        text = resolve_path(home, template).read_text(encoding="utf-8").strip()
        lines += ["", "## 评审要点", "", text]
    if notes:
        lines += ["", "## worker 的说明（已执行的验证等）", "", notes.strip()]
    lines += ["", "## 输出", "", OUTPUT_CONTRACT.format(head=head)]
    return "\n".join(lines) + "\n"


def _disposition(finding: Mapping[str, Any]) -> str:
    text = f"- {finding['finding_id']}（{finding['severity']}）：{finding['action'] or '未处置'}"
    if finding["reason"]:
        text += f"；理由：{finding['reason']}"
    if finding["verification"]:
        text += f"；验证：{finding['verification']}"
    return text


OUTPUT_CONTRACT = (
    "只输出一个 JSON 对象，不要输出其他文字："
    '{{"verdict":"' + "|".join(VERDICTS) + '","summary":"...","reviewed_head":"{head}",'
    '"findings":[{{"id":"F1","severity":"' + "|".join(SEVERITIES) + '",'
    '"category":"' + "|".join(CATEGORIES) + '",'
    '"location":{{"path":"...","line":0}},"claim":"...","evidence":"...","suggestion":"...",'
    '"acceptance_item":"..."}}],"unverified":["..."]}}。'
    "reviewed_head 原样填写 {head}。evidence 必须指向可核实的位置或命令输出，"
    "没有证据的推测只能是 info；无法评审（例如缺材料）时 verdict 为 blocked。"
)


def _write(home: Home, path: Path, text: str) -> None:
    with FsStorage(home).writer():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(path)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
