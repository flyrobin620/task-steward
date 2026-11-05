"""The STATUS page (``views/STATUS.html``), generated from the projection after every commit.

The page shows the recorded times of the ledger, never the time it was rendered, so generating
it again does not change any business time.
"""

from __future__ import annotations

import html
import sqlite3
from typing import Any

from tsw.commands import CommitContext
from tsw.machine.guards import invalidated
from tsw.machine.model import OCCUPYING, loads
from tsw.tasks import KINDS, STATUS_LABELS

ORDER = (
    "waiting_user",
    "verifying",
    "in_progress",
    "dispatching",
    "paused",
    "ready",
    "waiting_dependency",
    "intake",
    "done",
    "accepted",
    "cancelled",
)
CSS = """
body{font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;margin:24px;color:#1f2430}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:24px 0 8px}
.meta{color:#5b6475;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #d9dee8;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fc}.tag{display:inline-block;padding:0 6px;border-radius:4px;background:#eef2ff}
.warn{background:#fff4e5}.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{border:1px solid #d9dee8;border-radius:8px;padding:8px 12px;min-width:110px}
.card b{font-size:20px;display:block}
@media print{body{margin:0}}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def render(conn: sqlite3.Connection) -> str:
    tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY registered_seq")]
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    lease = conn.execute("SELECT * FROM leases WHERE name = 'scheduler'").fetchone()
    attempts = {
        (r["task_id"], r["attempt"]): dict(r) for r in conn.execute("SELECT * FROM attempts")
    }
    invalid = invalidated(conn)
    counts: dict[str, int] = {}
    for row in tasks:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    occupying = sum(counts.get(s, 0) for s in OCCUPYING)
    to_start = 0
    rows: list[str] = []
    for row in sorted(
        (t for t in tasks if t["status"] != "archived"),
        key=lambda t: (
            ORDER.index(t["status"]) if t["status"] in ORDER else 99,
            t["registered_seq"],
        ),
    ):
        attempt = attempts.get((row["task_id"], row["current_attempt"]))
        phase = attempt["phase"] if attempt else None
        if row["status"] == "ready" and phase in (None, "planned", "replaced", "closed"):
            to_start += 1
        notes: list[str] = []
        if row["status"] == "ready" and phase in (None, "planned", "replaced", "closed"):
            notes.append("待启动")
        if row["stale"]:
            notes.append("stale：上游已变化")
        if row["task_id"] in invalid and row["status"] in OCCUPYING:
            notes.append("依赖失效，需停止 worker")
        pause = loads(row["pause"], None)
        if pause:
            notes.append(
                f"暂停（{pause.get('category')}）负责人 {pause.get('owner')}：{pause.get('next_action')}；恢复条件：{pause.get('resume_condition')}"
            )
        warn = ' class="warn"' if notes and row["status"] != "done" else ""
        rows.append(
            f"<tr{warn}><td>{_e(row['task_id'])}</td><td>{_e(row['title'])}</td>"
            f"<td>{_e(row['repo'])}</td><td>{_e(KINDS.get(row['kind'], row['kind']))}</td>"
            f'<td><span class="tag">{_e(STATUS_LABELS.get(row["status"], row["status"]))}</span>'
            f"{' · ' + _e(row['substate']) if row['substate'] else ''}</td>"
            f"<td>{_e(row['revision'])}</td>"
            f"<td>{_e(row['current_attempt'] or '')}{' · ' + _e(phase) if phase else ''}</td>"
            f"<td>{_e(row['last_activity'])}</td><td>{'<br>'.join(_e(n) for n in notes)}</td></tr>"
        )
    judgments = conn.execute(
        "SELECT COUNT(*) AS n FROM judgments WHERE status = 'open'"
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM input_requests WHERE status = 'pending'"
    ).fetchone()["n"]
    archived = counts.get("archived", 0)
    cards = [
        ("占用名额", occupying),
        ("待启动", to_start),
        ("待判断", judgments),
        ("待用户答复", pending),
        ("待用户验收", counts.get("done", 0)),
        ("已归档", archived),
    ]
    lease_text = (
        f"调度租约：{_e(lease['holder_session'])}（{_e(lease['machine'])}，到期 {_e(lease['expires_at'])}）"
        if lease is not None and lease["holder_session"]
        else "调度租约：无人持有"
    )
    last = max((t["last_activity"] for t in tasks), default="—")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TaskSteward 状态</title><style>{CSS}</style></head>
<body>
<h1>TaskSteward 状态</h1>
<p class="meta">账本序号 {_e(meta.get("projected_seq", "0"))} · 最近记录 {_e(last)} · {lease_text}</p>
<div class="cards">{"".join(f'<div class="card"><b>{n}</b>{_e(label)}</div>' for label, n in cards)}</div>
<h2>任务</h2>
<table><thead><tr><th>任务</th><th>标题</th><th>仓库</th><th>类型</th><th>状态</th><th>revision</th><th>attempt</th><th>最近记录</th><th>说明</th></tr></thead>
<tbody>{"".join(rows) or '<tr><td colspan="9">没有任务</td></tr>'}</tbody></table>
<p class="meta">本页由 tsw 根据账本生成，请勿手工编辑；已归档任务不在此列出（{archived} 个）。</p>
</body>
</html>
"""


def write_status(ctx: CommitContext) -> None:
    ctx.storage.put_artifact("views/STATUS.html", render(ctx.conn).encode("utf-8"))
