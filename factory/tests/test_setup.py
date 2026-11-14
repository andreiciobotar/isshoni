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

PLUGIN = "factory@factory"
SOURCE = "andreiciobotar/isshoni"
IDENTITY = {"app_id": 123456, "slug": "factory-octocat", "client_id": "Iv23abc", "html_url": APP["html_url"],
            "owner": "octocat", "owner_type": "User"}
MUTATING_PREFIXES = (["claude", "plugin", "install"], ["claude", "plugin", "uninstall"], ["claude", "plugin", "marketplace", "add"],
                     ["claude", "plugin", "marketplace", "remove"], ["make"], ["crontab", "-"], ["launchctl"])


class Machine:
    """Every external command of the wizard and uninstall: records the calls and answers from a small world."""

    def __init__(self, clone):
        self.clone = clone
        self.tools = {"git", "gh", "claude", "openssl", "go", "make"}
        self.gh_login, self.claude_login = True, True
        self.plugins, self.markets, self.fail, self.crontab = [], [], set(), ""
        self.other_plugins, self.disabled = [], set()
        self.calls = []

    def which(self, tool):
        return f"/usr/bin/{tool}" if tool in self.tools else None

    def __call__(self, argv, cwd=None, timeout=None, input=None, env=None):
        self.calls.append(list(argv))
        a = list(argv)
        if a[:3] == ["gh", "auth", "status"]:
            return (0 if self.gh_login else 1), "", ""
        if a[:3] == ["gh", "api", "user"]:
            return 0, "octocat\n", ""
        if a[:3] == ["claude", "auth", "status"]:
            if isinstance(self.claude_login, tuple):
                return self.claude_login
            return 0, json.dumps({"loggedIn": self.claude_login}), ""
        if a[:3] == ["claude", "plugin", "list"]:
            return 0, json.dumps([{"id": i, "scope": "user", "enabled": i not in self.disabled} for i in self.plugins]
                                 + self.other_plugins), ""
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
        if a[:3] == ["claude", "plugin", "install"]:
            self.plugins.append(a[3])
        elif a[:3] == ["claude", "plugin", "uninstall"]:
            self.plugins.remove(a[3])
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
                        mock.patch.object(factory, "SETTINGS_PATH", self.store / "settings.json"),
                        mock.patch.object(factory, "IDENTITY", self.store / "identity"),
                        mock.patch.object(factory, "run", self.machine),
                        mock.patch.object(factory.shutil, "which", self.machine.which),
                        mock.patch.object(factory, "clone", lambda: self.root if self.has_clone else None),
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
        return json.loads((self.store / "settings.json").read_text())

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
        self.assertEqual(self.settings(), {"worktree_cap": 5, "lead_model": "fable", "lead_permission_mode": "auto",
                                           "task_time_limit_hours": 2, "monitor": str(self.root / "bin" / "isshoni"),
                                           "lgtm_from_author": True})
        self.assertEqual(self.created, [("octocat", None)])
        self.assertEqual(json.loads((self.store / "identity" / "app.json").read_text())["installation_id"], 7654321)
        self.assertNotIn("Nothing to do", self.out)
        self.assertIn("/rename dispatcher-<repository directory name>", self.out)
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
        self.assertIn("By hand: claude plugin marketplace add andreiciobotar/isshoni; claude plugin install factory@factory", self.out)
        self.assertIn("Then run the wizard again.", self.out)
        self.assertFalse(self.store.exists())
        self.machine.fail.clear()
        self.machine.calls.clear()
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations(), [["claude", "plugin", "install", PLUGIN]])
        self.assertTrue((self.store / "settings.json").exists())

    def test_dashboard_without_go_or_a_clone_is_skipped_and_the_run_goes_on(self):
        self.machine.tools.discard("go")
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertIn("skipped: Go 1.27+ is missing: https://go.dev/dl, then run the wizard again", self.out)
        self.assertNotIn(["make", "build"], self.machine.calls)
        self.assertIn(PLUGIN, self.machine.plugins)
        self.has_clone = False
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertIn("skipped: the dashboard needs a clone of the repository", self.out)
        self.assertIn("skipped: cost alerts and the daily digest need a clone", self.out)

    def test_a_failed_build_is_one_line_and_the_run_goes_on_but_exits_1(self):
        self.machine.fail.add("make")
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("needs you: make build failed: it broke", self.out)
        self.assertIn(PLUGIN, self.machine.plugins)
        self.assertTrue((self.store / "settings.json").exists())

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
        self.machine.other_plugins.append({"id": PLUGIN, "scope": "project", "enabled": True, "projectPath": "/x"})
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertIn(["claude", "plugin", "install", PLUGIN], self.machine.calls)
        self.machine.calls.clear()
        self.machine.disabled.add(PLUGIN)
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("installed but disabled: run `claude plugin enable factory@factory`", self.out)
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
        self.assertNotIn("monitor", self.settings())

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
        (self.store / "settings.json").write_text('{"worktree_cap": 0}')
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertIn("worktree_cap must be an integer", self.out)
        self.assertIn(str(self.store / "settings.json"), self.out)
        self.assertEqual((self.store / "settings.json").read_text(), '{"worktree_cap": 0}')
        (self.store / "settings.json").write_text("{not json")
        self.assertEqual(self.command("wizard", "--yes"), 1)
        self.assertEqual((self.store / "settings.json").read_text(), "{not json")

    def test_the_monitor_joins_the_settings_and_every_other_key_survives(self):
        self.store.mkdir(parents=True)
        (self.store / "settings.json").write_text('{"worktree_cap": 9, "dashboard_theme": "dark", "monitor": "/gone/isshoni"}')
        self.assertEqual(self.command("wizard", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.settings(), {"worktree_cap": 9, "dashboard_theme": "dark", "monitor": str(self.root / "bin" / "isshoni")})
        self.assertIn("it pointed at /gone/isshoni, which is missing", self.out)

    def test_a_declined_offer_changes_nothing(self):
        for stdin in ("", "\n", "n\n", "maybe\n"):
            self.machine.calls.clear()
            self.assertEqual(self.command("wizard", stdin=stdin), 0, self.out + self.err)
            self.assertEqual([c for c in self.machine.mutations() if c[0] == "make"], [])
            self.assertEqual(self.created, [])
            self.assertNotIn("monitor", self.settings())
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

    def test_ask(self):
        for text, want in (("y\n", True), ("YES\n", True), ("n\n", False), ("\n", False), ("", False), ("sure\n", False)):
            with mock.patch.object(sys, "stdin", io.StringIO(text)), redirect_stdout(io.StringIO()):
                self.assertEqual(factory.ask("ok?", False), want, text)
        with mock.patch.object(sys, "stdin", io.StringIO("")), redirect_stdout(io.StringIO()):
            self.assertTrue(factory.ask("ok?", True))


class Clone(Setup):
    def test_a_clone_holds_bin_isshoni_and_go_mod_two_levels_above_the_script(self):
        with mock.patch.object(factory, "clone", self.__class__.real_clone):
            for root, want in ((self.root, self.root), (self.tmp / "plugin-cache", None)):
                script = root / "factory" / "bin" / "factory"
                script.parent.mkdir(parents=True, exist_ok=True)
                with mock.patch.object(factory, "FACTORY", script):
                    self.assertEqual(factory.clone(), want)

    real_clone = factory.clone


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
        self.assertEqual((self.machine.plugins, self.machine.markets), ([], []))
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
                     str(self.store / "settings.json"), str(self.store / "identity") + " (",
                     "/settings/apps/factory-octocat/advanced", "found: " + str(self.root / "bin" / "isshoni-tui")):
            self.assertIn(line, self.out)

    def test_a_declined_step_keeps_its_things_and_says_so(self):
        self.set_up()
        self.assertEqual(self.command("uninstall", stdin=""), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations(), [])
        self.assertFalse(self.github.called("DELETE", "/app/installations/7654321"))
        self.assertTrue((self.store / "settings.json").exists())
        self.assertIn("the plugin and its marketplace: you declined", self.out)
        self.assertIn(f"the home {self.store}: you declined", self.out)
        self.assertIn("the dashboard binary: you declined", self.out)
        self.assertEqual(self.opened, ["https://github.com/settings/apps/factory-octocat/advanced"])

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
        self.machine.plugins.append(PLUGIN)
        self.machine.markets.append({"name": "factory", "source": "github", "repo": "someone/else"})
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual(self.machine.mutations(), [])
        self.assertEqual(self.machine.plugins, [PLUGIN])
        self.assertIn("not from andreiciobotar/isshoni", self.out)

    def test_an_unreadable_list_removes_no_plugin_and_no_marketplace(self):
        self.set_up()
        self.machine.calls.clear()
        self.machine.fail.add("market-list")
        self.assertEqual(self.command("uninstall", "--yes"), 1)
        self.assertIn("nothing removed", self.out)
        self.assertEqual([c for c in self.machine.mutations() if c[0] == "claude"], [])
        self.assertEqual(self.machine.plugins, [PLUGIN])

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
        for name in ("settings.json", "watch.log", "events.jsonl", "tasks-2026-11.jsonl", "retros-2026-01.jsonl", ".settings-ab12.tmp"):
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
        for name in ("settings.json", "watch.log", "events.jsonl", "tasks-2026-11.jsonl", ".settings-ab12.tmp"):
            self.assertIn(str(self.store / name), self.out)
        self.assertIn(f"{self.store / 'specs'} (1 files)", self.out)
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), ["events.jsonl.bak", "notes.txt"])
        self.assertIn(f"not the factory's, kept: {self.store / 'notes.txt'}", self.out)
        self.assertIn("directory stays", self.out)
        self.assertIn("notes.txt: not the factory's", self.out)

    def test_the_marketplace_of_the_factory_goes_alone_and_project_scope_is_left(self):
        self.machine.markets.append({"name": "factory", "source": "github", "repo": SOURCE})
        self.machine.other_plugins.append({"id": PLUGIN, "scope": "project", "enabled": True})
        self.assertEqual(self.command("uninstall", "--yes"), 0, self.out + self.err)
        self.assertEqual([c for c in self.machine.mutations() if c[0] == "claude"],
                         [["claude", "plugin", "marketplace", "remove", "factory"]])

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
            factory.uninstall_schedules(Namespace(yes=True, list=False), [])

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
            factory.uninstall_schedules(Namespace(yes=True, list=False), [])
        self.assertEqual(out.getvalue(), "  schedule: no scheduler here: nothing to remove\n")

    def test_the_step_lists_without_removing(self):
        backend = factory.Launchd()
        self.install(backend, self.labels()["mine"])
        out = io.StringIO()
        with mock.patch.object(factory, "scheduler", lambda: backend), redirect_stdout(out):
            factory.uninstall_schedules(Namespace(yes=True, list=True), [])
        self.assertIn("found: 1 watch job(s) of this home: com.claude.factory.watch.repo-aaaaaaaa", out.getvalue())
        self.assertTrue(backend.plist(self.labels()["mine"]).exists())


if __name__ == "__main__":
    unittest.main()
