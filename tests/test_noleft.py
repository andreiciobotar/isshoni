import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import ROOT

NOLEFT = os.path.join(ROOT, "tools", "noleft.py")
# Starts a detached sleep, records its pid, lives on for a moment, and exits 0.
LEAK = ("import subprocess, sys, time\n"
        "p = subprocess.Popen(['sleep', '63.25'], start_new_session=True)\n"
        "open(sys.argv[1], 'w').write(str(p.pid))\n"
        "time.sleep(0.5)\n")


def alive(pid):
    if pid <= 0:  # os.kill(0, ...) would signal our own process group
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class NoLeftTests(unittest.TestCase):
    def noleft(self, *command):
        """The finished run; its output goes through a file, because a leftover would hold a pipe open."""
        with tempfile.TemporaryFile("w+") as out:
            p = subprocess.run([sys.executable, NOLEFT, *command], stdout=out, timeout=60)
            out.seek(0)
            p.stdout = out.read()
        return p

    def test_a_detached_process_is_named_killed_and_fails_the_run(self):
        pidfile = os.path.join(self.enterContext(tempfile.TemporaryDirectory()), "pid")
        self.addCleanup(lambda: alive(self.pid(pidfile)) and os.kill(self.pid(pidfile), signal.SIGKILL))
        p = self.noleft(sys.executable, "-c", LEAK, pidfile)
        pid = self.pid(pidfile)
        self.assertEqual(p.returncode, 1)
        self.assertIn("left behind: 1", p.stdout)
        self.assertIn("%d sleep 63.25" % pid, p.stdout)
        time.sleep(0.2)
        self.assertFalse(alive(pid))

    @staticmethod
    def pid(path):
        if not os.path.exists(path):
            return 0
        with open(path) as f:
            return int(f.read())

    def test_a_clean_command_passes_its_exit_code_through(self):
        for code in (0, 7):
            p = self.noleft("sh", "-c", "exit %d" % code)
            self.assertEqual((p.returncode, p.stdout), (code, "left behind: 0\n"))


if __name__ == "__main__":
    unittest.main()
