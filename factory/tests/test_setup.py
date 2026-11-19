import io
import json
import os
import re
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_factory import factory
from test_wizard import APP, INSTALLATION, FakeGitHub, routes

import factory_setup

PLUGIN = "factory@factory"
SOURCE = "andreiciobotar/isshoni"
IDENTITY = {"app_id": 123456, "slug": "factory-octocat", "client_id": "Iv23abc", "html_url": APP["html_url"],
            "owner": "octocat", "owner_type": "User"}
MUTATING_PREFIXES = (["claude", "plugin", "install"], ["claude", "plugin", "uninstall"], ["claude", "plugin", "marketplace", "add"],
                     ["claude", "plugin", "marketplace", "remove"], ["claude", "plugin", "marketplace", "update"], ["make"], ["crontab", "-"], ["launchctl"])


class Calls(list):
    """The argv of every call; `dirs` holds the cwd of each, and both are cleared together."""

    def __init__(self):
        super().__init__()
        self.dirs = []

    def clear(self):
        super().clear()
        self.dirs.clear()


class Machine:
    """Every external command of the wizard and uninstall: records the calls and answers from a small world."""

    def __init__(self, clone):
        self.clone = clone
        self.tools = {"git", "gh", "claude", "openssl", "go", "make"}
        self.gh_login, self.claude_login = True, True
        self.entries, self.markets, self.fail, self.crontab = [], [], set(), ""  # entries: what `claude plugin list --json` holds
        self.commits = {}
        self.head, self.next_head = "a" * 40, None  # the marketplace's commit, and the one `marketplace update` moves it to
        self.non_repos, self.main_of = set(), {}  # directories that are no git repository; a task worktree's main checkout
        self.worktrees = {}  # a main checkout's other worktrees
        self.stack, self.gh_version = True, "2.104.0"  # the stack extension is installed unless a test says otherwise
        self.calls = Calls()

    def add(self, scope="user", path=None, enabled=True, sha=None):
        self.entries.append({"id": PLUGIN, "scope": scope, "enabled": enabled, **({"projectPath": path} if path else {})})
        self.commits[(scope, path)] = sha or self.head

    def recorded(self):
        """What installed_plugins.json holds: the commit of each entry (`plugin list --json` has none)."""
        return {key: sha for key, sha in self.commits.items() if key in self.installed()}

    def installed(self, scope=None):
        """The (scope, projectPath) of every entry, or of the entries at one scope."""
        return [(e["scope"], e.get("projectPath")) for e in self.entries if scope in (None, e["scope"])]

    def which(self, tool):
        return f"/usr/bin/{tool}" if tool in self.tools else None

    def __call__(self, argv, cwd=None, timeout=None, input=None, env=None, group=False):
        self.calls.append(list(argv))
        self.calls.dirs.append(cwd)
        a = list(argv)
        if a[:3] == ["git", "worktree", "list"]:
            if cwd in self.non_repos or not Path(cwd).is_dir():
                return 128, "", "fatal: not a git repository"
            main = self.main_of.get(cwd, cwd)
            return 0, f"worktree {main}\n\n" + "".join(f"worktree {w}\n\n" for w in self.worktrees.get(main, [])) + (f"worktree {cwd}\n" if main != cwd else ""), ""
        if a[:3] in (["git", "config", "--get-regexp"], ["git", "symbolic-ref", "refs/remotes/origin/HEAD"]):
            return 1, "", ""
        if a[:3] == ["gh", "auth", "status"]:
            return (0 if self.gh_login else 1), "", ""
        if a[:3] == ["gh", "api", "user"]:
            return 0, "octocat\n", ""
        if a == ["gh", "--version"]:
            return 0, f"gh version {self.gh_version} (2026-11-12)\nhttps://github.com/cli/cli/releases/tag/v{self.gh_version}\n", ""
        if a == ["gh", "extension", "list"]:
            return 0, "gh stack\tgithub/gh-stack\tv0.3.0\n" if self.stack else "gh stack\tsomeone/gh-stack\tv1\ngh stacks\tgithub/gh-stacks\tv1\n", ""
        if a == ["gh", "extension", "install", "github/gh-stack"]:
            if "extension" in self.fail:
                return 1, "", "download\nHTTP 404"
            self.stack = True
            return 0, "", ""
        if a[:3] == ["claude", "auth", "status"]:
            if isinstance(self.claude_login, tuple):
                return self.claude_login
            return 0, json.dumps({"loggedIn": self.claude_login}), ""
        if a[:3] == ["claude", "plugin", "list"]:
            # a local entry is enabled in its own repository only
            return 0, json.dumps([{**e, "enabled": e["enabled"] and (e["scope"] != "local" or e["projectPath"] == cwd)}
                                  for e in self.entries]), ""
        if a[:4] == ["claude", "plugin", "marketplace", "list"]:
            if "market-list" in self.fail:
                return 1, "", "no"
            return 0, json.dumps(self.markets), ""
        if a[:2] == ["git", "remote"]:
            return 0, "git@github.com:someone-else/repo.git\n", ""
        if a[:2] == ["crontab", "-l"]:
            return (0 if self.crontab else 1), self.crontab, ""
        if a[:2] == ["crontab", "-"]:
            self.crontab = input
            return 0, "", ""
        if a[0] in ("launchctl", "systemctl"):
            return 0, "", ""
        if (a[0] == "make" or a[:2] == ["claude", "plugin"]) and (a[0] if a[0] == "make" else a[2]) in self.fail:
            return 1, "", "first line\nit broke"
        if a[:4] == ["claude", "plugin", "marketplace", "update"] and "update" in self.fail:
            return 1, "", "first line\nit broke"
        if a[:3] in (["claude", "plugin", "install"], ["claude", "plugin", "uninstall"]):
            scope = a[a.index("--scope") + 1] if "--scope" in a else "user"
            if cwd is not None and not Path(cwd).is_dir():
                return 127, "", f"no such directory: {cwd}"
            key = (scope, cwd if scope == "local" else None)
            if a[2] == "install":
                assert key not in self.installed(), f"{a} runs twice for {key}"
                self.add(*key)
            else:
                assert a[3] == PLUGIN and key in self.installed(), f"{a} finds no entry for {key}"
                self.entries = [e for e in self.entries if (e["scope"], e.get("projectPath")) != key]
        elif a[:4] == ["claude", "plugin", "marketplace", "update"]:
            self.head = self.next_head or self.head
        elif a[:4] == ["claude", "plugin", "marketplace", "add"]:
            self.markets.append({"name": "factory", "source": "github", "repo": a[4]})
        elif a[:4] == ["claude", "plugin", "marketplace", "remove"]:
            self.markets = [m for m in self.markets if m["name"] != a[4]]
        elif a[0] == "make":
            (Path(cwd) / "bin" / "isshoni-tui").write_text("binary")
        else:
            raise AssertionError(f"unexpected command {a}")
        return 0, "", ""

    def mutations(self):
        return [c for c in self.calls if any(c[:len(p)] == p for p in MUTATING_PREFIXES)]


