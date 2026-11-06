"""The resume package of an imported task (DN005 §5.1).

A task imported as resumable carries its history in ``imported_tasks.legacy``. When a new
attempt is dispatched, its brief gets this section: where the earlier work is, what was
delivered, what is still open, and the reconciliation the worker does before continuing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _line(label: str, value: Any) -> str:
    if value in (None, "", [], {}):
        return f"- {label}：无"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return f"- {label}：{value}"


def resume_package(conn: sqlite3.Connection, task_id: str) -> str:
    """The brief section for a resumable imported task; empty for any other task."""
    imported = conn.execute(
        "SELECT * FROM imported_tasks WHERE task_id = ? AND resumable = 1", (task_id,)
    ).fetchone()
    if imported is None:
        return ""
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    legacy: dict[str, Any] = _loads(imported["legacy"], {})
    attempts: list[dict[str, Any]] = legacy.get("attempts") or []
    last: dict[str, Any] = attempts[-1] if attempts else {}
    outputs = conn.execute(
        "SELECT output_id, type, ref, role, delivery, verified_at FROM outputs "
        "WHERE task_id = ? ORDER BY output_id",
        (task_id,),
    ).fetchall()
    requests = conn.execute(
        "SELECT request_id, question, status FROM input_requests WHERE task_id = ? "
        "AND status = 'pending' ORDER BY request_id",
        (task_id,),
    ).fetchall()
    deps = conn.execute(
        "SELECT d.dep_id, t.status, i.legacy FROM dependencies d "
        "LEFT JOIN tasks t ON t.task_id = d.dep_id "
        "LEFT JOIN imported_tasks i ON i.task_id = d.dep_id WHERE d.task_id = ? ORDER BY d.dep_id",
        (task_id,),
    ).fetchall()
    lines = [
        "",
        f"## resume 资料包（导入自 {imported['source']}，DN005 §5.1）",
        "",
        "本任务是从旧系统导入的。开始前先读这些资料，再对账，最后才继续工作。",
        "",
        "### 已有资料",
        "",
        _line("任务书（当前 revision）", row["spec_path"] if row else None),
        _line("导入时的旧状态", imported["old_status"]),
        _line(
            "旧系统 revision / 定义摘要", [legacy.get("revision"), legacy.get("definition_hash")]
        ),
        _line("成果目录", imported["runs"]),
        _line("上一个 attempt 的 brief", last.get("brief")),
        _line("已有交付报告", legacy.get("worker_report")),
        _line("主控验收结论", legacy.get("review_report")),
        _line("最近反馈", legacy.get("last_feedback")),
        _line("暂停", _loads(row["pause"], None) if row else None),
        _line("用户验收 / 拒绝记录", [legacy.get("user_acceptance"), legacy.get("user_rejection")]),
        "",
        "### PR 与产出登记",
        "",
    ]
    lines += [
        f"- {o['output_id']}（{o['type']}）：{o['ref']}；交付状态 {o['delivery'] or '未记录'}；"
        f"核实时间 {o['verified_at'] or '未记录'}"
        for o in outputs
    ] or ["- 无"]
    lines += ["", "### 待答问题", ""]
    lines += [f"- {r['request_id']}：{r['question']}" for r in requests] or ["- 无"]
    lines += ["", "### 执行现场（旧 attempt）", ""]
    for attempt in attempts:
        place = attempt.get("worktree") or attempt.get("working_directory") or attempt.get("source")
        lines.append(
            f"- a{attempt.get('number')}：执行目录 {place or '未记录'}；分支 "
            f"{attempt.get('branch') or '未记录'}；base {attempt.get('base_commit') or '未记录'}；"
            f"worker {attempt.get('worker_id') or attempt.get('worker_channel') or '未记录'}"
        )
    if not attempts:
        lines.append("- 无")
    lines.append(_line("候选提交", legacy.get("candidate_commit")))
    lines.append(_line("集成提交", legacy.get("result") or legacy.get("integration")))
    lines += ["", "### 依赖成果", ""]
    for dep in deps:
        report = _loads(dep["legacy"], {}).get("worker_report")
        lines.append(f"- {dep['dep_id']}（{dep['status'] or '未知'}）：报告 {report or '未记录'}")
    if not deps:
        lines.append("- 无")
    lines += [
        "",
        "### 先对账（DN005 §5.1）",
        "",
        "1. 核对执行目录是否存在，分支与提交是否与上面的记录一致，有无未提交改动；",
        "2. 核对 PR 当前状态（open / merged / closed）与 CI；",
        "3. 对照报告列出“已完成 / 未完成”项。",
        "",
        "与记录不符时先用 progress_note 记录差异；需要用户决定时用 question 提问。"
        "旧 worker 句柄只是历史记录，不要向旧会话发消息。",
        "",
        "提交前对自己的改动做自评审（DN005 §5.2）：有新提交时评审范围是对账时的 HEAD..新 HEAD，"
        "并附导入时的报告作为背景；没有新代码提交时，对更新后的报告做一轮评审。",
        "",
    ]
    return "\n".join(lines)
