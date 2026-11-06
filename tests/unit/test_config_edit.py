"""Pure rules of configuration changes (DN003 §7): operations, changed paths, permission keys."""

from __future__ import annotations

import pytest

from tsw import confedit
from tsw.errors import TswError

pytestmark = pytest.mark.use_case("UC-CONFIG-002")


def test_set_and_unset_apply_in_order_without_touching_the_input() -> None:
    base = {"version": 1, "scheduling": {"max_workers": 5}}
    out = confedit.apply_ops(
        base,
        [
            {"op": "set", "path": "scheduling.prestart.enabled", "value": False},
            {"op": "unset", "path": "scheduling.max_workers"},
            {"op": "set", "path": ["repositories", "my.repo", "path"], "value": "~/r"},
        ],
    )
    assert out == {
        "version": 1,
        "scheduling": {"prestart": {"enabled": False}},
        "repositories": {"my.repo": {"path": "~/r"}},
    }
    assert base == {"version": 1, "scheduling": {"max_workers": 5}}
    assert confedit.apply_ops(base, [{"op": "set", "path": "", "value": {"version": 1}}]) == {
        "version": 1
    }


@pytest.mark.parametrize(
    "ops",
    [
        [],
        [{"op": "move", "path": "a"}],
        [{"op": "set", "path": "a"}],
        [{"op": "unset", "path": ""}],
        [{"op": "set", "path": "", "value": 3}],
        [{"op": "unset", "path": "missing.key"}],
        [{"op": "set", "path": 7, "value": 1}],
    ],
)
def test_malformed_operations_are_refused(ops: list[object]) -> None:
    with pytest.raises(TswError):
        confedit.apply_ops({"version": 1}, ops)


def test_changed_paths_cover_added_removed_and_changed_leaves() -> None:
    before = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    after = {"a": {"b": 2, "c": [1, 2]}, "e": {"f": True}}
    assert confedit.changed_paths(before, after) == ["a.b", "d", "e.f"]
    assert confedit.changed_paths(before, before) == []


@pytest.mark.parametrize(
    ("path", "permission"),
    [
        ("repositories.app.delivery.push", True),
        ("repositories.app.auth_profile", True),
        ("repositories.app.path", False),
        ("repositories.app.aliases", False),
        ("accounts.gh.auth.config_dir", True),
        ("policies.email", True),
        ("roles.worker.permission_mode", True),
        ("roles.orchestrator.session", True),
        ("roles.reviewer.model", True),
        ("roles.worker.model", False),
        ("roles.worker.allow_in_task_subagents", True),
        ("plugins.sessions", True),
        ("input.triage", True),
        ("input.decision_digest.channel", False),
        ("review.block_on", True),
        ("review.retry_limit", False),
        ("scheduling.max_workers", False),
        ("roles", True),  # replacing a parent object reaches the keys below it
    ],
)
def test_permission_keys(path: str, permission: bool) -> None:
    assert (confedit.permission_paths({}, {}, [path]) == [path]) is permission


def test_adding_or_removing_a_repository_is_a_permission_change() -> None:
    before = {"repositories": {"a": {"path": "~/a"}}}
    after = {"repositories": {"b": {"path": "~/b"}}}
    changed = confedit.changed_paths(before, after)
    assert confedit.permission_paths(before, after, changed) == [
        "repositories.a",
        "repositories.b",
    ]


def test_file_digests_and_rendering() -> None:
    assert confedit.text_sha(None) == confedit.NO_FILE
    assert confedit.parse(None) == {}
    assert confedit.parse("[1]") is None
    assert confedit.parse("{bad") is None
    assert confedit.parse(confedit.render({"version": 1, "x": "中"})) == {"version": 1, "x": "中"}