class Setup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "clone"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "bin" / "isshoni").write_text("#!/bin/sh\n")
        (self.root / "go.mod").write_text("module isshoni\n")
        self.store = self.home / ".claude" / "factory"
        self.machine = Machine(self.root)
        self.has_clone = True
        self.github = FakeGitHub({**routes("PEM"), ("DELETE", "/app/installations/7654321"): (204, None)})
        self.opened = []
        self.created = []
        for patcher in (mock.patch.dict(os.environ, {"HOME": str(self.home), "DISPLAY": ":0",
                                                     "XDG_CONFIG_HOME": str(self.home / ".config")}),
                        mock.patch.object(factory, "STORE", self.store),
                        mock.patch.object(factory, "SETTINGS_PATH", self.store / "settings.toml"),
                        mock.patch.object(factory, "IDENTITY", self.store / "identity"),
                        mock.patch.object(factory, "run", self.machine),
                        mock.patch.object(factory_setup, "recorded", self.machine.recorded),
                        mock.patch.object(factory.shutil, "which", self.machine.which),
                        mock.patch.object(factory_setup, "clone", lambda: self.root if self.has_clone else None),
                        mock.patch.object(factory, "github_api", lambda *a, **kw: self.github(*a, **kw)),
                        mock.patch.object(factory, "app_jwt", lambda identity: "JWT"),
                        mock.patch.object(factory, "create_app", self.create_app),
                        mock.patch.object(factory, "open_browser", self.opened.append)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_app(self, owner, name):
        self.created.append((owner, name))
        factory.store_identity(IDENTITY, "PEM")
        return IDENTITY

    def command(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            rc = factory.main(list(argv))
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def snapshot(self):
        return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.tmp.rglob("*") if p.is_file()}

    def settings(self):
        return factory.load_settings()

    def add_host(self):
        self.store.mkdir(parents=True, exist_ok=True)
        (self.store / "hosts.json").write_text(json.dumps({"nas": {"target": "me@nas", "state": "/srv/f", "dashboard_port": 2222,
                                                                  "repos": ["acme/widgets"], "alias": None, "rev": "x"}}))

    def set_up(self):
        """Everything the wizard sets up, in place and verified."""
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.machine.calls.clear()


class WizardFlow(Setup):
    def test_a_full_run_does_every_step_and_the_second_does_nothing(self):
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations(), [["make", "build"], ["claude", "plugin", "marketplace", "add", SOURCE],
                                                    ["claude", "plugin", "install", PLUGIN]])
        self.assertEqual(self.machine.calls[0], ["gh", "auth", "status"])
        self.assertTrue((self.root / "bin" / "isshoni-tui").exists())
        monitor = str(self.root / "bin" / "isshoni")
        self.assertEqual(self.settings(), {**factory.SETTINGS_DEFAULTS, "monitor": monitor})
        self.assertEqual((self.store / "settings.toml").read_text(), factory.settings_text() + f"monitor = {json.dumps(monitor)}\n")  # added in place, after the example
        self.assertEqual(self.created, [("octocat", None)])
        self.assertEqual(json.loads((self.store / "identity" / "app.json").read_text())["installation_id"], 7654321)
        self.assertNotIn("Nothing to do", self.out)
        self.assertNotIn("/rename", self.out)
        self.assertIn("/factory:dispatch", self.out)
        self.assertIn("site manager: starts with the dispatcher session", self.out)
        before, calls = self.snapshot(), len(self.machine.calls)
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations()[3:], [])
        self.assertEqual(self.snapshot(), before)
        self.assertGreater(len(self.machine.calls), calls)
        self.assertIn("Nothing to do: everything is set up.", self.out)
        self.assertEqual(self.out.count("  did:"), 0)
        self.assertEqual(self.created, [("octocat", None)])

    def test_missing_dependencies_are_listed_together_and_nothing_else_runs(self):
        self.machine.tools -= {"gh", "openssl"}
        self.machine.claude_login = False
        self.assertEqual(self.command("wizard", "--yes"), 1)
        for line in ("gh is missing: https://cli.github.com, then `gh auth login`", "openssl is missing",
                     "claude is not logged in: run `claude` and log in", "Then run the wizard again."):
            self.assertIn(line, self.out)
        self.assertEqual(self.machine.mutations(), [])
        self.assertFalse(self.store.exists())
        self.assertNotIn("plugin:", self.out)

    def test_not_logged_in_to_gh(self):
        self.machine.gh_login = False
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("gh is not logged in: run `gh auth login`", self.out)

    def test_an_unparsable_claude_answer_is_not_logged_in(self):
        for answer in ("garbage", (0, "not json", ""), (1, '{"loggedIn": true}', "")):
            with mock.patch.object(self.machine, "claude_login", answer):
                self.assertEqual(self.command("wizard", "--yes"), 1)
            self.assertIn("claude is not logged in", self.out)

    def test_an_interrupted_run_resumes_after_the_failed_step(self):
        self.machine.fail.add("install")
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("needs you: `claude plugin install factory@factory` failed: first line", self.out)
        self.assertIn("By hand:\n      claude plugin install factory@factory\n", self.out)
        self.assertIn("Then run the wizard again.", self.out)
        self.assertFalse(self.store.exists())
        self.machine.fail.clear()
        self.machine.calls.clear()
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations(), [["claude", "plugin", "install", PLUGIN]])
        self.assertTrue((self.store / "settings.toml").exists())

    def test_dashboard_without_go_or_a_clone_is_skipped_and_the_run_goes_on(self):
        self.machine.tools.discard("go")
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertIn("skipped: Go 1.27+ is missing: https://go.dev/dl, then run the wizard again", self.out)
        self.assertNotIn(["make", "build"], self.machine.calls)
        self.assertEqual(self.machine.installed(), [("user", None)])
        self.has_clone = False
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertIn("skipped: the dashboard needs a clone of the repository", self.out)
        self.assertIn("skipped: cost alerts and the daily digest need a clone", self.out)

    def test_a_failed_build_is_one_line_and_the_run_goes_on_but_exits_1(self):
        self.machine.fail.add("make")
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("needs you: make build failed: it broke", self.out)
        self.assertEqual(self.machine.installed(), [("user", None)])
        self.assertTrue((self.store / "settings.toml").exists())

    def test_a_long_name_exits_2_before_any_step(self):
        self.assertEqual(self.command("wizard", "--yes", "--name", "n" * 35), 2)
        self.assertIn("35 characters", self.err)
        self.assertEqual(self.machine.calls, [])

    def test_gh_auth_status_runs_outside_any_repository(self):
        seen = []

        def spy(argv, **kw):
            seen.append((argv[:3], kw.get("cwd")))
            return self.machine(argv, **kw)

        with mock.patch.object(factory, "run", spy):
            self.command("wizard", "--list")
        self.assertIn((["gh", "auth", "status"], "/"), seen)

    def test_the_plugin_must_be_in_user_scope_and_enabled(self):
        self.machine.add("project", "/x")
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertIn(["claude", "plugin", "install", PLUGIN], self.machine.calls)
        self.machine.calls.clear()
        self.machine.entries[-1]["enabled"] = False
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("installed but disabled: run `claude plugin enable factory@factory --scope user`", self.out)
        self.assertEqual(self.machine.mutations(), [])

    def test_an_unreadable_marketplace_list_stops_before_adding(self):
        self.machine.fail.add("market-list")
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("cannot read `claude plugin marketplace list --json`", self.out)
        self.assertEqual([c for c in self.machine.mutations() if c[0] == "claude"], [])

    def test_a_directory_marketplace_is_named_by_its_path(self):
        self.machine.markets.append({"name": "factory", "source": "directory", "path": "/some/dir"})
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("exists from /some/dir", self.out)
        self.assertNotIn("None", self.out)

    def test_cost_alerts_are_skipped_from_a_task_worktree(self):
        self.root = self.tmp / "repo" / ".claude" / "worktrees" / "task"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "bin" / "isshoni").write_text("x")
        (self.root / "go.mod").write_text("x")
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertIn("skipped: this clone is a task worktree", self.out)
        self.assertEqual((self.store / "settings.toml").read_text(), factory.settings_text())
        self.assertEqual(self.settings(), factory.SETTINGS_DEFAULTS)
        self.assertIsNone(self.settings()["monitor"])

    def test_an_old_settings_json_is_moved_over_by_the_settings_step(self):
        self.store.mkdir(parents=True)
        (self.store / "settings.json").write_text('{"worktree_cap": 9}')
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.settings()["worktree_cap"], 9)
        self.assertEqual((self.store / "settings.json").read_text(), '{"worktree_cap": 9}')  # left for older versions

    def test_the_plugin_step_reuses_the_marketplace_it_finds(self):
        self.machine.markets.append({"name": "factory", "source": "github", "repo": SOURCE})
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual([c for c in self.machine.mutations() if c[0] == "claude"], [["claude", "plugin", "install", PLUGIN]])

    def test_a_marketplace_from_another_repository_is_never_replaced(self):
        self.machine.markets.append({"name": "factory", "source": "github", "repo": "someone/else"})
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("exists from someone/else, not andreiciobotar/isshoni", self.out)
        self.assertEqual([c for c in self.machine.mutations() if c[0] == "claude"], [])
        self.assertEqual(self.machine.markets, [{"name": "factory", "source": "github", "repo": "someone/else"}])

    def test_an_invalid_settings_file_is_never_overwritten(self):
        self.store.mkdir(parents=True)
        (self.store / "settings.toml").write_text('[limits]\nworktree_cap = 0\n')
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("worktree_cap must be an integer", self.out)
        self.assertIn(str(self.store / "settings.toml"), self.out)
        self.assertEqual((self.store / "settings.toml").read_text(), '[limits]\nworktree_cap = 0\n')
        (self.store / "settings.toml").write_text("[not toml")
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertEqual((self.store / "settings.toml").read_text(), "[not toml")

    def test_the_monitor_joins_the_settings_and_every_other_key_survives(self):
        self.store.mkdir(parents=True)
        mine = '# mine\n[paths]\nmonitor = "/gone/isshoni" # old\n\n[limits]\nworktree_cap = 9\n[x]\ny = 1\n'
        (self.store / "settings.toml").write_text(mine)
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        monitor = str(self.root / "bin" / "isshoni")
        self.assertEqual((self.store / "settings.toml").read_text(), mine.replace('"/gone/isshoni"', json.dumps(monitor)))
        self.assertIn("it pointed at /gone/isshoni, which is missing", self.out)

    def test_a_declined_offer_changes_nothing(self):
        for stdin in ("", "\n", "n\n", "maybe\n"):
            self.machine.calls.clear()
            self.assertEqual(self.command("wizard", stdin=stdin), 0, self.out + self.err)
            self.assertEqual([c for c in self.machine.mutations() if c[0] == "make"], [])
            self.assertEqual(self.created, [])
            self.assertIsNone(self.settings()["monitor"])
            self.assertNotIn("Nothing to do", self.out)
        self.assertFalse((self.root / "bin" / "isshoni-tui").exists())
        self.assertFalse((self.store / "identity").exists())

    def test_an_offer_answered_yes_is_taken(self):
        self.assertEqual(self.command("wizard", stdin="y\ny\nYES\n"), 0, self.out + self.err)
        self.assertTrue((self.root / "bin" / "isshoni-tui").exists())
        self.assertIn("monitor", self.settings())
        self.assertEqual(len(self.created), 1)

    def test_a_failure_of_the_optional_app_step_is_printed_and_the_run_finishes_with_exit_1(self):
        self.set_up()
        self.assertEqual(self.command("wizard", "--yes", "--owner", "someone-else"), 1)
        self.assertIn("needs you: this machine's identity is the App factory-octocat of octocat, not someone-else", self.out)
        self.assertIn("Summary:", self.out)
        self.assertIn("/factory:dispatch", self.out)
        self.assertNotIn("Nothing to do", self.out)

    def test_the_app_owner_is_your_login_not_the_owner_of_origin(self):
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.created, [("octocat", None)])
        self.assertIn(["gh", "api", "user", "--jq", ".login"], self.machine.calls)

    def test_an_owner_and_a_name_go_to_the_new_app(self):
        self.assertEqual(self.command("wizard", "--yes", "--owner", "my-org", "--name", "mine"), 0, self.out + self.err)
        self.assertEqual(self.created, [("my-org", "mine")])

    def test_an_existing_app_is_verified_again_on_every_run(self):
        self.set_up()
        self.github.calls.clear()
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertTrue(self.github.called("GET", "/app"))

    def test_list_runs_nothing_and_writes_nothing(self):
        self.assertEqual(self.command("wizard", "--list"), 1)
        self.assertEqual(self.machine.mutations(), [])
        self.assertEqual(list(self.home.rglob("*")), [])
        self.assertEqual(self.github.calls, [])
        for name in ("dashboard", "plugin", "settings", "cost criteria", "GitHub App"):
            self.assertIn(f"  {name}: not done", self.out)
        self.assertNotIn("/factory:dispatch", self.out)
        self.set_up()
        self.assertEqual(self.command("wizard", "--list"), 0, self.out)
        self.assertEqual(self.machine.mutations(), [])
        self.assertIn("/factory:dispatch", self.out)
        self.assertIn("  settings: done", self.out)
        self.assertNotIn("Nothing to do", self.out)

    def test_the_last_line_points_to_the_remote_wizard_and_list_shows_the_hosts(self):
        self.set_up()
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.out.splitlines()[-1], "A server: `make wizard-remote` (docs/container.md).")
        self.has_clone = False
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertIn("needs a clone of the repository", self.out.splitlines()[-1])
        self.assertEqual(self.command("wizard", "--list"), 0, self.out)
        self.assertNotIn("A server:", self.out)
        self.assertEqual(self.out.splitlines()[-3:], ["hosts:", "  none", "  their live state: bin/isshoni host status"])
        self.add_host()
        self.assertEqual(self.command("wizard", "--list"), 0, self.out)
        self.assertEqual(self.out.splitlines()[-3:], ["hosts:", "  nas  me@nas  acme/widgets", "  their live state: bin/isshoni host status"])

    def test_ask(self):
        for text, want in (("y\n", True), ("YES\n", True), ("n\n", False), ("\n", False), ("", False), ("sure\n", False)):
            with mock.patch.object(sys, "stdin", io.StringIO(text)), redirect_stdout(io.StringIO()):
                self.assertEqual(factory.ask("ok?", False), want, text)
        with mock.patch.object(sys, "stdin", io.StringIO("")), redirect_stdout(io.StringIO()):
            self.assertTrue(factory.ask("ok?", True))


