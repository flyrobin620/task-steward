from __future__ import annotations

import pytest

from tests.support import spec
from tsw.errors import TswError
from tsw.tasks import render_spec, validate_spec

pytestmark = [pytest.mark.use_case("UC-TASK-001"), pytest.mark.transition("T01")]


def test_complete_spec_passes() -> None:
    validate_spec(spec(task_id="T010"))


@pytest.mark.parametrize("field", ["title", "repo", "goal"])
def test_missing_text_fields_are_rejected(field: str) -> None:
    with pytest.raises(TswError) as info:
        validate_spec(spec(**{field: "  "}))
    assert field in info.value.details["missing"]


def test_acceptance_needs_at_least_one_item() -> None:
    with pytest.raises(TswError) as info:
        validate_spec(spec(acceptance=["", " "]))
    assert info.value.details["missing"] == ["acceptance"]


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(TswError):
        validate_spec(spec(kind="ops"))


@pytest.mark.parametrize("task_id", ["1", "T1", "TX01", "T1234567"])
def test_task_id_format(task_id: str) -> None:
    with pytest.raises(TswError):
        validate_spec(spec(task_id=task_id))


def test_rendered_spec_lists_fields_in_chinese() -> None:
    text = render_spec("T007", spec(acceptance=["a", "", "b"]))
    assert text.splitlines()[0] == "### T007：示例任务"
    assert "- 类型：代码" in text
    assert "  - a\n  - b\n" in text
