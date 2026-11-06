"""Give a local task a new id so an imported task can take its old one (DN005 §4.4).

A data directory used before the import may already hold tasks whose ids the imported history
also uses. Only a finished local task can move: ``done``, ``accepted``, ``archived`` or
``cancelled`` with no attempt still running. The ledger keeps every earlier event under the old
id; the ``task.renumbered`` event moves the task's projection rows, its spec and its results to
the new id, and later events use the new id.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.errors import rejected
from tsw.events import EventDraft, sha256_hex
from tsw.home import Home

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


def _move(home: Home, old_rel: str, new_rel: str) -> None:
    source, target = home.root / old_rel, home.root / new_rel
    if not source.exists():
        return
    if target.exists():
        raise rejected("新编号的文件已存在", reason="renumber_taken", path=new_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(target)
    except OSError:  # a directory held open (Windows): copy, then remove the original
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
            shutil.rmtree(source)
        else:
            shutil.copy2(source, target)
            source.unlink()


def command(home: Home, old: str, new: str, *, floor: int) -> Command:
    def build(ctx: CommitContext) -> list[EventDraft]:
        row = check(ctx.conn, old, new, floor=floor)
        spec_old, spec_new = f"todo/{old}.md", f"todo/{new}.md"
        spec_sha = None
        spec = home.root / spec_old
        if spec.is_file():
            text = spec.read_text(encoding="utf-8")
            # The heading names the task: "### T001：…" becomes "### T238：…".
            text = re.sub(rf"^(#+\s*){old}(?=\W)", rf"\g<1>{new}", text, count=1, flags=re.M)
            spec.write_text(text, encoding="utf-8", newline="\n")
            spec_sha = sha256_hex(text.encode("utf-8"))
        _move(home, spec_old, spec_new)
        _move(home, f"runs/{old}", f"runs/{new}")
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
    conn.execute("UPDATE dependencies SET dep_id = ? WHERE dep_id = ?", (new, old))
    conn.execute(
        "UPDATE tasks SET spec_path = ?, spec_sha256 = COALESCE(?, spec_sha256), "
        "version = version + 1 WHERE task_id = ?",
        (event.data.get("spec_path"), event.data.get("spec_sha256"), new),
    )