class StackStep(Setup):
    """The optional step that offers GitHub's stack extension. Offers come in this order: dashboard, cost criteria, this one, the App."""
    INSTALL = ["gh", "extension", "install", "github/gh-stack"]

    def setUp(self):
        super().setUp()
        self.machine.stack = False

    def test_present_runs_nothing_more(self):
        self.machine.stack = True
        self.assertEqual(self.command("wizard", stdin="n\nn\nn\n"), 0, self.out + self.err)
        self.assertIn(["gh", "extension", "list"], self.machine.calls)
        self.assertNotIn(self.INSTALL, self.machine.calls)
        self.assertNotIn(["gh", "--version"], self.machine.calls)
        self.assertIn("gh stack: ", self.out)
        self.assertIn("done: github/gh-stack", self.out)

    def test_the_gh_calls_run_outside_any_repository_and_the_install_has_a_timeout(self):
        seen = []

        def spy(argv, **kw):
            seen.append((argv[:3], kw.get("cwd"), kw.get("timeout")))
            return self.machine(argv, **kw)

        with mock.patch.object(factory, "run", spy):
            self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        for argv in (["gh", "extension", "list"], ["gh", "--version"], self.INSTALL[:3]):
            self.assertIn("/", [cwd for a, cwd, _ in seen if a == argv[:3]], argv)
        self.assertIn(factory.RUN_TIMEOUT, [t for a, _, t in seen if a == self.INSTALL[:3]])

    def test_an_offer_answered_yes_installs_once_and_the_next_run_finds_it(self):
        for argv, stdin in (("wizard", "n\nn\ny\nn\n"), ("wizard --yes", "")):
            self.machine.stack = False
            self.machine.calls.clear()
            self.assertEqual(self.command(*argv.split(), stdin=stdin), 0, self.out + self.err)
            self.assertEqual(self.machine.calls.count(self.INSTALL), 1)
            self.assertIn("did: installed GitHub's stack extension (gh extension install github/gh-stack)", self.out)
        self.machine.calls.clear()
        self.assertEqual(self.command("wizard", stdin="n\nn\nn\n"), 0, self.out + self.err)
        self.assertNotIn(self.INSTALL, self.machine.calls)
        self.assertIn("done: github/gh-stack", self.out)

    def test_a_declined_offer_installs_nothing_and_is_asked_again(self):
        for stdin in ("n\nn\nn\nn\n", ""):
            self.machine.calls.clear()
            self.assertEqual(self.command("wizard", stdin=stdin), 0, self.out + self.err)
            self.assertNotIn(self.INSTALL, self.machine.calls)
            self.assertIn("  skipped: declined", self.out)
            self.assertIn("Install GitHub's stack extension (gh extension install github/gh-stack)? Optional", self.out)
        self.assertFalse(self.machine.stack)

    def test_a_gh_older_than_2_0_is_not_asked_and_not_installed(self):
        self.machine.gh_version = "1.14.0"
        self.assertEqual(self.command("wizard", stdin="n\nn\nn\n"), 0, self.out + self.err)
        self.assertNotIn("Install GitHub's stack extension", self.out)
        self.assertNotIn(self.INSTALL, self.machine.calls)
        self.assertIn("gh 1.14.0 is older than 2.0", self.out)
        self.assertIn("upgrade gh", self.out)

    def test_a_failed_install_is_one_line_and_the_run_goes_on_but_exits_1(self):
        self.machine.fail.add("extension")
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("needs you: gh extension install github/gh-stack failed: HTTP 404", self.out)
        self.assertIn("Summary:", self.out)

    def test_list_shows_it_missing_and_installs_nothing(self):
        self.assertEqual(self.command("wizard", "--list"), 1)
        self.assertIn("  gh stack: not done (not installed; optional, the factory works without it)", self.out)
        self.assertIn(["gh", "extension", "list"], self.machine.calls)
        self.assertNotIn(self.INSTALL, self.machine.calls)
        self.assertNotIn(["gh", "--version"], self.machine.calls)


