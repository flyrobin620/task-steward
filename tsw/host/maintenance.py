"""Data-directory maintenance the orchestrator may run through MCP (DN006 §9.7).

The user decided (2026-11-06) which host-side commands the orchestrator may run: backups, the
ledger audit, a projection rebuild and import dry runs; a real import needs the user's own words
and a dry run with the same arguments. ``restore``, ``quiesce`` / ``resume-writes`` and
``host render`` stay with the user on the command line.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.errors import rejected
from tsw.events import EventDraft, digest, utc_now
from tsw.home import Home
from tsw.host.identity import within
from tsw.importer import renumber
from tsw.importer.orchestrator_agent import run_import
from tsw.lease import require_lease
from tsw.ledger import Ledger
from tsw.machine.config_tools import CONFIG_CHANNELS
from tsw.machine.guards import verified_source
from tsw.storage.fs import FsStorage

EVENT = "maintenance.performed"
IMPORT_SOURCE = "orchestrator-agent"


class Maintenance:
    def __init__(self, home: Home, ledger: Ledger, session_id: str | None, machine: str) -> None:
        self.home = home
        self.ledger = ledger
        self.session_id = session_id
        self.machine = machine

    @property
    def actor(self) -> dict[str, Any]:
        return {"role": "orchestrator", "session_id": self.session_id}

    def _require_lease(self) -> None:
        if not self.session_id:
            raise rejected("主会话工具需要会话 ID", reason="lease_missing")
        with self.ledger.view() as conn:
            require_lease(conn, self.session_id, self.machine, utc_now())

    def _record(self, data: Mapping[str, Any]) -> int | None:
        session_id, machine = self.session_id, self.machine

        def build(ctx: CommitContext) -> list[EventDraft]:
            assert session_id is not None
            require_lease(ctx.conn, session_id, machine, ctx.now)
            return [EventDraft(type=EVENT, data=dict(data))]

        result = self.ledger.commit(Command(actor=self.actor, input=dict(data), build=build))
        return result.last_sequence

    # ------------------------------------------------------------------ tools

    def backup(self, dest_text: str) -> dict[str, Any]:
        self._require_lease()
        dest = Path(dest_text).expanduser()
        if not dest.is_absolute():
            raise rejected("备份目标应为绝对路径", reason="invalid_input", field="dest")
        if within(dest, self.home.root) or within(self.home.root, dest):
            raise rejected("备份目标必须在数据目录之外", reason="backup_inside_home")
        manifest = FsStorage(self.home).snapshot(dest)
        out = {
            "kind": "backup",
            "dest": str(dest),
            "ledger_tail": manifest["ledger_tail"],
            "files": len(manifest["files"]),
        }
        return {**out, "sequence": self._record(out)}

    def audit(self) -> dict[str, Any]:
        return self.ledger.audit()

    def rebuild(self) -> dict[str, Any]:
        self._require_lease()
        report = self.ledger.audit()
        if not report.get("ok"):
            raise rejected("账本审计有问题，不重建投影", reason="audit_failed", audit=report)
        tail = self.ledger.rebuild()
        out = {"kind": "ledger_rebuild", "projected_seq": tail.sequence}
        return {**out, "sequence": self._record(out)}

    def import_orchestrator_agent(self, args: Mapping[str, Any]) -> dict[str, Any]:
        self._require_lease()
        snapshot = args.get("snapshot")
        if not isinstance(snapshot, str) or not snapshot.strip():
            raise rejected("需要快照目录 snapshot", reason="invalid_input", field="snapshot")
        renumber_local = _strings(args, "renumber_local")
        exclude = _strings(args, "exclude")
        snapshot_dir = Path(snapshot).expanduser().resolve()
        params: dict[str, Any] = {
            "source": IMPORT_SOURCE,
            "snapshot": str(snapshot_dir),
            "renumber_local": renumber_local,
            "exclude": exclude,
        }
        params_digest = digest(params)
        pairs = renumber.parse(renumber_local)
        if args.get("dry_run"):
            result = run_import(
                self.home,
                self.ledger,
                snapshot_dir,
                dry_run=True,
                renumber_local=pairs,
                exclude=exclude,
            )
            record = {
                "kind": "import_dry_run",
                "params": params,
                "params_digest": params_digest,
                "summary": {k: v for k, v in result.items() if isinstance(v, (int, str, bool))},
            }
            return {**result, "params_digest": params_digest, "sequence": self._record(record)}
        verified_source(
            args.get("user_source") if isinstance(args.get("user_source"), Mapping) else None,
            ["import", IMPORT_SOURCE, "authorize"],
            channels=CONFIG_CHANNELS,
            mismatch="正式导入须附用户原话："
            'target 为 ["import", "orchestrator-agent", "authorize"]',
        )
        if not self._dry_run_since_last_import(params_digest):
            raise rejected(
                "正式导入前须先用同样的参数演练（dry_run）",
                reason="dry_run_required",
                params_digest=params_digest,
            )
        return run_import(
            self.home,
            self.ledger,
            snapshot_dir,
            dry_run=False,
            renumber_local=pairs,
            exclude=exclude,
        )

    def _dry_run_since_last_import(self, params_digest: str) -> bool:
        found = False
        for event in self.ledger.storage.read(since=0):
            if event.type == "import.completed":
                found = False
            elif (
                event.type == EVENT
                and event.data.get("kind") == "import_dry_run"
                and event.data.get("params_digest") == params_digest
            ):
                found = True
        return found


def _strings(args: Mapping[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if value is None:
        return []
    values: list[Any] = list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in values):
        raise rejected("字段应为字符串数组", reason="invalid_input", field=key)
    return [str(v) for v in values]
