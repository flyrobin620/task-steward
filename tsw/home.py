"""The data directory ``TSW_HOME`` (DN001 §8.1, DN006 §9.1–§9.2).

The data directory is separate from the product repository. It is located in this order:
the ``TSW_HOME`` environment variable, the path recorded in ``<repo>/.local/tsw-home``, and
finally ``~/.tsw``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsw import __version__
from tsw.errors import rejected
from tsw.events import utc_now

HOME_VERSION = 1
MARKER = "tsw-home.json"
LAYOUT = (
    "config",
    "config/prompts",
    "todo",
    "ledger/events",
    "ledger/imported",
    "runs",
    "outbox",
    "legacy",
    "work",
    "views",
    "cache",
    "cache/checkpoints",
    "locks",
)
# Directories that only tsw writes (DN006 §9.4) and that a backup copies (DN006 §9.5).
BACKED_UP = ("ledger", "runs", "todo", "config", "outbox", "legacy")


def workdir_path(home: Home, recorded: str) -> Path:
    """An execution directory as recorded in the ledger; relative ones are in the data directory."""
    path = Path(recorded).expanduser()
    return path if path.is_absolute() else home.root / path


def same_dir(home: Home, a: str, b: str) -> bool:
    """Whether two recorded execution directories name the same place."""

    def key(value: str) -> str:
        return os.path.normcase(os.path.normpath(str(workdir_path(home, value))))

    return key(a) == key(b)


def resolve_home(env: Mapping[str, str] | None = None, repo_root: Path | None = None) -> Path:
    environ = os.environ if env is None else env
    explicit = environ.get("TSW_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if repo_root is not None:
        pointer = repo_root / ".local" / "tsw-home"
        if pointer.is_file():
            recorded = pointer.read_text(encoding="utf-8").strip()
            if recorded:
                return Path(recorded).expanduser()
    return Path("~/.tsw").expanduser()


@dataclass(frozen=True)
class Home:
    root: Path

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger" / "events"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"


def read_marker(root: Path) -> dict[str, Any]:
    marker = root / MARKER
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise rejected("不是 TaskSteward 数据目录（缺少 tsw-home.json）", path=str(root)) from exc
    except json.JSONDecodeError as exc:
        raise rejected("tsw-home.json 不是合法的 JSON", path=str(marker)) from exc
    if not isinstance(raw, dict):
        raise rejected("tsw-home.json 格式错误", path=str(marker))
    return raw  # pyright: ignore[reportUnknownVariableType]


def open_home(root: Path) -> Home:
    marker = read_marker(root)
    version = marker.get("home_version")
    if version != HOME_VERSION:
        raise rejected(
            "数据目录版本不受支持，请升级 tsw 或迁移数据目录",
            path=str(root),
            home_version=version,
            supported=HOME_VERSION,
        )
    return Home(root)


def init_home(root: Path) -> tuple[Home, bool]:
    """Create the data directory layout. Returns ``(home, created)``; idempotent."""
    if (root / MARKER).exists():
        home = open_home(root)
        for rel in LAYOUT:
            (root / rel).mkdir(parents=True, exist_ok=True)
        return home, False
    if root.exists() and any(root.iterdir()):
        raise rejected("目录非空且不是 TaskSteward 数据目录，拒绝初始化", path=str(root))
    for rel in LAYOUT:
        (root / rel).mkdir(parents=True, exist_ok=True)
    marker: dict[str, Any] = {
        "home_version": HOME_VERSION,
        "created_at": utc_now(),
        "created_by": f"tsw {__version__}",
    }
    _write_marker(root, marker)
    return Home(root), True


def record_product_repo(home: Home, repo: Path) -> None:
    """Remember where the product repository is, for a plugin launched from Claude's cache."""
    path = home.root / MARKER
    marker: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    value = str(repo.resolve())
    if marker.get("product_repo") != value:
        marker["product_repo"] = value
        _write_marker(home.root, marker)


def _write_marker(root: Path, marker: dict[str, Any]) -> None:
    tmp = root / (MARKER + ".tmp")
    tmp.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(root / MARKER)
