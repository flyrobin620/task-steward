# Tool inputs are untyped JSON checked field by field; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""The orchestrator's writes for task context (DN007 §5.1, §6, §12): a task card's keywords and
summary (``task_summary``) and the user's standing preferences (``preference_record``).

Neither changes a task's state. Both write only tables that no earlier event writes, so a
projection made before them stays valid.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.errors import rejected
from tsw.events import Event, EventDraft, digest
from tsw.machine import guards
from tsw.machine.base import ToolEnv, items, mapping, opt_text, strings, text, tool
from tsw.machine.model import dumps, loads, task

STAGES = ("intake", "acceptance", "update")
MAX_KEYWORDS = 12
MAX_KEYWORD_CHARS = 60
MAX_SUMMARY_CHARS = 800
MAX_PREFERENCE_CHARS = 2000
PREFERENCE_TARGET = "standing"


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


def outputs_digest(conn: sqlite3.Connection, task_id: str) -> str:
    """The task's output generation: what is registered and how it was delivered (DN007 §5.1)."""
    rows = conn.execute(
        "SELECT output_id, type, ref, delivery FROM outputs WHERE task_id = ? ORDER BY output_id",
        (task_id,),
    ).fetchall()
    return digest([[r["output_id"], r["type"], r["ref"], r["delivery"]] for r in rows])


def _keywords(inp: Mapping[str, Any]) -> list[str]:
    values = strings(items(inp, "keywords"), "keywords")
    cleaned = list(dict.fromkeys(" ".join(v.split()) for v in values))
    if not cleaned or len(cleaned) > MAX_KEYWORDS:
        raise rejected(
            f"keywords 应为 1–{MAX_KEYWORDS} 个具体短语",
            reason="invalid_input",
            field="keywords",
        )
    long = [v for v in cleaned if len(v) > MAX_KEYWORD_CHARS]
    if long:
        raise rejected(
            f"关键词不超过 {MAX_KEYWORD_CHARS} 个字符", reason="invalid_input", field="keywords"
        )
    return cleaned


