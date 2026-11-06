"""``tsw import orchestrator-agent`` (DN005): the whole orchestrator-agent history, read-only.

The source system exports a snapshot with its own reader (``python -m orchestrator
export-snapshot``): every task's final state, its own lifecycle verdicts, the event volumes
(bytes, sequence range, digest) and the files to bring along. This module never parses the old
ledger format. It

1. checks the snapshot against the source (every volume still has the recorded bytes);
2. copies the event volumes byte for byte to ``ledger/imported/orchestrator-agent/``;
3. classifies every task by DN005 §3 and writes one ``import.task_imported`` event per task,
   with the original business times; task specs go to ``todo/``, results of read-only tasks to
   ``legacy/orchestrator-agent/runs/`` and of resumable tasks to ``runs/``;
4. writes ``import.completed`` and a report (``legacy/orchestrator-agent/import/report.md``
   and ``.html``, next to a copy of the snapshot).

Every task event carries ``command_id = import:orchestrator-agent:<task>:<source sequence>``,
so running the same import again appends nothing. A later snapshot re-imports the tasks that
changed in the source, unless the task has had events of its own in the new system since.
"""

# Snapshot JSON is walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tsw.commands import Command, CommitContext, CommitResult
from tsw.errors import ErrorCode, TswError, rejected
from tsw.events import EventDraft, sha256_hex, utc_now
from tsw.home import Home
from tsw.importer import renumber, verify
from tsw.importer.files import make_writable, remove_tree
from tsw.ledger import Ledger
from tsw.locks import FileLock
from tsw.storage.fs import FsStorage

SYSTEM = "orchestrator-agent"
SNAPSHOT_SCHEMA = 1
ACTOR = {"role": "cli", "op": "import"}
LEGACY = f"legacy/{SYSTEM}"
VOLUMES = f"ledger/imported/{SYSTEM}"
KINDS = {"research": "research", "design": "design", "code": "code"}
TASK_ID = re.compile(r"T[0-9]{1,6}")
# Old statuses that were still running when the source stopped: handed over (DN005 §3 rule 7).
ACTIVE = frozenset({"starting", "running", "waiting_user", "review", "integrating", "conflict"})
READ_ONLY = frozenset({"archived", "accepted", "cancelled"})


@dataclass
class Planned:
    task_id: str
    old_status: str
    new_status: str
    rule: int
    data: dict[str, Any]
    files: list[tuple[Path, str]] = field(default_factory=lambda: [])  # (source, home-relative)

    @property
    def resumable(self) -> bool:
        return self.new_status not in READ_ONLY


# ---------------------------------------------------------------------- snapshot


def load_snapshot(directory: Path) -> tuple[dict[str, Any], str]:
    path = directory / "snapshot.json"
    try:
        raw = path.read_bytes()
        snapshot = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise rejected("读不到导入快照", reason="snapshot_unreadable", path=str(path)) from exc
    if not isinstance(snapshot, dict):
        raise rejected("导入快照的格式版本不受支持", reason="snapshot_schema", schema=None)
    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise rejected(
            "导入快照的格式版本不受支持", reason="snapshot_schema", schema=snapshot.get("schema")
        )
    source = snapshot.get("source") or {}
    if source.get("system") != SYSTEM:
        raise rejected("快照不是 orchestrator-agent 导出的", reason="snapshot_system")
    return snapshot, sha256_hex(raw)


def safe_rel(value: Any) -> str:
    """A relative path from the snapshot; refuses absolute paths, drives and ``..``."""
    text = str(value or "").replace("\\", "/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or text.startswith("/") or ":" in text or ".." in parts:
        raise rejected("快照中的路径不安全", reason="snapshot_path", path=str(value))
    return "/".join(parts)


def inside(path: Path, base: Path) -> bool:
    target = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(base))
    return target == root or target.startswith(root.rstrip("\\/") + os.sep)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_volumes(source: Path, snapshot: Mapping[str, Any]) -> list[str]:
    """Problems with the source's event volumes compared with the snapshot (DN005 §6 item 2)."""
    problems: list[str] = []
    for volume in snapshot.get("volumes") or []:
        path = source / safe_rel(volume["path"])
        if not path.is_file():
            problems.append(f"缺少事件卷 {volume['path']}")
        elif path.stat().st_size != volume["bytes"] or _file_sha256(path) != volume["sha256"]:
            problems.append(f"事件卷 {volume['path']} 在导出后发生了变化")
    return problems


