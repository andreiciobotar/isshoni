"""docker/start: the dispatchers a restart left out are started, and nothing there stops the container coming up."""
import json
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import ROOT
from tests.test_host import CLAUDE_STUB, write_exe

START = Path(ROOT) / "docker/start"
SLEEP = "#!/bin/sh\nexec sleep 30\n"


class StartScript(unittest.TestCase):
    def setUp(self):
        self.fresh()

    def fresh(self):
        """A new HOME with stub claude and a supercronic that ends at once."""
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        write_exe(self.bin / "claude", CLAUDE_STUB)
        write_exe(self.bin / "supercronic", "#!/bin/sh\nexit 0\n")
        (self.home / "tmp").mkdir()
        (self.home / ".container-id").write_text("box1\n")  # this container has started before

    def settings(self, content):
        path = self.home / ".claude/factory/settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))

    def repos(self, *names):
        for name in names:
            (self.home / "code" / name).mkdir(parents=True)

    def running(self, *names):
        (self.home / "stub-state.json").write_text(json.dumps({"agents": list(names), "plugins": [], "marketplaces": []}))

    def dashboard(self):
        keys = self.home / ".claude/factory/dashboard/authorized_keys"
        keys.parent.mkdir(parents=True, exist_ok=True)
        keys.write_text("write ssh-ed25519 AAAA\n")
        (self.home / ".isshoni/bin").mkdir(parents=True)
        write_exe(self.home / ".isshoni/bin/isshoni-tui", SLEEP)

    def env(self):
        return {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(self.home), "HOSTNAME": "box1", "TMPDIR": str(self.home / "tmp")}

    def start(self):
        # The pipes stay open until the background `revive` ends, so run() returns after it.
        return subprocess.run(["/bin/sh", str(START)], env=self.env(), capture_output=True, text=True, timeout=30)

    def bg(self):
        log = self.home / "stub-claude.log"
        calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        return [c for c in calls if "--bg" in c["argv"]]

    def test_starts_the_missing_ones_and_not_the_running_ones(self):
        for dashboard in (False, True):
            with self.subTest(dashboard=dashboard):
                self.fresh()
                self.settings({"dispatchers": ["dispatcher-a", "dispatcher-b"], "monitor": "x"})
                self.repos("a", "b")
                self.running("dispatcher-a")
                if dashboard:
                    self.dashboard()
                done = self.start()
                # Linux's sh reports the dashboard child that the stop killed; nothing else may be said.
                self.assertEqual((done.returncode, done.stderr.replace("Terminated\n", "")), (0, ""))
                self.assertEqual(self.bg(), [{"argv": ["--bg", "-n", "dispatcher-b", "--permission-mode", "auto", "/factory:dispatch"],
                                              "cwd": str(self.home / "code/b")}])

    def test_a_new_container_records_its_id_and_starts_nothing(self):
        for before in (None, "other\n"):
            with self.subTest(before=before):
                self.fresh()
                (self.home / ".container-id").unlink()
                if before:
                    (self.home / ".container-id").write_text(before)
                self.settings({"dispatchers": ["dispatcher-a"]})
                self.repos("a")
                done = self.start()
                self.assertEqual((done.returncode, done.stderr, self.bg()), (0, "", []))
                self.assertEqual((self.home / ".container-id").read_text(), "box1\n")

    def test_skips_and_still_comes_up(self):
        broken = lambda: (self.settings({"dispatchers": ["dispatcher-a"]}), (self.home / "agents-broken").write_text(""))
        cases = {  # name: (arrange, stderr lines)
            "no settings file": (lambda: None, 0),
            "no key": (lambda: self.settings({"monitor": "x"}), 0),
            "not json": (lambda: self.settings("{"), 1),
            "not an object": (lambda: self.settings("[]"), 1),
            "no repository directory": (lambda: self.settings({"dispatchers": ["dispatcher-gone"]}), 1),
            "not logged in": (broken, 1),
            "no claude": (lambda: (self.settings({"dispatchers": ["dispatcher-a"]}), (self.bin / "claude").unlink()), 1),
        }
        for name, (arrange, lines) in cases.items():
            with self.subTest(name):
                self.fresh()
                self.repos("a")
                arrange()
                done = self.start()
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.bg(), [])
                self.assertEqual(len(done.stderr.splitlines()), lines, done.stderr)

    def test_term_ends_the_script_with_the_dashboard(self):
        self.settings({"dispatchers": ["dispatcher-a"]})
        self.repos("a")
        self.dashboard()
        write_exe(self.bin / "supercronic", SLEEP)
        p = subprocess.Popen(["/bin/sh", str(START)], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(p.kill)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.stderr.close)
        for _ in range(100):
            if self.bg():
                break
            time.sleep(0.1)
        self.assertEqual([c["cwd"] for c in self.bg()], [str(self.home / "code/a")])
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
