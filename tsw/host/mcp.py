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

from tsw import __version__, lease
from tsw.config import EffectiveConfig, load_config
from tsw.errors import ErrorCode, TswError, rejected
from tsw.events import utc_now
from tsw.home import Home, open_home
from tsw.host import inbox as inbox_mod
from tsw.host.identity import Identity, resolve, within
from tsw.ledger import Ledger
from tsw.machine.base import REGISTRY, Caller
from tsw.machine.model import loads, row_dict, task
from tsw.machine.runner import run_tool
from tsw.machine.schemas import SCHEMAS
from tsw.review.run import run_round
from tsw.storage.fs import FsStorage
from tsw.tasks import task_row

# Protocol revisions this server implements (tools only, no batching), newest first. A client
# asking for another revision gets the newest one and decides whether to continue.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
EMPTY: dict[str, Any] = {"type": "object", "properties": {}}
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
        ("orchestrator", "worker", "revoked", "none"),
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
    "lease_acquire": (("orchestrator",), "领取或续期调度租约（本会话）", EMPTY),
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
        self.listed: tuple[str, str | None, str | None] | None = None  # identity at tools/list
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
        listed: list[dict[str, Any]] = []
        for name, (roles, description, schema) in HOST_TOOLS.items():
            if who.role in roles:
                listed.append({"name": name, "description": description, "inputSchema": schema})
        if who.role in ("orchestrator", "worker"):
            for name, spec in sorted(REGISTRY.tools.items()):
                if who.role in spec.roles:
                    listed.append(
                        {
                            "name": name,
                            "description": spec.description,
                            "inputSchema": SCHEMAS[name],
                        }
                    )
            if who.role == "orchestrator":
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
            return who.to_dict()
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
        out = run_tool(ledger, config.require_valid(), caller, name, dict(args))
        if name == "task_dispatch":
            self._prepare_workdir(args)
        return out

    def _prepare_workdir(self, args: Mapping[str, Any]) -> None:
        """A research task's execution directory lives under ``work/`` and is created here."""
        workdir = Path(str(args.get("workdir") or "")).expanduser()
        if not workdir.is_absolute():
            workdir = self.home.root / workdir
        if within(workdir, self.home.root / "work"):
            workdir.mkdir(parents=True, exist_ok=True)

    def _host_tool(
        self,
        name: str,
        args: Mapping[str, Any],
        who: Identity,
        config: EffectiveConfig,
        ledger: Ledger,
    ) -> Any:
        if name == "lease_acquire":
            command = lease.acquire(
                str(who.session_id),
                machine=self.machine,
                lease_minutes=int(config.require_valid().get("scheduling.lease_minutes")),
                actor={"role": "orchestrator", "session_id": who.session_id},
            )
            return {"expires_at": ledger.commit(command).events[-1].data["expires_at"]}
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
                return inbox_mod.attention(conn, ledger.storage, config)
            row = row_dict(conn.execute("SELECT * FROM leases WHERE name = 'scheduler'").fetchone())
            if row is None or row["holder_session"] is None:
                return {"lease": None}
            return {
                "lease": {
                    **row,
                    "expired": lease.is_expired(row, utc_now()),
                    "mine": row["holder_session"] == who.session_id,
                }
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


def _list_key(who: Identity) -> tuple[str, str | None, str | None]:
    return (who.role, who.task_id, who.attempt)


def tool_names() -> list[str]:
    return sorted(HOST_TOOLS)