def _moment(value: Any) -> datetime | None:
    """An ISO 8601 time with its full precision, in UTC (no offset means UTC)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(
        timezone.utc
    )


def source_lease(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """The source's scheduling lease at import time (DN005 §4.1): released, expired or active.

    A holder whose expiry is missing or unreadable counts as active.
    """
    lease = snapshot.get("controller") or {}
    holder = lease.get("holder") or lease.get("session")
    expires = _moment(lease.get("expires_at"))
    if not holder:
        state = "released"
    elif expires is not None and expires <= now:
        state = "expired"
    else:
        state = "active"
    return {
        "state": state,
        "holder": holder,
        "expires_at": lease.get("expires_at"),
        "checked_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------- classification


def classify(task: Mapping[str, Any], derived: Mapping[str, Any]) -> tuple[str, int]:
    """DN005 §3: the new status and the number of the rule that decided it."""
    status = str(task.get("status") or "pending")
    if task.get("retirement") or task.get("archived") or derived.get("phase") == "archived":
        return "archived", 1
    if status == "cancelled":
        return "cancelled", 2
    if status == "done" and derived.get("user_acceptance_valid"):
        return "accepted", 3
    if status == "done":
        return "done", 4
    if status in ("pending", "ready"):
        return "intake", 5
    if status in ("paused", "failed"):
        return "paused", 6
    return "paused", 7


def _pause(task: Mapping[str, Any], rule: int) -> dict[str, Any] | None:
    if rule == 7:
        return {
            "category": "import_handover",
            "owner": "orchestrator",
            "next_action": "核实旧 worker 已停止并交接",
            "resume_condition": "有旧 worker 停止证据或用户确认",
            "imported": True,
            "source_input_request": task.get("input_request"),
        }
    if rule != 6:
        return None
    follow = task.get("follow_up") or {}
    failed = task.get("status") == "failed"
    return {
        "category": "failure" if failed else "external",
        "owner": str(follow.get("owner") or "orchestrator"),
        "next_action": str(follow.get("next_action") or "按导入的历史核对后决定"),
        "resume_condition": str(follow.get("resume_condition") or "主会话核对后确认"),
        "imported": True,
        "source_follow_up": dict(follow) if follow else None,
        "source_input_request": task.get("input_request"),
    }


def _time(value: Any) -> str | None:
    """An old timestamp (any ISO 8601 offset) in this system's form: UTC, seconds, ``Z``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _acceptance(definition: Mapping[str, Any]) -> list[str]:
    value = definition.get("acceptance")
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value or "").strip()
    return [line.strip() for line in text.splitlines() if line.strip()] or ([text] if text else [])


