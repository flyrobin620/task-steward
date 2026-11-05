"""Run ``tsw`` from the plugin: the product repository (this plugin's parent) is put on the path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tsw.cli import main

sys.exit(main(sys.argv[1:]))