class Clone(Setup):
    def test_a_clone_holds_bin_isshoni_and_go_mod_two_levels_above_the_script(self):
        with mock.patch.object(factory_setup, "clone", self.__class__.real_clone):
            for root, want in ((self.root, self.root), (self.tmp / "plugin-cache", None)):
                script = root / "factory" / "bin" / "factory"
                script.parent.mkdir(parents=True, exist_ok=True)
                with mock.patch.object(factory, "FACTORY", script):
                    self.assertEqual(factory_setup.clone(), want)

    real_clone = factory_setup.clone


class UninstallFlow(Setup):
    def test_everything_goes_and_the_second_run_finds_nothing(self):
        self.set_up()
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual([c for c in self.machine.mutations() if c[0] == "claude"],
                         [["claude", "plugin", "uninstall", PLUGIN], ["claude", "plugin", "marketplace", "remove", "factory"]])
        self.assertTrue(self.github.called("DELETE", "/app/installations/7654321"))
        self.assertEqual(self.opened, ["https://github.com/settings/apps/factory-octocat/advanced"])
        self.assertFalse(self.store.exists())
        self.assertFalse((self.root / "bin" / "isshoni-tui").exists())
        self.assertEqual((self.machine.entries, self.machine.markets), ([], []))
        self.assertIn("Delete the App there (Advanced, Delete GitHub App): only you can", self.out)
        self.assertIn("left behind:", self.out)
        self.assertIn("~/.claude/projects", self.out)
        self.assertIn("the App factory-octocat on GitHub", self.out)
        self.machine.calls.clear()
        self.opened.clear()
        self.github.calls.clear()
        before = self.snapshot()
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.out.count("nothing to remove"), 5)
        self.assertEqual(self.machine.mutations(), [])
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.opened, [])
        self.assertEqual(self.snapshot(), before)

    def test_the_stack_extension_is_left_behind_and_never_removed(self):
        self.set_up()
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertIn("  git, gh and its stack extension (gh extension remove stack), Go, Claude Code", self.out)
        self.assertFalse([c for c in self.machine.calls if c[:3] == ["gh", "extension", "remove"]])

    def test_list_removes_and_calls_nothing_and_opens_no_page(self):
        self.set_up()
        before = self.snapshot()
        self.github.calls.clear()
        self.assertEqual(self.command("uninstall", "--list"), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations(), [])
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.opened, [])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.root / "bin" / "isshoni-tui").exists())
        for line in ("found: the marketplace andreiciobotar/isshoni and factory@factory", "found: the App factory-octocat",
                     str(self.store / "settings.toml"), str(self.store / "identity") + " (",
                     "/settings/apps/factory-octocat/advanced", "found: " + str(self.root / "bin" / "isshoni-tui")):
            self.assertIn(line, self.out)

    def test_a_declined_step_keeps_its_things_and_says_so(self):
        self.set_up()
        self.assertEqual(self.command("uninstall", stdin=""), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations(), [])
        self.assertFalse(self.github.called("DELETE", "/app/installations/7654321"))
        self.assertTrue((self.store / "settings.toml").exists())
        self.assertIn("the plugin and its marketplace: you declined", self.out)
        self.assertIn(f"the home {self.store}: you declined", self.out)
        self.assertIn("the dashboard binary: you declined", self.out)
        self.assertEqual(self.opened, ["https://github.com/settings/apps/factory-octocat/advanced"])

    def test_hosts_keep_the_app_and_the_identity_until_the_last_is_removed(self):
        self.set_up()
        self.add_host()
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertIn("kept: 1 host(s) use it (nas)", self.out)
        self.assertIn("`make uninstall-remote`", self.out)
        self.assertIn("the App factory-octocat and its installation: 1 host(s) use it (nas)", self.out)
        self.assertNotIn("Delete the App there", self.out)
        self.assertFalse(self.github.called("DELETE", "/app/installations/7654321"))
        self.assertEqual(self.opened, [])
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), ["hosts.json", "identity"])
        self.assertEqual((self.machine.entries, self.machine.markets), ([], []))
        self.assertIn("kept, hosts still use it", self.out)
        (self.store / "hosts.json").write_text("{}")
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertTrue(self.github.called("DELETE", "/app/installations/7654321"))
        self.assertEqual(self.opened, ["https://github.com/settings/apps/factory-octocat/advanced"])
        self.assertFalse(self.store.exists())

    def test_a_copy_of_the_identity_never_removes_the_app(self):
        self.set_up()
        app = self.store / "identity" / "app.json"
        app.write_text(json.dumps({**json.loads(app.read_text()), "copy": True}))
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertIn("this identity is a copy", self.out)
        self.assertFalse(self.github.called("DELETE", "/app/installations/7654321"))
        self.assertEqual(self.opened, [])
        self.assertFalse(self.store.exists())

    def test_an_unreadable_hosts_file_fails_and_removes_nothing_of_the_app(self):
        self.set_up()
        for text in ("{", "[]"):
            (self.store / "hosts.json").write_text(text)
            self.assertEqual(self.command("uninstall", "--yes"), 1, self.out + self.err)
            self.assertIn(f"{self.store / 'hosts.json'}", self.out)
            self.assertFalse(self.github.called("DELETE", "/app/installations/7654321"))
            self.assertEqual(self.opened, [])
            self.assertTrue((self.store / "identity" / "key.pem").exists())
            self.assertTrue((self.store / "settings.toml").exists())

    def test_list_shows_the_hosts(self):
        self.set_up()
        self.add_host()
        self.assertEqual(self.command("uninstall", "--list"), 0, self.out + self.err)
        self.assertIn("kept: 1 host(s) use it (nas)", self.out)
        self.assertEqual(self.out.splitlines()[-3:], ["hosts:", "  nas  me@nas  acme/widgets", "  their live state: bin/isshoni host status"])
        self.assertEqual(self.opened, [])

    def test_the_app_installation_answers(self):
        self.set_up()
        for status, text, rc in ((404, "already uninstalled", 0), (204, "  uninstalled", 0), (500, "failed: GitHub DELETE", 1)):
            self.github.routes[("DELETE", "/app/installations/7654321")] = (status, {"message": "boom"})
            self.assertEqual(self.command("uninstall", "--yes"), rc, self.out + self.err)
            self.assertIn(text, self.out)
            self.assertIn("Delete the App there", self.out)
            self.assertFalse(self.store.exists())
            self.set_up()

    def test_the_plugin_from_another_source_is_kept(self):
        self.machine.add()
        self.machine.markets.append({"name": "factory", "source": "github", "repo": "someone/else"})
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations(), [])
        self.assertEqual(self.machine.installed(), [("user", None)])
        self.assertIn("not from andreiciobotar/isshoni", self.out)

    def test_an_unreadable_list_removes_no_plugin_and_no_marketplace(self):
        self.set_up()
        self.machine.calls.clear()
        self.machine.fail.add("market-list")
        self.assertEqual(self.command("uninstall", "--yes"), 1)
        self.assertIn("nothing removed", self.out)
        self.assertEqual([c for c in self.machine.mutations() if c[0] == "claude"], [])
        self.assertEqual(self.machine.installed(), [("user", None)])

    def test_without_claude_the_plugin_step_says_so_and_the_rest_goes_on(self):
        self.set_up()
        self.machine.tools.discard("claude")
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertIn("claude is not on PATH: nothing to remove", self.out)
        self.assertFalse(self.store.exists())

    def test_a_failing_plugin_removal_is_reported_and_the_run_goes_on(self):
        self.set_up()
        self.machine.fail.add("uninstall")
        self.assertEqual(self.command("uninstall", "--yes"), 1)
        self.assertIn("failed: `claude plugin uninstall factory@factory` failed", self.out)
        self.assertFalse(self.store.exists())

    def test_the_home_keeps_what_is_not_the_factorys_and_the_directory_with_it(self):
        self.store.mkdir(parents=True)
        for name in ("settings.toml", "settings.json", "watch.log", "events.jsonl", "tasks-2026-11.jsonl",
                     "retros-2026-01.jsonl", ".settings-ab12.tmp"):
            (self.store / name).write_text("x")
        (self.store / "specs" / "repo").mkdir(parents=True)
        (self.store / "specs" / "repo" / "a.md").write_text("x")
        (self.store / "deliverables" / "t").mkdir(parents=True)
        (self.store / "deliverables" / "t" / "b.txt").write_text("x")
        (self.store / "dashboard").mkdir()
        (self.store / "dashboard" / "host_key").write_text("x")
        (self.store / "notes.txt").write_text("not ours")
        (self.store / "events.jsonl.bak").write_text("not ours")
        self.assertEqual(self.command("uninstall", "--list"), 0)
        for name in ("settings.toml", "settings.json", "watch.log", "events.jsonl", "tasks-2026-11.jsonl",
                     ".settings-ab12.tmp"):
            self.assertIn(str(self.store / name), self.out)
        self.assertIn(f"{self.store / 'specs'} (1 files)", self.out)
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), ["events.jsonl.bak", "notes.txt"])
        self.assertIn(f"not the factory's, kept: {self.store / 'notes.txt'}", self.out)
        self.assertIn("directory stays", self.out)
        self.assertIn("notes.txt: not the factory's", self.out)

    def test_the_marketplace_of_the_factory_goes_alone_and_project_scope_is_left(self):
        self.machine.markets.append({"name": "factory", "source": "github", "repo": SOURCE})
        self.machine.add("project", "/x")
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual([c for c in self.machine.mutations() if c[0] == "claude"],
                         [["claude", "plugin", "marketplace", "remove", "factory"]])
        self.assertIn("/x/.claude/settings.json: the project-scope entry of factory@factory, made by hand; removing the marketplace drops its install record", self.out)

    def test_a_foreign_file_under_another_factory_home_stops_the_home_step(self):
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        (scratch / "settings.json").write_text("{}")
        (scratch / "thesis.tex").write_text("mine")
        with mock.patch.object(factory, "STORE", scratch):
            self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), ["settings.json", "thesis.tex"])
        self.assertIn("does not look like a factory home: thesis.tex; nothing removed", self.out)
        self.assertIn("does not look like a factory home", self.out.split("left behind:")[1])

    def test_a_home_of_strangers_loses_nothing(self):
        self.store.mkdir(parents=True)
        (self.store / "thesis.tex").write_text("mine")
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual((self.store / "thesis.tex").read_text(), "mine")

    def test_without_a_clone_there_is_no_binary_step(self):
        self.has_clone = False
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertNotIn("this clone", self.out)
        self.assertIn("dashboard binary:\n  nothing to remove", self.out)


