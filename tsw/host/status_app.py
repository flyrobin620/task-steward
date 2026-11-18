"""The Dashboard app: ``tsw status serve``, also ``tsw dashboard`` (DN006 §8.10).

A small HTTP server on the loopback address, started by the user in a terminal. Every page load
renders the Dashboard page from the ledger's projection with the user's direct actions as buttons;
a button runs :func:`tsw.direct.perform` on the ``status-app`` channel, the same implementation as
``tsw task accept``. Each task has its own read-only view at ``/tasks/<task>``
(:mod:`tsw.host.task_view`). Report links on the page are served read-only from ``runs/`` and
``legacy/``; a Markdown file asked for with ``?view=html`` comes back rendered
(:mod:`tsw.host.markdown`) for the page's document dialog (T278). The standing preferences have
their own page at ``/preferences`` (:mod:`tsw.host.preference_view`), whose forms record, replace
and revoke them as the user on the same channel, the same implementation as ``tsw preference
record`` / ``revoke`` (DN007 §6.2, T290). An embedding portal asks ``POST /workdeck/labels``
which of its sessions are this data directory's workers, orchestrators and routine sessions
(:mod:`tsw.host.session_labels`, T297).

Requests are admitted only with this run's token (DN006 §8.10, DN008 §9; the hosted service
checks tokens against the hashes ``tsw dashboard open`` stored, T283): it is printed in the
terminal and put in the address the browser is opened with; the first visit trades it for an
HttpOnly, SameSite=Strict cookie and drops it from the address. A button request must also carry
the token in a header, and ``Host`` and ``Origin`` must name this server, so neither another page
nor a rebound host name can press a button. The label query only reads, so it needs the cookie
and ``Host`` but not the header token, which an embedding portal's proxy does not add.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import secrets
import sqlite3
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
from tsw.context.index import index_path, read_card
from tsw.errors import TswError, rejected
from tsw.events import Event, utc_now
from tsw.home import Home
from tsw.host.markdown import MAX_SOURCE_CHARS, render_markdown
from tsw.host.preference_view import parse_filter, render_preferences
from tsw.host.session_labels import LABELS_PATH, MAX_LABELS_BODY, answer, requested_ids
from tsw.host.status import AppView, Revision, render
from tsw.host.task_view import card_state, not_found, render_task, timeline
from tsw.ledger import Ledger
from tsw.storage.fs import FsStorage

HEALTH_PATH = "/healthz"
# The cookie is named per port: browsers share cookies across the ports of one host.
COOKIE = "tsw_status"
TOKEN_HEADER = "X-TSW-Token"
# Files a page may link to (``tsw.host.status.link_for``), served read-only.
SERVED_DIRS = ("runs", "legacy")
MAX_BODY = 16 * 1024
# The preference page's retries (command_id) remembered by one server run.
REMEMBERED_COMMANDS = 256
COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{8,80}")
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

    def __init__(
        self,
        home: Home,
        token: str | None = None,
        *,
        cookie_max_age: int | None = None,
        health: Mapping[str, Any] | None = None,
        verify: Callable[[str], bool] | None = None,
    ) -> None:
        self.home = home
        self.token = token or secrets.token_urlsafe(24)
        # The hosted service (T283, CC006 §12): tokens checked against the stored hashes (each
        # browser keeps its own), a persistent cookie, and a token-free /healthz.
        self.verify = verify
        self.cookie_max_age = cookie_max_age
        self.health = dict(health) if health is not None else None
        self.instance = secrets.token_hex(4)
        self.port = 0
        self._count = 0
        self._lock = threading.Lock()
        # The message reference given to each command_id the preference page sent, so a retry
        # carries the same source and the ledger returns the first result (PR #53 review).
        self._refs: dict[str, str] = {}

    def admits(self, token: str) -> bool:
        """Whether a presented token signs in: this run's, or one the hosted service lists."""
        if not token:
            return False
        if self.verify is not None:
            return self.verify(token)
        return hmac.compare_digest(token, self.token)

    def next_ref(self) -> str:
        with self._lock:
            self._count += 1
            return f"status-app:{self.instance}:{self._count}"

    def ref_for(self, command_id: str | None) -> str:
        """The message reference of a request: a new one, or for a command_id seen before (a
        retry) the one it had. After a restart a retry gets a new reference, so the ledger
        refuses it as a different command rather than recording it twice."""
        if command_id is None:
            return self.next_ref()
        with self._lock:
            found = self._refs.get(command_id)
            if found is None:
                self._count += 1
                found = f"status-app:{self.instance}:{self._count}"
                if len(self._refs) >= REMEMBERED_COMMANDS:
                    self._refs.pop(next(iter(self._refs)))
                self._refs[command_id] = found
            return found

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

    def page(self, token: str | None = None) -> str:
        """The board; ``token`` is the requesting browser's, which its buttons send back."""
        ledger, _ = self.ledger()
        with ledger.view() as conn:
            actions: dict[str, tuple[tuple[str, ...], str | None]] = {}
            for row in conn.execute("SELECT * FROM tasks"):
                found = direct.availability(conn, row)
                actions[str(row["task_id"])] = (found.actions, found.cancel_blocked)
            past = history(ledger.storage.read(since=0))
            view = AppView(token=token or self.token, actions=actions, history=past)
            return render(conn, view)

    def task_page(self, task_id: str, token: str | None = None) -> tuple[bool, str]:
        """The task's view (DN006 §8.10); False with the not-found page when there is no task."""
        ledger, _ = self.ledger()
        with ledger.view() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            actions: dict[str, tuple[tuple[str, ...], str | None]] = {}
            if row is not None:
                found = direct.availability(conn, row)
                actions[task_id] = (found.actions, found.cancel_blocked)
            events = list(ledger.storage.read(since=0))
            view = AppView(token=token or self.token, actions=actions, history=history(events))
            steps = timeline(events).get(task_id, [])
            # The index card is read without touching the index (DN006 §8.10, T278).
            card = card_state(
                conn,
                task_id,
                read_card(index_path(self.home), task_id),
                {e.sequence: e.checksum for e in events},
                max((s.sequence for s in steps), default=0),
            )
            page = render_task(conn, task_id, view, steps, card)
            if page is None:
                return False, not_found(task_id, view)
            return True, page

    def preferences_page(self, query: str = "", token: str | None = None) -> str:
        """The standing-preferences page (DN006 §8.10, T290) for the address's filter."""
        ledger, _ = self.ledger()
        with ledger.view() as conn:
            view = AppView(token=token or self.token, actions={})
            return render_preferences(conn, view, parse_filter(query))

    def labels(self, body: Any) -> dict[str, Any]:
        """The session labels an embedding portal asked for (DN006 §8.10, T297); reads only."""
        ids = requested_ids(body)
        ledger, _ = self.ledger()
        with ledger.view() as conn:
            return answer(conn, ids)

    def preference(self, action: str, ref: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """Record (``ref`` None; with ``supersedes`` a replacement) or revoke ``ref`` as the user
        on the ``status-app`` channel (DN007 §6.2): the words typed on the page are the source's
        text, and every rule is the tool's."""
        fields = ("scope", "topic", "text") if action == "record" else ("reason",)
        values: dict[str, str] = {}
        for name in fields:
            value = body.get(name)
            if not isinstance(value, str) or not value.strip():
                raise rejected(f"缺少或无效的字段：{name}", reason="invalid_input", field=name)
            values[name] = value
        if action == "record":
            listed = body.get("supersedes")
            refs: list[Any] = listed if isinstance(listed, list) else []  # pyright: ignore[reportUnknownVariableType]
            if listed is not None and (
                not isinstance(listed, list) or not all(isinstance(r, str) and r for r in refs)
            ):
                raise rejected(
                    "supersedes 应为编号数组", reason="invalid_input", field="supersedes"
                )
            scope = values["scope"].strip()
            payload: dict[str, Any] = {
                "topic": values["topic"],
                "text": values["text"],
                "scope": scope,
                "supersedes": [str(r).strip() for r in refs],
            }
            target: list[Any] = ["preference", scope, "standing"]
            words = values["text"]
        elif action == "revoke" and ref:
            payload = {"ref": ref, "reason": values["reason"]}
            target = ["preference", ref, "revoke"]
            words = values["reason"]
        else:
            raise rejected("未知的操作", reason="invalid_input", action=action)
        # One confirmed submission keeps its command_id across retries (PR #53 review).
        command_id = body.get("command_id")
        if command_id is not None and (
            not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id)
        ):
            raise rejected("command_id 无效", reason="invalid_input", field="command_id")
        ledger, config = self.ledger()
        config.require_valid()
        return direct.preference(
            ledger,
            config,
            action,
            payload,
            target=target,
            channel="status-app",
            message_ref=self.ref_for(command_id),
            text=words,
            command_id=command_id,
        )

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


