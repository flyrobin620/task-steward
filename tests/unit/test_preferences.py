"""Pure rules of standing preferences (DN007 §6, §6.1, §6.2): which reach a scope, which are in
force there once replaced, overridden or revoked, and the numbers a goal cites."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tsw.context.index import decision_docs
from tsw.events import Event
from tsw.preferences import applies, cited, in_force, standing_items


def row(
    ref: str, scope: str, *, supersedes: tuple[str, ...] = (), by: str | None = None
) -> dict[str, Any]:
    return {
        "ref": ref,
        "kind": "standing",
        "scope": scope,
        "task_id": None,
        "topic": "t",
        "text": f"text of {ref}",
        "verified_source": json.dumps({"channel": "main-session"}),
        "supersedes": json.dumps(list(supersedes)),
        "superseded_by": by,
        "recorded_at": "2026-11-15T00:00:00Z",
        "source_seq": int(ref.split("-")[1]),
    }


def state(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {i["ref"]: i["usable_as_basis"] for i in items}


@pytest.mark.use_case("UC-CONTEXT-006")
@pytest.mark.parametrize(
    ("scope", "where", "reaches"),
    [
        ("global", ("demo", {"demo"}), True),
        ("repo:demo", ("demo", {"demo"}), True),
        ("repo:demo", ("other", {"other"}), False),
        ("project:alpha", ("demo", {"demo", "alpha"}), True),
        ("project:alpha", ("demo", {"demo"}), False),
        ("repo:demo", None, True),
    ],
)
def test_a_preference_reaches_its_own_scope(
    scope: str, where: tuple[str, set[str]] | None, reaches: bool
) -> None:
    assert applies(scope, where) is reaches


@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_revoked_preference_is_out_of_force_and_lifts_the_override_it_made() -> None:
    rows = [row("P-1", "global"), row("P-2", "repo:demo", supersedes=("P-1",))]
    demo = ("demo", {"demo"})
    assert state(standing_items(rows, demo)) == {"P-1": False, "P-2": "in_scope"}
    revoked = {"P-2": {"time": "2026-11-15T01:00:00Z", "reason": "不要了"}}
    items = standing_items(rows, demo, revoked)
    assert state(items) == {"P-1": "in_scope", "P-2": False}
    assert items[1]["revoked"] == {"time": "2026-11-15T01:00:00Z", "reason": "不要了"}
    assert not in_force(items[1]) and in_force(items[0])
    # Globally, the override no longer shows either.
    assert "overridden_in" not in standing_items(rows, None, revoked)[0]


@pytest.mark.use_case("UC-CONTEXT-006")
def test_an_override_replaced_in_its_scope_holds_until_the_replacement_is_revoked() -> None:
    rows = [
        row("P-1", "global"),
        row("P-2", "repo:demo", supersedes=("P-1",), by="P-3"),
        row("P-3", "repo:demo", supersedes=("P-2",)),
    ]
    demo = ("demo", {"demo"})
    assert state(standing_items(rows, demo)) == {"P-1": False, "P-2": False, "P-3": "in_scope"}
    lifted = standing_items(rows, demo, {"P-3": {"time": "t", "reason": "r"}})
    assert state(lifted) == {"P-1": "in_scope", "P-2": False, "P-3": False}


@pytest.mark.use_case("UC-CONTEXT-006")
def test_a_goal_cites_preference_numbers_only_as_whole_words() -> None:
    goal = "适用：P-4（global）、P-12；重复 P-4；不算：SP-1、P-1x、P-0、P-3-a、XP-7"
    assert cited(goal) == ["P-4", "P-12"]
    assert cited("没有引用") == []


@pytest.mark.use_case("UC-CONTEXT-006")
def test_revocations_are_not_task_decisions_in_the_index() -> None:
    revoked = Event(
        schema=1,
        sequence=9,
        time="2026-11-15T00:00:00Z",
        type="user.preference_revoked",
        actor={"role": "orchestrator"},
        prev="0" * 64,
        checksum="1" * 64,
        task_id=None,
        revision=None,
        data={
            "ref": "P-1",
            "reason": "r",
            "source": {
                "channel": "main-session",
                "principal": "owner",
                "message_ref": "m",
                "text": "不要了",
                "target": ["preference", "P-1", "revoke"],
            },
        },
    )
    assert decision_docs(revoked) == []
