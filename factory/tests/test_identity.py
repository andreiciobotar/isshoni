import io
import json
import os
import stat
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import test_wizard
from test_factory import ROOT, WT, factory, invoke, repo_answers
from test_wizard import TOKEN, FakeGitHub

real_subprocess_run = subprocess.run
BOT = "factory-octocat[bot]"
EMAIL = "99999999+factory-octocat[bot]@users.noreply.github.com"
MINT = ("POST", "/app/installations/7654321/access_tokens")
TASK = f"{WT}/my-task"
RETRO = {"defects": [], "slowed": [], "rules_missing": [], "rules_dead_weight": [], "brief_questions": []}


def expiring(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


class Identity(test_wizard.Wizard):
    def installed(self):
        self.stored(installation_id=7654321, bot_login=BOT, bot_id=99999999, bot_email=EMAIL)

    def github(self, status=201, lifetime=3600):
        self.gh = FakeGitHub({MINT: (status, {"token": TOKEN, "expires_at": expiring(lifetime)})})
        patcher = mock.patch.object(factory, "github_api", self.gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cache(self, content):
        (self.identity / "token.json").write_text(content if isinstance(content, str) else json.dumps(content))

    def mints(self):
        return sum(c[:2] == MINT for c in self.gh.calls)

    def cli(self, *argv, **env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            rc = factory.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class AppTokenTests(Identity):
    def setUp(self):
        super().setUp()
        self.installed()

    def token(self):
        return factory.app_token(factory.acting_identity())

    def test_first_call_mints_and_caches_privately(self):
        self.github()
        self.assertEqual(self.token(), TOKEN)
        self.assertEqual(self.mints(), 1)
        cache = self.identity / "token.json"
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
        self.assertEqual(json.loads(cache.read_text())["token"], TOKEN)
        self.assertEqual(factory.epoch(json.loads(cache.read_text())["expires_at"]) > time.time() + 3000, True)
        self.assertEqual(self.files(), ["app.json", "key.pem", "token.json"])

    def test_cached_token_is_reused(self):
        self.github()
        self.token()
        self.assertEqual(self.token(), TOKEN)
        self.assertEqual(self.mints(), 1)

    def test_minted_again_when_the_cache_is_not_good_enough(self):
        stale = {
            "under the margin": {"token": "old", "expires_at": expiring(600)},
            "expired": {"token": "old", "expires_at": expiring(-60)},
            "no expiry": {"token": "old"},
            "corrupt": "{not json",
            "not an object": "[]",
        }
        for name, content in stale.items():
            with self.subTest(name):
                self.github()
                self.cache(content)
                self.assertEqual(self.token(), TOKEN)
                self.assertEqual(self.mints(), 1)
                self.assertEqual(json.loads((self.identity / "token.json").read_text())["token"], TOKEN)

    def test_a_failing_cache_write_still_returns_the_token(self):
        self.github()
        self.identity.chmod(0o500)
        self.addCleanup(self.identity.chmod, 0o700)
        if os.access(self.identity, os.W_OK):
            self.skipTest("directory modes are not enforced here (root)")
        self.assertEqual(self.token(), TOKEN)
        self.assertEqual(self.files(), ["app.json", "key.pem"])
        rc, out, err = self.cli("env")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(f"export GH_TOKEN={TOKEN}", out.splitlines())

    def test_a_failed_mint_uses_a_cached_token_that_has_not_expired(self):
        self.github(status=500)
        self.cache({"token": "cached", "expires_at": expiring(600)})
        self.assertEqual(self.token(), "cached")
        self.cache({"token": "cached", "expires_at": expiring(-5)})
        with self.assertRaises(factory.Fail):
            self.token()
        rc, out, err = self.cli("env")
        self.assertEqual(rc, 1)
        self.assertIn("export GH_TOKEN=unavailable", out.splitlines())

    def test_a_refused_mint_fails_and_leaves_nothing_behind(self):
        self.github(status=401)
        with self.assertRaises(factory.Fail):
            self.token()
        self.assertEqual(self.files(), ["app.json", "key.pem"])


class ActingIdentityTests(Identity):
    def test_only_an_installed_app_acts(self):
        self.assertIsNone(factory.acting_identity())
        self.stored()
        self.assertIsNone(factory.acting_identity())
        self.installed_over()
        self.assertEqual(factory.acting_identity()["bot_login"], BOT)

    def installed_over(self):
        data = json.loads((self.identity / "app.json").read_text())
        (self.identity / "app.json").write_text(json.dumps({**data, "bot_login": BOT, "bot_email": EMAIL}))


class EnvTests(Identity):
    def test_nothing_without_an_acting_identity(self):
        self.assertEqual(self.cli("env"), (0, "", ""))
        self.stored()
        self.assertEqual(self.cli("env"), (0, "", ""))

    def test_the_exports(self):
        self.installed()
        self.github()
        rc, out, err = self.cli("env")
        self.assertEqual((rc, err), (0, ""))
        for line in (f"export GH_TOKEN={TOKEN}", "export GIT_TERMINAL_PROMPT=0", "export GIT_CONFIG_COUNT=4",
                     "export GIT_AUTHOR_NAME='factory-octocat[bot]'", "export GIT_COMMITTER_NAME='factory-octocat[bot]'",
                     f"export GIT_AUTHOR_EMAIL='{EMAIL}'", f"export GIT_COMMITTER_EMAIL='{EMAIL}'",
                     "export GIT_CONFIG_KEY_0=credential.https://github.com.helper", "export GIT_CONFIG_VALUE_0=''",
                     "export GIT_CONFIG_KEY_2=url.https://github.com/.insteadOf",
                     "export GIT_CONFIG_VALUE_2=git@github.com:", "export GIT_CONFIG_VALUE_3=ssh://git@github.com/"):
            self.assertIn(line, out.splitlines())
        self.assertNotIn(TOKEN, out.split("GIT_CONFIG_VALUE_1")[1])

    def test_a_mint_failure_makes_git_and_gh_fail(self):
        self.installed()
        self.github(status=500)
        rc, out, err = self.cli("env")
        self.assertEqual(rc, 1)
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(err.startswith("factory: no App token: GitHub POST /app/installations/7654321/access_tokens failed"))
        self.assertIn("export GH_TOKEN=unavailable", out.splitlines())
        for text in (out, err):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("PRIVATE KEY", text)

    def test_a_corrupt_app_json_fails_closed(self):
        self.installed()
        (self.identity / "app.json").write_text("{")
        rc, out, err = self.cli("env")
        self.assertEqual((rc, out), (1, "export GH_TOKEN=unavailable\n"))
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(err.startswith("factory: no App token: "))

    def test_in_a_real_shell_with_real_git(self):
        self.installed()
        self.github()
        _, out, _ = self.cli("env")
        answering = "!f() { echo username=human; echo password=leaked; }; f"
        (self.home / ".gitconfig").write_text(f"[credential]\n\thelper = {answering}\n"
                                              f"[credential \"https://github.com\"]\n\thelper = {answering}\n"
                                              "[user]\n\tname = Human\n\temail = human@example.com\n")
        repo = self.home / "repo"
        repo.mkdir()
        script = ('eval "$OUT"; git init -q . && git remote add origin git@github.com:octocat/one.git && '
                  'git remote get-url --push origin && printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill && '
                  'git commit -q --allow-empty -m x && git log -1 --format="%an|%ae|%cn|%ce"')
        done = subprocess.run(["sh", "-c", script], cwd=repo, capture_output=True, text=True, timeout=30,
                              env={"PATH": os.environ["PATH"], "HOME": str(self.home), "OUT": out, "GIT_CONFIG_NOSYSTEM": "1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), [
            "https://github.com/octocat/one.git", "protocol=https", "host=github.com", "username=x-access-token",
            f"password={TOKEN}", f"{BOT}|{EMAIL}|{BOT}|{EMAIL}"])


class HookTests(Identity):
    LINE = f'eval "$({factory.shlex.quote(str(factory.FACTORY))} env)"'

    def setUp(self):
        super().setUp()
        self.env_file = self.home / "claude-env.sh"

    def hook(self, event="SessionStart", project=TASK, env_file=True, **payload):
        env = {"CLAUDE_PROJECT_DIR": project, **({"CLAUDE_ENV_FILE": str(self.env_file)} if env_file else {})}
        stdin = io.StringIO(json.dumps({"hook_event_name": event, "session_id": "s1", "cwd": project, **payload}))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(factory, "follow_dispatcher"), redirect_stdout(out), redirect_stderr(err):
            rc = factory.main(["hook"])
        self.assertEqual((rc, out.getvalue(), err.getvalue()), (0, "", ""))

    def test_a_lead_gets_the_line_once_and_keeps_other_content(self):
        self.env_file.write_text("export OTHER=1")
        self.hook(source="startup")
        self.hook(source="resume")
        self.assertEqual(self.env_file.read_text(), f"export OTHER=1\n{self.LINE}\n")

    def test_the_line_does_not_need_an_identity_or_a_file(self):
        self.hook()
        self.assertEqual(self.env_file.read_text(), self.LINE + "\n")

    def test_other_sessions_and_events_get_nothing(self):
        cases = {"outside a worktree": dict(project=ROOT), "a subagent": dict(agent_id="a1"),
                 "another event": dict(event="UserPromptSubmit"), "no env file": dict(env_file=False)}
        for name, kw in cases.items():
            with self.subTest(name):
                self.hook(**kw)
                self.assertFalse(self.env_file.exists())

    def test_a_broken_env_file_path_still_exits_zero(self):
        self.env_file = self.home / "missing" / "env.sh"
        self.hook()


class RunTests(Identity):
    def spawn(self, argv, **kw):
        """subprocess.run, except that `gh` and `git` are recorded instead of started (openssl signs for real)."""
        if argv[0] == "openssl":
            return real_subprocess_run(argv, **kw)
        self.started.append((argv, kw["env"]))
        return subprocess.CompletedProcess(argv, 0, "out", "")

    def run_with(self, argv):
        self.started = []
        with mock.patch.object(factory.subprocess, "run", self.spawn):
            result = factory.run(argv)
        return result, self.started[0][1] if self.started else "not started"

    def test_gh_acts_as_the_app(self):
        self.installed()
        self.github()
        result, env = self.run_with(["gh", "pr", "list"])
        self.assertEqual(result, (0, "out", ""))
        self.assertEqual(env, {**os.environ, "GH_TOKEN": TOKEN})

    def test_other_commands_and_no_identity_pass_the_environment_on(self):
        self.github()
        self.assertIsNone(self.run_with(["gh", "pr", "list"])[1])
        self.installed()
        self.assertIsNone(self.run_with(["git", "status"])[1])
        self.assertEqual(self.mints(), 0)

    def test_an_unwritable_identity_directory_does_not_break_gh(self):
        self.installed()
        self.github()
        self.identity.chmod(0o500)
        self.addCleanup(self.identity.chmod, 0o700)
        if os.access(self.identity, os.W_OK):
            self.skipTest("directory modes are not enforced here (root)")
        result, env = self.run_with(["gh", "pr", "list"])
        self.assertEqual(result, (0, "out", ""))
        self.assertEqual(env["GH_TOKEN"], TOKEN)

    def test_a_mint_failure_is_a_failed_gh_call(self):
        self.installed()
        self.github(status=500)
        result, env = self.run_with(["gh", "pr", "list"])
        rc, out, err = result
        self.assertEqual(env, "not started")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("failed", err)


class StatusTests(Identity):
    def status(self, stored, *flags):
        with mock.patch.object(factory, "load_identity", return_value=stored):
            return invoke(["status", *flags], repo_answers([]))[1]

    def test_the_line_says_who_acts(self):
        self.assertIn("\nacting on GitHub with your gh login (no App identity: factory wizard)\n", self.status(None))
        self.assertIn("\nacting on GitHub with your gh login (the App is stored but not installed: factory wizard)\n",
                      self.status({"app_id": 1}))
        self.assertIn(f"\nacting on GitHub as {BOT} (the factory's App)\n", self.status({"bot_login": BOT}))

    def test_json_names_the_bot_or_null(self):
        self.assertIsNone(json.loads(self.status(None, "--json"))["identity"])
        self.assertIsNone(json.loads(self.status({"app_id": 1}, "--json"))["identity"])
        self.assertEqual(json.loads(self.status({"bot_login": BOT}, "--json"))["identity"], BOT)


class RetroTests(Identity):
    def retro(self, *argv, stdin=""):
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            return self.cli("retro", *argv, CLAUDE_PROJECT_DIR=TASK)

    def test_reads_a_file(self):
        path = self.home / "retro.json"
        path.write_text(json.dumps(RETRO))
        self.assertEqual(self.retro(str(path), stdin="not json"), (0, "", ""))
        (record,) = list(factory.read_log("retros"))
        self.assertEqual({k: record[k] for k in RETRO}, RETRO)

    def test_stdin_is_still_the_default(self):
        self.assertEqual(self.retro(stdin=json.dumps(RETRO)), (0, "", ""))
        self.assertEqual(len(list(factory.read_log("retros"))), 1)

    def test_an_unreadable_file_is_refused(self):
        rc, _, err = self.retro(str(self.home / "missing.json"))
        self.assertEqual(rc, 2)
        self.assertIn("retro: cannot read", err)
        self.assertEqual(list(factory.read_log("retros")), [])


if __name__ == "__main__":
    unittest.main()
