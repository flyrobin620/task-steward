"""Parsing ``--renumber-local OLD=NEW`` (DN005 §4.4)."""

from __future__ import annotations

import pytest

from tsw.errors import TswError
from tsw.importer.renumber import parse

pytestmark = pytest.mark.use_case("UC-IMPORT-001")


def test_pairs_are_parsed_in_order() -> None:
    assert parse(["T001=T238", "T002=T239"]) == [("T001", "T238"), ("T002", "T239")]
    assert parse(None) == []


@pytest.mark.parametrize(
    "values",
    [
        ["T001"],
        ["T001=T001"],
        ["T001=x"],
        ["T1=T2", "T1=T3"],
        ["T1=T2", "T3=T2"],
        ["T1=T2", "T2=T3"],
    ],
)
def test_malformed_or_overlapping_pairs_are_refused(values: list[str]) -> None:
    with pytest.raises(TswError) as caught:
        parse(values)
    assert caught.value.details["reason"] == "renumber_syntax"
