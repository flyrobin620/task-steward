"""A running session's inbox for the tests (DN006 §8.10, T357; CC001 §6.1).

It publishes what Claude Code publishes for a running session in the test's own sessions folder
(``<pid>.json`` with ``hostSessionId`` and ``messagingSocketPath``, and the ``.key`` file with a
made-up peer token) and serves the pipe, keeping every line written to it. Nothing here touches
the user's real ``~/.claude``: the folder is the test's ``CLAUDE_CONFIG_DIR``.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import threading
from pathlib import Path
from typing import Any

from tsw.host.session_inbox import key_file

TOKEN = "test-peer-token-not-a-secret"


class FakeInbox:
    def __init__(self, folder: Path, host_session_id: str, pid: int = 4242) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        self.pid = pid
        self.received: list[bytes] = []
        self._stop = threading.Event()
        if os.name == "nt":
            from multiprocessing.connection import Listener  # noqa: PLC0415

            self.pipe = rf"\\.\pipe\tsw-test-{secrets.token_hex(8)}"
            self._listener: Any = Listener(self.pipe, family="AF_PIPE")
        else:
            self.pipe = str(folder / f"inbox-{secrets.token_hex(4)}.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
            server.bind(self.pipe)
            server.listen(4)
            self._listener = server
        entry = {
            "pid": pid,
            "sessionId": "00000000-0000-4000-8000-000000000000",
            "hostSessionId": host_session_id,
            "messagingSocketPath": self.pipe,
            "kind": "interactive",
            "status": "idle",
            "updatedAt": 1,
        }
        (folder / f"{pid}.json").write_text(json.dumps(entry), encoding="utf-8")
        key_file(folder, pid, self.pipe).write_text(
            json.dumps({"peerToken": TOKEN}), encoding="utf-8"
        )
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except OSError:
                return
            data = b""
            try:
                if os.name == "nt":
                    while True:
                        try:
                            data += conn.recv_bytes()
                        except (EOFError, OSError):
                            break
                else:
                    while chunk := conn[0].recv(65536):
                        data += chunk
            finally:
                (conn if os.name == "nt" else conn[0]).close()
            if data:
                self.received.append(data)

    def messages(self) -> list[dict[str, Any]]:
        """The ``user`` lines written so far, each with the token it was authenticated by."""
        out: list[dict[str, Any]] = []
        for data in self.received:
            lines = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
            auth = next((x for x in lines if x.get("type") == "auth"), {})
            for line in lines:
                if line.get("type") == "user":
                    out.append({**line, "token": auth.get("token")})
        return out

    def wait(self, count: int, timeout: float = 5.0) -> list[dict[str, Any]]:
        deadline = threading.Event()
        for _ in range(int(timeout / 0.05)):
            if len(self.messages()) >= count:
                break
            deadline.wait(0.05)
        return self.messages()

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._listener.close()
