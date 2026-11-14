import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_factory import ROOT, factory

BIN = Path(factory.__file__).resolve()


class FactoryHome(unittest.TestCase):
    """`factory/bin/factory` as a subprocess with `FACTORY_HOME` and a separate `HOME` holding a stored App."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home, self.fh, self.repo = self.tmp / "home", self.tmp / "fh", self.tmp / "repo"
        for d in (self.home / ".claude" / "factory" / "identity", self.fh, self.repo):
            d.mkdir(parents=True)
        (self.home / ".claude" / "factory" / "identity" / "app.json").write_text("[]")  # a stored App that cannot be read
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def factory(self, *argv, home="unset", stdin=""):
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_HOME"}
        env["HOME"] = str(self.home)
        if home != "unset":
            env["FACTORY_HOME"] = home
        p = subprocess.run([sys.executable, str(BIN), *argv], cwd=self.repo, env=env, input=stdin,
                           capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def test_settings_come_from_it(self):
        (self.fh / "settings.toml").write_text("[bad")
        code, _, err = self.factory("status", home=str(self.fh))
        self.assertEqual(code, 2)
        self.assertIn(f"{self.fh / 'settings.toml'}: ", err)
        self.assertEqual(self.factory("status")[0], 0)  # the real home has none

    def test_a_write_lands_in_it(self):
        code, out, _ = self.factory("feedback", "decide", "x. Why: y", home=str(self.fh))
        self.assertEqual(code, 0)
        (log,) = self.fh.glob("feedback-*.jsonl")
        self.assertEqual(json.loads(log.read_text())["id"], out.strip())
        self.assertFalse((self.home / ".claude" / "factory" / log.name).exists())
        self.assertEqual(self.factory("feedback", "--json", home=str(self.fh))[1].count('"id"'), 1)
        self.assertEqual(self.factory("feedback", "--json")[1].strip(), "[]")

    def test_the_hook_records_in_it(self):
        payload = json.dumps({"hook_event_name": "Stop", "session_id": "s1", "cwd": str(self.repo)})
        self.assertEqual(self.factory("hook", home=str(self.fh), stdin=payload), (0, "", ""))
        self.assertEqual(len(list(self.fh.glob("events-*.jsonl"))), 1)
        self.assertEqual(list((self.home / ".claude" / "factory").glob("events-*")), [])

    def test_env_reads_the_identity_from_it(self):
        code, out, _ = self.factory("env")
        self.assertEqual(code, 1)  # the unreadable App in HOME: the lock-down comes back
        self.assertIn("GH_TOKEN", out)
        self.assertEqual(self.factory("env", home=str(self.fh)), (0, "", ""))

    def test_a_relative_value_is_refused(self):
        for argv in (("status",), ("env",), ("feedback",)):
            with self.subTest(argv):
                code, out, err = self.factory(*argv, home="relative/dir")
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(len(err.splitlines()), 1)
                self.assertIn("FACTORY_HOME", err)
        self.assertEqual(self.factory("hook", home="relative/dir", stdin="{}"), (0, "", ""))  # a hook never fails
        self.assertEqual(list(self.repo.iterdir()), [self.repo / ".git"])

    def test_an_empty_value_and_a_tilde_are_the_default_and_the_home(self):
        self.assertEqual(self.factory("env", home="")[0], 1)  # empty: ~/.claude/factory, where the stored App is
        self.assertEqual(self.factory("env", home="~/.claude/factory")[0], 1)
        (self.home / "other").mkdir()
        self.assertEqual(self.factory("env", home="~/other"), (0, "", ""))


class Schedule(unittest.TestCase):
    def label_and_env(self, home):
        store = Path(home) if home else factory.DEFAULT_STORE
        installed = []

        class Backend:
            def install(self, *args):
                installed.append(args)
        with mock.patch.object(factory, "STORE", store), mock.patch.object(factory, "job_path", return_value="/bin"), \
                mock.patch.object(factory.Path, "mkdir"):
            label = factory.schedule_install(mock.Mock(root=ROOT), "d", Backend())
        return label, installed[0][3]

    def test_without_the_variable_the_job_is_what_it_was(self):
        label, env = self.label_and_env(None)
        self.assertEqual(label, "com.claude.factory.watch.repo-" + hashlib.sha256(ROOT.encode()).hexdigest()[:8])
        self.assertEqual(env, {"PATH": "/bin"})

    def test_with_it_the_job_is_another_one_and_carries_it(self):
        real, _ = self.label_and_env(None)
        a, env = self.label_and_env("/tmp/a")
        b, _ = self.label_and_env("/tmp/b")
        self.assertEqual(len({real, a, b}), 3)
        self.assertEqual(a, self.label_and_env("/tmp/a")[0])
        self.assertEqual(env, {"PATH": "/bin", "FACTORY_HOME": "/tmp/a"})

    def test_the_real_home_written_out_is_the_real_home(self):
        real = Path(factory.DEFAULT_STORE).expanduser()
        label, env = self.label_and_env(str(real))
        self.assertEqual(label, self.label_and_env(None)[0])
        self.assertEqual(env, {"PATH": "/bin"})

    def test_another_home_leaves_a_legacy_plist_alone(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}), \
                mock.patch.object(factory, "STORE", Path("/tmp/a")), \
                mock.patch.object(factory, "run", lambda argv, **kw: (0, "", "")):
            label = factory.job_label(ROOT)
            legacy = Path(home) / "Library" / "LaunchAgents" / f"{label.rsplit('-', 1)[0]}.plist"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(factory.plistlib.dumps({"WorkingDirectory": ROOT}))
            backend = factory.Launchd()
            backend.install(label, ["/py"], ROOT, {"PATH": "/bin"}, "/log")
            backend.remove(label, ROOT)
            self.assertTrue(legacy.exists())

    def test_each_backend_writes_the_environment(self):
        env, calls = {"PATH": "/bin", "FACTORY_HOME": "/tmp/a b"}, []
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home, "HOME": home}), \
                mock.patch.object(factory, "run", lambda argv, **kw: calls.append((argv, kw.get("input"))) or (0, "", "")):
            factory.Systemd().install("job", ["/py"], "/repo", env, "/log")
            unit = (Path(home) / "systemd" / "user" / "job.service").read_text()
            factory.Cron().install("job", ["/py"], "/repo", env, "/log")
            factory.Launchd().install("job", ["/py"], "/repo", env, "/log")
            plist = (Path(home) / "Library" / "LaunchAgents" / "job.plist").read_bytes()
        self.assertIn('Environment="PATH=/bin"\nEnvironment="FACTORY_HOME=/tmp/a b"\nExecStart=/py\n', unit)
        self.assertIn("&& PATH=/bin FACTORY_HOME='/tmp/a b' /py >>", next(i for _, i in calls if i))
        self.assertEqual(factory.plistlib.loads(plist)["EnvironmentVariables"], env)


if __name__ == "__main__":
    unittest.main()
