"""Run ``tsw`` from the Claude Code plugin.

Claude Code copies an installed plugin into its plugin cache, so this directory may have no
product repository around it. The ``tsw`` package is looked up in this order: the ``TSW_REPO``
environment variable; the repository this plugin directory sits in (a source checkout or a
local marketplace used in place); the ``product_repo`` that ``tsw init`` recorded in the data
directory (one that claims the session's directory, else ``TSW_HOME``, else ``~/.tsw``, as
``tsw.home.resolve_home``); finally an installed ``tsw`` package.
"""

import json
import os
import sys
from pathlib import Path


def _has_runtime(root: Path) -> bool:
    return (root / "tsw" / "__init__.py").is_file()


def _claiming_home() -> Path | None:
    """The data directory that claims the session's directory (``tsw.home.claiming_home``)."""
    start = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve()
    for folder in (start, *start.parents):
        try:
            marker = json.loads((folder / "tsw-home.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return folder if isinstance(marker, dict) and marker.get("claim_sessions") is True else None
    return None


def _recorded_repo() -> Path | None:
    explicit = os.environ.get("TSW_HOME")
    home = _claiming_home() or (
        Path(explicit).expanduser() if explicit else Path("~/.tsw").expanduser()
    )
    try:
        marker = json.loads((home / "tsw-home.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recorded = marker.get("product_repo") if isinstance(marker, dict) else None
    return Path(recorded) if isinstance(recorded, str) and recorded else None


def locate() -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("TSW_REPO"):
        candidates.append(Path(os.environ["TSW_REPO"]).expanduser())
    candidates.append(Path(__file__).resolve().parent.parent.parent)
    recorded = _recorded_repo()
    if recorded is not None:
        candidates.append(recorded)
    return next((root for root in candidates if _has_runtime(root)), None)


def launch() -> int:
    root = locate()
    if root is not None:
        sys.path.insert(0, str(root))
    try:
        from tsw.cli import main  # noqa: PLC0415 - the path is only known at run time
    except ImportError:
        sys.stderr.write(
            "tsw: 找不到 TaskSteward 运行时。请设置 TSW_REPO 指向产品仓库，"
            "或在产品仓库中运行 tsw init 记录其位置。\n"
        )
        # A hook must never break the session; the MCP server reports the failure.
        return 0 if sys.argv[1:2] == ["guard"] else 1
    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(launch())
