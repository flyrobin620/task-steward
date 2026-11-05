from __future__ import annotations

from collections.abc import Callable

import pytest

from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.ledger import Ledger

pytestmark = [pytest.mark.use_case("UC-LEASE-001"), pytest.mark.transition("G-LEASE")]

ACTOR = {"role": "orchestrator"}
SOURCE = {
    "channel": "main-session",
    "principal": "owner",
    "message_ref": "msg-1",
    "text": "原主会话已经停了，接管吧",
}


class Clock:
    def __init__(self, now: str = "2026-11-05T00:00:00Z") -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def timed(home: Home, clock: Clock) -> Ledger:
    return Ledger(home, clock=clock)


def _acquire(ledger: Ledger, session: str, machine: str = "m1") -> str:
    event = ledger.commit(
        lease.acquire(session, machine=machine, lease_minutes=60, actor=ACTOR)
    ).events[-1]
    return str(event.data["expires_at"])


def _check(ledger: Ledger, session: str, machine: str, now: str) -> Callable[[], None]:
    def run() -> None:
        with ledger.view() as conn:
            lease.require_lease(conn, session, machine, now)

    return run


def test_acquire_renew_and_guard(timed: Ledger, clock: Clock) -> None:
    assert _acquire(timed, "s1") == "2026-11-05T01:00:00Z"
    _check(timed, "s1", "m1", clock.now)()
    clock.now = "2026-11-05T00:30:00Z"
    assert _acquire(timed, "s1") == "2026-11-05T01:30:00Z"  # the holder renews
    with pytest.raises(TswError) as info:
        _check(timed, "s2", "m1", clock.now)()
    assert info.value.details["reason"] == "lease_held"
    with pytest.raises(TswError) as info:
        _check(timed, "s1", "m1", "2026-11-05T01:30:00Z")()
    assert info.value.details["reason"] == "lease_expired"


def test_guard_without_any_lease(timed: Ledger) -> None:
    with pytest.raises(TswError) as info:
        _check(timed, "s1", "m1", "2026-11-05T00:00:00Z")()
    assert info.value.details["reason"] == "lease_missing"


def test_another_session_waits_until_expiry_on_the_same_machine(
    timed: Ledger, clock: Clock
) -> None:
    _acquire(timed, "s1")
    with pytest.raises(TswError) as info:
        _acquire(timed, "s2")
    assert info.value.details["reason"] == "lease_held"
    clock.now = "2026-11-05T01:00:00Z"
    _acquire(timed, "s2")
    with timed.view() as conn:
        row = lease.lease_row(conn)
    assert row is not None
    assert row["holder_session"] == "s2"


def test_another_machine_never_acquires_silently(timed: Ledger, clock: Clock) -> None:
    _acquire(timed, "s1", machine="m1")
    clock.now = "2026-11-06T00:00:00Z"  # long expired
    with pytest.raises(TswError) as info:
        _acquire(timed, "s2", machine="m2")
    assert info.value.details["reason"] == "lease_other_machine"
    with pytest.raises(TswError) as info:
        timed.commit(
            lease.takeover(
                "s2",
                machine="m2",
                from_session="s1",
                user_source=SOURCE,
                lease_minutes=60,
                from_machine=False,
                actor=ACTOR,
            )
        )
    assert info.value.details["reason"] == "lease_other_machine"
    event = timed.commit(
        lease.takeover(
            "s2",
            machine="m2",
            from_session="s1",
            user_source=SOURCE,
            lease_minutes=60,
            from_machine=True,
            actor=ACTOR,
        )
    ).events[-1]
    assert event.type == "controller.lease_taken_over"
    assert event.data["from_machine"] == "m1"
    assert event.data["user_source"] == SOURCE
    _check(timed, "s2", "m2", clock.now)()


def test_takeover_needs_the_users_confirmation_and_the_right_holder(timed: Ledger) -> None:
    _acquire(timed, "s1")
    with pytest.raises(TswError) as info:
        lease.takeover(
            "s2",
            machine="m1",
            from_session="s1",
            user_source={**SOURCE, "text": " "},
            lease_minutes=60,
            from_machine=False,
            actor=ACTOR,
        )
    assert info.value.details == {"reason": "user_source_missing", "missing": ["text"]}
    with pytest.raises(TswError) as info:
        timed.commit(
            lease.takeover(
                "s2",
                machine="m1",
                from_session="s9",
                user_source=SOURCE,
                lease_minutes=60,
                from_machine=False,
                actor=ACTOR,
            )
        )
    assert info.value.details["reason"] == "lease_holder_mismatch"


def test_release_frees_the_lease_for_any_session(timed: Ledger) -> None:
    _acquire(timed, "s1", machine="m1")
    with pytest.raises(TswError) as info:
        timed.commit(lease.release("s2", machine="m1", actor=ACTOR))
    assert info.value.details["reason"] == "lease_held"
    timed.commit(lease.release("s1", machine="m1", actor=ACTOR))
    with timed.view() as conn:
        assert lease.lease_row(conn) is None
    _acquire(timed, "s3", machine="m2")  # a released lease moves even across machines


def test_lease_survives_a_rebuild(timed: Ledger, clock: Clock) -> None:
    _acquire(timed, "s1")
    timed.rebuild()
    _check(timed, "s1", "m1", clock.now)()
