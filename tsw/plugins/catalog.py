"""Capability declarations of the plugin implementations this version ships (DN006 §7.3, §8.6).

Configuration validation (DN003 §4 rule 4 and 8) decides from these declarations, never from a
host name. Implementations not listed here are not available in this version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SessionCaps:
    host: str
    execution: Literal["direct", "delegated"]
    interactive: bool
    oneshot: bool
    read_only_enforced: bool
    incremental: bool
    user_direct_chat: bool
    wake: Literal["push", "poll", "none"]
    dispatch_window: bool
    prestart: bool
    reuse_session: bool
    open_existing_dir: bool
    timer: bool = False  # the host wakes the session on a schedule (DN006 §8.9)


@dataclass(frozen=True)
class ChannelCaps:
    request_response: bool
    notify: bool
    collect: Literal["push", "poll", "none"]
    execution: Literal["direct", "delegated"]


SESSIONS: dict[str, SessionCaps] = {
    "claude-app": SessionCaps(
        host="claude-code",
        execution="delegated",
        interactive=True,
        oneshot=False,
        read_only_enforced=False,
        incremental=False,
        user_direct_chat=True,
        wake="push",
        dispatch_window=True,
        prestart=True,
        reuse_session=True,
        open_existing_dir=True,
        timer=True,  # CronCreate in the session (CC006 §6)
    ),
    "claude-headless": SessionCaps(
        host="claude-code",
        execution="direct",
        interactive=False,  # not verified for workers yet (DN006 §7.3)
        oneshot=True,
        read_only_enforced=True,
        incremental=True,
        user_direct_chat=False,
        wake="none",
        dispatch_window=False,
        prestart=False,
        reuse_session=True,
        open_existing_dir=True,
    ),
    "codex-app-server": SessionCaps(
        host="codex",
        execution="direct",
        interactive=False,  # B7: threads cannot be found again in time; reviewer only
        oneshot=True,
        read_only_enforced=True,
        incremental=True,
        user_direct_chat=True,
        wake="push",
        dispatch_window=False,
        prestart=False,
        reuse_session=True,
        open_existing_dir=True,
    ),
    "codex-exec": SessionCaps(
        host="codex",
        execution="direct",
        interactive=False,
        oneshot=True,
        read_only_enforced=True,
        incremental=False,
        user_direct_chat=False,
        wake="none",
        dispatch_window=False,
        prestart=False,
        reuse_session=False,
        open_existing_dir=True,
    ),
}

CHANNELS: dict[str, ChannelCaps] = {
    "in-session": ChannelCaps(
        request_response=True, notify=False, collect="push", execution="direct"
    ),
    # Cards go out from the orchestrator; replies come back through the routine board
    # (DN006 §8.7). No notify in this version.
    "teams-chat": ChannelCaps(
        request_response=True, notify=False, collect="poll", execution="delegated"
    ),
}

HOST_INTEGRATIONS = ("claude-code",)
STORAGE_BACKENDS = ("fs",)
