import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import test_wizard
from test_factory import ROOT, WT, factory, invoke, repo_answers
from test_wizard import INSTALL_URL, TOKEN, FakeGitHub

real_subprocess_run = subprocess.run
BOT = "factory-octocat[bot]"
EMAIL = "99999999+factory-octocat[bot]@users.noreply.github.com"
MINT = ("POST", "/app/installations/7654321/access_tokens")
TASK = f"{WT}/my-task"
RETRO = {"defects": [], "slowed": [], "rules_missing": [], "rules_dead_weight": [], "brief_questions": []}


def expiring(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


class Identity(test_wizard.Wizard):
    def setUp(self):
        super().setUp()
        self.remote = ("octocat", "one")  # what `git remote get-url origin` says in the repository the command runs in
        patcher = mock.patch.object(factory, "remote_repo", lambda cwd=None: self.remote)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bodies = []

        def no_network(*args, **kw):
            raise AssertionError(f"a test reached GitHub: {args}")

        patcher = mock.patch.object(factory, "github_api", no_network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def installed(self, **over):
        self.stored(installation_id=7654321, installation_url=INSTALL_URL, bot_login=BOT, bot_id=99999999, bot_email=EMAIL,
                    **over)

    def github(self, status=201, lifetime=3600):
        self.gh = FakeGitHub({MINT: (status, {"token": TOKEN, "expires_at": expiring(lifetime)})})

        def stub(method, path, bearer=None, body=None):
            self.bodies.append(body)
            return self.gh(method, path, bearer, body)

        patcher = mock.patch.object(factory, "github_api", stub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cache(self, content, name="one"):
        (self.identity / f"token-{name}.json").write_text(content if isinstance(content, str) else json.dumps(content))

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

    def token(self, name="one"):
        return factory.app_token(factory.load_identity(), name)

    def test_first_call_mints_and_caches_privately(self):
        self.github()
        self.assertEqual(self.token(), TOKEN)
        self.assertEqual(self.mints(), 1)
        cache = self.identity / "token-one.json"
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
        self.assertEqual(json.loads(cache.read_text())["token"], TOKEN)
        self.assertEqual(factory.epoch(json.loads(cache.read_text())["expires_at"]) > time.time() + 3000, True)
        self.assertEqual(self.files(), ["app.json", "key.pem", "token-one.json"])

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
                self.assertEqual(json.loads((self.identity / "token-one.json").read_text())["token"], TOKEN)

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

    def test_the_token_is_scoped_to_the_repository(self):
        self.github()
        self.token()
        self.assertEqual(self.bodies, [{"repositories": ["one"]}])
        self.assertEqual(factory.installation_token(factory.load_identity())[0], TOKEN)
        self.assertEqual(self.bodies[-1], None)

    def test_each_repository_has_its_own_cache(self):
        self.github()
        self.token("one")
        self.token("two")
        self.assertEqual(self.mints(), 2)
        self.assertEqual(self.bodies, [{"repositories": ["one"]}, {"repositories": ["two"]}])
        self.assertEqual(self.files(), ["app.json", "key.pem", "token-one.json", "token-two.json"])
        self.cache({"token": "only-for-one", "expires_at": expiring(3000)}, "one")
        self.assertEqual(self.token("one"), "only-for-one")
        self.assertEqual(self.token("two"), TOKEN)

    def test_a_repository_the_app_is_not_installed_on(self):
        self.github(status=422)
        with self.assertRaises(factory.Fail) as caught:
            self.token()
        self.assertEqual(str(caught.exception), f"the App is not installed on octocat/one: add it at {INSTALL_URL} (HTTP 422)")

    def test_a_name_that_is_not_a_repository_name_is_refused(self):
        self.github()
        for name in ("", "a/b", "../x", "a b"):
            with self.subTest(name), self.assertRaises(factory.Fail):
                self.token(name)
        self.assertEqual(self.gh.calls, [])

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
        data = json.loads((self.identity / "app.json").read_text())
        (self.identity / "app.json").write_text(json.dumps({**data, "bot_login": BOT, "bot_email": EMAIL}))
        identity, name = factory.acting_identity()
        self.assertEqual((identity["bot_login"], name), (BOT, "one"))

    def test_only_in_a_repository_of_the_apps_owner(self):
        self.installed()
        self.remote = ("OCTOCAT", "one")
        self.assertEqual(factory.acting_identity()[1], "one")
        for remote in (("someone-else", "one"), None):
            self.remote = remote
            self.assertIsNone(factory.acting_identity())


class EnvTests(Identity):
    def test_nothing_without_an_acting_identity(self):
        self.assertEqual(self.cli("env"), (0, "", ""))
        self.stored()
        self.assertEqual(self.cli("env"), (0, "", ""))

    def test_nothing_outside_a_repository_of_the_apps_owner(self):
        self.installed()
        self.github()
        for remote in (("someone-else", "one"), None):
            self.remote = remote
            self.assertEqual(self.cli("env"), (0, "", ""))
        self.assertEqual(self.gh.calls, [])

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

    LOCKDOWN = ("export GIT_TERMINAL_PROMPT=0", "export GIT_CONFIG_COUNT=4",
                "export GIT_CONFIG_KEY_0=credential.https://github.com.helper", "export GIT_CONFIG_VALUE_0=''",
                "export GIT_CONFIG_KEY_1=credential.https://github.com.helper",
                "export GIT_CONFIG_KEY_2=url.https://github.com/.insteadOf", "export GIT_CONFIG_VALUE_2=git@github.com:",
                "export GIT_CONFIG_KEY_3=url.https://github.com/.insteadOf", "export GIT_CONFIG_VALUE_3=ssh://git@github.com/")
    AUTHOR = (f"export GIT_AUTHOR_NAME='{BOT}'", f"export GIT_COMMITTER_NAME='{BOT}'",
              f"export GIT_AUTHOR_EMAIL='{EMAIL}'", f"export GIT_COMMITTER_EMAIL='{EMAIL}'")

    def fresh(self):
        for path in (self.identity, self.home / "repo"):
            shutil.rmtree(path, ignore_errors=True)

    def in_shell(self, out):
        """Evaluates `out` in a real shell, in a repository with an ssh origin and a global gitconfig whose credential helpers
        all answer (so a helper that survives shows). Returns the output lines: the push URL, what `git credential fill`
        answers for github.com and for gitlab.com, and the author and committer of a commit."""
        answering = '"!f() { echo username=human; echo password=leaked; }; f"'
        (self.home / ".gitconfig").write_text(f"[credential]\n\thelper = {answering}\n"
                                              f"[credential \"https://github.com\"]\n\thelper = {answering}\n"
                                              "[user]\n\tname = Human\n\temail = human@example.com\n")
        repo = self.home / "repo"
        repo.mkdir()
        fill = 'printf "protocol=https\\nhost=%s\\n\\n" {} | git credential fill'
        script = ('eval "$OUT"; git init -q . && git remote add origin git@github.com:octocat/one.git && '
                  'git remote get-url --push origin && ' + fill.format("github.com") + " && " + fill.format("gitlab.com") +
                  ' && git commit -q --allow-empty -m x && git log -1 --format="%an|%ae|%cn|%ce"')
        done = subprocess.run(["sh", "-c", script], cwd=repo, capture_output=True, text=True, timeout=30,
                              env={"PATH": os.environ["PATH"], "HOME": str(self.home), "OUT": out, "GIT_CONFIG_NOSYSTEM": "1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.splitlines()

    def assert_locked_down(self, out, token, author=True):
        lines = out.splitlines()
        for line in (f"export GH_TOKEN={token}", *self.LOCKDOWN, *(self.AUTHOR if author else ())):
            self.assertIn(line, lines)
        self.assertEqual(any(line.startswith("export GIT_AUTHOR_NAME") for line in lines), author)

    def assert_in_shell(self, out, token, author):
        shell = self.in_shell(out)
        self.assertEqual(shell[:5], ["https://github.com/octocat/one.git", "protocol=https", "host=github.com",
                                     "username=x-access-token", f"password={token}"])
        # the global helpers do answer elsewhere: this is what the reset entry keeps from github.com
        self.assertEqual(shell[5:9], ["protocol=https", "host=gitlab.com", "username=human", "password=leaked"])
        self.assertEqual(shell[9], f"{BOT}|{EMAIL}|{BOT}|{EMAIL}" if author else "Human|human@example.com|Human|human@example.com")

    def test_in_a_real_shell_with_real_git(self):
        self.installed()
        self.github()
        _, out, _ = self.cli("env")
        self.assert_in_shell(out, TOKEN, author=True)

    def test_a_mint_failure_makes_git_and_gh_fail(self):
        for status in (500, 422):
            with self.subTest(status):
                self.fresh()
                self.installed()
                self.github(status=status)
                rc, out, err = self.cli("env")
                self.assertEqual(rc, 1)
                self.assertEqual(err.count("\n"), 1)
                self.assertTrue(err.startswith("factory: no App token: "))
                self.assertIn("will fail until one can be minted", err)
                self.assert_locked_down(out, "unavailable")
                for text in (out, err):
                    self.assertNotIn(TOKEN, text)
                    self.assertNotIn("PRIVATE KEY", text)
                self.assert_in_shell(out, "unavailable", author=True)

    def test_an_unreadable_or_malformed_app_json_still_locks_git_down(self):
        cases = {"corrupt": "{", "a list": "[1]", "an empty list": "[]", "no owner": json.dumps({"bot_login": BOT}),
                 "no key": json.dumps({"owner": "octocat", "bot_login": BOT, "installation_id": 7654321,
                                       "client_id": "Iv23abc"})}
        for name, content in cases.items():
            with self.subTest(name):
                self.fresh()
                self.installed()
                (self.identity / "app.json").write_text(content)
                rc, out, err = self.cli("env")
                self.assertEqual(rc, 1)
                self.assertEqual(err.count("\n"), 1)
                self.assertTrue(err.startswith("factory: no App token: "))
                self.assertNotIn("Traceback", err)
                self.assert_locked_down(out, "unavailable", author=False)

    def test_a_corrupt_app_json_is_one_shell_session_with_no_credentials(self):
        self.installed()
        (self.identity / "app.json").write_text("{")
        _, out, _ = self.cli("env")
        self.assert_in_shell(out, "unavailable", author=False)


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

    def test_gh_outside_a_repository_of_the_apps_owner_gets_no_token(self):
        self.installed()
        self.github()
        for remote in (("someone-else", "one"), None):
            self.remote = remote
            self.assertIsNone(self.run_with(["gh", "pr", "list"])[1])
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

    INSTALLED = {"owner": "octocat", "bot_login": BOT, "installation_url": INSTALL_URL}

    def test_the_line_says_who_acts(self):
        self.assertIn("\nacting on GitHub with your gh login (no App identity: factory wizard)\n", self.status(None))
        self.assertIn("\nacting on GitHub with your gh login (the App is stored but not installed: factory wizard)\n",
                      self.status({"app_id": 1, "owner": "octocat"}))
        self.assertIn(f"\nacting on GitHub as {BOT} (the factory's App)\n", self.status(self.INSTALLED))
        for remote in (("someone-else", "one"), None):
            self.remote = remote
            self.assertIn("\nacting on GitHub with your gh login (this repository is not on GitHub under octocat, the App's owner)\n",
                          self.status(self.INSTALLED))

    def test_an_unreadable_app_json_does_not_stop_status(self):
        broken = factory.Fail("app.json cannot be read as JSON", 1)
        with mock.patch.object(factory, "load_identity", side_effect=broken):
            out = invoke(["status"], repo_answers([]))[1]
            doc = json.loads(invoke(["status", "--json"], repo_answers([]))[1])
        self.assertIn("\nacting on GitHub with your gh login (the stored App cannot be read: app.json cannot be read as JSON)\n", out)
        self.assertIsNone(doc["identity"])

    def test_all_repositories_are_flagged(self):
        everything = {**self.INSTALLED, "repository_selection": "all"}
        self.assertIn(f"\nacting on GitHub as {BOT} (the factory's App); installed on ALL repositories: narrow it at "
                      f"{INSTALL_URL}\n", self.status(everything))
        self.assertNotIn("ALL", self.status({**self.INSTALLED, "repository_selection": "selected"}))

    def test_json_names_the_bot_and_the_selection(self):
        doc = json.loads(self.status(None, "--json"))
        self.assertEqual((doc["identity"], doc["repository_selection"]), (None, None))
        self.assertIsNone(json.loads(self.status({"app_id": 1, "owner": "octocat"}, "--json"))["identity"])
        doc = json.loads(self.status({**self.INSTALLED, "repository_selection": "all"}, "--json"))
        self.assertEqual((doc["identity"], doc["repository_selection"]), (BOT, "all"))
        self.remote = ("someone-else", "one")
        self.assertIsNone(json.loads(self.status(self.INSTALLED, "--json"))["identity"])


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
