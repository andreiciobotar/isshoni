import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import ROOT

NOLEFT = os.path.join(ROOT, "tools", "noleft.py")
# Starts a detached sleep, records its pid, lives on for a moment, and exits 0. The child is a shell that
# becomes the sleep after 1 s (same pid): the report must name the sleep. The 1 s only makes the test fail
# without the fix, which reported a process under the command it had when first seen; it is not a pass condition.
LEAK = ("import subprocess, sys, time\n"
        "p = subprocess.Popen(['sh', '-c', 'sleep 1; exec sleep 63.25'], start_new_session=True)\n"
        "open(sys.argv[1], 'w').write(str(p.pid))\n"
        "time.sleep(2)\n")
# Sends itself SIGTERM just before the command starts, then runs `sleep 30` through the wrapper.
EARLY = ("import importlib.util, os, signal, sys\n"
         "spec = importlib.util.spec_from_file_location('noleft', sys.argv[1])\n"
         "noleft = importlib.util.module_from_spec(spec)\n"
         "spec.loader.exec_module(noleft)\n"
         "real = noleft.subprocess.Popen\n"
         "def popen(*args, **kwargs):\n"
         "    os.kill(os.getpid(), signal.SIGTERM)\n"
         "    return real(*args, **kwargs)\n"
         "noleft.subprocess.Popen = popen\n"
         "sys.exit(noleft.main(['sleep', '30']))\n")


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
        self.assertGone(pid)

    def assertGone(self, pid):
        deadline = time.monotonic() + 10  # the failure bound, not the fix
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(alive(pid))

    @staticmethod
    def pid(path):
        if not os.path.exists(path):
            return 0
        with open(path) as f:
            return int(f.read() or 0)  # created but not yet written

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
        self.assertGone(self.pid(pidfile))

    def test_a_first_signal_after_the_command_ended_still_reports_and_kills(self):
        d = self.enterContext(tempfile.TemporaryDirectory())
        pidfile = os.path.join(d, "pid")
        # The pid file is written last, after the wrapper had polls to see the sleep; the command then ends at once.
        leak = ("import subprocess, sys, time\n"
                "p = subprocess.Popen(['sleep', '63.25'], start_new_session=True)\n"
                "time.sleep(0.5)\n"
                "open(sys.argv[1], 'w').write(str(p.pid))\n")
        out = open(os.path.join(d, "out"), "w+")
        self.addCleanup(out.close)
        wrapper = subprocess.Popen([sys.executable, NOLEFT, sys.executable, "-c", leak, pidfile], stdout=out, start_new_session=True)
        self.addCleanup(self.kill_group, wrapper)
        self.addCleanup(lambda: alive(self.pid(pidfile)) and os.kill(self.pid(pidfile), signal.SIGKILL))
        while not self.pid(pidfile):
            time.sleep(0.005)
        os.kill(wrapper.pid, signal.SIGTERM)  # the command ends at once: the wrapper is in its join or its 1 s sweep
        self.assertEqual(wrapper.wait(timeout=10), 1)
        out.seek(0)
        self.assertIn("left behind: 1", out.read())
        self.assertGone(self.pid(pidfile))

    def test_a_signal_gives_exit_code_128_plus_its_number(self):
        started = os.path.join(self.enterContext(tempfile.TemporaryDirectory()), "started")
        wrapper = subprocess.Popen([sys.executable, NOLEFT, "sh", "-c", "echo > %s; exec sleep 30" % started],
                                   stdout=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.kill_group, wrapper)
        while not os.path.exists(started):  # the command runs, so the handlers are installed
            time.sleep(0.01)
        os.kill(wrapper.pid, signal.SIGHUP)
        self.assertEqual(wrapper.wait(timeout=10), 129)

    def test_a_signal_before_the_command_started_still_reports(self):
        p = subprocess.Popen([sys.executable, "-c", EARLY, NOLEFT], stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(self.kill_group, p)
        out, _ = p.communicate(timeout=60)
        self.assertEqual((p.returncode, out), (143, "left behind: 0\n"))

    @staticmethod
    def kill_group(wrapper):
        try:
            os.killpg(wrapper.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wrapper.wait()


if __name__ == "__main__":
    unittest.main()
