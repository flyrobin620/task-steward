"""The Dashboard app: ``tsw status serve``, also ``tsw dashboard`` (DN006 §8.10).

A small HTTP server on the loopback address, started by the user in a terminal. Every page load
renders the Dashboard page from the ledger's projection with the user's direct actions as buttons;
a button runs :func:`tsw.direct.perform` on the ``status-app`` channel, the same implementation as
``tsw task accept``. Each task has its own read-only view at ``/tasks/<task>``
(:mod:`tsw.host.task_view`). Report links on the page are served read-only from ``runs/`` and
``legacy/``.

Requests are admitted only with this run's token (DN006 §8.10, DN008 §9): it is printed in the
terminal and put in the address the browser is opened with; the first visit trades it for an
HttpOnly, SameSite=Strict cookie and drops it from the address. A button request must also carry
the token in a header, and ``Host`` and ``Origin`` must name this server, so neither another page
nor a rebound host name can press a button.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterable, Mapping
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from tsw import direct
from tsw.config import load_config
from tsw.errors import TswError, rejected
from tsw.events import Event
from tsw.home import Home
from tsw.host.status import AppView, Revision, render
from tsw.host.task_view import not_found, render_task, timeline
from tsw.ledger import Ledger
from tsw.storage.fs import FsStorage

# The cookie is named per port: browsers share cookies across the ports of one host.
COOKIE = "tsw_status"
TOKEN_HEADER = "X-TSW-Token"
# Files a page may link to (``tsw.host.status.link_for``), served read-only.
SERVED_DIRS = ("runs", "legacy")
MAX_BODY = 16 * 1024
# Served documents (reports) keep their own images and styles but run no script in this origin.
SERVED_CSP = (
    "default-src 'self'; script-src 'none'; object-src 'none'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
)


def _strings(value: Any) -> tuple[str, ...]:
    items: list[Any] = value if isinstance(value, list) else []  # pyright: ignore[reportUnknownVariableType]
    return tuple(str(item) for item in items)


def history(events: Iterable[Event]) -> dict[str, list[Revision]]:
    """Every revision of every task's requirement, oldest first, read from the ledger.

    A revision starts with the registration or an import (DN005: the imported revision is where
    the history here begins), a requirement change (with the user's words) or a rejection (T23:
    the requirement stays, the user's reason is recorded). A renumbered task (``task.renumbered``)
    takes its history to the new number; the old number starts afresh if it is used again.
    """
    out: dict[str, list[Revision]] = {}
    for event in events:
        data: Mapping[str, Any] = event.data
        task_id = event.task_id
        if task_id is None:
            continue
        if event.type == "task.renumbered":
            out[str(data["to"])] = out.pop(str(data["from"]), [])
            continue
        past = out.setdefault(task_id, [])
        if event.type in ("task.registered", "import.task_imported"):
            past.clear()  # a new task under this number, or an import replacing the snapshot
            how = "登记" if event.type == "task.registered" else "导入"
            revision, words = event.revision or 1, ""
            goal, acceptance = str(data.get("goal") or ""), _strings(data.get("acceptance"))
        elif event.type == "task.requirement_changed":
            source: Mapping[str, Any] = data.get("user_source") or {}
            how, revision = "需求变更", int(data["revision"])
            words = str(source.get("text") or "")
            goal, acceptance = str(data.get("goal") or ""), _strings(data.get("acceptance"))
        elif event.type == "task.user_rejected" and past:
            how, revision = "用户退回", int(data["revision"])
            words = str(data.get("reason") or "")
            goal, acceptance = past[-1].goal, past[-1].acceptance
        else:
            continue
        past.append(Revision(revision, event.time, how, goal, acceptance, words))
    return {task_id: past for task_id, past in out.items() if past}


class StatusApp:
    """The state shared by the request handlers of one ``tsw status serve`` run."""

    def __init__(self, home: Home, token: str | None = None) -> None:
        self.home = home
        self.token = token or secrets.token_urlsafe(24)
        self.instance = secrets.token_hex(4)
        self.port = 0
        self._count = 0
        self._lock = threading.Lock()

    def next_ref(self) -> str:
        with self._lock:
            self._count += 1
            return f"status-app:{self.instance}:{self._count}"

    def ledger(self) -> tuple[Ledger, Any]:
        config = load_config(self.home)
        if not config.valid:
            return Ledger(self.home), config
        storage = FsStorage(
            self.home, segment_max_bytes=int(config.get("ledger.segment_max_mb")) << 20
        )
        ledger = Ledger(
            self.home, storage, checkpoint_every=int(config.get("ledger.checkpoint_every"))
        )
        return ledger, config

    def page(self) -> str:
        ledger, _ = self.ledger()
        with ledger.view() as conn:
            actions: dict[str, tuple[tuple[str, ...], str | None]] = {}
            for row in conn.execute("SELECT * FROM tasks"):
                found = direct.availability(conn, row)
                actions[str(row["task_id"])] = (found.actions, found.cancel_blocked)
            past = history(ledger.storage.read(since=0))
            return render(conn, AppView(token=self.token, actions=actions, history=past))

    def task_page(self, task_id: str) -> tuple[bool, str]:
        """The task's view (DN006 §8.10); False with the not-found page when there is no task."""
        ledger, _ = self.ledger()
        with ledger.view() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            actions: dict[str, tuple[tuple[str, ...], str | None]] = {}
            if row is not None:
                found = direct.availability(conn, row)
                actions[task_id] = (found.actions, found.cancel_blocked)
            events = list(ledger.storage.read(since=0))
            view = AppView(token=self.token, actions=actions, history=history(events))
            page = render_task(conn, task_id, view, timeline(events).get(task_id, []))
            if page is None:
                return False, not_found(task_id, view)
            return True, page

    def act(self, task_id: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        revision = body.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise rejected("缺少页面上显示的 revision", reason="invalid_input")
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise rejected("理由应为文本", reason="invalid_input")
        label = direct.ACTIONS[action][2] if action in direct.ACTIONS else action
        text = f"在 Dashboard 页面点击「{label}」：{task_id} revision {revision}" + (
            f"；理由：{reason}" if reason else ""
        )
        ledger, config = self.ledger()
        config.require_valid()
        return direct.perform(
            ledger,
            config,
            action,
            task_id,
            channel="status-app",
            message_ref=self.next_ref(),
            text=text,
            revision=revision,
            reason=reason,
        )


def _handler(app: StatusApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tsw-status"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            return  # quiet: the terminal shows only the address and errors

        # ------------------------------------------------------------ admission

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin.lower() in (
                f"http://127.0.0.1:{app.port}",
                f"http://localhost:{app.port}",
            )

        def _cookie_ok(self) -> bool:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie") or "")
            except CookieError:  # a malformed cookie header is just not admitted
                return False
            morsel = cookie.get(f"{COOKIE}_{app.port}")
            return morsel is not None and hmac.compare_digest(morsel.value, app.token)

        # ------------------------------------------------------------ responses

        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, status: HTTPStatus, text: str) -> None:
            self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

        def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        # ------------------------------------------------------------ GET

        def do_GET(self) -> None:
            if not self._host_ok():
                self._text(HTTPStatus.FORBIDDEN, "只接受本机地址的访问")
                return
            url = urlsplit(self.path)
            given = parse_qs(url.query).get("token", [""])[0]
            if url.path == "/" and given:
                if not hmac.compare_digest(given, app.token):
                    self._text(HTTPStatus.FORBIDDEN, "令牌无效：请打开 tsw status serve 输出的地址")
                    return
                cookie = f"{COOKIE}_{app.port}={app.token}; HttpOnly; SameSite=Strict; Path=/"
                self._send(
                    HTTPStatus.SEE_OTHER,
                    b"",
                    "text/plain; charset=utf-8",
                    {"Location": "/", "Set-Cookie": cookie},
                )
                return
            if not self._cookie_ok():
                self._text(
                    HTTPStatus.FORBIDDEN,
                    "需要令牌：请打开运行 tsw status serve 的终端中输出的地址",
                )
                return
            if url.path == "/":
                self._serve(lambda: app.page().encode("utf-8"), "text/html; charset=utf-8")
                return
            parts = url.path.split("/")
            if len(parts) == 3 and parts[1] == "tasks" and parts[2]:
                self._serve_task(unquote(parts[2]))
                return
            self._serve_file(unquote(url.path))

        def _serve(self, produce: Callable[[], bytes], content_type: str) -> None:
            try:
                body = produce()
            except TswError as exc:
                self._text(HTTPStatus.SERVICE_UNAVAILABLE, f"错误 [{exc.code.value}] {exc.message}")
                return
            self._send(HTTPStatus.OK, body, content_type)

        def _serve_task(self, task_id: str) -> None:
            try:
                found, page = app.task_page(task_id)
            except TswError as exc:
                self._text(HTTPStatus.SERVICE_UNAVAILABLE, f"错误 [{exc.code.value}] {exc.message}")
                return
            status = HTTPStatus.OK if found else HTTPStatus.NOT_FOUND
            self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")

        def _serve_file(self, path: str) -> None:
            parts = [p for p in path.split("/") if p]
            if not parts or parts[0] not in SERVED_DIRS or any(p in ("..", ".") for p in parts):
                self._text(HTTPStatus.NOT_FOUND, "没有这个页面")
                return
            root = app.home.root.resolve()
            target = root.joinpath(*parts).resolve()
            if not target.is_relative_to(root / parts[0]) or not target.is_file():
                self._text(HTTPStatus.NOT_FOUND, "没有这个文件")
                return
            # Markdown (briefs, reports' sources) is shown as text rather than downloaded.
            plain = target.suffix.lower() in (".md", ".txt", ".log")
            kind = "text/plain" if plain else mimetypes.guess_type(target.name)[0]
            kind = kind or "application/octet-stream"
            if kind.startswith("text/") or kind in ("application/json", "application/xml"):
                kind += "; charset=utf-8"
            extra = {"Content-Security-Policy": SERVED_CSP}
            self._send(HTTPStatus.OK, target.read_bytes(), kind, extra)

        # ------------------------------------------------------------ POST

        def do_POST(self) -> None:
            if not (self._host_ok() and self._origin_ok()):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "只接受本页面的请求"})
                return
            given = self.headers.get(TOKEN_HEADER) or ""
            if not (self._cookie_ok() and hmac.compare_digest(given, app.token)):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "令牌无效"})
                return
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            if len(parts) != 4 or parts[:2] != ["api", "tasks"] or parts[3] not in direct.ACTIONS:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "没有这个操作"})
                return
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "message": "需要 JSON"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请求体过大或无效"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请求体应为 JSON 对象"})
                return
            task_id, action = unquote(parts[2]), parts[3]
            try:
                result = app.act(task_id, action, body)  # pyright: ignore[reportUnknownArgumentType]
            except TswError as exc:
                self._json(
                    HTTPStatus.CONFLICT,
                    {"ok": False, "code": exc.code.value, "message": exc.message, **exc.details},
                )
                return
            print(
                f"tsw status: {result['task_id']} 已{direct.ACTIONS[action][2]}"
                f"（账本序号 {result['sequence']}）",
                flush=True,
            )
            self._json(HTTPStatus.OK, {"ok": True, **result})

    return Handler


def make_server(app: StatusApp, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(app))
    server.daemon_threads = True
    app.port = int(server.server_address[1])
    return server


def serve_status(home: Home, *, port: int = 0, open_browser: bool = True) -> None:
    """Run the Dashboard app until Ctrl+C."""
    app = StatusApp(home)
    server = make_server(app, port)
    url = f"http://127.0.0.1:{app.port}/?token={app.token}"
    print(f"TaskSteward Dashboard：{url}", flush=True)
    print("这个地址带本次启动的令牌，只给你自己用；按 Ctrl+C 停止。", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止", file=sys.stderr)
    finally:
        server.server_close()
