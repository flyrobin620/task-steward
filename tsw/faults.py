"""Named fault-injection points for tests.

Faults are active only when ``TSW_TEST_FAULTS`` is set (comma-separated ``point`` or
``point:exit`` entries). Without the variable every call is a no-op and behaviour is unchanged.

- ``point``: raise :class:`FaultInjected` at that point.
- ``point:exit``: terminate the process immediately (simulates a crash, no cleanup runs).
"""

from __future__ import annotations

import os

ENV = "TSW_TEST_FAULTS"
CRASH_EXIT_CODE = 97


class FaultInjected(RuntimeError):
    """Raised at an enabled fault point."""


def fault(point: str) -> None:
    spec = os.environ.get(ENV)
    if not spec:
        return
    for entry in spec.split(","):
        name, _, action = entry.strip().partition(":")
        if name != point:
            continue
        if action == "exit":
            os._exit(CRASH_EXIT_CODE)
        raise FaultInjected(point)
