"""Reproduction of the flake a fake clock on the `time` module causes: children that close their pipes but exit late.

Every child process looks "still running" for its first LATE seconds after its pipes close, as a real child does on a
loaded machine; `Popen._wait` then loops on `time.sleep`. With a fake `time.sleep` that loop spins, each spin moves the fake
clock, and the clock jumps by tens of minutes. It fails while the fixture patches the `time` module and passes while it patches only the script's own name for it.

    PYTHONPATH=factory/tests python3 factory/tests/late_exit.py test_collision
"""
import os
import subprocess
import sys
import time
import unittest

LATE = 0.2
real, first = subprocess.Popen._try_wait, {}


def late(self, flags):
    if flags == os.WNOHANG:
        if time.monotonic() - first.setdefault(self.pid, time.monotonic()) < LATE:
            return 0, 0
    return real(self, flags)


subprocess.Popen._try_wait = late
unittest.main(module=None, argv=["late_exit", *sys.argv[1:]])
