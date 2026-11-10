# JSON payloads from the host are walked generically; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""The tsw MCP server (DN001 §7): stdio, newline-delimited JSON-RPC 2.0, standard library only.

Each session starts its own server process. The tools a session sees depend on its role
(:mod:`tsw.host.identity`), resolved again on every call so that a session bound during the
conversation gets the worker tools at once. Write tools go through the same registry and runner
as ``tsw tool run``; the server adds no rules of its own beyond role and task scoping.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any

from tsw import __version__, confedit, lease
from tsw.config import EffectiveConfig, lease_minutes, load_config
from tsw.errors import ErrorCode, TswError, rejected
from tsw.events import utc_now
from tsw.home import Home, open_home, workdir_path
from tsw.host import inbox as inbox_mod
from tsw.host.claude_code import clear_worker_settings
from tsw.host.identity import Identity, resolve, within
from tsw.host.maintenance import Maintenance
from tsw.ledger import Ledger
from tsw.machine import routine as routine_mod
from tsw.machine.base import REGISTRY, Caller
from tsw.machine.config_tools import CONFIG_TOOLS
from tsw.machine.model import loads, row_dict, task
from tsw.machine.runner import run_tool
from tsw.machine.schemas import SCHEMAS, SOURCE
from tsw.review.run import run_round
from tsw.storage.fs import FsStorage, atomic_write
from tsw.tasks import task_row

