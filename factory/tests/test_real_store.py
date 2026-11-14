"""The live probes refuse a FACTORY_HOME under the real ~/.claude and fail when the real store names their scratch
directory. Each case runs a probe in a subprocess with HOME and TMPDIR in a scratch directory and a fake `claude`; no model runs."""
import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FAKE = '#!/bin/sh\necho called >> "$HOME/claude-calls"\n{}\necho "{{}}"\n'
LEAK = 'echo "$(basename "$(dirname "$PWD")")" >> "$HOME/.claude/factory/x.jsonl"'


class RealStore(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name).resolve()
        (self.home / ".claude" / "factory").mkdir(parents=True)
        (self.home / "bin").mkdir()
        (self.home / "tmp").mkdir()

    def run_script(self, script, *args, fake="", **env):
        claude = self.home / "bin" / "claude"
        claude.write_text(FAKE.format(fake))
        claude.chmod(0o755)
        env = {**{k: v for k, v in os.environ.items() if k != "PROBE_CWD"}, "HOME": str(self.home),
               "TMPDIR": str(self.home / "tmp"), "PATH": f"{self.home / 'bin'}:{os.environ['PATH']}", **env}
        child = subprocess.Popen([sys.executable, str(TESTS / script), *args], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, cwd=self.home, start_new_session=True)
        try:
            out, err = child.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)  # the probe's sleep is a grandchild
            child.communicate()
            raise
        return subprocess.CompletedProcess(child.args, child.returncode, out, err)

    def test_site_manager_refuses_a_home_under_the_real_store(self):
        cwd = self.home / ".claude" / "x"
        cwd.mkdir()
        r = self.run_script("live_site_manager.py", PROBE_CWD=str(cwd))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to run", r.stderr)
        self.assertFalse((self.home / "claude-calls").exists())

    def test_site_manager_fails_when_the_real_store_names_its_scratch_directory(self):
        leaked = self.run_script("live_site_manager.py", fake=LEAK)
        self.assertEqual(leaked.returncode, 1, leaked.stderr)
        scratch = re.search(r"(?m)^scratch: .*/(probe-\w+)$", leaked.stdout).group(1)
        self.assertIn(f"\nEFFECT 1 line(s) of the real store's logs name {scratch}\n", leaked.stdout)
        clean = self.run_script("live_site_manager.py")
        self.assertIn("the run gave no answer", clean.stdout, clean.stderr)
        self.assertNotIn("of the real store's logs name", clean.stdout)

    def test_tech_lead_refuses_a_home_under_the_real_store(self):
        tmp = self.home / ".claude" / "t"
        tmp.mkdir()
        r = self.run_script("live_tech_lead.py", "--dry", TMPDIR=str(tmp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to run", r.stderr)


if __name__ == "__main__":
    unittest.main()
