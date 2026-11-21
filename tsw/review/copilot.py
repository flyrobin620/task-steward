"""The ``copilot-cli`` reviewer (CP004): one ``copilot -p`` run per review round.

The prompt only points at the request file (the request is not put on the command line); the
model sees nothing but the read-only file tools (``--available-tools``), and writing, shell
commands, memory and URLs are denied as well. tsw names the session (``--session-id``) so a
later round can continue it (``--resume``). The JSON Lines output is read for the final answer,
the model that produced it and the exit code. Standard library only.
"""

# The CLI's JSON Lines events are walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tsw.config import normalize_model_id
from tsw.errors import ErrorCode, TswError
from tsw.review.codex import ReviewRun
from tsw.review.process import ProcessTree

DEFAULT_EXECUTABLE = "copilot"
READ_TOOLS = ("view", "grep", "glob")
DENIED = ("write", "shell", "memory", "url")


def command(
    executable: str,
    prompt: str,
    *,
    session: str,
    resume: bool,
    model: str,
    effort: str,
    add_dirs: Iterable[Path],
) -> list[str]:
    """The ``copilot`` command line of one round (CP004 §3)."""
    argv = [executable, "-p", prompt, "--output-format", "json"]
    argv += ["--resume", session] if resume else ["--session-id", session]
    argv += ["--model", model, "--reasoning-effort", effort]
    argv += ["--available-tools", *READ_TOOLS]
    for rule in DENIED:
        argv += ["--deny-tool", rule]
    for folder in add_dirs:
        argv += ["--add-dir", str(folder)]
    argv += ["--no-ask-user", "--disable-builtin-mcps", "--no-auto-update", "--disallow-temp-dir"]
    return argv


def run_review(
    prompt: str,
    *,
    cwd: Path,
    model: str,
    effort: str,
    timeout_seconds: int,
    executable: str | None = None,
    resume_thread: str | None = None,
    add_dirs: Iterable[Path] = (),
    **_: Any,
) -> ReviewRun:
    """One read-only round; ``resume_thread`` continues the session of an earlier round."""
    session = resume_thread or str(uuid.uuid4())
    argv = command(
        executable or DEFAULT_EXECUTABLE,
        prompt,
        session=session,
        resume=resume_thread is not None,
        model=model,
        effort=effort,
        add_dirs=add_dirs,
    )
    tree = ProcessTree(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    errors: list[str] = []
    readers = [
        threading.Thread(target=_drain, args=(tree.proc.stdout, lines), daemon=True),
        threading.Thread(target=_drain, args=(tree.proc.stderr, errors), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        tree.proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        tree.end()
        for reader in readers:
            reader.join(timeout=10)
        return ReviewRun(
            thread_id=session,
            status="timedOut",
            final_message=None,
            model=model,
            error=f"copilot 在 {timeout_seconds} 秒内没有结束",
        )
    finally:
        tree.end()
    for reader in readers:
        reader.join(timeout=10)
    return read_output(lines, session=session, model=model, stderr="".join(errors))


def _drain(stream: Any, into: list[str]) -> None:
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        for line in stream:
            into.append(line)
    with contextlib.suppress(OSError):
        stream.close()


def read_output(lines: Iterable[str], *, session: str, model: str, stderr: str = "") -> ReviewRun:
    """The round's outcome from the JSON Lines events (CP004 §4)."""
    events: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(json.JSONDecodeError):
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
    final: str | None = None
    fallback: str | None = None
    used: set[str] = set()
    problems: list[str] = []
    exit_code: Any = None
    reported: Any = None
    for event in events:
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "assistant.message":
            if data.get("model"):
                used.add(str(data["model"]))
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                if data.get("phase") == "final_answer":
                    final = content
                elif not data.get("toolRequests"):
                    fallback = content
        elif kind == "session.model_change" and data.get("newModel"):
            used.add(str(data["newModel"]))
        elif kind == "session.error":
            problems.append(str(data.get("message") or data))
        elif kind == "result":
            exit_code = event.get("exitCode")
            reported = event.get("sessionId")
    wanted = normalize_model_id(model)
    others = sorted(m for m in used if normalize_model_id(m) != wanted)
    if others:
        problems.append(f"Copilot 使用的模型（{'、'.join(others)}）与配置的 {model} 不一致")
    if exit_code is None:
        problems.append(
            "copilot 输出中没有 result 事件" + (f"：{stderr.strip()[:500]}" if stderr else "")
        )
    elif exit_code != 0:
        problems.append(f"copilot 以退出码 {exit_code} 结束")
    if reported is not None and reported != session:
        problems.append(f"copilot 的会话 ID（{reported}）与请求的 {session} 不一致")
    answer = final if final is not None else fallback
    return ReviewRun(
        thread_id=session,
        status="failed" if problems else "completed",
        final_message=answer,
        model=sorted(used)[0] if len(used) == 1 else model,
        error="；".join(problems) or None,
    )


def resolve(configured: str | None) -> str:
    from shutil import which  # noqa: PLC0415

    found = which(configured or DEFAULT_EXECUTABLE)
    if found is None and configured and Path(configured).is_file():
        found = configured
    if found is None:
        raise TswError(
            ErrorCode.UNAVAILABLE,
            "找不到评审器命令（roles.reviewer.executable 或 PATH 中的 copilot）",
            reason="reviewer_unavailable",
            executable=configured or DEFAULT_EXECUTABLE,
        )
    return found