_REFUSED = object()  # a request body that was not read: the response is already sent


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

        def _cookie_token(self) -> str | None:
            """The browser's token from its cookie, when it signs in."""
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie") or "")
            except CookieError:  # a malformed cookie header is just not admitted
                return None
            morsel = cookie.get(f"{COOKIE}_{app.port}")
            return morsel.value if morsel is not None and app.admits(morsel.value) else None

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
            if url.path == HEALTH_PATH and app.health is not None:
                self._json(HTTPStatus.OK, app.health)
                return
            given = parse_qs(url.query).get("token", [""])[0]
            if url.path == "/" and given:
                if not app.admits(given):
                    self._text(HTTPStatus.FORBIDDEN, "令牌无效：请打开 tsw status serve 输出的地址")
                    return
                cookie = f"{COOKIE}_{app.port}={given}; HttpOnly; SameSite=Strict; Path=/"
                if app.cookie_max_age:
                    cookie += f"; Max-Age={app.cookie_max_age}"
                self._send(
                    HTTPStatus.SEE_OTHER,
                    b"",
                    "text/plain; charset=utf-8",
                    {"Location": "/", "Set-Cookie": cookie},
                )
                return
            token = self._cookie_token()
            if token is None:
                self._text(
                    HTTPStatus.FORBIDDEN,
                    "需要令牌：请打开运行 tsw status serve 的终端中输出的地址"
                    "（常驻服务：在终端运行 tsw dashboard open）",
                )
                return
            if url.path == "/":
                self._serve(lambda: app.page(token).encode("utf-8"), "text/html; charset=utf-8")
                return
            if url.path == "/preferences":
                self._serve(
                    lambda: app.preferences_page(url.query, token).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            parts = url.path.split("/")
            if len(parts) == 3 and parts[1] == "tasks" and parts[2]:
                self._serve_task(unquote(parts[2]), token)
                return
            self._serve_file(unquote(url.path), parse_qs(url.query).get("view", [""])[0])

        def _serve(self, produce: Callable[[], bytes], content_type: str) -> None:
            try:
                body = produce()
            except TswError as exc:
                self._text(HTTPStatus.SERVICE_UNAVAILABLE, f"错误 [{exc.code.value}] {exc.message}")
                return
            self._send(HTTPStatus.OK, body, content_type)

        def _serve_task(self, task_id: str, token: str) -> None:
            try:
                found, page = app.task_page(task_id, token)
            except TswError as exc:
                self._text(HTTPStatus.SERVICE_UNAVAILABLE, f"错误 [{exc.code.value}] {exc.message}")
                return
            status = HTTPStatus.OK if found else HTTPStatus.NOT_FOUND
            self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")

        def _serve_file(self, path: str, view: str = "") -> None:
            parts = [p for p in path.split("/") if p]
            if not parts or parts[0] not in SERVED_DIRS or any(p in ("..", ".") for p in parts):
                self._text(HTTPStatus.NOT_FOUND, "没有这个页面")
                return
            root = app.home.root.resolve()
            target = root.joinpath(*parts).resolve()
            if not target.is_relative_to(root / parts[0]) or not target.is_file():
                self._text(HTTPStatus.NOT_FOUND, "没有这个文件")
                return
            extra = {"Content-Security-Policy": SERVED_CSP}
            markdown = target.suffix.lower() == ".md"
            if markdown and view == "html":
                # The page's document dialog (DN006 §8.10, T278): the file rendered, escaped.
                source = target.read_bytes().decode("utf-8", errors="replace")
                body = (
                    render_markdown(source)
                    if len(source) <= MAX_SOURCE_CHARS
                    else f'<p class="none">文件过大（{len(source)} 个字符），请打开原文。</p>'
                )
                self._send(HTTPStatus.OK, body.encode("utf-8"), "text/html; charset=utf-8", extra)
                return
            # Markdown (briefs, reports' sources) is otherwise shown as text, not downloaded.
            plain = markdown or target.suffix.lower() in (".txt", ".log")
            kind = "text/plain" if plain else mimetypes.guess_type(target.name)[0]
            kind = kind or "application/octet-stream"
            if kind.startswith("text/") or kind in ("application/json", "application/xml"):
                kind += "; charset=utf-8"
            self._send(HTTPStatus.OK, target.read_bytes(), kind, extra)

        # ------------------------------------------------------------ POST

        def do_POST(self) -> None:
            if not (self._host_ok() and self._origin_ok()):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "只接受本页面的请求"})
                return
            if urlsplit(self.path).path == LABELS_PATH:
                self._labels()
                return
            given = self.headers.get(TOKEN_HEADER) or ""
            token = self._cookie_token()
            if token is None or not hmac.compare_digest(given, token):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "令牌无效"})
                return
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            task = len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] in direct.ACTIONS
            # /api/preferences/record and /api/preferences/<ref>/revoke (DN006 §8.10, T290)
            record = parts == ["api", "preferences", "record"]
            revoke = (
                len(parts) == 4 and parts[:2] == ["api", "preferences"] and parts[3] == "revoke"
            )
            if not (task or record or revoke):
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "没有这个操作"})
                return
            body = self._json_body(MAX_BODY)
            if body is _REFUSED:
                return
            if not isinstance(body, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请求体应为 JSON 对象"})
                return
            if not task:
                self._preference(
                    "record" if record else "revoke",
                    None if record else unquote(parts[2]),
                    body,  # pyright: ignore[reportUnknownArgumentType]
                )
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

        def _json_body(self, limit: int) -> Any:
            """The request's JSON body, or ``_REFUSED`` after answering why it is not read."""
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "message": "需要 JSON"})
                return _REFUSED
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if not 0 <= length <= limit:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请求体过大或无效"})
                return _REFUSED
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return None

        def _labels(self) -> None:
            """``POST /workdeck/labels`` (DN006 §8.10, T297): the page's cookie and ``Host``
            (checked by the caller with ``Origin``), no header token: it only reads."""
            if self._cookie_token() is None:
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "令牌无效"})
                return
            body = self._json_body(MAX_LABELS_BODY)
            if body is _REFUSED:
                return
            try:
                result = app.labels(body)
            except TswError as exc:
                status = (
                    HTTPStatus.BAD_REQUEST
                    if exc.details.get("reason") == "invalid_input"
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self._json(status, {"ok": False, "code": exc.code.value, "message": exc.message})
                return
            except (sqlite3.Error, OSError) as exc:
                # An unreadable projection: the portal keeps its last labels (no local paths).
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "message": f"无法读取投影（{type(exc).__name__}）"},
                )
                return
            self._json(HTTPStatus.OK, result)

        def _preference(self, action: str, ref: str | None, body: dict[str, Any]) -> None:
            try:
                result = app.preference(action, ref, body)
            except TswError as exc:
                self._json(
                    HTTPStatus.CONFLICT,
                    {"ok": False, "code": exc.code.value, "message": exc.message, **exc.details},
                )
                return
            done = "已记录" if action == "record" else "已撤销"
            print(
                f"tsw status: 长期偏好 {result['ref']} {done}（账本序号 {result['sequence']}）",
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


def serve_hosted(
    home: Home,
    *,
    verify: Callable[[str], bool],
    port: int,
    commit: str | None,
    key: str,
    cookie_max_age: int,
) -> None:
    """Run the hosted Dashboard service (``tsw dashboard service``) until it is ended."""
    health = {
        "service": "tsw-dashboard",
        "commit": commit,
        "home_key": key,
        "started_at": utc_now(),
    }
    app = StatusApp(home, cookie_max_age=cookie_max_age, health=health, verify=verify)
    server = make_server(app, port)
    print(f"TaskSteward Dashboard 服务：http://127.0.0.1:{app.port}/（{utc_now()}）", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
