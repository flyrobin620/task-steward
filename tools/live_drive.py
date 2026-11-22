"""What a live run does instead of the operator (T340): links, scripted messages, conditions.

The live scenarios used to have the user do every step in the Claude app. Most of those steps
are deterministic and are done here instead (docs/development/test-environments.md, the table
of operator steps):

- ``deeplink``: a ``claude://code/new`` link that opens a new session of the Claude desktop app in
  a folder with its first message filled in. The app reads only ``q`` (or ``prompt``),
  ``folder``, ``file``, ``environment`` and ``runner_pool`` from such a link (checked in the
  app's handler, app 6.28899): it cannot set the permission mode and never sends by itself, so
  the user still presses send (and confirms the folder's trust the first time).
- ``Daemon`` and ``WorkdeckClient``: a workdeck daemon of the user's own installation, started
  for the run in local mode (``--no-gateway``, loopback only, no sign-in) and limited to the
  sessions of the run (``messaging.allowSessions``). A message sent through it reaches the
  session as the user's own words (DN008 §2, DN006 §8.11: ``from: workdeck``), which a message
  from another session never is. This script only speaks workdeck's portal protocol; reading
  the sessions' pipes and keys is the daemon's own business, as when the user sends from the
  portal.
- ``Rule`` and ``due``: when to send what. A rule fires once, after every event it waits for is
  in the run's ledger; the scenario defines the rules (``drive``), so no model decides what the
  "user" says or when.

Nothing here reads or writes the user's data directory, ``~/.claude`` or any session's
transcript.
"""

# Ledger events, scenario JSON and portal frames are walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEEPLINK = "claude://code/new"


# ---------------------------------------------------------------------------- deep links


def deeplink(folder: Path | str, prompt: str | None = None) -> str:
    """A link that opens a new Claude Code session in ``folder`` with ``prompt`` filled in."""
    query = [("folder", str(folder))]
    if prompt:
        if prompt.startswith("/"):
            raise ValueError("the app refuses a prompt that starts with '/'")
        query.append(("q", prompt))
    return DEEPLINK + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


def open_link(url: str) -> None:
    """Hand the link to the operating system, which gives it to the Claude app."""
    if not url.startswith("claude://"):
        raise ValueError(f"not a Claude link: {url}")
    starter = getattr(os, "startfile", None)
    if starter is not None:
        starter(url)
        return
    opener = shutil.which("open") or shutil.which("xdg-open")
    if opener is None:
        raise SystemExit("live: 本机没有打开链接的程序；请手动打开 OPERATOR.md 中的链接")
    subprocess.run([opener, url], check=True)


# ---------------------------------------------------------------------------- rules


@dataclass(frozen=True)
class Rule:
    """Send ``text`` to ``to`` (``main`` or a task ID) once every ``after`` condition holds."""

    id: str
    to: str
    text: str
    after: tuple[Mapping[str, Any], ...]
    tasks: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def parse(cls, raw: Mapping[str, Any], values: Mapping[str, str]) -> Rule:
        text = str(raw["text"])
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        after = tuple(dict(c) for c in raw.get("after") or ())
        needs = {str(c["task"]) for c in after if c.get("task")}
        if raw["to"] != "main":
            needs.add(str(raw["to"]))
        needs |= {str(t) for t in raw.get("tasks") or ()}
        return cls(str(raw["id"]), str(raw["to"]), text, after, frozenset(needs))


def holds(condition: Mapping[str, Any], events: Iterable[Mapping[str, Any]]) -> bool:
    """An event of the given type (and task, and data values) is in the ledger."""
    data = condition.get("data") or {}
    for e in events:
        if e.get("type") != condition["event"]:
            continue
        if condition.get("task") and e.get("task_id") != condition["task"]:
            continue
        if all((e.get("data") or {}).get(k) == v for k, v in data.items()):
            return True
    return False


def applicable(rules: Iterable[Rule], tasks: Iterable[str]) -> list[Rule]:
    """The rules a run uses: those whose tasks are all in the run."""
    chosen = set(tasks)
    return [r for r in rules if r.tasks <= chosen]


def due(
    rules: Iterable[Rule], events: Sequence[Mapping[str, Any]], sent: Iterable[str]
) -> list[Rule]:
    """The rules not yet sent whose conditions all hold, in the scenario's order."""
    done = set(sent)
    return [r for r in rules if r.id not in done and all(holds(c, events) for c in r.after)]


def session_of(target: str, events: Iterable[Mapping[str, Any]]) -> str | None:
    """The session a message goes to: the main session holding the lease, or the session the
    task's current attempt is bound to (the last ``attempt.bound``)."""
    found: str | None = None
    for e in events:
        if target == "main":
            actor = e.get("actor") or {}
            if actor.get("role") == "orchestrator" and actor.get("session_id"):
                found = str(actor["session_id"])
        elif e.get("type") == "attempt.bound" and e.get("task_id") == target:
            ref = (e.get("data") or {}).get("session_ref")
            found = str(ref) if ref else found
    return found


# ---------------------------------------------------------------------------- workdeck