def _attempts(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    keep = (
        "number",
        "worker_channel",
        "worker_id",
        "worker_controller",
        "branch",
        "base_commit",
        "source",
        "report",
        "brief",
        "started_at",
        "revision",
        "execution_mode",
        "repo",
        "worktree",
        "working_directory",
    )
    return [{k: a.get(k) for k in keep if k in a} for a in task.get("attempts") or []]


def _outputs(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """PRs and registered outputs as rows of the outputs table (evidence kept as recorded)."""
    rows: list[dict[str, Any]] = []
    for i, pr in enumerate(task.get("pull_requests") or [], 1):
        if not isinstance(pr, dict) or not pr.get("url"):
            continue
        rows.append(
            {
                "output_id": f"pr-{i}",
                "type": "pr",
                "ref": pr["url"],
                "role": pr.get("role"),
                "delivery": pr.get("state") or pr.get("status"),
                "verified_at": pr.get("verified_at"),
                "evidence": str(pr.get("evidence") or "imported"),
            }
        )
    for i, out in enumerate(task.get("outputs") or [], 1):
        if not isinstance(out, dict) or not out.get("ref"):
            continue
        rows.append(
            {
                "output_id": str(out.get("id") or f"output-{i}"),
                "type": str(out.get("type") or "output"),
                "ref": str(out["ref"]),
                "role": out.get("form"),
                "delivery": out.get("delivery"),
                "verified_at": out.get("verified_at"),
                "evidence": str(out.get("evidence") or "imported"),
            }
        )
    return rows


def _local_ref(task_id: str, ref: Any, read_only: bool) -> Any:
    """A result reference of the source, as it resolves after the import (DN005 §4.2 step 6)."""
    if not isinstance(ref, str) or not ref.startswith(f"runs/{task_id}/") or not read_only:
        return ref
    return f"legacy://{SYSTEM}/{ref}"


def resolve_ref(home: Home, ref: str) -> Path | None:
    """Where a ``runs/`` or ``legacy://`` reference lives in the data directory."""
    if ref.startswith("legacy://"):
        system, _, rest = ref[len("legacy://") :].partition("/")
        return home.root / "legacy" / system / rest
    if ref.startswith("runs/"):
        return home.root / ref
    return None


def _request(task: Mapping[str, Any]) -> dict[str, Any] | None:
    """The source's question: pending, or resolved with the user's answer kept."""
    request = task.get("input_request")
    if not isinstance(request, dict) or request.get("status") == "cancelled":
        return None
    if not request.get("id") or not request.get("question"):
        return None
    resolved = request.get("status") == "resolved"
    return {
        "request_id": f"{SYSTEM}:{request['id']}",
        "question": str(request["question"]),
        "status": "resolved" if resolved else "pending",
        "answer": request.get("answer") if resolved else None,
        "source": dict(request),
    }


def plan_task(
    task_id: str,
    task: Mapping[str, Any],
    derived: Mapping[str, Any],
    *,
    source: Path,
    revisions: Mapping[str, int] | None = None,
) -> Planned:
    definition: Mapping[str, Any] = task.get("definition") or {}
    new_status, rule = classify(task, derived)
    read_only = new_status in READ_ONLY
    runs_rel = f"{LEGACY}/runs/{task_id}" if read_only else f"runs/{task_id}"
    retirement = task.get("retirement") or {}
    acceptance = task.get("user_acceptance") or {}
    data: dict[str, Any] = {
        "source": SYSTEM,
        "old_status": task.get("status"),
        "new_status": new_status,
        "rule": rule,
        "resumable": not read_only,
        "title": str(definition.get("title") or task_id),
        "repo": str(definition.get("repo") or ""),
        "kind": KINDS.get(str(definition.get("kind")), "code"),
        "goal": str(definition.get("goal") or ""),
        "acceptance": _acceptance(definition),
        "spec_path": f"todo/{task_id}.md",
        "last_activity": _time(derived.get("activity_time")) or _time(task.get("created_at")),
        "created_at": _time(task.get("created_at")),
        "done_at": _time(task.get("completed_at")),
        "accepted_at": _time(acceptance.get("accepted_at")) if new_status == "accepted" else None,
        "archived_at": _time(retirement.get("archived_at")),
        "previous_status": (
            ("accepted" if derived.get("user_acceptance_valid") else task.get("status"))
            if new_status == "archived"
            else None
        ),
        "attempt_count": len(task.get("attempts") or []),
        "pause": _pause(task, rule),
        "dependencies": [
            {
                "dep_id": str(d),
                "kind": "explicit",
                # Bound to the upstream revision the source last saw; the source's own stale
                # flag says whether that upstream changed since (DN002 G-DEP).
                "bound_revision": int((revisions or {}).get(str(d), 0)),
            }
            for d in (task.get("dependencies") or [])
            if isinstance(d, str)
        ],
        "stale": bool(task.get("stale")),
        "resources": [str(r) for r in definition.get("resources") or []],
        # Resources a retired task still holds, and the resources a paused task keeps
        # (DN002 T14), stay claimed until there is evidence they were released.
        "held_resources": sorted(
            {str(r) for r in retirement.get("retained_resources") or []}
            | (
                {str(r) for r in definition.get("resources") or []}
                if new_status == "paused"
                else set()
            )
        ),
        "input_request": None if read_only else _request(task),
        "outputs": [
            {**out, "ref": _local_ref(task_id, out["ref"], read_only)} for out in _outputs(task)
        ],
        "runs": runs_rel if (source / "runs" / task_id).is_dir() else None,
        "legacy": {
            "revision": task.get("revision"),
            "definition_hash": task.get("definition_hash"),
            "attempts": _attempts(task),
            "user_acceptance": acceptance or None,
            "user_rejection": task.get("user_rejection"),
            "retirement": retirement or None,
            "worker_report": task.get("worker_report"),
            "review_report": task.get("review_report"),
            "last_feedback": task.get("last_feedback"),
            "result": task.get("result"),
            "candidate_commit": task.get("candidate_commit"),
            "integration": task.get("integration"),
        },
    }
    files: list[tuple[Path, str]] = []
    spec = source / "todo" / f"{task_id}.md"
    if spec.is_file():
        files.append((spec, data["spec_path"]))
    if data["runs"]:
        files.append((source / "runs" / task_id, runs_rel))
    return Planned(task_id, str(task.get("status")), new_status, rule, data, files)


def prepare(plan: Planned, source: Path) -> list[tuple[Path, str]]:
    """Check a task's source files and record its spec digest; nothing is written yet.

    The files are copied by the task's command under the commit lock, after the conflict
    checks (:func:`_task_imported`), so a refused import never overwrites anything.
    """
    for src, _ in plan.files:
        if not inside(src, source):
            raise rejected("来源文件越出来源目录", reason="snapshot_path", path=str(src))
    spec = next((src for src, rel in plan.files if rel == plan.data["spec_path"]), None)
    plan.data["spec_sha256"] = _file_sha256(spec) if spec is not None else None
    return list(plan.files)


def place(home: Home, files: list[tuple[Path, str]]) -> None:
    """Copy a task's files into the data directory, replacing what an earlier import put there.

    Copied in place rather than renamed from a staging area: on Windows a freshly written tree
    of tens of thousands of files is often still held open by scanners, and the rename fails.
    """
    for src, rel in files:
        target = home.root / rel
        if not inside(target.parent if target.parent.exists() else home.root, home.root):
            raise rejected("导入目标越出数据目录", reason="snapshot_path", path=rel)
        try:
            if target.is_dir():
                remove_tree(target)
            elif target.exists():  # copy2 keeps a read-only source file read-only
                make_writable(target)
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, symlinks=True)
            else:
                shutil.copy2(src, target)
        except OSError as exc:
            raise TswError(
                ErrorCode.UNAVAILABLE,
                "复制任务文件失败；重新运行导入会从这个任务继续",
                reason="import_copy",
                path=rel,
                error=str(exc),
            ) from exc


# ---------------------------------------------------------------------- import


@dataclass
class Report:
    snapshot: dict[str, Any]
    snapshot_sha256: str
    planned: list[Planned]
    imported: list[str] = field(default_factory=lambda: [])
    unchanged: list[str] = field(default_factory=lambda: [])
    conflicts: list[dict[str, Any]] = field(default_factory=lambda: [])
    todo_only: list[str] = field(default_factory=lambda: [])
    excluded: list[str] = field(default_factory=lambda: [])
    missing_files: list[str] = field(default_factory=lambda: [])
    volume_problems: list[str] = field(default_factory=lambda: [])
    invalid: list[str] = field(default_factory=lambda: [])
    renumbered: list[dict[str, str]] = field(default_factory=lambda: [])
    checks: list[verify.Check] = field(default_factory=lambda: [])
    lease: dict[str, Any] = field(default_factory=lambda: {})
    floor: int = 0  # the highest task number the source used
    appended: int = 0  # events this run appended
    rerun: bool = False
    dry_run: bool = False

    def count(self, result: CommitResult) -> CommitResult:
        if not result.duplicate:
            self.appended += len(result.events)
        return result

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for plan in self.planned:
            out[plan.new_status] = out.get(plan.new_status, 0) + 1
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "source_sequence": self.snapshot["source"]["last_sequence"],
            "tasks": len(self.planned),
            "classes": self.counts(),
            "imported": len(self.imported),
            "unchanged": len(self.unchanged),
            "conflicts": self.conflicts,
            "todo_only": self.todo_only,
            "excluded_todo_only": self.excluded,
            "missing_files": self.missing_files,
            "volume_problems": self.volume_problems,
            "invalid_task_ids": self.invalid,
            "renumbered_local": self.renumbered,
            "source_lease": self.lease,
        }


