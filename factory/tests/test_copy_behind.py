import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_factory import factory

LINE = ("the installed factory is {n} plugin commits behind main (installed 2026-11-16): refresh with "
        "factory reinstall (from a clone: make reinstall)")
COULD_NOT = "the installed factory could not be compared with main: "
TASK = "/work/repo/.claude/worktrees/some-task"


class CopyBehindTests(unittest.TestCase):
    """An installed copy of a plugin laid out as Claude Code does, over a local source repository: five commits after the
    installed one, two of them touching `factory/`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t"}
        self.source = self.tmp / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-q", "-b", "main")
        self.git(self.source, "config", "uploadpack.allowFilter", "true")
        self.git(self.source, "config", "uploadpack.allowAnySHA1InWant", "true")
        (self.source / ".claude-plugin").mkdir()
        (self.source / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "factory", "plugins": [{"name": "factory", "source": "./factory"}]}))
        (self.source / "factory").mkdir()
        self.git(self.source, "add", ".claude-plugin")
        self.commits = [self.commit("factory/a.txt")]
        self.commits += [self.commit(f) for f in ("factory/a.txt", "docs.txt", "docs.txt", "factory/a.txt", "docs.txt")]
        plugins = self.tmp / "plugins"
        self.git(self.tmp, "clone", "-q", f"file://{self.source}", str(plugins / "marketplaces" / "factory"))
        self.root = plugins / "cache" / "factory" / "factory" / "0.1.0"
        self.root.mkdir(parents=True)
        (self.root / ".claude-plugin").mkdir()
        (self.root / ".claude-plugin" / "plugin.json").write_text('{"version": "0.1.0"}')  # `record` reads it
        self.install(self.commits[0])
        (plugins / "known_marketplaces.json").write_text(json.dumps(
            {"factory": {"source": {"source": "github", "repo": "o/r"}, "installLocation": str(plugins / "marketplaces" / "factory")}}))
        self.store = self.tmp / "store"
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        for d in (self.home, self.repo):
            d.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        for patch in (mock.patch.object(factory, "PLUGIN_ROOT", self.root), mock.patch.object(factory, "STORE", self.store),
                      mock.patch.object(factory, "CACHE", self.store / "cache"),
                      mock.patch.dict(os.environ, {"HOME": str(self.home), "FACTORY_SESSION": "1"}),
                      mock.patch.object(factory, "follow_dispatcher"), mock.patch.object(factory, "list_sessions", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, name):
        path = self.source / name
        path.write_text(path.read_text() + "x\n" if path.exists() else "x\n")
        self.git(self.source, "add", name)
        self.git(self.source, "commit", "-q", "-m", name)
        return self.git(self.source, "rev-parse", "HEAD")

    def install(self, sha):
        (self.tmp / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {"factory@factory": [
            {"scope": "user", "installPath": str(self.root), "version": "0.1.0", "installedAt": "2026-11-15T10:00:00.000Z",
             "lastUpdated": "2026-11-16T19:55:29.849Z", "gitCommitSha": sha}]}}))

    def hook(self, cwd="/work/repo", **payload):
        stdin = io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": cwd, **payload}))
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = factory.main(["hook"])
        return rc, out.getvalue()

    def status(self):
        out = io.StringIO()
        with contextlib.chdir(self.repo), redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = factory.main(["status"])
        return rc, out.getvalue()

    def recorded(self):
        return [json.loads(l) for f in self.store.glob("events-*.jsonl") for l in f.read_text().splitlines()]

    def test_behind_counts_only_commits_touching_the_plugin(self):
        self.assertEqual(factory.copy_behind(), LINE.format(n=2))

    def test_status_prints_it_after_the_identity_line(self):
        rc, out = self.status()
        lines = out.splitlines()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1], LINE.format(n=2))
        self.assertTrue(lines[-2].startswith("acting on GitHub"))

    def test_the_hook_prints_one_json_object_for_the_main_checkout(self):
        rc, out = self.hook()
        self.assertEqual((rc, json.loads(out)), (0, {"systemMessage": LINE.format(n=2)}))

    def test_a_session_that_is_not_the_factorys_gets_no_notice_and_no_fetch(self):
        del os.environ["FACTORY_SESSION"]
        self.assertEqual(self.hook(), (0, ""))
        self.assertFalse((self.store / "cache" / "copy-behind.json").exists())

    def test_the_hook_prints_nothing_in_a_task_worktree_or_for_a_subagent(self):
        self.assertEqual(self.hook(cwd=TASK), (0, ""))
        self.assertEqual(self.hook(agent_id="a1"), (0, ""))
        self.assertEqual(self.hook(cwd=TASK + "/sub"), (0, ""))
        self.assertFalse((self.store / "cache" / "copy-behind.json").exists())

    def test_the_hook_prints_nothing_for_another_event(self):
        stdin = io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/work/repo"}))
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(out):
            self.assertEqual(factory.main(["hook"]), 0)
        self.assertEqual(out.getvalue(), "")

    def test_at_head_nothing_in_either_place(self):
        self.install(self.commits[-1])
        self.assertIsNone(factory.copy_behind())
        self.assertNotIn("installed factory", self.status()[1])
        self.assertEqual(self.hook(), (0, ""))

    def test_an_unreachable_source_says_so_and_changes_nothing_else(self):
        marketplace = self.tmp / "plugins" / "marketplaces" / "factory"
        self.git(marketplace, "remote", "set-url", "origin", str(self.tmp / "nowhere"))
        rc, out = self.status()
        self.assertEqual(rc, 0)
        self.assertTrue(out.splitlines()[-1].startswith(COULD_NOT), out)
        self.assertEqual(len(out.splitlines()[-1].splitlines()), 1)
        rc, out = self.hook()
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["systemMessage"].startswith(COULD_NOT))
        self.assertEqual([e["event"] for e in self.recorded()], ["SessionStart"])
        self.assertFalse((self.store / "cache" / "plugin-source.git").exists())

    def test_status_cached_asks_no_remote_and_prints_no_line(self):
        out = io.StringIO()
        with contextlib.chdir(self.repo), redirect_stdout(out), redirect_stderr(io.StringIO()), \
                mock.patch.object(factory, "copy_behind", side_effect=AssertionError("asked")):
            self.assertEqual(factory.main(["status", "--cached"]), 0)
        self.assertNotIn("installed factory", out.getvalue())

    def test_a_repointed_marketplace_source_is_followed(self):
        self.assertEqual(factory.copy_behind(), LINE.format(n=2))
        other = self.tmp / "other"
        self.git(self.tmp, "clone", "-q", str(self.source), str(other))
        (other / "factory" / "b.txt").write_text("x\n")
        self.git(other, "add", "factory/b.txt")
        self.git(other, "commit", "-q", "-m", "b")
        self.git(self.tmp / "plugins" / "marketplaces" / "factory", "remote", "set-url", "origin", f"file://{other}")
        (self.store / "cache" / "copy-behind.json").unlink()
        self.assertEqual(factory.copy_behind(), LINE.format(n=3))

    def test_an_entry_without_dates_is_compared_with_a_question_mark(self):
        path = self.tmp / "plugins" / "installed_plugins.json"
        doc = json.loads(path.read_text())
        for k in ("installedAt", "lastUpdated"):
            del doc["plugins"]["factory@factory"][0][k]
        path.write_text(json.dumps(doc))
        self.assertEqual(factory.copy_behind(), LINE.format(n=2).replace("2026-11-16", "?"))

    def test_an_entry_without_a_commit_says_so(self):
        path = self.tmp / "plugins" / "installed_plugins.json"
        doc = json.loads(path.read_text())
        del doc["plugins"]["factory@factory"][0]["gitCommitSha"]
        path.write_text(json.dumps(doc))
        self.assertTrue(factory.copy_behind().startswith(COULD_NOT))

    def test_a_missing_marketplace_file_says_so(self):
        (self.tmp / "plugins" / "known_marketplaces.json").unlink()
        self.assertTrue(factory.copy_behind().startswith(COULD_NOT))

    def test_an_installed_commit_missing_from_the_source_says_so(self):
        self.install("0" * 40)
        self.assertTrue(factory.copy_behind().startswith(COULD_NOT))

    def test_a_copy_that_is_not_installed_is_not_checked(self):
        with mock.patch.object(factory, "PLUGIN_ROOT", Path(factory.__file__).resolve().parent.parent), \
                mock.patch.object(factory, "run") as run:
            self.assertIsNone(factory.copy_behind())
        run.assert_not_called()
        self.assertFalse((self.store / "cache").exists())

    def test_a_second_call_within_the_hour_runs_no_git(self):
        first = factory.copy_behind()
        with mock.patch.object(factory, "run", side_effect=AssertionError("git ran")):
            self.assertEqual(factory.copy_behind(), first)

    def test_a_failure_is_not_retried_within_the_hour(self):
        self.git(self.tmp / "plugins" / "marketplaces" / "factory", "remote", "set-url", "origin", str(self.tmp / "nowhere"))
        first = factory.copy_behind()
        self.assertTrue(first.startswith(COULD_NOT))
        with mock.patch.object(factory, "run", side_effect=AssertionError("git ran")):
            self.assertEqual(factory.copy_behind(), first)

    def test_an_old_answer_is_asked_again(self):
        factory.copy_behind()
        path = self.store / "cache" / "copy-behind.json"
        held = json.loads(path.read_text())
        path.write_text(json.dumps({**held, "asked": held["asked"] - factory.COPY_CHECK_SECONDS - 1, "line": "old"}))
        self.assertEqual(factory.copy_behind(), LINE.format(n=2))

    def test_a_changed_installed_commit_ignores_the_cache(self):
        self.assertEqual(factory.copy_behind(), LINE.format(n=2))
        self.install(self.commits[1])
        self.assertEqual(factory.copy_behind(), LINE.format(n=1).replace("commits", "commit"))
        self.install(self.commits[4])
        self.assertIsNone(factory.copy_behind())

    def test_a_later_fetch_sees_new_commits(self):
        factory.copy_behind()
        self.commit("factory/b.txt")
        (self.store / "cache" / "copy-behind.json").unlink()
        self.assertEqual(factory.copy_behind(), LINE.format(n=3))

    def test_the_commit_of_an_installed_copy_is_its_recorded_one(self):
        self.assertEqual(factory.copy_commit(), self.commits[0])

    def test_the_commit_of_a_checkout_is_its_head(self):
        root = Path(factory.__file__).resolve().parent.parent
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
        with mock.patch.object(factory, "PLUGIN_ROOT", root):
            self.assertEqual(factory.copy_commit(), head)


if __name__ == "__main__":
    unittest.main()
