"""How ``tools/gate.py`` splits a tier's test files into parallel pytest processes."""

from __future__ import annotations

from pathlib import Path

from tools.gate import _shards


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#" * size, encoding="utf-8")
    return path


def test_every_test_file_is_in_exactly_one_shard_subdirectories_included(tmp_path: Path) -> None:
    tier = tmp_path / "component"
    files = {
        _write(tier / "test_big.py", 900),
        _write(tier / "test_mid.py", 500),
        _write(tier / "test_small.py", 100),
        _write(tier / "nested" / "test_deep.py", 400),
        _write(tier / "other_test.py", 300),
    }
    _write(tier / "helpers.py", 5000)
    _write(tier / "conftest.py", 5000)

    shards = _shards(tier, 3)

    assert len(shards) == 3
    flat = [path for shard in shards for path in shard]
    assert sorted(flat) == sorted(files)
    sizes = sorted(sum(path.stat().st_size for path in shard) for shard in shards)
    assert sizes == [600, 700, 900]
    assert _shards(tier, 3) == shards


def test_no_more_shards_than_files_and_one_job_is_one_shard(tmp_path: Path) -> None:
    tier = tmp_path / "unit"
    _write(tier / "test_a.py", 10)
    _write(tier / "test_b.py", 20)

    assert len(_shards(tier, 8)) == 2
    assert _shards(tier, 1) == [[tier / "test_a.py", tier / "test_b.py"]]


def test_a_tier_without_test_files_runs_the_directory(tmp_path: Path) -> None:
    tier = tmp_path / "surface"
    tier.mkdir()

    assert _shards(tier, 4) == [[tier]]
