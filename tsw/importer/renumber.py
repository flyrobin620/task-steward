"""Give a local task a new id so an imported task can take its old one (DN005 §4.4).

A data directory used before the import may already hold tasks whose ids the imported history
also uses. Only a finished local task can move: ``done``, ``accepted``, ``archived`` or
``cancelled`` with no attempt still running. The ledger keeps every earlier event under the old
id; the ``task.renumbered`` event moves the task's projection rows, its spec and its results to
the new id, and later events use the new id.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.errors import ErrorCode, TswError, rejected
from tsw.events import EventDraft, sha256_hex
from tsw.home import Home
from tsw.importer.files import make_writable

MOVABLE = frozenset({"done", "accepted", "archived", "cancelled"})
RUNNING = frozenset({"starting", "standby", "live"})
TASK_ID = re.compile(r"T[0-9]{1,6}")
ACTOR = {"role": "cli", "op": "import"}


def parse(values: list[str] | None) -> list[tuple[str, str]]:
    """``["T001=T238"]`` → ``[("T001", "T238")]``; refuses malformed or repeated ids."""
    pairs: list[tuple[str, str]] = []
    for value in values or []:
        old, sep, new = value.partition("=")
        if not sep or not TASK_ID.fullmatch(old) or not TASK_ID.fullmatch(new) or old == new:
            raise rejected(
                "改号写成 旧ID=新ID，例如 T001=T238", reason="renumber_syntax", value=value
            )
        pairs.append((old, new))
    olds = [o for o, _ in pairs]
    news = [n for _, n in pairs]
    if len(set(olds)) != len(olds) or len(set(news)) != len(news) or set(olds) & set(news):
        raise rejected("改号中有重复的编号", reason="renumber_syntax")
    return pairs


def _number(task_id: str) -> int:
    return int(task_id[1:])


def check(conn: sqlite3.Connection, old: str, new: str, *, floor: int) -> dict[str, Any]:
    """Why the move is refused, or the task row; ``floor`` is the highest imported id number."""
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (old,)).fetchone()
    if row is None:
        raise rejected("没有要改号的任务", reason="renumber_unknown", task_id=old)
    if conn.execute("SELECT 1 FROM imported_tasks WHERE task_id = ?", (old,)).fetchone():
        raise rejected("导入的任务不改号", reason="renumber_imported", task_id=old)
    if row["status"] not in MOVABLE:
        raise rejected(
            "只能给已结束的任务改号（done / accepted / archived / cancelled）",
            reason="renumber_active",
            task_id=old,
            status=row["status"],
        )
    running = conn.execute(
        "SELECT attempt FROM attempts WHERE task_id = ? AND phase IN ('starting', 'standby', "
        "'live')",
        (old,),
    ).fetchone()
    if running is not None:
        raise rejected(
            "任务还有未结束的 attempt", reason="renumber_active", task_id=old, attempt=running[0]
        )
    if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (new,)).fetchone():
        raise rejected("新编号已被占用", reason="renumber_taken", task_id=new)
    if _number(new) <= floor:
        raise rejected(
            "新编号要排在导入的任务之后", reason="renumber_floor", task_id=new, floor=floor
        )
    return dict(row)


def _plan_moves(home: Home, old: str, new: str) -> list[tuple[str, str]]:
    """The moves still to do; refuses before anything is written when a target is taken.

    A move an interrupted earlier run already made (source gone, target present) is done.
    """
    moves: list[tuple[str, str]] = []
    for old_rel, new_rel in ((f"todo/{old}.md", f"todo/{new}.md"), (f"runs/{old}", f"runs/{new}")):
        source, target = home.root / old_rel, home.root / new_rel
        if source.exists() and target.exists():
            raise rejected("新编号的文件已存在", reason="renumber_taken", path=new_rel)
        if source.exists():
            moves.append((old_rel, new_rel))
    return moves


def _move(home: Home, old_rel: str, new_rel: str) -> None:
    """A rename, retried while Windows scanners hold the files; never a partial copy."""
    source, target = home.root / old_rel, home.root / new_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    for wait in (0.5, 1, 2, 4, 0):
        try:
            source.replace(target)
            return
        except OSError as exc:
            if not wait:
                raise TswError(
                    ErrorCode.UNAVAILABLE,
                    "改号时移动文件失败；关闭占用这些文件的程序后重新运行",
                    reason="renumber_move",
                    path=old_rel,
                    error=str(exc),
                ) from exc
            time.sleep(wait)


def _renamed_spec(text: str, old: str, new: str) -> str:
    # The heading names the task: "### T001：…" becomes "### T238：…".
    return re.sub(rf"^(#+\s*){old}(?=\W)", rf"\g<1>{new}", text, count=1, flags=re.M)


def command(home: Home, old: str, new: str, *, floor: int) -> Command:
    def build(ctx: CommitContext) -> list[EventDraft]:
        row = check(ctx.conn, old, new, floor=floor)
        spec_old, spec_new = f"todo/{old}.md", f"todo/{new}.md"
        moves = _plan_moves(home, old, new)  # every check happens before the first write
        for old_rel, new_rel in moves:
            _move(home, old_rel, new_rel)
        spec_sha = None
        spec = home.root / spec_new
        if spec.is_file():  # renamed after the move, so a retry finds it in either state
            text = _renamed_spec(spec.read_text(encoding="utf-8"), old, new)
            make_writable(spec)
            spec.write_text(text, encoding="utf-8", newline="\n")
            spec_sha = sha256_hex(text.encode("utf-8"))
        return [
            EventDraft(
                type="task.renumbered",
                task_id=new,
                revision=int(row["revision"]),
                data={
                    "from": old,
                    "to": new,
                    "reason": "import_id_conflict",
                    "spec_path": spec_new,
                    "spec_sha256": spec_sha,
                    "moved": {f"runs/{old}/": f"runs/{new}/", spec_old: spec_new},
                },
            )
        ]

    return Command(
        actor=ACTOR,
        input={"op": "task.renumbered", "from": old, "to": new},
        build=build,
        command_id=f"renumber:{old}:{new}",
    )


def reduce(conn: sqlite3.Connection, event: Any) -> None:
    """Move every projection row of the task to its new id; path references follow."""
    old, new = str(event.data["from"]), str(event.task_id)
    moved: dict[str, str] = event.data.get("moved") or {}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        columns = [(c[1], c[2]) for c in conn.execute(f"PRAGMA table_info({table})")]
        names = [n for n, _ in columns]
        for key in ("task_id", "owner_task"):
            if key not in names:
                continue
            for name, kind in columns:
                if name == key or kind.upper() not in ("TEXT", ""):
                    continue
                for before, after in moved.items():
                    conn.execute(
                        f"UPDATE {table} SET {name} = REPLACE({name}, ?, ?) "
                        f"WHERE {key} = ? AND typeof({name}) = 'text'",
                        (before, after, old),
                    )
            conn.execute(f"UPDATE {table} SET {key} = ? WHERE {key} = ?", (new, old))
    # Only the system's own tasks refer to the local task; an imported task's dependency on the
    # same id means the imported task of the source system.
    conn.execute(
        "UPDATE dependencies SET dep_id = ? WHERE dep_id = ? "
        "AND task_id NOT IN (SELECT task_id FROM imported_tasks)",
        (new, old),
    )
    for task_id, attempt, read_set in conn.execute(
        "SELECT task_id, attempt, read_set FROM attempts WHERE read_set LIKE ? "
        "AND task_id NOT IN (SELECT task_id FROM imported_tasks)",
        (f'%"{old}"%',),
    ).fetchall():
        names = [new if r == old else r for r in json.loads(read_set or "[]")]
        conn.execute(
            "UPDATE attempts SET read_set = ? WHERE task_id = ? AND attempt = ?",
            (json.dumps(sorted(set(names)), ensure_ascii=False), task_id, attempt),
        )
    # A submission records the dependency bindings it was made against; a pure renumbering
    # must not invalidate it (g_accept compares them with the dependency table).
    for task_id, submission in conn.execute(
        "SELECT task_id, submission FROM tasks WHERE submission LIKE ? "
        "AND task_id NOT IN (SELECT task_id FROM imported_tasks)",
        (f'%"{old}"%',),
    ).fetchall():
        value: dict[str, Any] = json.loads(submission)
        deps: dict[str, Any] = value.get("dependencies") or {}
        if old in deps:
            deps[new] = deps.pop(old)
            conn.execute(
                "UPDATE tasks SET submission = ? WHERE task_id = ?",
                (json.dumps(value, ensure_ascii=False, sort_keys=True), task_id),
            )
    conn.execute(
        "UPDATE tasks SET spec_path = ?, spec_sha256 = COALESCE(?, spec_sha256), "
        "version = version + 1 WHERE task_id = ?",
        (event.data.get("spec_path"), event.data.get("spec_sha256"), new),
    )