class Schedules(Setup):
    """The watch jobs of this home go; a job of another home and anything not a factory job stay."""

    def labels(self):
        return {"mine": "com.claude.factory.watch.repo-aaaaaaaa", "legacy": "com.claude.factory.watch.repo",
                "elsewhere": "com.claude.factory.watch.repo-bbbbbbbb"}

    def env(self, home=None):
        return {"PATH": "/usr/bin", **({"FACTORY_HOME": home} if home else {})}

    def install(self, backend, label, home=None):
        backend.install(label, ["python3", "/x/factory", "watch"], "/repo", self.env(home), self.store / "watch.log")

    def uninstall(self, backend):
        with mock.patch.object(factory, "scheduler", lambda: backend), redirect_stdout(io.StringIO()):
            factory_setup.uninstall_schedules(Namespace(yes=True, list=False), [])

    def check(self, backend, others, list_present):
        """Install jobs of this home and of another, plus the strangers, remove, and compare what is left."""
        names = self.labels()
        self.install(backend, names["elsewhere"], "/elsewhere")
        self.install(backend, names["mine"])
        self.install(backend, names["legacy"])
        others()
        self.assertEqual(sorted(backend.owned()), sorted([names["legacy"], names["mine"]]))
        self.uninstall(backend)
        self.assertEqual(list_present(), [names["elsewhere"]])
        with mock.patch.object(factory, "STORE", Path("/elsewhere")):
            self.assertEqual(backend.owned(), [names["elsewhere"]])

    def test_launchd(self):
        agents = self.home / "Library" / "LaunchAgents"
        backend = factory.Launchd()

        def others():
            agents.joinpath("com.example.other.plist").write_bytes(b"not a factory job")
            agents.joinpath("com.claude.factory.watch.broken-cccccccc.plist").write_bytes(b"\x00 unreadable")

        self.check(backend, others, lambda: sorted(p.stem for p in agents.glob("com.claude.factory.watch.*.plist")
                                                  if p.stem != "com.claude.factory.watch.broken-cccccccc"))
        self.assertTrue(agents.joinpath("com.example.other.plist").exists())
        self.assertTrue(agents.joinpath("com.claude.factory.watch.broken-cccccccc.plist").exists())
        self.assertIn(["launchctl", "bootout", f"gui/{os.getuid()}/com.claude.factory.watch.repo-aaaaaaaa"], self.machine.calls)

    def test_systemd(self):
        units = self.home / ".config" / "systemd" / "user"
        backend = factory.Systemd()

        def others():
            units.joinpath("com.example.other.timer").write_text("[Timer]\n")
            units.joinpath("com.example.other.service").write_text("[Service]\n")

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")}):
            self.check(backend, others, lambda: sorted(p.stem for p in units.glob("com.claude.factory.watch.*.timer")))
        self.assertTrue(units.joinpath("com.example.other.timer").exists())
        self.assertFalse(units.joinpath("com.claude.factory.watch.repo-aaaaaaaa.service").exists())

    def test_cron(self):
        backend = factory.Cron()

        def others():
            self.machine.crontab += "0 * * * * backup.sh\n0 1 * * * echo hi # com.example.other\n"

        self.check(backend, others, lambda: re.findall(r"# (com\.claude\.factory\.watch\.\S+)$", self.machine.crontab, re.M))
        self.assertIn("0 * * * * backup.sh", self.machine.crontab)
        self.assertIn("# com.example.other", self.machine.crontab)

    def test_under_another_home_only_its_own_jobs_are_ours(self):
        agents = self.home / "Library" / "LaunchAgents"
        backend = factory.Launchd()
        names = self.labels()
        self.install(backend, names["mine"])
        self.install(backend, names["elsewhere"], str(self.tmp / "scratch"))
        with mock.patch.object(factory, "STORE", self.tmp / "scratch"):
            self.assertEqual(backend.owned(), [names["elsewhere"]])
            self.uninstall(backend)
        self.assertEqual([p.stem for p in agents.glob("*.plist")], [names["mine"]])

    def test_no_scheduler_is_one_line(self):
        out = io.StringIO()
        with mock.patch.object(factory, "scheduler", mock.Mock(side_effect=factory.Fail("schedule: no scheduler here"))), \
                redirect_stdout(out):
            factory_setup.uninstall_schedules(Namespace(yes=True, list=False), [])
        self.assertEqual(out.getvalue(), "  schedule: no scheduler here: nothing to remove\n")

    def test_the_step_lists_without_removing(self):
        backend = factory.Launchd()
        self.install(backend, self.labels()["mine"])
        out = io.StringIO()
        with mock.patch.object(factory, "scheduler", lambda: backend), redirect_stdout(out):
            factory_setup.uninstall_schedules(Namespace(yes=True, list=True), [])
        self.assertIn("found: 1 watch job(s) of this home: com.claude.factory.watch.repo-aaaaaaaa", out.getvalue())
        self.assertTrue(backend.plist(self.labels()["mine"]).exists())


