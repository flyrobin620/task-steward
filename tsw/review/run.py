"""One self-review round for the calling worker (DN004 §2–§4): ``tsw review run`` / ``review_run``.

The round is recorded before the reviewer is called (``review_start``), the request and the
result are written by tsw into ``runs/<task>/<attempt>/review/`` (workers never write ``runs/``),
and the round always ends in the ledger: ``review_complete`` with a result that satisfies the
output contract, or ``review_fail`` (E02 retry, or ``review.unavailable`` once retries are used
up). Triage and the iterate / finalize / escalate decision stay with the worker.
"""

# Ledger rows and reviewer JSON are walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tsw import lease
from tsw.config import SEVERITIES, EffectiveConfig, load_config, resolve_path
from tsw.errors import ErrorCode, TswError, rejected
from tsw.home import Home
from tsw.host.identity import Identity, resolve
from tsw.ledger import Ledger
from tsw.machine.base import Caller
from tsw.machine.model import loads, row_dict
from tsw.machine.runner import run_tool
from tsw.machine.worker import CATEGORIES, VERDICTS, validate_result
from tsw.review.codex import ReviewRun, extract_json, run_review
from tsw.storage.fs import FsStorage

Reviewer = Callable[..., ReviewRun]
# Session adapters that can review (DN006 §7.4) and are implemented in this version.
REVIEWERS: dict[str, Reviewer] = {"codex-app-server": run_review}


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
        row = row_dict(
            conn.execute("SELECT * FROM tasks WHERE task_id = ?", (who.task_id,)).fetchone()
        )
        attempt = row_dict(
            conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? AND attempt = ?",
                (who.task_id, who.attempt),
            ).fetchone()
        )
        previous = _previous_round(conn, str(who.task_id), str(who.attempt))
        retry = None
        if row is not None and row["substate"] == "reviewing":  # E02: the same round again
            retry = row_dict(
                conn.execute(
                    "SELECT round, head FROM review_rounds WHERE task_id = ? AND attempt = ? "
                    "AND verdict IS NULL ORDER BY round DESC LIMIT 1",
                    (who.task_id, who.attempt),
                ).fetchone()
            )
    assert row is not None and attempt is not None
    session = str(config.get("roles.reviewer.session"))
    reviewer = REVIEWERS.get(session)
    if reviewer is None:
        raise TswError(
            ErrorCode.UNSUPPORTED,
            f"本版本尚未实现评审会话适配器 {session}",
            reason="reviewer_not_implemented",
        )
    workdir = Path(
        str(attempt["workdir"] or home.root / "work" / str(who.task_id) / str(who.attempt))
    )
    is_git = git(workdir, "rev-parse", "--is-inside-work-tree") == "true"
    model = str(config.get("roles.reviewer.model"))
    caller = Caller(role="worker", session_id=who.session_id, machine=machine)
    if retry is not None:
        round_no = int(retry["round"])
        if head and head != retry["head"]:
            raise rejected(
                "评审调用失败后的重试必须评审同一个 HEAD",
                reason="retry_head_mismatch",
                head=head,
                round_head=retry["head"],
            )
        head = str(retry["head"])
    else:
        head = head or (git(workdir, "rev-parse", "HEAD") if is_git else None)
        if not head:
            raise rejected(
                "无法确定评审的 HEAD：执行目录不是 git 仓库时用 --head 指定",
                reason="head_missing",
            )
        started = run_tool(
            ledger,
            config,
            caller,
            "review_start",
            {
                "task_id": who.task_id,
                "head": head,
                **({"base": base} if base else {}),
                "reviewer": {"session": session, "model": model},
            },
        )
        round_no = int(started["events"][0]["data"]["round"])
    review_dir = home.root / "runs" / str(who.task_id) / str(who.attempt) / "review"
    resume = None
    if previous is not None and config.get("review.incremental") == "resume_thread":
        resume = _thread_of(review_dir, int(previous["round"]))
    prompt = render_request(
        home,
        config,
        row,
        workdir=workdir,
        is_git=is_git,
        round_no=round_no,
        head=head,
        base=base,
        notes=notes,
        previous=previous if resume else None,
    )
    rel = f"runs/{who.task_id}/{who.attempt}/review"
    _write(home, review_dir / f"r{round_no}-request.md", prompt)
    run, error, result = _call(
        reviewer,
        prompt,
        workdir=workdir,
        config=config,
        resume=resume,
    )
    meta = {
        "round": round_no,
        "head": head,
        "base": base,
        "session": session,
        "model": model,
        "effort": config.get("roles.reviewer.effort"),
        "thread_id": run.thread_id if run else None,
        "resumed": resume,
        "retry": retry is not None,
        "status": run.status if run else None,
        "error": error,
    }
    _write(home, review_dir / f"r{round_no}-run.json", _dumps(meta))
    if result is None:
        if run is not None and run.final_message:
            _write(home, review_dir / f"r{round_no}-raw.txt", run.final_message)
        failed = run_tool(
            ledger,
            config,
            caller,
            "review_fail",
            {"task_id": who.task_id, "round": round_no, "error": str(error)},
        )
        return {
            "ok": False,
            "round": round_no,
            "outcome": failed["events"][0]["type"],
            "error": error,
            "request_path": f"{rel}/r{round_no}-request.md",
        }
    result = _normalize_head(result, head)
    _write(home, review_dir / f"r{round_no}.json", _dumps(result))
    completed = run_tool(
        ledger,
        config,
        caller,
        "review_complete",
        {
            "task_id": who.task_id,
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
    reviewer: Reviewer,
    prompt: str,
    *,
    workdir: Path,
    config: EffectiveConfig,
    resume: str | None,
) -> tuple[ReviewRun | None, str | None, dict[str, Any] | None]:
    """Run the reviewer; returns (run, error, result) with exactly one of error / result set."""
    try:
        run = reviewer(
            prompt,
            cwd=workdir,
            model=str(config.get("roles.reviewer.model")),
            effort=str(config.get("roles.reviewer.effort")),
            timeout_seconds=int(config.get("roles.reviewer.timeout_seconds")),
            executable=config.get("roles.reviewer.executable"),
            resume_thread=resume,
            context_window=config.get("roles.reviewer.context_window"),
        )
    except TswError as exc:
        return None, f"{exc.message} {json.dumps(exc.details, ensure_ascii=False)}", None
    if run.status != "completed":
        return run, f"评审线程未正常完成（{run.status}）{run.error or ''}", None
    result = extract_json(run.final_message)
    if result is None:
        return run, "评审输出不是 JSON 对象", None
    try:
        validate_result(result)
    except TswError as exc:
        return (
            run,
            f"评审输出不满足契约：{exc.message} {json.dumps(exc.details, ensure_ascii=False)}",
            None,
        )
    return run, None, result


def _normalize_head(result: dict[str, Any], head: str) -> dict[str, Any]:
    """A reviewer may report the short form of the head it was given; record the full one."""
    reported = str(result.get("reviewed_head") or "")
    if reported and reported != head and (head.startswith(reported) or reported.startswith(head)):
        return {**result, "reviewed_head": head}
    return result


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
            f"现在的 HEAD 是 {head}。worker 对上一轮意见的处置："
        )
        for finding in previous["findings"]:
            lines.append(
                f"- {finding['finding_id']}（{finding['severity']}）："
                f"{finding['action'] or '未处置'}"
                + (f"；理由：{finding['reason']}" if finding["reason"] else "")
                + (f"；验证：{finding['verification']}" if finding["verification"] else "")
            )
        scope = (
            f"`git diff {previous['reviewed_head']}..{head}`" if is_git else "报告草稿与证据目录"
        )
        lines += [
            "",
            f"请核对 {scope}：上述处置是否真正解决了对应问题、拒绝的理由是否成立，"
            "以及这次改动有没有引入新的问题。新意见的 id 接着上一轮编号。",
        ]
    else:
        lines += [
            f"第 {round_no} 轮评审（新线程）。你是只读评审者（{model}，{effort}）。"
            f"当前目录是任务 {task['task_id']} 的执行目录。"
            "不要修改任何文件，不要运行会写入的命令。",
            "",
            f"## 任务（revision {task['revision']}）",
            "",
            f"- 标题：{task['title']}",
            f"- 类型：{kind}",
            f"- 目标：{task['goal']}",
            "- 验收要求：",
        ]
        for i, item in enumerate(loads(task["acceptance"], []), 1):
            lines.append(f"  {i}. {item}")
        lines += ["", "## 评审范围", ""]
        if is_git:
            span = f"{base}..{head}" if base else f"{head}（以及它之前与本任务相关的提交）"
            lines.append(f"代码与文档改动：`git diff {span}`。")
            stat = git(workdir, "diff", "--stat", f"{base}..{head}") if base else None
            if stat:
                lines += ["", "```", stat, "```"]
        else:
            lines.append(f"报告草稿与证据：`{workdir}`。HEAD 标识为 {head}。")
        draft = home.root / "work" / str(task["task_id"])
        lines += [
            "",
            f"worker 的报告草稿位于 `{draft}` 下本 attempt 的目录（如有）。"
            "执行目录中的 AGENTS.md、CLAUDE.md 等仓库约定请自行只读查阅。",
        ]
    template = config.get(f"review.prompt_templates.{kind}")
    if isinstance(template, str):
        lines += [
            "",
            "## 评审要点",
            "",
            resolve_path(home, template).read_text(encoding="utf-8").strip(),
        ]
    if notes:
        lines += ["", "## worker 的说明（已执行的验证等）", "", notes.strip()]
    lines += ["", "## 输出", "", OUTPUT_CONTRACT.format(head=head)]
    return "\n".join(lines) + "\n"


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