IMPORT_LOCK_TIMEOUT = 5.0


def run_import(
    home: Home,
    ledger: Ledger,
    snapshot_dir: Path,
    *,
    dry_run: bool = False,
    renumber_local: list[tuple[str, str]] | None = None,
    exclude: list[str] | None = None,
    on_locked: Callable[[], None] | None = None,
    file_guard: Callable[[], AbstractContextManager[None]] = contextlib.nullcontext,
) -> dict[str, Any]:
    """One import at a time: the event volumes, manifest and legacy files are shared targets.

    ``on_locked`` runs once the import lock is held, before anything is written (a real run
    only): the place to check and record the authorization of this run. ``file_guard`` wraps
    each direct file write outside a ledger commit (event volumes, legacy files, the report),
    so that a caller can keep its own checks, such as the scheduling lease, around them.
    """
    if dry_run:
        return _run_import(
            home, ledger, snapshot_dir, dry_run=True, renumber_local=renumber_local, exclude=exclude
        )
    lock = FileLock(home.locks_dir / "import.lock", timeout=IMPORT_LOCK_TIMEOUT)
    try:
        lock.acquire()
    except TswError as exc:
        raise TswError(
            ErrorCode.UNAVAILABLE, "另一个导入正在进行，等它结束后再运行", reason="import_running"
        ) from exc
    try:
        if on_locked is not None:
            on_locked()
        return _run_import(
            home,
            ledger,
            snapshot_dir,
            dry_run=False,
            renumber_local=renumber_local,
            exclude=exclude,
            file_guard=file_guard,
        )
    finally:
        lock.release()


