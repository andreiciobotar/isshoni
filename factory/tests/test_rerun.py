import json
import unittest
from pathlib import Path
from unittest import mock

from test_factory import FakeRun, factory, repo_answers

PREFIX = "test\tRun go test -race ./...\t2026-11-14T16:28:27.2263559Z "
GO_LOG = (f"test\tRun go test -race ./...\t﻿2026-11-14T16:27:48.8671256Z ##[group]Run go test -race ./...\n"
          f"{PREFIX}--- FAIL: TestAttachFollows (10.36s)\n"
          f"{PREFIX}    attach_test.go:138: timed out\n"
          f"{PREFIX}FAIL\tisshoni/cmd/isshoni-tui\t19.405s\n")
UNITTEST_LOG = ("test\tRun make test\t2026-11-14T16:28:27.2263559Z FAIL: test_lander_stops (test_land.LandTests.test_lander_stops)\n"
                "test\tRun make test\t2026-11-14T16:28:27.2263559Z ERROR: test_parse (test_merge.MergeTests.test_parse)\n"
                "test\tRun make test\t2026-11-14T16:28:27.2263559Z FAIL: test_lander_stops (test_land.LandTests.test_lander_stops)\n")
RUN = "43615514332"
LINK = f"https://github.com/o/r/actions/runs/{RUN}/job/1"
REFUSED = "HTTP 403: Resource not accessible by integration"


def answers(over):
    """A red `test` check on #7 whose failing test lives far from the branch's files, green on main."""
    base = repo_answers([], (), {
        ("gh", "pr", "view", "7"): (0, json.dumps({"state": "OPEN", "headRefName": "feat", "baseRefName": "main"}), ""),
        ("gh", "pr", "checks", "7"): (0, json.dumps([{"name": "test", "bucket": "fail", "link": LINK},
                                                    {"name": "container", "bucket": "pass", "link": LINK}]), ""),
        ("gh", "run", "view", RUN, "--log-failed"): (0, GO_LOG, ""),
        ("gh", "run", "view", RUN, "--json", "attempt"): (0, '{"attempt": 1}', ""),
        ("git", "fetch"): (0, "", ""),
        ("git", "merge-base"): (0, "fork\n", ""),
        ("git", "diff", "--name-only"): (0, "factory/bin/factory\n", ""),
        ("git", "grep"): (0, "origin/feat:cmd/isshoni-tui/attach_test.go\n", ""),
        ("git", "rev-parse", "origin/main"): (0, "0123456789abcdef\n", ""),
        ("gh", "api"): (0, json.dumps({"check_runs": [{"name": "test", "conclusion": "success"}]}), ""),
        ("gh", "run", "rerun"): (0, "", ""),
        ("gh", "pr", "comment"): (0, "", ""),
    })
    base.update(over)
    return base


class Rerun(unittest.TestCase):
    def rerun(self, over=None):
        self.fake, self.bodies = FakeRun(answers(over or {})), []

        def run(argv, **kw):
            if argv[:3] == ["gh", "pr", "comment"]:
                self.bodies.append(Path(argv[-1]).read_text())
            return self.fake(argv, **kw)
        with mock.patch.object(factory, "run", run), mock.patch.object(factory, "act_git", lambda path, *a: run(["git", *a])):
            return factory.rerun_line(factory.Repo(), "7")

    def assertNoRerun(self, result, text, asked=False):
        self.assertEqual(result[0], False)
        self.assertEqual(result[1], f"no rerun: {text}")
        self.assertEqual(self.fake.ran("gh", "run", "rerun"), asked)
        self.assertEqual(self.bodies, [])


