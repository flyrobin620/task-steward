"""A server that exits at end of input but leaves a child holding its output pipe.

Arguments: the stubborn child script and the file it writes its PID to.
"""

import subprocess
import sys

# The child keeps the output pipe; its own input is not the parent's.
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], stdin=subprocess.DEVNULL)
sys.stdin.read()
sys.exit(0)