# Protocol revisions this server implements (tools only, no batching), newest first. A client
# asking for another revision gets the newest one and decides whether to continue.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
EMPTY: dict[str, Any] = {"type": "object", "properties": {}}
# What a listing depends on: role, pending worker, task, attempt.
ListKey = tuple[str, bool, str | None, str | None]
INVALID_REQUEST = -32600
POLL_ENV = "TSW_MCP_POLL_SECONDS"
POLL_SECONDS = 2.0  # how often an idle session checks whether its role changed
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Read and lease tools that only the MCP surface offers; the state-machine tools come from the
# registry. Keys are tool names, values (roles, description, input schema).
HOST_TOOLS: dict[str, tuple[tuple[str, ...], str, dict[str, Any]]] = {
    "whoami": (
        ("orchestrator", "worker", "routine", "revoked", "none"),
        "调用者的角色、任务、attempt 与可写路径",
        EMPTY,
    ),
    "task_read": (
        ("orchestrator", "worker"),
        "读取任务（worker 只能读本任务）",
        {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    ),
    "task_list": (("orchestrator",), "列出任务", EMPTY),
    "inbox": (("orchestrator",), "未处理的可回传事件（worker 提交、提问、预算用完等）", EMPTY),
    "inbox_ack": (
        ("orchestrator",),
        "确认已处理的可回传事件",
        {
            "type": "object",
            "properties": {"sequences": {"type": "array", "items": {"type": "integer"}}},
            "required": ["sequences"],
        },
    ),
    "attention": (
        ("orchestrator",),
        "只读自检：待判断、待答复、需对账、需停止、stale、待启动、待核验、暂停",
        EMPTY,
    ),
    "lease_status": (("orchestrator",), "调度租约状态", EMPTY),
    "routine_board": (
        ("routine",),
        "例行表中这次要执行的例行项与维护动作（DN006 §8.9.3）：逐行执行，原样转发结果",
        EMPTY,
    ),
    "lease_acquire": (
        ("orchestrator",),
        "领取或续期调度租约（本会话）；让出过租约的会话只在用户要求时调用",
        EMPTY,
    ),
    "lease_release": (
        ("orchestrator",),
        "让出本会话持有的调度租约（用户要求换主会话时）：另一个主会话随后可直接领取，"
        "本会话之后只读运行，会话开始时不再自动领回（DN001 §6.4）",
        EMPTY,
    ),
    "config_show": (
        ("orchestrator",),
        "有效配置、每个值的来源、校验问题，以及配置文件的 sha256"
        "（config_change 的 user_source 要用）",
        EMPTY,
    ),
    "config_history": (
        ("orchestrator",),
        "配置修改记录；给出 change_id 时返回该次修改前后的全文",
        {"type": "object", "properties": {"change_id": {"type": "string"}}},
    ),
    "home_backup": (
        ("orchestrator",),
        "在线备份数据目录到外部的新目录或空目录（tsw backup）",
        {
            "type": "object",
            "properties": {"dest": {"type": "string", "description": "数据目录之外的绝对路径"}},
            "required": ["dest"],
        },
    ),
    "ledger_audit": (("orchestrator",), "全量审计账本摘要链与投影（只读）", EMPTY),
    "ledger_rebuild": (
        ("orchestrator",),
        "先全量审计，再从账本重建投影与派生文件（tsw ledger rebuild）",
        EMPTY,
    ),
    "import_orchestrator_agent": (
        ("orchestrator",),
        "导入 orchestrator-agent 历史（DN005）；dry_run 只演练，正式导入须先同参数演练并附用户原话",
        {
            "type": "object",
            "properties": {
                "snapshot": {"type": "string", "description": "export-snapshot 写出的快照目录"},
                "dry_run": {"type": "boolean"},
                "renumber_local": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "user_source": {
                    **SOURCE,
                    "description": "正式导入必填；target 为 "
                    '["import", "orchestrator-agent", "authorize"]',
                },
            },
            "required": ["snapshot"],
        },
    ),
    "review_run": (
        ("worker",),
        "运行一轮自评审（DN004）：记录 review_start，调用配置的评审器，"
        "按输出契约记录 review_complete 或 review_fail；可能需要数分钟到半小时",
        {
            "type": "object",
            "properties": {
                "head": {"type": "string", "description": "评审的 HEAD；git 执行目录默认取 HEAD"},
                "base": {"type": "string", "description": "评审范围的起点（base..HEAD）"},
                "notes": {"type": "string", "description": "已执行的验证命令与结果摘要等说明"},
            },
        },
    ),
}


class Server:
    def __init__(self, home: Home, env: Mapping[str, str], cwd: Path) -> None:
        self.home = home
        self.env = dict(env)
        self.cwd = cwd
        self.machine = lease.this_machine()
        self.listed: ListKey | None = None  # identity at tools/list
        self._listed_lock = threading.Lock()
        self._marker: tuple[tuple[str, int, int], ...] | None = None

    # ------------------------------------------------------------------ context

    def _config(self) -> EffectiveConfig:
        return load_config(self.home)

    def _ledger(self, config: EffectiveConfig) -> Ledger:
        if not config.valid:
            return Ledger(self.home, machine=self.machine)
        storage = FsStorage(
            self.home, segment_max_bytes=int(config.get("ledger.segment_max_mb")) << 20
        )
        return Ledger(
            self.home,
            storage,
            checkpoint_every=int(config.get("ledger.checkpoint_every")),
            machine=self.machine,
        )

    def identity(self) -> Identity:
        with Ledger(self.home, machine=self.machine).view() as conn:
            return resolve(conn, self.home, self.env, self.cwd)

    def tools(self, who: Identity) -> list[dict[str, Any]]:
        # A session started for an attempt that is not bound yet sees the worker's tools from
        # its first listing (hosts may never ask again); calls still wait for the binding.
        role = (who.pending_role or "worker") if who.pending else who.role
        listed: list[dict[str, Any]] = []
        for name, (roles, description, schema) in HOST_TOOLS.items():
            if role in roles:
                listed.append({"name": name, "description": description, "inputSchema": schema})
        if role in ("orchestrator", "worker", "routine"):
            for name, spec in sorted(REGISTRY.tools.items()):
                if role in spec.roles:
                    listed.append(
                        {
                            "name": name,
                            "description": spec.description,
                            "inputSchema": SCHEMAS[name],
                        }
                    )
            if role == "orchestrator":
                listed.append(
                    {
                        "name": "sync",
                        "description": "系统规则：依赖传播与自动归档",
                        "inputSchema": SCHEMAS["sync"],
                    }
                )
        return listed

    # ------------------------------------------------------------------ calls

    def call(self, name: str, args: Mapping[str, Any]) -> Any:
        who = self.identity()
        names = {tool["name"] for tool in self.tools(who)}
        if name not in names:
            raise rejected("当前角色不能使用该工具", reason="role_denied", role=who.role, tool=name)
        if name == "whoami":
            with Ledger(self.home, machine=self.machine).view() as conn:
                # Where a worker or the routine session sends its wake-up message (DN001 §6.3).
                target = lease.orchestrator_session(conn)
            return {**who.to_dict(), "orchestrator_session": target}
        if who.pending and who.pending_role == "routine":
            raise rejected(
                "例行会话还没有绑定：等主会话完成绑定（routine_bind）后再调用",
                reason="not_bound",
                generation=who.attempt,
            )
        if who.pending:
            raise rejected(
                "本会话还没有绑定到 attempt：等主会话完成绑定（attempt_reconcile）后再调用",
                reason="not_bound",
                task_id=who.task_id,
                attempt=who.attempt,
            )
        config = self._config()
        ledger = self._ledger(config)
        if name == "review_run":
            return run_round(
                self.home,
                self.env,
                self.cwd,
                head=_opt_str(args, "head"),
                base=_opt_str(args, "base"),
                notes=_opt_str(args, "notes"),
                machine=self.machine,
            )
        if name in HOST_TOOLS:
            return self._host_tool(name, args, who, config, ledger)
        if who.role == "worker":
            if not who.cwd_consistent:
                raise rejected(
                    "会话目录与 attempt 的执行目录不一致，拒绝写工具", reason="cwd_mismatch"
                )
            args = {"task_id": who.task_id, **args}
            if args["task_id"] != who.task_id:
                raise rejected(
                    "worker 只能操作自己的任务", reason="task_scope", task_id=args["task_id"]
                )
        caller = Caller(role=who.role, session_id=who.session_id, machine=self.machine)
        # Configuration tools must be able to repair an invalid configuration (DN003 §7.1).
        usable = config if name in CONFIG_TOOLS else config.require_valid()
        out = run_tool(ledger, usable, caller, name, dict(args))
        if who.role == "worker":
            wake = self._wake(out, ledger, usable)
            if wake is not None:
                out["wake"] = wake
        if name == "task_dispatch":
            self._prepare_workdir(args)
        if name == "attempt_start":
            self._session_dir(out, ledger)
        if name == "routine_start" and out.get("events"):
            # The app refuses a cwd spelled differently from the folder on disk.
            out["cwd"] = os.path.realpath(self.home.root / routine_mod.WORKDIR)
        if name == "routine_run":
            job_id = str(args.get("job_id") or "")
            out["forward"] = self._forward(job_id, out, ledger)
            if job_id == routine_mod.TEAMS_REPLIES:
                alert = self._note_fetch(args, ledger)
                if alert is not None and not out["forward"].get("send"):
                    out["forward"] = alert
            with ledger.view() as conn:
                following = routine_mod.run_next(
                    conn, job_id, args, str(config.get("plugins.host_integration"))
                )
            if following is not None:
                out["next"] = following
        return out

    @staticmethod
    def _wake(
        out: Mapping[str, Any], ledger: Ledger, config: EffectiveConfig
    ) -> dict[str, Any] | None:
        """The message that wakes the orchestrator after a worker recorded a reportable event:
        sent to whichever session holds the scheduling lease, not to the session that started
        the worker, which may have handed over since (DN001 §6.3, T254)."""
        found = [*(out.get("events") or []), *((out.get("sync") or {}).get("events") or [])]
        wanted = inbox_mod.reportable(config)
        reported = [e for e in found if e.get("type") in wanted]
        if not reported:
            return None
        with ledger.view() as conn:
            target = lease.orchestrator_session(conn)
        if target is None:
            return {
                "send": False,
                "note": "没有主会话持有调度租约：回传已在账本中，下一个主会话领取租约后处理",
            }
        items = "、".join(
            f"{e.get('task_id') or ''} {e['type']}（事件 {e['sequence']}）".strip()
            for e in reported
        )
        return {
            "send": True,
            "session_id": target,
            "message": f"TaskSteward 回传：{items}。调用 inbox 处理。",
        }

    def _note_fetch(self, args: Mapping[str, Any], ledger: Ledger) -> dict[str, Any] | None:
        """Count a failed or good Teams read in the routine cache; the forward that tells the
        orchestrator once a chat could not be read several times in a row (T253)."""
        chat_id = str(args.get("chat_id") or "")
        failure = routine_mod.fetch_failure(args.get("fetched"))
        kept = routine_mod.load_cache(self.home)
        now = utc_now()
        entry = routine_mod.note_fetch(kept, chat_id, failure, now)
        try:
            self.home.cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write(self.home.cache_dir / "routine.json", json.dumps(kept).encode("utf-8"))
        except OSError:  # a lost count only delays the message
            pass
        if entry is None:
            return None
        with ledger.view() as conn:
            message = routine_mod.fetch_alert_text(conn, chat_id, entry)
            target = routine_mod.orchestrator_session(conn, now)
        if target is None:
            return {"send": False, "note": "主会话不在线：attention 的 routine 中列出了读取失败"}
        return {"send": True, "session_id": target, "message": message}

    def _forward(self, job_id: str, out: Mapping[str, Any], ledger: Ledger) -> dict[str, Any]:
        """What the routine session passes on to the orchestrator, word for word."""
        with ledger.view() as conn:
            message = routine_mod.forward_text(conn, job_id, out.get("events") or [])
            target = routine_mod.orchestrator_session(conn, utc_now())
        if not message:
            return {"send": False}
        if target is None:
            return {"send": False, "note": "主会话不在线：结果已在账本中，主会话下次运行时会看到"}
        return {"send": True, "session_id": target, "message": message}

    def _routine_board(self, who: Identity, config: EffectiveConfig, ledger: Ledger) -> Any:
        cache_path = self.home.cache_dir / "routine.json"
        with ledger.view() as conn:
            result, kept = routine_mod.board(
                conn, config, utc_now(), routine_mod.load_cache(self.home), who.session_id
            )
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(cache_path, json.dumps(kept).encode("utf-8"))
        except OSError:  # a lost run time only makes the next run arm once more
            pass
        return result

    def _prepare_workdir(self, args: Mapping[str, Any]) -> None:
        """A research task's execution directory lives under ``work/`` and is created here."""
        workdir = workdir_path(self.home, str(args.get("workdir") or ""))
        if within(workdir, self.home.root / "work"):
            workdir.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, out: Any, ledger: Ledger) -> None:
        """Before the worker's session is created, ready its execution directory (CC008 §6).

        The directory gets no permission rules of its own: a folder with its own rules asks for
        trust again even under the trusted data directory (decision 0003). A settings file that
        holds only the rules an earlier tsw wrote is removed; anything else in it is the user's.
        Only directories under ``work/`` are touched: one elsewhere (a git worktree of the
        repository) is not tsw's to write into.
        """
        events = out.get("events") if isinstance(out, dict) else None
        if not events:
            return  # a duplicate call: the first one readied the directory
        task_id, attempt_id = events[0].get("task_id"), events[0]["data"].get("attempt")
        with ledger.view() as conn:
            attempt = row_dict(
                conn.execute(
                    "SELECT workdir FROM attempts WHERE task_id = ? AND attempt = ?",
                    (task_id, attempt_id),
                ).fetchone()
            )
        recorded = (attempt or {}).get("workdir") or f"work/{task_id}/{attempt_id}"
        workdir = workdir_path(self.home, str(recorded))
        # attempt_start has created the directory (wherever it is).
        if within(workdir, self.home.root / "work") and workdir.is_dir():
            clear_worker_settings(workdir)
        # A repository task's session is created in the worktree attempt_start made inside the
        # execution directory, so the host loads the repository's own configuration (CC006 §8).
        worktree = events[0]["data"].get("worktree") or {}
        session_dir = Path(str(worktree["path"])) if worktree.get("path") else workdir
        if session_dir.is_dir():
            # The app refuses a cwd spelled differently from the folder on disk (``A1`` for
            # ``a1``): hand back the real spelling for start_session.
            out["cwd"] = os.path.realpath(session_dir)

    def _host_tool(
        self,
        name: str,
        args: Mapping[str, Any],
        who: Identity,
        config: EffectiveConfig,
        ledger: Ledger,
    ) -> Any:
        if name == "routine_board":
            return self._routine_board(who, config.require_valid(), ledger)
        if name == "lease_acquire":
            command = lease.acquire(
                str(who.session_id),
                machine=self.machine,
                lease_minutes=lease_minutes(config),
                actor={"role": "orchestrator", "session_id": who.session_id},
            )
            return {"expires_at": ledger.commit(command).events[-1].data["expires_at"]}
        if name == "lease_release":
            ledger.commit(
                lease.release(
                    str(who.session_id),
                    machine=self.machine,
                    actor={"role": "orchestrator", "session_id": who.session_id},
                )
            )
            return {
                "released": True,
                "stood_down": True,
                "next": "另一个主会话现在可以领取（会话开始时自动，或 lease_acquire）；本会话"
                "只读运行，不再派发、验收或处理回传。用户要求本会话重新担任主会话时调用 "
                "lease_acquire。",
            }
        maintenance = Maintenance(self.home, ledger, who.session_id, self.machine)
        if name == "home_backup":
            return maintenance.backup(str(args.get("dest") or ""))
        if name == "ledger_audit":
            return maintenance.audit()
        if name == "ledger_rebuild":
            config.require_valid()
            return maintenance.rebuild()
        if name == "import_orchestrator_agent":
            config.require_valid()
            return maintenance.import_orchestrator_agent(args)
        if name == "config_show":
            current = confedit.read_text(self.home)
            return {
                "valid": config.valid,
                "values": config.values,
                "sources": config.sources,
                "problems": config.problems,
                "warnings": config.warnings,
                "file_sha256": confedit.text_sha(current),
            }
        if name == "config_history":
            change_id = _opt_str(args, "change_id")
            if change_id:
                event = confedit.find_change(ledger.storage, change_id)
                return {
                    **confedit.summary(event),
                    "before": event.data.get("before"),
                    "after": event.data.get("after"),
                    "ops": event.data.get("ops"),
                }
            return [confedit.summary(e) for e in confedit.change_events(ledger.storage)]
        if name == "inbox_ack":
            sequences = args.get("sequences")
            if not isinstance(sequences, list):
                raise rejected("sequences 应为数组", reason="invalid_input")
            result = ledger.commit(
                inbox_mod.ack(
                    sequences,
                    actor={"role": "orchestrator", "session_id": who.session_id},
                    config=config.require_valid(),
                    session_id=who.session_id,
                    machine=self.machine,
                )
            )
            return {"acked": [e.data["sequences"] for e in result.events]}
        with ledger.view() as conn:
            if name == "task_read":
                task_id = str(args.get("task_id") or who.task_id)
                if who.role == "worker" and task_id not in self._readable(conn, who):
                    raise rejected("不在授权读取集合中", reason="read_scope", task_id=task_id)
                task(conn, task_id)
                return task_row(conn, task_id)
            if name == "task_list":
                return [
                    row_dict(r)
                    for r in conn.execute(
                        "SELECT task_id, title, repo, kind, status, revision, version "
                        "FROM tasks ORDER BY registered_seq"
                    )
                ]
            if name == "inbox":
                return inbox_mod.inbox(conn, ledger.storage, config)
            if name == "attention":
                return inbox_mod.attention(
                    conn, ledger.storage, config, routine_mod.load_cache(self.home)
                )
            # Whether this session gave the lease up and should not take it back by itself.
            stood_down = lease.stood_down(conn, who.session_id)
            row = row_dict(conn.execute("SELECT * FROM leases WHERE name = 'scheduler'").fetchone())
            if row is None or row["holder_session"] is None:
                return {"lease": None, "stood_down": stood_down}
            return {
                "lease": {
                    **row,
                    "expired": lease.is_expired(row, utc_now()),
                    "mine": row["holder_session"] == who.session_id,
                },
                "stood_down": stood_down,
            }

    @staticmethod
    def _readable(conn: Any, who: Identity) -> set[str]:
        row = conn.execute(
            "SELECT read_set FROM attempts WHERE task_id = ? AND attempt = ?",
            (who.task_id, who.attempt),
        ).fetchone()
        return {str(who.task_id), *loads(row["read_set"] if row else None, [])}

    # ------------------------------------------------------------------ protocol

    def handle(self, message: Any) -> list[dict[str, Any]]:
        """What to send back for one incoming message: a response and/or notifications."""
        problem = envelope_problem(message)
        if problem is not None:
            mid = message.get("id") if isinstance(message, dict) else None
            return [_error(mid if _valid_id(mid) else None, INVALID_REQUEST, problem)]
        if "method" not in message:
            return []  # a response to something we never send
        if "id" not in message:
            return self.list_changes()  # a notification
        method, mid = message["method"], message["id"]
        params = message.get("params", {})
        try:
            if method == "initialize":
                if not isinstance(params, dict):
                    return [_error(mid, INVALID_PARAMS, "initialize 的 params 必须是对象")]
                requested = params.get("protocolVersion")
                result: Any = {
                    "protocolVersion": (
                        requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                    ),
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "tsw", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                with self._listed_lock:  # one snapshot for the list and the poll's marker
                    marker = ledger_marker(self.home)
                    who = self.identity()  # on failure neither the list nor the marker moves
                    self.listed, self._marker = _list_key(who), marker
                result = {"tools": self.tools(who)}
            elif method == "tools/call":
                if (
                    not isinstance(params, dict)
                    or not isinstance(params.get("name"), str)
                    or not isinstance(params.get("arguments", {}), dict)
                ):
                    return [
                        _error(mid, INVALID_PARAMS, "tools/call 需要字符串 name 与对象 arguments")
                    ]
                result = self._tool_result(params["name"], params.get("arguments") or {})
            else:
                return [_error(mid, METHOD_NOT_FOUND, f"method not found: {method}")]
        except Exception as exc:  # protocol-level failure; tool errors are reported as results
            return [_error(mid, INTERNAL_ERROR, str(exc))]
        return [{"jsonrpc": "2.0", "id": mid, "result": result}, *self.list_changes()]

    def list_changes(self) -> list[dict[str, Any]]:
        """``notifications/tools/list_changed`` once the caller's role or binding has changed.

        Called after every message and, by :func:`serve`, periodically while the client is idle:
        a session is usually bound by the orchestrator while it is waiting, not while it talks.
        """
        with self._listed_lock:
            if self.listed is None:
                return []
            # Identity only changes with the ledger: compare file sizes and times first, so an
            # idle session neither reads the ledger nor takes the commit lock.
            marker = ledger_marker(self.home)
            if marker == self._marker:
                return []
            try:
                current = _list_key(self.identity())
            except Exception:  # an unreadable ledger is reported by the next call, not here
                return []
            self._marker = marker
            if current == self.listed:
                return []
            self.listed = current
        return [{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]

    def _tool_result(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            payload = self.call(name, args)
        except TswError as exc:
            return {
                "content": [
                    {"type": "text", "text": json.dumps(exc.to_dict(), ensure_ascii=False)}
                ],
                "isError": True,
            }
        except Exception as exc:  # an unexpected failure is reported to the model, not raised
            error = TswError(ErrorCode.UNKNOWN, "工具执行失败", error=str(exc)).to_dict()
            return {
                "content": [{"type": "text", "text": json.dumps(error, ensure_ascii=False)}],
                "isError": True,
            }
        return {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}
            ]
        }


def serve(
    home_root: Path,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    opener: Callable[[Path], Home] = open_home,
    poll_seconds: float | None = None,
) -> int:
    environ = env if env is not None else os.environ
    server = Server(opener(home_root), environ, cwd or Path.cwd())
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    interval = poll_seconds or float(environ.get(POLL_ENV) or POLL_SECONDS)
    lock = threading.Lock()
    stop = threading.Event()

    def emit(items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with lock:
            for item in items:
                writer.write(json.dumps(item, ensure_ascii=False) + "\n")
            writer.flush()

    def watch() -> None:
        while not stop.wait(interval):
            emit(server.list_changes())

    threading.Thread(target=watch, name="tsw-mcp-watch", daemon=True).start()
    try:
        for raw in reader:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                emit([_error(None, -32700, "parse error")])
            else:
                emit(server.handle(message))
    finally:
        stop.set()
    return 0


def ledger_marker(home: Home) -> tuple[tuple[str, int, int], ...]:
    """Names, sizes and modification times of the ledger segments: changes with every commit."""
    found: list[tuple[str, int, int]] = []
    try:
        with os.scandir(home.ledger_dir) as entries:
            for entry in entries:
                if entry.is_file():
                    stat = entry.stat()
                    found.append((entry.name, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return ()
    return tuple(sorted(found))


def _opt_str(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise rejected(f"{key} 应为字符串", reason="invalid_input", field=key)
    return value or None


def _valid_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def envelope_problem(message: Any) -> str | None:
    """Why a message is not a valid JSON-RPC 2.0 request, notification or response."""
    if not isinstance(message, dict):
        return "消息必须是 JSON 对象（不支持批量请求）"
    if message.get("jsonrpc") != "2.0":
        return 'jsonrpc 必须是 "2.0"'
    if "method" in message:
        if not isinstance(message["method"], str):
            return "method 必须是字符串"
        if "id" in message and not _valid_id(message["id"]):
            return "id 必须是字符串或整数"
        if "params" in message and not isinstance(message["params"], (dict, list)):
            return "params 必须是对象或数组"
        return None
    if _valid_id(message.get("id")) and ("result" in message or "error" in message):
        return None
    return "缺少 method"


def _error(mid: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}


def _list_key(who: Identity) -> ListKey:
    """What the listed tools depend on: the role, whether it is a pending worker, the attempt."""
    return (who.role, who.pending, who.task_id, who.attempt)


def tool_names() -> list[str]:
    return sorted(HOST_TOOLS)