def _run_import(
    home: Home,
    ledger: Ledger,
    snapshot_dir: Path,
    *,
    dry_run: bool,
    renumber_local: list[tuple[str, str]] | None,
    exclude: list[str] | None,
    file_guard: Callable[[], AbstractContextManager[None]] = contextlib.nullcontext,
) -> dict[str, Any]:
    snapshot, snapshot_sha = load_snapshot(snapshot_dir)
    source = Path(snapshot["source"]["root"])
    last_seq = int(snapshot["source"]["last_sequence"])
    report = Report(snapshot, snapshot_sha, [], dry_run=dry_run)
    report.volume_problems = check_volumes(source, snapshot)
    if report.volume_problems and not dry_run:
        raise TswError(
            ErrorCode.REJECTED,
            "来源账本与快照不一致，先停止旧系统调度并重新导出",
            reason="snapshot_stale",
            problems=report.volume_problems,
        )
    report.lease = source_lease(snapshot, datetime.now(timezone.utc))
    if report.lease["state"] == "active" and not dry_run:
        raise rejected(
            "旧系统的调度租约仍有效，先停止旧系统调度，等租约过期或释放后重新导出",
            reason="source_lease_active",
            lease=report.lease,
        )
    derived: Mapping[str, Any] = snapshot.get("derived") or {}
    revisions = {
        str(tid): int(task.get("revision") or 1) for tid, task in snapshot["tasks"].items()
    }
    for task_id in sorted(snapshot["tasks"], key=lambda t: _task_number(str(t))):
        if not TASK_ID.fullmatch(str(task_id)):
            report.invalid.append(str(task_id))
            continue
        report.planned.append(
            plan_task(
                task_id,
                snapshot["tasks"][task_id],
                derived.get(task_id) or {},
                source=source,
                revisions=revisions,
            )
        )
    known = {p.task_id for p in report.planned}
    report.todo_only = sorted(
        (
            Path(t).stem
            for t in (snapshot.get("files") or {}).get("todo") or []
            if Path(t).stem not in known
        ),
        key=_task_number,
    )
    # Spec-only tasks the user decided not to keep: not imported, not listed as open (their
    # numbers still count for the floor, so they are never reused).
    unknown = sorted(set(exclude or []) - set(report.todo_only))
    if unknown:
        raise rejected(
            "只能排除只有任务书、旧账本中没有登记的任务",
            reason="exclude_unknown",
            task_ids=unknown,
        )
    report.excluded = sorted(set(exclude or []), key=_task_number)
    floor = max(
        [_task_number(p.task_id) for p in report.planned]
        + [_task_number(t) for t in report.todo_only]
        + [0]
    )
    report.floor = floor
    report.todo_only = [t for t in report.todo_only if t not in report.excluded]
    for old, new in renumber_local or []:
        if dry_run:
            with ledger.view() as conn:
                done = conn.execute(
                    "SELECT 1 FROM commands WHERE command_id = ?", (f"renumber:{old}:{new}",)
                ).fetchone()
                if done is None:
                    renumber.check(conn, old, new, floor=floor)
        else:
            report.count(ledger.commit(renumber.command(home, old, new, floor=floor)))
        report.renumbered.append({"from": old, "to": new})
    with ledger.view() as conn:
        existing = {
            str(r["task_id"]): r
            for r in conn.execute(
                "SELECT t.task_id, t.version, i.event_seq, i.source_sequence FROM tasks t "
                "LEFT JOIN imported_tasks i ON i.task_id = t.task_id"
            )
        }
    todo: list[Planned] = []
    for plan in report.planned:
        row = existing.get(plan.task_id)
        if row is None:
            todo.append(plan)
        elif row["event_seq"] is None:
            report.conflicts.append(
                {"task_id": plan.task_id, "reason": "数据目录中已有同 ID 的任务（非导入）"}
            )
        elif int(row["source_sequence"]) >= last_seq:
            report.unchanged.append(plan.task_id)
        elif int(row["version"]) != 1:  # the import sets version 1; any later event moves it
            report.conflicts.append(
                {"task_id": plan.task_id, "reason": "导入后已在新系统中有事件，不覆盖"}
            )
        else:
            todo.append(plan)  # a later snapshot of an untouched task: an update
    if dry_run:
        return report.summary()
    storage = ledger.storage
    assert isinstance(storage, FsStorage)
    batch = _batch(ledger, last_seq)
    # A repeat of a finished import with nothing left to write decides DN005 §6 item 6.
    report.rerun = not todo and batch == "completed" and report.appended == 0
    if not todo and batch == "completed":  # nothing new: no events at all (DN005 §6 item 6)
        report.missing_files = _unresolved_refs(home, report.planned)
        _verify(home, ledger, report)
        with storage.writer(), file_guard():
            _write_report(home, report)
        return report.summary()
    with storage.writer(), file_guard():
        _copy_volumes(home, source, snapshot)
        _copy_legacy(home, source, snapshot, snapshot_dir)
    report.count(
        _commit_started(
            ledger, snapshot, snapshot_sha, last_seq, lease=report.lease, floor=report.floor
        )
    )
    for plan in todo:
        files = prepare(plan, source)
        try:
            result = report.count(ledger.commit(_task_imported(home, plan, files, last_seq)))
        except TswError as exc:
            if exc.details.get("reason") != "import_conflict":
                raise
            report.conflicts.append(
                {"task_id": plan.task_id, "reason": "导入时发现新系统中的事件，不覆盖"}
            )
            continue
        if result.duplicate or not result.events:
            report.unchanged.append(plan.task_id)
        else:
            report.imported.append(plan.task_id)
    report.missing_files = _unresolved_refs(home, report.planned)
    report.count(_commit_completed(ledger, report, last_seq))
    _verify(home, ledger, report)
    with storage.writer(), file_guard():
        _write_report(home, report)
    return report.summary()