@tool(
    "task_summary",
    "orchestrator",
    description="写入任务卡片的关键词与摘要（DN007 §5.1）：受理时 stage=intake 写初版关键词，"
    "技术验收时 stage=acceptance 写实际成果的摘要与关键词；不改变任务状态",
)
def task_summary(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    keywords = _keywords(inp)
    summary = " ".join(text(inp, "summary", required=False).split())
    if len(summary) > MAX_SUMMARY_CHARS:
        raise rejected(
            f"摘要不超过 {MAX_SUMMARY_CHARS} 个字符", reason="invalid_input", field="summary"
        )
    stage = text(inp, "stage")
    if stage not in STAGES:
        raise rejected("stage 无效", reason="invalid_input", field="stage", allowed=list(STAGES))
    if stage == "acceptance" and not summary:
        raise rejected("技术验收时须写摘要", reason="invalid_input", field="summary")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        revision = int(row["revision"])
        return [
            EventDraft(
                type="task.summary_recorded",
                task_id=task_id,
                revision=revision,
                data={
                    "keywords": keywords,
                    "summary": summary,
                    "stage": stage,
                    "bound": {
                        "revision": revision,
                        "outputs": outputs_digest(ctx.conn, task_id),
                    },
                },
            )
        ]

    return _cmd(env, "task_summary", inp, build)


def scope_of(value: str) -> str:
    """``global``, ``repo:<name>`` or ``project:<id>``."""
    kind, _, name = value.partition(":")
    if value == "global" or (kind in ("repo", "project") and name.strip() and name == name.strip()):
        return value
    raise rejected(
        "scope 应为 global、repo:<仓库> 或 project:<项目>", reason="invalid_input", field="scope"
    )


def narrower_or_same(new: str, old: str) -> bool:
    return old in (new, "global")


@tool(
    "preference_record",
    "orchestrator",
    description="记录用户明确说出的长期偏好（DN007 §6）：附用户原话，target 为 "
    '["preference", <scope>, "standing"]；替代旧偏好时在 supersedes 中列出（同范围或更窄）',
)
def preference_record(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    topic = " ".join(text(inp, "topic").split())
    statement = text(inp, "text").strip()
    if len(statement) > MAX_PREFERENCE_CHARS or len(topic) > MAX_KEYWORD_CHARS * 2:
        raise rejected("偏好或主题过长", reason="invalid_input", field="text")
    scope = scope_of(text(inp, "scope"))
    supersedes = list(
        dict.fromkeys(strings(items(inp, "supersedes", required=False), "supersedes"))
    )
    source = guards.verified_source(
        mapping(inp, "user_source"),
        ["preference", scope, PREFERENCE_TARGET],
        mismatch='用户来源的决定对象应为 ["preference", <scope>, "standing"]',
    )

    def build(ctx: CommitContext) -> list[EventDraft]:
        for ref in supersedes:
            old = ctx.conn.execute(
                "SELECT scope, superseded_by FROM user_decisions WHERE ref = ?", (ref,)
            ).fetchone()
            if old is None:
                raise rejected("要替代的偏好不存在", reason="invalid_input", ref=ref)
            if old["superseded_by"]:
                raise rejected(
                    "要替代的偏好已被替代", reason="invalid_input", ref=ref, by=old["superseded_by"]
                )
            if not narrower_or_same(scope, str(old["scope"])):
                raise rejected(
                    "新偏好的范围须与被替代的相同或更窄",
                    reason="invalid_input",
                    ref=ref,
                    scope=old["scope"],
                )
        count = int(ctx.conn.execute("SELECT COUNT(*) AS n FROM user_decisions").fetchone()["n"])
        ref = f"P-{count + 1}"
        return [
            EventDraft(
                type="user.preference_recorded",
                data={
                    "ref": ref,
                    "kind": PREFERENCE_TARGET,
                    "topic": topic,
                    "text": statement,
                    "scope": scope,
                    "supersedes": supersedes,
                    "source": source,
                },
            )
        ]

    return _cmd(env, "preference_record", inp, build)


# ---------------------------------------------------------------------- reducers


def reduce_summary_recorded(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    bound = data.get("bound") or {}
    conn.execute(
        "INSERT INTO task_summaries(task_id, keywords, summary, stage, bound_revision, "
        "bound_outputs, source_seq) VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE "
        "SET keywords = excluded.keywords, summary = excluded.summary, stage = excluded.stage, "
        "bound_revision = excluded.bound_revision, bound_outputs = excluded.bound_outputs, "
        "source_seq = excluded.source_seq",
        (
            event.task_id,
            dumps(list(data.get("keywords") or [])),
            str(data.get("summary") or ""),
            str(data.get("stage") or ""),
            int(bound.get("revision") or event.revision or 0),
            str(bound.get("outputs") or ""),
            event.sequence,
        ),
    )


def reduce_preference_recorded(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    ref = str(data["ref"])
    conn.execute(
        "INSERT INTO user_decisions(ref, task_id, kind, topic, text, scope, verified_source, "
        "supersedes, superseded_by, recorded_at, source_seq) "
        "VALUES(?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            ref,
            str(data.get("kind") or PREFERENCE_TARGET),
            str(data.get("topic") or ""),
            str(data.get("text") or ""),
            str(data.get("scope") or "global"),
            dumps(data.get("source") or {}),
            dumps(list(data.get("supersedes") or [])),
            event.time,
            event.sequence,
        ),
    )
    for old in data.get("supersedes") or []:
        conn.execute("UPDATE user_decisions SET superseded_by = ? WHERE ref = ?", (ref, str(old)))


def reduce_context_read(conn: sqlite3.Connection, event: Event) -> None:
    """``context.read`` is an audit record of what a session read and why: nothing to project."""


REDUCERS = {
    "task.summary_recorded": reduce_summary_recorded,
    "user.preference_recorded": reduce_preference_recorded,
    "context.read": reduce_context_read,
}


def summary_row(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM task_summaries WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return {
        "keywords": loads(row["keywords"], []),
        "summary": row["summary"],
        "stage": row["stage"],
        "bound_revision": row["bound_revision"],
        "bound_outputs": row["bound_outputs"],
        "source_seq": row["source_seq"],
    }
