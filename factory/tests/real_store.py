"""What the live probes check about the real factory store, `~/.claude/factory`."""
import os
import sys
from pathlib import Path

REAL_STORE = Path("~/.claude/factory").expanduser()


def refuse(home):
    """Exit when `home` is under the real `~/.claude`."""
    home, real = os.path.realpath(home), os.path.realpath(REAL_STORE.parent)
    if (home + os.sep).startswith(real + os.sep):
        sys.exit(f"FACTORY_HOME {home} is under the real {real}: refusing to run")


def leaked(name):
    """Lines of every `*.jsonl` of the real store (the gh log included) that name this run's temp directory."""
    return sum(name in line for path in REAL_STORE.glob("*.jsonl")
               for line in path.read_text(errors="replace").splitlines())
