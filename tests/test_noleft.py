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
        "time.sleep(2)\n")


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

    def test_a_second_signal_during_the_grace_does_not_cut_the_run_short(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        pidfile = os.path.join(d, "pid")
        # The shell ignores TERM, so the wrapper waits its grace; a sleep detaches from the shell and outlives it.
        script = 'trap "" TERM; sleep 63.25 & echo $! > %s; sleep 4' % pidfile
        out = open(os.path.join(d, "out"), "w+")
        self.addCleanup(out.close)
        wrapper = subprocess.Popen([sys.executable, NOLEFT, "sh", "-c", script], stdout=out, start_new_session=True)
        self.addCleanup(self.kill_group, wrapper)
        self.addCleanup(lambda: alive(self.pid(pidfile)) and os.kill(self.pid(pidfile), signal.SIGKILL))
        while not self.pid(pidfile):
            time.sleep(0.05)
        for _ in range(2):
            os.kill(wrapper.pid, signal.SIGTERM)
            time.sleep(1)
        self.assertEqual(wrapper.wait(timeout=10), 1)  # without the fix it polls `ps` for ever
        out.seek(0)
        self.assertIn("left behind: 1", out.read())
        time.sleep(0.2)
        self.assertFalse(alive(self.pid(pidfile)))

    def test_a_signal_gives_exit_code_128_plus_its_number(self):
        wrapper = subprocess.Popen([sys.executable, NOLEFT, "sleep", "30"], stdout=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.kill_group, wrapper)
        time.sleep(1)
        os.kill(wrapper.pid, signal.SIGHUP)
        self.assertEqual(wrapper.wait(timeout=10), 129)

    @staticmethod
    def kill_group(wrapper):
        try:
            os.killpg(wrapper.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wrapper.wait()


if __name__ == "__main__":
    unittest.main()
