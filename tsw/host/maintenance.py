"""Data-directory maintenance the orchestrator may run through MCP (DN006 §9.7).

The user decided (2026-11-06) which host-side commands the orchestrator may run: backups, the
ledger audit, a projection rebuild and import dry runs; a real import needs the user's own words
and a dry run with the same arguments. ``restore``, ``quiesce`` / ``resume-writes`` and
``host render`` stay with the user on the command line.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator, Mapping, Sequence
from pathlib import Path
from typing import Any

from tsw import confedit
from tsw.commands import Command, CommitContext, CommitResult
from tsw.errors import rejected
from tsw.events import EventDraft, digest, utc_now
from tsw.home import Home
from tsw.host.identity import within
from tsw.importer import renumber
from tsw.importer.orchestrator_agent import run_import
from tsw.lease import require_lease
from tsw.ledger import COMMIT_LOCK, Ledger
from tsw.machine.config_tools import CONFIG_CHANNELS
from tsw.machine.guards import verified_source
from tsw.storage.fs import FsStorage

EVENT = "maintenance.performed"
FILE_LOCK_SECONDS = 3600  # copying a large history may take minutes
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
        source = verified_source(
            args.get("user_source") if isinstance(args.get("user_source"), Mapping) else None,
            ["import", IMPORT_SOURCE, "authorize"],
            channels=CONFIG_CHANNELS,
            mismatch="正式导入须附用户原话："
            'target 为 ["import", "orchestrator-agent", "authorize"]',
        )

        def authorize() -> None:
            # Runs under the import lock, before anything is imported: the dry run and the
            # user's words are checked against the ledger as it is now, then recorded.
            self.ledger.commit(
                Command(
                    actor=self.actor,
                    input={"op": "import_authorized", "params_digest": params_digest},
                    build=lambda ctx: self._authorize_import(ctx, params, params_digest, source),
                )
            )

        return run_import(
            self.home,
            _LeaseGuarded(self.ledger, self._lease_guard),
            snapshot_dir,
            dry_run=False,
            renumber_local=pairs,
            exclude=exclude,
            on_locked=authorize,
            file_guard=self._files_under_lease,
        )

    @contextlib.contextmanager
    def _files_under_lease(self) -> Generator[None, None, None]:
        """Hold the commit lock around the importer's direct file writes and check the lease
        under it: no other orchestrator can take the lease while the files are written."""
        ledger = self.ledger
        with (
            ledger.storage.lock(COMMIT_LOCK, ttl=FILE_LOCK_SECONDS),
            ledger.projection.connection() as conn,
        ):
            ledger.projection.reduce_to(conn)
            if not self.session_id:
                raise rejected("主会话工具需要会话 ID", reason="lease_missing")
            require_lease(conn, self.session_id, self.machine, utc_now())
            yield

    def _lease_guard(self, ctx: CommitContext) -> None:
        if not self.session_id:
            raise rejected("主会话工具需要会话 ID", reason="lease_missing")
        require_lease(ctx.conn, self.session_id, self.machine, ctx.now)

    def _authorize_import(
        self,
        ctx: CommitContext,
        params: Mapping[str, Any],
        params_digest: str,
        source: Mapping[str, Any],
    ) -> list[EventDraft]:
        """A matching dry run since the last completed import; words not used by a completed one."""
        self._lease_guard(ctx)
        rehearsed = False
        pending: list[tuple[str, str] | None] = []  # authorizations since the last completion
        used: set[tuple[str, str] | None] = set()
        for event in ctx.storage.read(since=0):
            if event.type == "import.completed":
                rehearsed = False
                used.update(pending)
                pending = []
            elif event.type == EVENT and event.data.get("params_digest") == params_digest:
                if event.data.get("kind") == "import_dry_run":
                    rehearsed = True
            if event.type == EVENT and event.data.get("kind") == "import_authorized":
                pending.append(confedit.source_ref(event.data.get("user_source")))
        if not rehearsed:
            raise rejected(
                "正式导入前须先用同样的参数演练（dry_run）",
                reason="dry_run_required",
                params_digest=params_digest,
            )
        if confedit.source_ref(source) in used:
            raise rejected("这条用户原话已经授权过一次导入", reason="source_reused")
        return [
            EventDraft(
                type=EVENT,
                data={
                    "kind": "import_authorized",
                    "params": dict(params),
                    "params_digest": params_digest,
                    "user_source": dict(source),
                },
            )
        ]


class _LeaseGuarded(Ledger):
    """The same ledger, with the caller's lease checked inside every commit (DN006 §9.7)."""

    def __init__(self, inner: Ledger, guard: Callable[[CommitContext], None]) -> None:  # pyright: ignore[reportMissingSuperCall]
        self.__dict__.update(inner.__dict__)
        self._guard = guard

    def commit(self, command: Command) -> CommitResult:
        build = command.build

        def guarded(ctx: CommitContext) -> Sequence[EventDraft]:
            self._guard(ctx)
            return build(ctx)

        command.build = guarded
        return super().commit(command)


def _strings(args: Mapping[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if value is None:
        return []
    values: list[Any] = list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in values):
        raise rejected("字段应为字符串数组", reason="invalid_input", field=key)
    return [str(v) for v in values]