def _verify(home: Home, ledger: Ledger, report: Report) -> None:
    """The DN005 §6 checks, after the import's events (they are not part of the summary)."""
    report.checks = verify.checks(
        home,
        ledger,
        report.snapshot,
        classes={p.task_id: p.new_status for p in report.planned},
        conflicts=[str(c["task_id"]) for c in report.conflicts],
        missing=report.missing_files,
        volume_problems=check_volumes(Path(report.snapshot["source"]["root"]), report.snapshot),
        new_events=report.appended,
        rerun=report.rerun,
    )


def _batch(ledger: Ledger, last_seq: int) -> str | None:
    """How far an import of this source sequence got: None, "started" or "completed"."""
    state: str | None = None
    for event in ledger.storage.read(since=0):
        if event.type in ("import.started", "import.completed") and (
            event.data.get("source_sequence", (event.data.get("source") or {}).get("last_sequence"))
            == last_seq
        ):
            state = "completed" if event.type == "import.completed" else state or "started"
    return state


def _task_number(task_id: str) -> int:
    digits = "".join(ch for ch in task_id if ch.isdigit())
    return int(digits) if digits else 0


def _copy(home: Home, src: Path, rel: str) -> bool:
    target = home.root / rel
    if not src.exists():
        return False
    if src.is_dir():
        shutil.copytree(src, target, dirs_exist_ok=True, symlinks=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return True


def _copy_volumes(home: Home, source: Path, snapshot: Mapping[str, Any]) -> None:
    """Byte-identical copies of every event volume, plus a manifest (DN005 §4.2 step 2)."""
    directory = home.root / VOLUMES
    directory.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for volume in snapshot.get("volumes") or []:
        src = source / str(volume["path"])
        name = Path(str(volume["path"])).name
        if not volume["compressed"]:  # the tail grows: name it by the sequences it holds
            name = f"events-{volume['first']}-{volume['last']}.jsonl"
        target = directory / name
        if not (target.is_file() and _file_sha256(target) == volume["sha256"]):
            shutil.copyfile(src, target)
            if _file_sha256(target) != volume["sha256"]:
                raise TswError(
                    ErrorCode.UNAVAILABLE,
                    "事件卷复制后内容不一致",
                    reason="volume_copy",
                    volume=volume["path"],
                )
        manifest.append({**volume, "file": name})
    payload = {
        "source": snapshot["source"],
        "volumes": manifest,
        "note": "orchestrator-agent 的原始事件卷，按原字节保存；内容摘要只针对账本文本",
    }
    (directory / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _copy_legacy(home: Home, source: Path, snapshot: Mapping[str, Any], snapshot_dir: Path) -> None:
    files = snapshot.get("files") or {}
    legacy = home.root / LEGACY
    legacy.mkdir(parents=True, exist_ok=True)
    for given in [files.get("index"), *(files.get("config") or []), files.get("keywords")]:
        if not given:
            continue
        rel = safe_rel(given)
        if inside(source / rel, source) and (source / rel).exists():
            _copy(home, source / rel, f"{LEGACY}/{rel}")
    imports = home.root / LEGACY / "import"
    imports.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot_dir / "snapshot.json", imports / "snapshot.json")


def _unresolved_refs(home: Home, planned: list[Planned]) -> list[str]:
    """Result references that do not resolve inside the data directory (DN005 §6 item 3)."""
    missing: list[str] = []
    for plan in planned:
        read_only = not plan.resumable
        refs = [plan.data["legacy"].get(k) for k in ("worker_report", "review_report")]
        refs += [o["ref"] for o in plan.data["outputs"]]
        for ref in refs:
            local = _local_ref(plan.task_id, ref, read_only)
            path = resolve_ref(home, local) if isinstance(local, str) else None
            # A link that leads out of the data directory does not count as kept history.
            if path is not None and (not path.exists() or not inside(path, home.root)):
                missing.append(str(local))
    return sorted(set(missing))


# ---------------------------------------------------------------------- events


def _commit_started(
    ledger: Ledger,
    snapshot: Mapping[str, Any],
    sha: str,
    last_seq: int,
    *,
    lease: dict[str, Any],
    floor: int,
) -> CommitResult:
    def build(ctx: CommitContext) -> list[EventDraft]:
        return [
            EventDraft(
                type="import.started",
                data={
                    "source": dict(snapshot["source"]),
                    "snapshot_sha256": sha,
                    "tasks": len(snapshot["tasks"]),
                    "controller": snapshot.get("controller"),
                    # Recorded before anything is imported: the old system had stopped.
                    "source_lease": lease,
                    # Every number the source used, specs only included: never handed out again.
                    "id_floor": floor,
                },
            )
        ]

    return ledger.commit(
        Command(
            actor=ACTOR,
            input={"op": "import.started", "source_sequence": last_seq, "snapshot": sha},
            build=build,
            command_id=f"import:{SYSTEM}:started:{last_seq}",
        )
    )


def _task_imported(
    home: Home, plan: Planned, files: list[tuple[Path, str]], last_seq: int
) -> Command:
    """The task's event; its files are published here, under the commit lock, after the checks."""
    data = {**plan.data, "source_sequence": last_seq}

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = ctx.conn.execute(
            "SELECT t.version, i.event_seq, i.source_sequence FROM tasks t "
            "LEFT JOIN imported_tasks i ON i.task_id = t.task_id WHERE t.task_id = ?",
            (plan.task_id,),
        ).fetchone()
        if row is not None and (row["event_seq"] is None or int(row["version"]) != 1):
            raise rejected("任务已在新系统中有自己的事件，不覆盖", reason="import_conflict")
        if row is not None and int(row["source_sequence"]) >= last_seq:
            return []  # an import of the same or a later snapshot got here first
        place(home, files)
        # New or update is decided here, under the lock: another import may have brought the
        # task in since this run looked (DN005 §4.4).
        return [
            EventDraft(
                type="import.task_imported",
                task_id=plan.task_id,
                revision=int(plan.data["legacy"].get("revision") or 1),
                data={**data, "update": row is not None},
            )
        ]

    return Command(
        actor=ACTOR,
        input={"op": "import.task_imported", "task_id": plan.task_id, "data": data},
        build=build,
        command_id=f"import:{SYSTEM}:{plan.task_id}:{last_seq}",
    )


def _digest(value: Any) -> str:
    return sha256_hex(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _commit_completed(ledger: Ledger, report: Report, last_seq: int) -> CommitResult:
    summary = report.summary()

    def build(ctx: CommitContext) -> list[EventDraft]:
        return [EventDraft(type="import.completed", data=summary)]

    return ledger.commit(
        Command(
            actor=ACTOR,
            input={"op": "import.completed", "summary": summary},
            build=build,
            command_id=f"import:{SYSTEM}:completed:{last_seq}:{_digest(summary)[:16]}",
        )
    )


# ---------------------------------------------------------------------- report


def _write_report(home: Home, report: Report) -> None:
    summary = report.summary()
    source = report.snapshot["source"]
    lines = [
        "# orchestrator-agent 历史导入报告",
        "",
        f"- 导入时间：{utc_now()}",
        f"- 来源：{source['root']}（提交 {source.get('git_head')}）",
        f"- 来源账本末端序号：{summary['source_sequence']}；快照摘要：{report.snapshot_sha256}",
        f"- 旧调度租约：{report.lease.get('state')}（持有者 {report.lease.get('holder')}，"
        f"到期 {report.lease.get('expires_at')}，核对于 {report.lease.get('checked_at')}）",
        f"- 任务：{summary['tasks']}；本次写入 {summary['imported']}，"
        f"未变化 {summary['unchanged']}",
        "",
        "## 分类（DN005 §3）",
        "",
        "| 导入后状态 | 数量 | 可 resume |",
        "| --- | --- | --- |",
    ]
    for status, count in sorted(summary["classes"].items()):
        lines.append(f"| {status} | {count} | {'否' if status in READ_ONLY else '是'} |")
    lines += [
        "",
        "## 可 resume 的任务",
        "",
        "| 任务 | 原状态 | 导入后 | 标题 |",
        "| --- | --- | --- | --- |",
    ]
    for plan in report.planned:
        if plan.resumable:
            title = str(plan.data["title"]).replace("|", "\\|")
            lines.append(f"| {plan.task_id} | {plan.old_status} | {plan.new_status} | {title} |")
    lines += ["", "## 需要处理", ""]
    if report.conflicts:
        lines += [f"- 冲突 {c['task_id']}：{c['reason']}" for c in report.conflicts]
    if report.todo_only:
        lines.append(
            "- 只有任务书、旧账本中没有登记的任务（未导入，需要决定）："
            + "、".join(report.todo_only)
        )
    if report.missing_files:
        lines.append(f"- 无法解析的成果引用（{len(report.missing_files)}）：")
        lines += [f"  - `{m}`" for m in report.missing_files[:200]]
    if report.volume_problems:
        lines += [f"- 事件卷：{p}" for p in report.volume_problems]
    if not (report.conflicts or report.todo_only or report.missing_files or report.volume_problems):
        lines.append("- 无")
    if report.excluded:
        lines += [
            "",
            "## 按用户决定排除",
            "",
            "- 只有任务书、旧账本中没有登记，用户决定不导入、不建任务、不存档："
            + "、".join(report.excluded)
            + "（旧索引 TODO.md 原样存档在 legacy/，其中仍有它们的索引行）",
        ]
    lines += ["", "## 验证清单（DN005 §6）", "", "| 项 | 检查 | 结果 |", "| --- | --- | --- |"]
    for check in sorted(report.checks, key=lambda c: c.number):
        lines.append(f"| {check.number} | {check.title} | {check.mark()} |")
    for check in sorted(report.checks, key=lambda c: c.number):
        lines += ["", f"### {check.number}. {check.title}：{check.mark()}", ""]
        lines += [f"- {d}" for d in check.details[:200]]
    lines += [
        "",
        "## 其他",
        "",
        "- 旧系统的仓库、账号配置已原样复制到 "
        f"`{LEGACY}/config/`，没有自动并入统一配置；按需手工转换（DN003 §3.4）。",
    ]
    text = "\n".join(lines) + "\n"
    directory = home.root / LEGACY / "import"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.md").write_text(text, encoding="utf-8", newline="\n")
    (directory / "report.html").write_text(_html(text), encoding="utf-8", newline="\n")


def _html(markdown: str) -> str:
    body: list[str] = []
    in_table = False
    for line in markdown.splitlines():
        if line.startswith("|"):
            cells = [html.escape(c.strip()) for c in line.strip("|").split("|")]
            if set("".join(cells)) <= {"-", " "}:
                continue
            if not in_table:
                body.append("<table>")
                in_table = True
            body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if line.startswith("# "):
            body.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            body.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.lstrip().startswith("- "):
            body.append(f"<p>• {html.escape(line.strip()[2:])}</p>")
        elif line.strip():
            body.append(f"<p>{html.escape(line)}</p>")
    if in_table:
        body.append("</table>")
    style = (
        "body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;line-height:1.5}"
        "table{border-collapse:collapse}td{border:1px solid #ccc;padding:.2rem .5rem}"
        "@media print{body{margin:0}}"
    )
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        f"<title>导入报告</title><style>{style}</style><body>{''.join(body)}</body></html>\n"
    )