def workdeck_command(env: Mapping[str, str] | None = None) -> list[str]:
    """How to start the user's workdeck: ``TSW_LIVE_WORKDECK`` (a ``cli.js`` or a command), or
    the ``workdeck`` on ``PATH`` (its ``cli.js`` run by node, so no shell wrapper is involved)."""
    environ = os.environ if env is None else env
    named = environ.get("TSW_LIVE_WORKDECK")
    node = shutil.which("node")
    if named:
        path = Path(named)
        return [node or "node", str(path)] if path.suffix == ".js" else [str(path)]
    found = shutil.which("workdeck")
    if found is None:
        raise SystemExit(
            "live: 找不到 workdeck（PATH 中没有，也没有设 TSW_LIVE_WORKDECK）；"
            "脚本无法替用户发消息，请按 OPERATOR.md 手动发送"
        )
    cli = Path(found).parent / "node_modules" / "@m365-core" / "workdeck" / "dist" / "cli.js"
    if node and cli.is_file():
        return [node, str(cli)]
    return [found]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Daemon:
    """A workdeck daemon for one run: local mode only, messages only to ``allow``."""

    def __init__(self, folder: Path, allow: Iterable[str], command: list[str]) -> None:
        self.folder = folder
        self.allow = sorted(set(allow))
        self.command = command
        self.port = 0
        self.proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> Daemon:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        config = self.folder / "config.json"
        config.write_text(
            json.dumps(
                {
                    "port": self.port,
                    "claudeCode": {"messaging": {"enabled": True, "allowSessions": self.allow}},
                    "plugins": [],
                }
            ),
            encoding="utf-8",
        )
        log = self.folder / "daemon.log"
        self.proc = subprocess.Popen(
            [*self.command, "--config", str(config), "--no-gateway", "--log-file", str(log)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"live: workdeck 退出（{self.proc.returncode}），见 {log}")
            try:
                url = f"http://127.0.0.1:{self.port}/healthz"
                with urllib.request.urlopen(url, timeout=2) as reply:
                    if reply.status == 200:
                        return self
            except OSError:
                time.sleep(0.3)
        self.__exit__(None, None, None)
        raise SystemExit(f"live: workdeck 30 秒内没有就绪，见 {log}")

    def __exit__(self, *_exc: object) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def frame(payload: bytes, opcode: int = 1, mask: bytes | None = None) -> bytes:
    """One masked client frame (RFC 6455 §5.2)."""
    key = mask if mask is not None else os.urandom(4)
    head = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        head += bytes([0x80 | size])
    elif size < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", size)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", size)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


class WorkdeckClient:
    """The portal side of workdeck's WebSocket (JSON text frames), as the portal speaks it."""

    def __init__(self, port: int, timeout: float = 30) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                "GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("workdeck closed the connection during the handshake")
            head += chunk
        head, self.buffer = head.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError(f"workdeck refused the WebSocket: {lines[0]}")
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (x.partition(":") for x in lines[1:])
        }
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise ConnectionError("workdeck answered with a wrong Sec-WebSocket-Accept")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.sendall(frame(b"", opcode=8))
        self.sock.close()

    def __enter__(self) -> WorkdeckClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def send(self, message: Mapping[str, Any]) -> None:
        self.sock.sendall(frame(json.dumps(message, ensure_ascii=False).encode("utf-8")))

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("workdeck closed the connection")
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def frames(self) -> Iterator[dict[str, Any]]:
        """Every JSON message the daemon sends, until the connection closes."""
        parts: list[bytes] = []
        while True:
            if not self.buffer and not parts:
                try:
                    chunk = self.sock.recv(65536)
                except TimeoutError:
                    yield {"type": "idle"}  # nothing arrived: callers check their deadline
                    continue
                if not chunk:
                    return
                self.buffer = chunk
            b0, b1 = self._read(2)
            opcode, size = b0 & 0x0F, b1 & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(size)
            if mask is not None:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                return
            if opcode == 9:
                self.sock.sendall(frame(data, opcode=10))
                continue
            if opcode in (1, 0):
                parts.append(data)
                if b0 & 0x80:
                    text = b"".join(parts).decode("utf-8")
                    parts = []
                    yield json.loads(text)


def sessions(projects: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every session node of a ``tree`` frame, side sessions included."""
    out: list[dict[str, Any]] = []
    stack = [dict(s) for p in projects for s in p.get("sessions") or ()]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(dict(c) for c in node.get("children") or ())
    return out


def inside(path: str, base: Path) -> bool:
    if not path:
        return False
    target, root = Path(path).resolve(), base.resolve()
    return target == root or root in target.parents


def tree(client: WorkdeckClient, deadline: float) -> list[dict[str, Any]]:
    """The sessions workdeck sees (the first ``tree`` frame)."""
    for message in client.frames():
        if message.get("type") == "tree":
            return sessions(message.get("projects") or ())
        if time.monotonic() > deadline:
            break
    raise TimeoutError("workdeck sent no session tree")


def deliver(
    client: WorkdeckClient,
    session: str,
    text: str,
    ref: str,
    deadline: float,
) -> dict[str, Any]:
    """Send ``text`` to ``session`` as a new turn (``later``); wait for workdeck's verdict
    (``delivered`` or ``failed``; the last state seen when the deadline passes)."""
    client.send(
        {"type": "send", "sessionId": session, "text": text, "priority": "later", "ref": ref}
    )
    last: dict[str, Any] = {"state": "sending"}
    for message in client.frames():
        if message.get("type") == "error" and message.get("ref") == ref:
            return {"state": "failed", "detail": message.get("message")}
        if message.get("type") == "delivery" and message.get("ref") == ref:
            last = dict(message.get("delivery") or {})
            if last.get("state") in ("delivered", "failed"):
                return last
        if time.monotonic() > deadline:
            break
    return last