class Flake(Rerun):
    def test_a_test_outside_the_branch_that_is_green_on_the_base_is_rerun_once_and_commented(self):
        started, line = self.rerun()
        self.assertTrue(started)
        test = "TestAttachFollows (cmd/isshoni-tui/attach_test.go)"
        self.assertEqual(line, f"rerun: test: {test} is outside what the branch changes and test is green on main; attempt 2 started")
        self.assertEqual([c for c in self.fake.calls if c[:3] == ["gh", "run", "rerun"]], [["gh", "run", "rerun", RUN, "--failed"]])
        self.assertEqual(self.bodies, [f"Re-ran the failed jobs of test once (by factory rerun): {test} is outside what this "
                                       "branch changes, and test is green on main at 0123456."])

    def test_the_second_attempt_is_not_rerun(self):
        result = self.rerun({("gh", "run", "view", RUN, "--json", "attempt"): (0, '{"attempt": 2}', "")})
        self.assertNoRerun(result, "already re-run once; looks like a flake: TestAttachFollows (cmd/isshoni-tui/attach_test.go)")

    def test_a_refusal_hands_the_human_the_command(self):
        result = self.rerun({("gh", "run", "rerun"): (1, "", REFUSED)})
        self.assertNoRerun(result, "the App may not re-run checks; looks like a flake: TestAttachFollows "
                                   f"(cmd/isshoni-tui/attach_test.go). From your own terminal: gh run rerun {RUN} --failed ({REFUSED})", asked=True)


class OwnTest(Rerun):
    def test_a_test_in_a_file_the_branch_changes_is_the_tasks(self):
        result = self.rerun({("git", "diff", "--name-only"): (0, "cmd/isshoni-tui/attach_test.go\n", "")})
        self.assertNoRerun(result, "TestAttachFollows (cmd/isshoni-tui/attach_test.go) is in what the branch changes")

    def test_a_test_in_a_directory_the_branch_changes_is_the_tasks(self):
        result = self.rerun({("git", "diff", "--name-only"): (0, "cmd/isshoni-tui/main.go\nREADME.md\n", "")})
        self.assertNoRerun(result, "TestAttachFollows (cmd/isshoni-tui/attach_test.go) is in what the branch changes")

    def test_a_test_missing_from_the_branch(self):
        self.assertNoRerun(self.rerun({("git", "grep"): (1, "", "")}), "TestAttachFollows was not found in the branch")


class Stops(Rerun):
    def test_a_check_not_green_on_the_base(self):
        red = (0, json.dumps({"check_runs": [{"name": "test", "conclusion": "failure"}]}), "")
        self.assertNoRerun(self.rerun({("gh", "api"): red}), "test is not green on main")

    def test_no_test_can_be_named(self):
        self.assertNoRerun(self.rerun({("gh", "run", "view", RUN, "--log-failed"): (0, f"{PREFIX}##[error]exit 1\n", "")}),
                           "no failing test could be named in test's log")

    def test_no_failed_check(self):
        checks = (0, json.dumps([{"name": "test", "bucket": "cancel", "link": LINK}]), "")
        self.assertNoRerun(self.rerun({("gh", "pr", "checks", "7"): checks}), "no failed check")

    def test_a_check_that_is_not_an_actions_run(self):
        checks = (0, json.dumps([{"name": "lint", "bucket": "fail", "link": "https://elsewhere/ci/1"}]), "")
        self.assertNoRerun(self.rerun({("gh", "pr", "checks", "7"): checks}), "lint is not an Actions run")

    def test_a_pull_request_that_is_not_open(self):
        closed = (0, json.dumps({"state": "MERGED", "headRefName": "feat", "baseRefName": "main"}), "")
        self.assertNoRerun(self.rerun({("gh", "pr", "view", "7"): closed}), "the pull request is not open")


class LogFormats(unittest.TestCase):
    def test_go(self):
        self.assertEqual(factory.failing_tests(GO_LOG), ["TestAttachFollows"])

    def test_a_go_subtest_names_its_parent(self):
        self.assertEqual(factory.failing_tests(f"{PREFIX}    --- FAIL: TestA/sub_1 (0.00s)\n{PREFIX}--- FAIL: TestA (0.00s)\n"), ["TestA"])

    def test_unittest_fail_and_error_once_each(self):
        self.assertEqual(factory.failing_tests(UNITTEST_LOG), ["test_lander_stops", "test_parse"])

    def test_nothing(self):
        self.assertEqual(factory.failing_tests(f"{PREFIX}FAIL\tisshoni/x\t1s\n{PREFIX}exit 1\n"), [])


if __name__ == "__main__":
    unittest.main()
