"""The Dashboard's 「新建会话」: a new conversation of the user's in a spare session (DN006 §8.10,
DN002 §5.9, T363).

No session is created: one of the spare sessions made earlier in the user's own turn is claimed
(``spare.claimed``, ``as: chat``, channel ``status-app``) and the words the user confirmed on the
page are written to its inbox, the way the Dashboard delivers an answer (CC001 §6.1). The session
then retitles itself and answers; the page links it (``data-workdeck-session``) so the user goes on
in the workdeck portal.

Order: the claim is recorded first, so the orchestrator never hands the same session to a task;
then the write. A write that never started (the session is not running) loses that spare
(``spare.lost``) and the next one is tried; a write that failed part-way is ``unknown`` and is not
retried. A retry of the same submission (its ``command_id``) is answered from memory, never
claimed or written again.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tsw import direct, lease
from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.events import EventDraft
from tsw.host import inbox as inbox_mod
from tsw.host import session_inbox
from tsw.host.status import session_href
from tsw.ledger import Ledger
from tsw.machine import spares as spares_mod
from tsw.machine.base import Caller
from tsw.machine.runner import run_tool

COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
REMEMBERED = 256
NO_SPARE_HINT = (
    "在 Claude app 中给主会话发一句话（例如“补充备用会话”），"
    "它会在那一轮补足备用会话；之后回到这里。"
)


def pool_status(
    ledger: Ledger, config: EffectiveConfig, sessions_dir: Path | None = None
) -> dict[str, Any]:
    """What the dialog shows: the pool's target, the sessions waiting and whether they run."""
    with ledger.view() as conn:
        found = inbox_mod.spare_attention(conn, config, directory=sessions_dir)
    summary: dict[str, Any] = found[0] if found else {}
    available: list[dict[str, Any]] = list(summary.get("available") or [])
    retire: list[dict[str, Any]] = list(summary.get("retire") or [])
    return {
        "enabled": bool(config.get("scheduling.spare.enabled")),
        "target": int(summary.get("target") or 0),
        "available": available,
        "retire": [{"spare_id": r["spare_id"], "reason": r["reason"]} for r in retire],
        "hint": NO_SPARE_HINT,
    }


class Starter:
    """Claims spare sessions for the page's 「新建会话」; one per Dashboard run."""

    def __init__(self, next_ref: Callable[[], str]) -> None:
        self.next_ref = next_ref
        self._lock = threading.Lock()
        self._done: dict[str, dict[str, Any]] = {}

    def start(
        self,
        ledger: Ledger,
        config: EffectiveConfig,
        body: Mapping[str, Any],
        sessions_dir: Path | None = None,
    ) -> tuple[int, dict[str, Any]]:
        text, command_id = body.get("text"), body.get("command_id")
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False, "message": "第一条消息为空"}
        if len(text) > session_inbox.MAX_CHARS:
            return 400, {
                "ok": False,
                "message": f"消息过长（至多 {session_inbox.MAX_CHARS} 个字符）",
            }
        if not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id):
            return 400, {"ok": False, "message": "缺少或无效的 command_id"}
        config.require_valid()
        with self._lock:
            seen = self._done.get(command_id)
            if seen is not None:
                if seen["text"] != text:
                    return 409, {
                        "ok": False,
                        "code": "command_reused",
                        "message": "同一个 command_id 已用于另一条消息",
                    }
                return 200, {**seen["result"], "duplicate": True}
            status, result = self._start(ledger, config, text, sessions_dir)
            if result.get("state") in ("sent", "unknown"):
                if len(self._done) >= REMEMBERED:
                    self._done.pop(next(iter(self._done)))
                self._done[command_id] = {"text": text, "result": result}
            return status, result

    def _start(
        self, ledger: Ledger, config: EffectiveConfig, text: str, sessions_dir: Path | None
    ) -> tuple[int, dict[str, Any]]:
        if not config.get("scheduling.spare.enabled"):
            return 409, {
                "ok": False,
                "code": "spare_disabled",
                "message": "备用会话池已关闭（scheduling.spare.enabled）",
            }
        tried: list[str] = []
        while True:
            with ledger.view() as conn:
                waiting = [r for r in spares_mod.available(conn) if r["spare_id"] not in tried]
                # The ones attention keeps (running, not too old, within the target), as the
                # dialog lists them; the others are the orchestrator's to retire.
                found = inbox_mod.spare_attention(conn, config, directory=sessions_dir)
            kept = {a["spare_id"] for a in (found[0]["available"] if found else [])}
            running = [r for r in waiting if r["spare_id"] in kept]
            if not running:
                gone = "备用会话都不在运行（app 可能重启过）。" if waiting or tried else ""
                return 409, {
                    "ok": False,
                    "code": "no_spare",
                    "message": f"{gone}没有可用的备用会话。",
                    "hint": NO_SPARE_HINT,
                }
            row = running[0]
            sid, session = str(row["spare_id"]), str(row["session_ref"])
            tried.append(sid)
            self._claim(ledger, config, sid, text)
            outcome = session_inbox.deliver(session, text, sessions_dir)
            if outcome.state == "written":
                print(f"tsw status: 已在备用会话 {sid} 开始新对话", flush=True)
                return 200, {
                    "ok": True,
                    "state": "sent",
                    "spare_id": sid,
                    "session_id": session,
                    "href": session_href("claude-app", session),
                }
            if outcome.state == "unknown":
                return 502, {
                    "ok": False,
                    "state": "unknown",
                    "code": "unknown",
                    "spare_id": sid,
                    "session_id": session,
                    "href": session_href("claude-app", session),
                    "message": outcome.detail or "写入时出错，不知道消息是否到达",
                }
            # Nothing was written: the session is gone; it leaves the pool, try the next.
            _lost(ledger, sid, outcome.state)

    def _claim(self, ledger: Ledger, config: EffectiveConfig, sid: str, text: str) -> None:
        source = {
            "channel": "status-app",
            "principal": direct.principal(),
            "message_ref": self.next_ref(),
            "text": text,
            "target": ["spare", sid, "chat"],
        }
        caller = Caller(role="user", session_id=None, machine=lease.this_machine())
        run_tool(ledger, config, caller, "spare_claim", {"spare_id": sid, "user_source": source})


def _lost(ledger: Ledger, sid: str, why: str) -> None:
    def build(ctx: CommitContext) -> list[EventDraft]:
        del ctx
        return [EventDraft(type="spare.lost", data={"spare_id": sid, "reason": why})]

    ledger.commit(
        Command(
            actor={"role": "user", "via": "status-app"}, input={"op": "spare_lost"}, build=build
        )
    )
