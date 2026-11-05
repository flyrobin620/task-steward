"""The ``codex-app-server`` reviewer (DN004 §3.1): one read-only thread per review round.

Talks to ``codex app-server`` over stdio JSON-RPC: ``initialize`` → ``thread/start`` (read-only
sandbox, approvals never) → ``turn/start`` (model and effort) → wait for ``turn/completed`` and
take the last non-commentary ``agentMessage`` as the answer → ``thread/archive``. Requests the
server sends (approvals, elicitations) are declined. Standard library only.
"""

# The app-server protocol is untyped JSON walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import contextlib
import json
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsw.errors import ErrorCode, TswError


@dataclass
class ReviewRun:
    thread_id: str | None
    status: str
    final_message: str | None
    model: str | None
    error: str | None = None


class AppServer:
    def __init__(self, executable: str) -> None:
        self.proc = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.next_id = 0
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.pending: list[dict[str, Any]] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.messages.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def next_message(self, deadline: float) -> dict[str, Any] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if message is None:
            raise TswError(
                ErrorCode.UNAVAILABLE, "codex app-server 意外退出", reason="reviewer_unavailable"
            )
        if "method" in message and "id" in message:  # a server request: decline it
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "not handled"},
                }
            )
            return {}
        return message

    def call(self, method: str, params: dict[str, Any], timeout: float = 60) -> Any:
        self.next_id += 1
        mid = self.next_id
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self.next_message(deadline)
            if message and message.get("id") == mid:
                if "error" in message:
                    raise TswError(
                        ErrorCode.UNAVAILABLE,
                        f"{method} 失败",
                        reason="reviewer_error",
                        error=message["error"],
                    )
                return message.get("result")
            if message:
                self.pending.append(message)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


def run_review(
    prompt: str,
    *,
    cwd: Path,
    model: str,
    effort: str,
    timeout_seconds: int,
    executable: str | None = None,
    resume_thread: str | None = None,
    keep_thread: bool = True,
    context_window: int | None = None,
) -> ReviewRun:
    """One review turn on a read-only thread; ``resume_thread`` continues an earlier round."""
    codex = executable or shutil.which("codex")
    if not codex:
        raise TswError(ErrorCode.UNAVAILABLE, "找不到 codex 命令行", reason="reviewer_unavailable")
    server = AppServer(codex)
    thread_id: str | None = None
    try:
        server.call(
            "initialize",
            {"clientInfo": {"name": "tsw-review", "version": "1"}, "capabilities": {}},
        )
        server.notify("initialized")
        settings: dict[str, Any] = {
            "model": model,
            "cwd": str(cwd),
            "approvalPolicy": "never",
            "sandbox": "read-only",
        }
        if context_window:
            settings["config"] = {"model_context_window": context_window}
        if resume_thread:
            with contextlib.suppress(TswError, TimeoutError):
                server.call("thread/unarchive", {"threadId": resume_thread}, timeout=30)
            started = server.call(
                "thread/resume", {"threadId": resume_thread, "excludeTurns": True, **settings}
            )
            if started["thread"]["id"] != resume_thread:
                raise TswError(
                    ErrorCode.UNAVAILABLE, "续用的评审线程不一致", reason="resume_rejected"
                )
        else:
            started = server.call("thread/start", settings)
        thread_id = str(started["thread"]["id"])
        if started.get("model") not in (None, model):
            raise TswError(
                ErrorCode.UNAVAILABLE,
                "评审线程使用的模型与配置不一致",
                reason="model_mismatch",
                requested=model,
                actual=started.get("model"),
            )
        turn = server.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "model": model,
                "effort": effort,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            },
        )["turn"]
        turn_id = turn["id"]
        final: str | None = None
        status = "timedOut"
        error: str | None = None
        deadline = time.monotonic() + timeout_seconds
        backlog = list(server.pending)
        server.pending.clear()
        try:
            while True:
                message = backlog.pop(0) if backlog else server.next_message(deadline)
                if not message:
                    continue
                params = message.get("params") or {}
                if params.get("threadId") != thread_id:
                    continue
                item = params.get("item") or {}
                done = params.get("turn") or {}
                if (
                    message.get("method") == "item/completed"
                    and item.get("type") == "agentMessage"
                    and item.get("phase") != "commentary"
                    and params.get("turnId") == turn_id
                ):
                    final = item.get("text")
                elif message.get("method") == "turn/completed" and done.get("id") == turn_id:
                    status = str(done.get("status") or "completed")
                    if done.get("error"):
                        error = json.dumps(done["error"], ensure_ascii=False)
                    break
        except TimeoutError:
            with contextlib.suppress(TswError, TimeoutError):
                server.call(
                    "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=30
                )
        if final is None and status == "completed":
            final = _read_final(server, thread_id, turn_id)
        return ReviewRun(
            thread_id=thread_id, status=status, final_message=final, model=model, error=error
        )
    except TimeoutError as exc:
        raise TswError(
            ErrorCode.UNAVAILABLE, "codex app-server 无响应", reason="reviewer_timeout"
        ) from exc
    finally:
        if thread_id is not None and not keep_thread:
            with contextlib.suppress(TswError, TimeoutError):
                server.call("thread/archive", {"threadId": thread_id}, timeout=30)
        server.close()


def archive_thread(thread_id: str, *, executable: str | None = None) -> bool:
    """Archive a kept review thread once the attempt is done with it."""
    codex = executable or shutil.which("codex")
    if not codex:
        return False
    server = AppServer(codex)
    try:
        server.call(
            "initialize",
            {"clientInfo": {"name": "tsw-review", "version": "1"}, "capabilities": {}},
        )
        server.notify("initialized")
        server.call("thread/archive", {"threadId": thread_id}, timeout=30)
        return True
    except (TswError, TimeoutError):
        return False
    finally:
        server.close()


def _read_final(server: AppServer, thread_id: str, turn_id: str) -> str | None:
    with contextlib.suppress(TswError, TimeoutError, KeyError, TypeError):
        read = server.call("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout=30)
        for turn in read["thread"]["turns"]:
            if turn.get("id") == turn_id:
                texts = [
                    i.get("text")
                    for i in turn.get("items") or []
                    if i.get("type") == "agentMessage" and i.get("phase") != "commentary"
                ]
                return texts[-1] if texts else None
    return None


def extract_json(text: str | None) -> dict[str, Any] | None:
    """The reviewer must answer with one JSON object; take the outermost braces."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
