"""The live probes refuse a FACTORY_HOME under the real ~/.claude and fail when the real store names their scratch
directory. Each case runs a probe in a subprocess with HOME and TMPDIR in a scratch directory and a fake `claude`; no model runs."""
import os
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
        env = {**os.environ, "HOME": str(self.home), "TMPDIR": str(self.home / "tmp"),
               "PATH": f"{self.home / 'bin'}:{os.environ['PATH']}", **env}
        return subprocess.run([sys.executable, str(TESTS / script), *args], env=env, capture_output=True, text=True,
                              cwd=self.home, timeout=120)

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
        self.assertRegex(leaked.stdout, r"(?m)^EFFECT 1 line\(s\) of the real store's logs name probe-")
        clean = self.run_script("live_site_manager.py")
        self.assertNotIn("of the real store's logs name", clean.stdout, clean.stderr)

    def test_tech_lead_refuses_a_home_under_the_real_store(self):
        tmp = self.home / ".claude" / "t"
        tmp.mkdir()
        r = self.run_script("live_tech_lead.py", "--dry", TMPDIR=str(tmp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to run", r.stderr)


if __name__ == "__main__":
    unittest.main()
