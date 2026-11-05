"""A server that ignores the end of its input: records its PID, then sleeps."""

import os
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(120)
