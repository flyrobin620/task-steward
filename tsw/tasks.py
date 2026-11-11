"""Task registration (DN002 T01) and task notes.

Registering writes the task specification ``todo/<id>.md`` and regenerates the index
``todo/TODO.md`` from the projection; the ``task.registered`` event records the specification's
path and SHA-256.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.errors import rejected
from tsw.events import EventDraft

KINDS = {"research": "研究", "design": "设计", "code": "代码"}
STATUS_LABELS = {
    "intake": "待受理",
    "waiting_dependency": "等待依赖",
    "ready": "就绪",
    "dispatching": "派发中",
    "in_progress": "执行中",
    "waiting_user": "等待用户",
    "paused": "暂停",
    "verifying": "主控核验",
    "done": "完成·待用户验收",
    "accepted": "已验收",
    "cancelled": "已取消",
    "archived": "已归档",
}
TASK_ID = re.compile(r"^T\d{3,6}$")


@dataclass(frozen=True)
class TaskSpec:
    title: str
    repo: str
    kind: str
    goal: str
    acceptance: Sequence[str]
    task_id: str | None = None
    notes: Sequence[str] = field(default_factory=lambda: ())

    def to_input(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "repo": self.repo,
            "kind": self.kind,
            "goal": self.goal,
            "acceptance": list(self.acceptance),
            "notes": list(self.notes),
        }


def validate_spec(spec: TaskSpec) -> None:
    """T01 guard: the specification names a repository, a kind, a goal and acceptance."""
    missing = [
        name
        for name, value in (
            ("title", spec.title),
            ("repo", spec.repo),
            ("kind", spec.kind),
            ("goal", spec.goal),
        )
        if not value.strip()
    ]
    if not [item for item in spec.acceptance if item.strip()]:
        missing.append("acceptance")
    if missing:
        raise rejected("任务书缺少必填项", reason="spec_incomplete", missing=missing)
    if spec.kind not in KINDS:
        raise rejected("任务类型必须是 research / design / code", kind=spec.kind)
    if spec.task_id is not None and not TASK_ID.match(spec.task_id):
        raise rejected("任务 ID 格式应为 T 加数字，例如 T001", task_id=spec.task_id)


def render_spec(task_id: str, spec: TaskSpec) -> str:
    lines = [
        f"### {task_id}：{spec.title}",
        f"- 仓库：{spec.repo}",
        f"- 类型：{KINDS[spec.kind]}",
        f"- 目标：{spec.goal}",
        "- 验收：",
        *[f"  - {item}" for item in spec.acceptance if item.strip()],
    ]
    if spec.notes:
        lines += ["", *spec.notes]
    return "\n".join(lines) + "\n"


def next_task_id(conn: sqlite3.Connection) -> str:
    """After every task, and after every number an imported history used (DN005 §4.4)."""
    floor = conn.execute("SELECT value FROM meta WHERE key = 'task_id_floor'").fetchone()
    highest = int(floor["value"]) if floor is not None else 0
    for row in conn.execute("SELECT task_id FROM tasks"):
        highest = max(highest, int(str(row["task_id"])[1:]))
    return f"T{highest + 1:03d}"


def render_index(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT task_id, title, repo, kind, status FROM tasks ORDER BY registered_seq"
    ).fetchall()
    lines = [
        "# TODO",
        "",
        "本文件由 tsw 根据账本生成，请勿手工编辑。",
        "",
        "| 任务 | 标题 | 仓库 | 类型 | 状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        title = str(row["title"]).replace("|", "\\|")
        lines.append(
            f"| [{row['task_id']}]({row['task_id']}.md) | {title} | {row['repo']} | "
            f"{KINDS.get(str(row['kind']), row['kind'])} | "
            f"{STATUS_LABELS.get(str(row['status']), row['status'])} |"
        )
    return "\n".join(lines) + "\n"


def write_index(ctx: CommitContext) -> None:
    ctx.storage.put_artifact("todo/TODO.md", render_index(ctx.conn).encode("utf-8"))


def registration(
    ctx: CommitContext, spec: TaskSpec, extra: Mapping[str, Any] | None = None
) -> EventDraft:
    """The ``task.registered`` event of T01; writes the task specification ``todo/<id>.md``.

    ``extra`` adds fields to the event (``follow_up_of`` for a task made from follow-ups,
    DN002 §5.7).
    """
    validate_spec(spec)
    task_id = spec.task_id or next_task_id(ctx.conn)
    taken = ctx.conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if taken is not None:
        raise rejected("任务 ID 已被使用", reason="task_exists", task_id=task_id)
    spec_path = f"todo/{task_id}.md"
    spec_sha = ctx.storage.put_artifact(spec_path, render_spec(task_id, spec).encode("utf-8"))
    return EventDraft(
        type="task.registered",
        task_id=task_id,
        revision=1,
        data={
            "title": spec.title,
            "repo": spec.repo,
            "kind": spec.kind,
            "goal": spec.goal,
            "acceptance": [item for item in spec.acceptance if item.strip()],
            "spec_path": spec_path,
            "spec_sha256": spec_sha,
            **(extra or {}),
        },
    )


def register(spec: TaskSpec, *, actor: Mapping[str, Any], command_id: str | None = None) -> Command:
    validate_spec(spec)

    def build(ctx: CommitContext) -> list[EventDraft]:
        return [registration(ctx, spec)]

    return Command(
        actor=actor,
        input={"op": "task.register", **spec.to_input()},
        build=build,
        command_id=command_id,
    )


def note(
    task_id: str,
    text: str,
    *,
    actor: Mapping[str, Any],
    expected_version: int | None = None,
    command_id: str | None = None,
) -> Command:
    if not text.strip():
        raise rejected("备注不能为空")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = ctx.conn.execute(
            "SELECT version, revision FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise rejected("任务不存在", reason="task_not_found", task_id=task_id)
        if expected_version is not None and row["version"] != expected_version:
            raise rejected(
                "任务已被修改（版本不一致）",
                reason="version_conflict",
                expected=expected_version,
                actual=row["version"],
            )
        return [
            EventDraft(
                type="task.note", task_id=task_id, revision=row["revision"], data={"text": text}
            )
        ]

    return Command(
        actor=actor,
        input={
            "op": "task.note",
            "task_id": task_id,
            "text": text,
            "expected_version": expected_version,
        },
        build=build,
        command_id=command_id,
    )


def task_row(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise rejected("任务不存在", reason="task_not_found", task_id=task_id)
    out = {key: row[key] for key in row.keys()}  # noqa: SIM118 - sqlite3.Row has no __iter__ keys
    out["notes"] = [
        {"sequence": note_row["sequence"], "text": note_row["text"]}
        for note_row in conn.execute(
            "SELECT sequence, text FROM task_notes WHERE task_id = ? ORDER BY sequence",
            (task_id,),
        )
    ]
    out["follow_ups"] = follow_ups_of(conn, task_id)
    out["follow_up_of"] = follow_up_of(conn, task_id)
    return out


def _json(value: Any, default: Any) -> Any:
    return json.loads(value) if value else default


def follow_up(row: Mapping[str, Any]) -> dict[str, Any]:
    """One follow-up as the tools and pages show it (DN002 §5.7)."""
    return {
        "follow_up_id": row["follow_up_id"],
        "task_id": row["task_id"],
        "revision": row["revision"],
        "kind": row["kind"],
        "severity": row["severity"],
        "title": row["title"],
        "detail": row["detail"],
        "status": row["status"],
        "recorded_at": row["recorded_at"],
        "recorded_seq": row["recorded_seq"],
        "recorded_by": _json(row["recorded_by"], {}),
        "source": _json(row["source"], None),
        "recheck_after": row["recheck_after"],
        "converted_to": row["converted_to"],
        "resolution": _json(row["resolution"], None),
        "updated_at": row["updated_at"],
    }


def follow_ups_of(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """The task's follow-ups, oldest first."""
    return [
        follow_up(r)
        for r in conn.execute(
            "SELECT * FROM follow_ups WHERE task_id = ? ORDER BY recorded_seq, follow_up_id",
            (task_id,),
        )
    ]


def follow_up_of(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """The follow-ups the task was made from or took in."""
    return [
        {"task_id": r["task_id"], "follow_up_id": r["follow_up_id"], "title": r["title"]}
        for r in conn.execute(
            "SELECT task_id, follow_up_id, title FROM follow_ups WHERE converted_to = ? "
            "ORDER BY task_id, recorded_seq, follow_up_id",
            (task_id,),
        )
    ]


def list_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {key: row[key] for key in row.keys()}  # noqa: SIM118
        for row in conn.execute(
            "SELECT task_id, title, repo, kind, status, revision, version FROM tasks "
            "ORDER BY registered_seq"
        )
    ]