class Scoped(Setup):
    """The plugin at user and repository scope: repositories P, Q, R are directories, and the marketplace is the factory's."""

    def setUp(self):
        super().setUp()
        self.p, self.q, self.r = (self.repository(n) for n in "pqr")
        self.machine.markets.append({"name": "factory", "source": "github", "repo": SOURCE})

    def repository(self, name):
        path = self.tmp / "repos" / name
        path.mkdir(parents=True)
        return str(path.resolve())

    def claude(self):
        """The (argv, cwd) of every `claude plugin` command that changed something."""
        return [(c, d) for c, d in zip(self.machine.calls, self.machine.calls.dirs) if c in self.machine.mutations() and c[0] == "claude"]

    def plugin_calls(self):
        return [c for c in self.machine.mutations() if c[0] == "claude"]


def uninstall(scope, path=None):
    return (["claude", "plugin", "uninstall", PLUGIN, "--scope", scope, "--keep-data"], path)


def install(scope, path=None):
    return (["claude", "plugin", "install", PLUGIN, "--scope", scope], path)


UPDATE = (["claude", "plugin", "marketplace", "update", "factory"], None)


class WizardScope(Scoped):
    def test_without_the_flag_every_local_install_goes_in_its_own_directory_and_the_second_run_does_nothing(self):
        self.machine.add("local", self.p)
        self.machine.add("local", self.q)
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.claude(), [(["claude", "plugin", "install", PLUGIN], None), uninstall("local", self.p), uninstall("local", self.q)])
        self.assertEqual(self.machine.installed(), [("user", None)])
        self.assertIn("also removes the 2 repository-scope install(s)", self.out)
        self.assertIn("for one repository only: `factory wizard --repository <path>`", self.out)
        self.machine.calls.clear()
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.plugin_calls(), [])

    def test_a_local_entry_of_a_gone_directory_is_skipped_and_the_user_install_still_goes_in(self):
        gone = str(self.tmp / "gone")
        self.machine.add("local", gone)
        self.machine.add("local", self.p)
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.claude(), [(["claude", "plugin", "install", PLUGIN], None), uninstall("local", self.p)])
        self.assertIn(f"{gone} no longer exists: its entry is skipped", self.out)

    def test_the_full_uninstall_skips_a_gone_directory_and_removes_the_user_install_and_the_marketplace(self):
        gone = str(self.tmp / "gone")
        self.machine.add("local", gone)
        self.machine.add()
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertIn(f"{gone} no longer exists: its entry is not uninstalled, and removing the marketplace drops its record", self.out)
        self.assertEqual(self.claude(), [(["claude", "plugin", "uninstall", PLUGIN], None),
                                         (["claude", "plugin", "marketplace", "remove", "factory"], None)])
        self.assertNotIn("By hand", self.err)

    def test_the_flag_installs_for_that_repository_only(self):
        self.assertEqual(self.command("wizard", "--yes", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.claude(), [install("local", self.p)])
        self.assertEqual(self.machine.installed(), [("local", self.p)])
        self.assertIn(f"for {self.p} only (for the whole user: `factory wizard`)", self.out)
        list_calls = [(c, d) for c, d in zip(self.machine.calls, self.machine.calls.dirs) if c[:3] == ["claude", "plugin", "list"]]
        self.assertEqual({d for _, d in list_calls}, {self.p})  # a local entry is enabled only in its own repository
        self.machine.calls.clear()
        self.assertEqual(self.command("wizard", "--yes", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.plugin_calls(), [])

    def test_the_flag_removes_the_user_install_and_leaves_the_local_install_of_another_repository(self):
        self.machine.add()
        self.machine.add("local", self.q)
        self.assertEqual(self.command("wizard", "--yes", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.claude(), [install("local", self.p), uninstall("user")])
        self.assertEqual(sorted(self.machine.installed()), sorted([("local", self.p), ("local", self.q)]))
        self.assertIn("also removes the user-scope install", self.out)
        self.machine.calls.clear()
        self.assertEqual(self.command("wizard", "--yes", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.plugin_calls(), [])

    def test_a_task_worktree_is_resolved_to_its_main_checkout(self):
        worktree = self.repository("p-task")
        self.machine.main_of[worktree] = self.p
        self.assertEqual(self.command("wizard", "--yes", "--repository", worktree), 0, self.out + self.err)
        self.assertEqual(self.machine.installed(), [("local", self.p)])

    def test_a_path_outside_a_repository_fails_and_runs_nothing(self):
        self.machine.non_repos.add(self.r)
        self.assertEqual(self.command("wizard", "--yes", "--repository", self.r), 2)
        self.assertIn("not inside a git repository", self.err)
        self.assertEqual(self.machine.mutations(), [])
        self.assertEqual(self.plugin_calls(), [])
        self.assertEqual(self.machine.calls, [["git", "worktree", "list", "--porcelain"]])

    def test_list_with_the_flag_runs_nothing(self):
        self.assertEqual(self.command("wizard", "--list", "--repository", self.p), 1)
        self.assertEqual(self.machine.mutations(), [])
        self.assertIn("plugin: not done (factory@factory)", self.out)

    def test_a_disabled_entry_at_the_wanted_scope_names_its_scope(self):
        self.machine.add("local", self.p, enabled=False)
        self.assertEqual(self.command("wizard", "--yes", "--repository", self.p), 1)
        self.assertIn("run `claude plugin enable factory@factory --scope local`", self.out)
        self.assertEqual(self.plugin_calls(), [])


class Reinstall(Scoped):
    MESSAGE = "Running sessions keep the copy they loaded: restart them, or run /reload-plugins in each."

    def test_one_user_entry(self):
        self.machine.add(sha="b" * 40)
        self.machine.next_head = "c" * 40
        self.assertEqual(self.command("reinstall"), 0, self.out + self.err)
        self.assertEqual(self.claude(), [UPDATE, uninstall("user"), install("user")])
        self.assertEqual(self.out, f"before: {'b' * 12}\nafter: {'c' * 12} user\n{self.MESSAGE}\n")

    def test_three_repositories_are_all_uninstalled_before_the_first_install(self):
        for path in (self.p, self.q, self.r):
            self.machine.add("local", path, sha="b" * 40)
        self.machine.next_head = "c" * 40
        self.assertEqual(self.command("reinstall"), 0, self.out + self.err)
        self.assertEqual(self.claude(), [UPDATE, *(uninstall("local", x) for x in (self.p, self.q, self.r)),
                                         *(install("local", x) for x in (self.p, self.q, self.r))])
        self.assertEqual(self.out.splitlines(), [f"before: {'b' * 12}", *(f"after: {'c' * 12} repository {x}" for x in (self.p, self.q, self.r)),
                                                 self.MESSAGE])

    def test_a_user_entry_and_a_repository_entry_keep_their_scopes(self):
        self.machine.add(sha="b" * 40)
        self.machine.add("local", self.p, sha="d" * 40)
        self.machine.next_head = "c" * 40
        self.assertEqual(self.command("reinstall"), 0, self.out + self.err)
        self.assertEqual(self.claude(), [UPDATE, uninstall("user"), uninstall("local", self.p), install("user"), install("local", self.p)])
        self.assertEqual(self.out.splitlines()[:2], [f"before: {'b' * 12}", f"before: {'d' * 12}"])
        self.assertEqual(sorted(self.machine.installed()), sorted([("user", None), ("local", self.p)]))

    def test_at_the_same_commit_it_says_so_and_still_reinstalls(self):
        self.machine.add()
        self.assertEqual(self.command("reinstall"), 0, self.out + self.err)
        self.assertEqual(self.claude(), [UPDATE, uninstall("user"), install("user")])
        self.assertIn(f"already at {'a' * 12}\n", self.out)

    def test_it_refuses_without_the_marketplace_and_changes_nothing(self):
        self.machine.add()
        self.machine.markets.clear()
        self.assertEqual(self.command("reinstall"), 2)
        self.assertIn("the marketplace factory is not added: run `factory wizard`", self.err)
        self.assertEqual(self.plugin_calls(), [])

    def test_it_refuses_a_marketplace_from_another_source_and_changes_nothing(self):
        self.machine.add()
        self.machine.markets[0]["repo"] = "someone/else"
        self.assertEqual(self.command("reinstall"), 2)
        self.assertIn("a marketplace named factory exists from someone/else, not andreiciobotar/isshoni", self.err)
        self.assertEqual(self.plugin_calls(), [])

    def test_it_refuses_when_nothing_is_installed_and_ignores_project_scope(self):
        self.machine.add("project", self.p)
        self.assertEqual(self.command("reinstall"), 2)
        self.assertIn("factory@factory is not installed: run `factory wizard`", self.err)
        self.assertEqual(self.plugin_calls(), [])

    def test_a_failed_install_lists_what_is_left_with_the_directories_and_exits_1(self):
        self.machine.add("local", self.p)
        self.machine.add("local", self.q)
        self.machine.fail.add("install")
        self.assertEqual(self.command("reinstall"), 1)
        self.assertIn("`claude plugin install factory@factory --scope local` failed: first line", self.err)
        self.assertIn(f"By hand:\n      cd {self.p} && claude plugin install factory@factory --scope local\n"
                      f"      cd {self.q} && claude plugin install factory@factory --scope local\n", self.err)
        self.assertNotIn("after:", self.out)

    def test_a_failed_update_lists_every_command(self):
        self.machine.add()
        self.machine.fail.add("update")
        self.assertEqual(self.command("reinstall"), 1)
        self.assertIn("By hand:\n      claude plugin marketplace update factory\n"
                      "      claude plugin uninstall factory@factory --scope user --keep-data\n"
                      "      claude plugin install factory@factory --scope user\n", self.err)
        self.assertEqual(self.machine.installed(), [("user", None)])

    def test_a_repository_that_is_gone_is_named_and_skipped_and_the_rest_goes_on(self):
        gone = str(self.tmp / "gone")
        self.machine.add("local", gone)
        self.machine.add("local", self.p)
        self.assertEqual(self.command("reinstall"), 0, self.out + self.err)
        self.assertIn(f"{gone} no longer exists: its entry is skipped", self.out)
        self.assertEqual(self.claude(), [UPDATE, uninstall("local", self.p), install("local", self.p)])
        self.assertEqual(self.machine.installed(), [("local", gone), ("local", self.p)])

    def test_every_external_call_has_the_network_timeout(self):
        self.machine.add()
        with mock.patch.object(factory, "run", wraps=self.machine) as run:
            self.assertEqual(self.command("reinstall"), 0, self.out + self.err)
        timeouts = [c.kwargs.get("timeout") for c in run.call_args_list if c.args[0] in self.machine.mutations()]
        self.assertEqual(timeouts, [factory.NETWORK_TIMEOUT] * 3)


class UninstallRepository(Scoped):
    def setUp(self):
        super().setUp()
        self.machine.add("local", self.p)
        self.machine.add("local", self.q)
        self.store.mkdir(parents=True)
        for name in ("settings.toml", "events.jsonl"):
            (self.store / name).write_text("x")
        (self.store / "identity").mkdir()
        (self.store / "identity" / "app.json").write_text("{}")
        self.records = {r: [factory.repo_state(r, name, ext) for name, ext in (("tasks", "jsonl"), ("watch", "json"))] for r in (self.p, self.q)}
        for paths in self.records.values():
            for path in paths:
                path.write_text("x")
        self.backend = factory.Launchd()
        for r in (self.p, self.q):
            self.backend.install(factory.job_label(r), ["python3", "/x/factory", "watch"], r, {"PATH": "/usr/bin"}, self.store / "watch.log")
        patcher = mock.patch.object(factory, "scheduler", lambda: self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.machine.calls.clear()

    def job(self, root):
        return self.backend.plist(factory.job_label(root)).exists()

    def test_it_removes_that_repository_and_nothing_else(self):
        self.assertEqual(self.command("uninstall", "--yes", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.claude(), [uninstall("local", self.p)])
        self.assertEqual(self.machine.installed(), [("local", self.q)])
        self.assertEqual((self.job(self.p), self.job(self.q)), (False, True))
        self.assertEqual([p.exists() for p in self.records[self.p]], [False, False])
        self.assertEqual([p.exists() for p in self.records[self.q]], [True, True])
        for kept in ("settings.toml", "events.jsonl", "identity/app.json"):
            self.assertTrue((self.store / kept).exists(), kept)
        self.assertEqual(self.machine.markets, [{"name": "factory", "source": "github", "repo": SOURCE}])
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.opened, [])
        self.assertIn("left behind:", self.out)
        self.assertIn(f"{self.p}/.claude/settings.local.json", self.out)
        self.assertNotIn("no installation is left", self.out)

    def test_a_worktree_entry_of_the_repository_goes_with_it_and_a_gone_worktree_is_named(self):
        worktrees = Path(self.p) / ".claude" / "worktrees"
        task, gone = worktrees / "task", worktrees / "gone"  # git lists neither: an entry is P's by its path
        task.mkdir(parents=True)
        for path in (task, gone, Path(self.p + "-other")):
            self.machine.add("local", str(path))
        self.assertEqual(self.command("uninstall", "--yes", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.claude(), [uninstall("local", self.p), uninstall("local", str(task))])
        self.assertEqual(sorted(self.machine.installed()),
                         sorted([("local", self.q), ("local", str(gone)), ("local", self.p + "-other")]))
        self.assertIn(f"{gone} no longer exists: its entry is skipped", self.out)

    def test_the_line_about_what_remains_appears_when_the_last_installation_goes(self):
        self.assertEqual(self.command("uninstall", "--yes", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.command("uninstall", "--yes", "--repository", self.q), 0, self.out + self.err)
        self.assertIn("no installation is left; factory uninstall removes the rest (the store, the App's installation, the marketplace)",
                      self.out)

    def test_a_user_install_is_not_removed_for_one_repository_but_its_schedule_and_records_go(self):
        self.machine.entries[:] = [e for e in self.machine.entries if e["projectPath"] != self.p]
        self.machine.add()
        self.assertEqual(self.command("uninstall", "--yes", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.plugin_calls(), [])
        self.assertIn("installed for the whole user: it is not removed for one repository", self.out)
        self.assertNotIn("settings.local.json", self.out)
        self.assertEqual((self.job(self.p), self.records[self.p][0].exists()), (False, False))
        self.assertNotIn("no installation is left", self.out)

    def test_list_changes_nothing(self):
        before = self.snapshot()
        self.assertEqual(self.command("uninstall", "--list", "--repository", self.p), 0, self.out + self.err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.plugin_calls(), [])
        self.assertTrue(self.job(self.p))
        self.assertIn(f"found: {PLUGIN} installed for {self.p}", self.out)

    def test_a_path_outside_a_repository_fails_and_changes_nothing(self):
        self.machine.non_repos.add(self.r)
        before = self.snapshot()
        self.assertEqual(self.command("uninstall", "--yes", "--repository", self.r), 2)
        self.assertIn("not inside a git repository", self.err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.machine.mutations(), [])

    def test_everything_removes_every_local_entry_then_the_user_entry_then_the_marketplace(self):
        self.machine.add()
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.claude(), [uninstall("local", self.p), uninstall("local", self.q),
                                         (["claude", "plugin", "uninstall", PLUGIN], None),
                                         (["claude", "plugin", "marketplace", "remove", "factory"], None)])
        self.assertEqual((self.machine.entries, self.machine.markets), ([], []))


if __name__ == "__main__":
    unittest.main()
