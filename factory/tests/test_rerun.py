import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import ROOT, FakeRun, factory, invoke, repo_answers

PREFIX = "test\tRun go test -race ./...\t2026-11-14T16:28:27.2263559Z "
GO_LOG = (f"test\tRun go test -race ./...\t\ufeff2026-11-14T16:27:48.8671256Z ##[group]Run go test -race ./...\n"
          f"{PREFIX}--- FAIL: TestAttachFollows (10.36s)\n"
          f"{PREFIX}    attach_test.go:138: timed out\n"
          f"{PREFIX}FAIL\tisshoni/cmd/isshoni-tui\t19.405s\n")
UNITTEST_LOG = ("test\tRun make test\t2026-11-14T16:28:27.2263559Z FAIL: test_lander_stops (test_land.LandTests.test_lander_stops)\n"
                "test\tRun make test\t2026-11-14T16:28:27.2263559Z ERROR: test_parse (test_merge.MergeTests.test_parse)\n"
                "test\tRun make test\t2026-11-14T16:28:27.2263559Z FAIL: test_lander_stops (test_land.LandTests.test_lander_stops)\n")
RUN = "43615514332"
LINK = f"https://github.com/o/r/actions/runs/{RUN}/job/1"
RUN2 = "43615514333"
LINK2 = f"https://github.com/o/r/actions/runs/{RUN2}/job/1"
Q = ("git", "-c", "core.quotePath=false")
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
        (*Q, "merge-base"): (0, "fork\n", ""),
        (*Q, "diff", "--name-only"): (0, "factory/bin/factory\n", ""),
        (*Q, "grep"): (0, "origin/feat:cmd/isshoni-tui/attach_test.go\n", ""),
        ("git", "rev-parse", "origin/main"): (0, "0123456789abcdef\n", ""),
        ("gh", "api"): (0, json.dumps({"check_runs": [{"name": "test", "conclusion": "success"}]}), ""),
        ("gh", "run", "rerun"): (0, "", ""),
        ("gh", "pr", "comment"): (0, "", ""),
    })
    base.update(over)
    return base


class Rerun(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": tmp.name}))

    def hold(self, n="7"):
        """The queue holds pull request `n` as red for a reason not its branch's."""
        factory.write_lander(SimpleNamespace(root=ROOT), {"stopped": {n: {"head": "h1", "kind": "not-ours", "text": "t", "ts": factory.now(),
                                                                          "tests": ["TestAttachFollows"], "checks": ["test"]}}})

    def rerun(self, over=None, word=False):
        self.fake, self.bodies = FakeRun(answers(over or {})), []

        def run(argv, **kw):
            if argv[:3] == ["gh", "pr", "comment"]:
                self.bodies.append(Path(argv[-1]).read_text())
            return self.fake(argv, **kw)
        with mock.patch.object(factory, "run", run), mock.patch.object(factory, "act_git", lambda path, *a: run(["git", *a])):
            return factory.rerun_line(factory.Repo(), "7", word=word)

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


    def test_two_runs_one_refused_after_the_first_started_says_which_started(self):
        checks = [{"name": "test", "bucket": "fail", "link": LINK}, {"name": "linux-walk", "bucket": "fail", "link": LINK2}]
        green = {"check_runs": [{"name": n, "conclusion": "success"} for n in ("test", "linux-walk")]}
        started, line = self.rerun({
            ("gh", "pr", "checks", "7"): (0, json.dumps(checks), ""), ("gh", "api"): (0, json.dumps(green), ""),
            ("gh", "run", "view", RUN2, "--log-failed"): (0, GO_LOG, ""),
            ("gh", "run", "view", RUN2, "--json", "attempt"): (0, '{"attempt": 1}', ""),
            ("gh", "run", "rerun"): [(0, "", ""), (1, "", REFUSED)]})
        self.assertTrue(started)
        test = "TestAttachFollows (cmd/isshoni-tui/attach_test.go)"
        self.assertEqual(line, f"rerun: test: {test} is outside what the branch changes and test is green on main; attempt 2 started; "
                               f"the App may not re-run checks; looks like a flake: {test}. From your own terminal: "
                               f"gh run rerun {RUN2} --failed ({REFUSED})")
        self.assertEqual(len(self.bodies), 1)

    def test_a_second_attempt_on_one_run_starts_nothing_on_the_other(self):
        checks = [{"name": "test", "bucket": "fail", "link": LINK}, {"name": "container", "bucket": "fail", "link": LINK2}]
        green = {"check_runs": [{"name": n, "conclusion": "success"} for n in ("test", "container")]}
        result = self.rerun({
            ("gh", "pr", "checks", "7"): (0, json.dumps(checks), ""), ("gh", "api"): (0, json.dumps(green), ""),
            ("gh", "run", "view", RUN2, "--log-failed"): (0, GO_LOG, ""),
            ("gh", "run", "view", RUN2, "--json", "attempt"): (0, '{"attempt": 2}', "")})
        self.assertNoRerun(result, "already re-run once; looks like a flake: TestAttachFollows (cmd/isshoni-tui/attach_test.go)")


class Held(Rerun):
    """With `word` (the caller has the human's word for a held pull request): the three flake conditions do not apply."""

    LINE = "rerun: test: attempt 2 started on the human's word"

    def test_a_held_pull_request_is_rerun_past_the_directory_the_base_and_the_attempt(self):
        self.hold()
        started, line = self.rerun({(*Q, "diff", "--name-only"): (0, "cmd/isshoni-tui/main.go\n", ""),
                                    ("gh", "api"): (0, json.dumps({"check_runs": []}), ""),
                                    ("gh", "run", "view", RUN, "--json", "attempt"): (0, '{"attempt": 2}', "")}, word=True)
        self.assertEqual((started, line), (True, self.LINE))
        self.assertEqual([c for c in self.fake.calls if c[:3] == ["gh", "run", "rerun"]], [["gh", "run", "rerun", RUN, "--failed"]])
        self.assertEqual(self.bodies, ["Re-ran the failed jobs of test on the human's word (by factory rerun)."])
        self.assertFalse(self.fake.ran("gh", "api") or self.fake.ran("gh", "run", "view", RUN, "--json"))

    def test_another_pull_request_held_changes_nothing_for_this_one(self):
        self.hold("8")
        self.assertNoRerun(self.rerun({(*Q, "diff", "--name-only"): (0, "cmd/isshoni-tui/main.go\n", "")}),
                           "TestAttachFollows (cmd/isshoni-tui/attach_test.go) is in what the branch changes")

    def test_the_other_conditions_still_apply(self):
        self.hold()
        self.assertNoRerun(self.rerun({("gh", "run", "view", RUN, "--log-failed"): (0, f"{PREFIX}##[error]exit 1\n", "")}, word=True),
                           "no failing test could be named in test's log")
        self.assertNoRerun(self.rerun({(*Q, "grep"): (1, "", "")}, word=True), "TestAttachFollows was not found in the branch")
        closed = (0, json.dumps({"state": "MERGED", "headRefName": "feat", "baseRefName": "main"}), "")
        self.assertNoRerun(self.rerun({("gh", "pr", "view", "7"): closed}, word=True), "the pull request is not open")

    def test_a_refusal_hands_the_human_the_command(self):
        self.hold()
        result = self.rerun({("gh", "run", "rerun"): (1, "", REFUSED)}, word=True)
        self.assertNoRerun(result, "the App may not re-run checks; looks like a flake: TestAttachFollows "
                                   f"(cmd/isshoni-tui/attach_test.go). From your own terminal: gh run rerun {RUN} --failed ({REFUSED})", asked=True)


class OwnTest(Rerun):
    def test_a_test_in_a_file_the_branch_changes_is_the_tasks(self):
        result = self.rerun({(*Q, "diff", "--name-only"): (0, "cmd/isshoni-tui/attach_test.go\n", "")})
        self.assertNoRerun(result, "TestAttachFollows (cmd/isshoni-tui/attach_test.go) is in what the branch changes")

    def test_a_test_in_a_directory_the_branch_changes_is_the_tasks(self):
        result = self.rerun({(*Q, "diff", "--name-only"): (0, "cmd/isshoni-tui/main.go\nREADME.md\n", "")})
        self.assertNoRerun(result, "TestAttachFollows (cmd/isshoni-tui/attach_test.go) is in what the branch changes")

    def test_the_file_named_is_the_one_the_branch_changes(self):
        found = "origin/feat:vendor/attach_test.go\norigin/feat:cmd/isshoni-tui/attach_test.go\n"
        result = self.rerun({(*Q, "grep"): (0, found, ""), (*Q, "diff", "--name-only"): (0, "cmd/isshoni-tui/main.go\n", "")})
        self.assertNoRerun(result, "TestAttachFollows (cmd/isshoni-tui/attach_test.go) is in what the branch changes")

    def test_a_changed_path_with_a_space_is_one_path(self):
        result = self.rerun({(*Q, "grep"): (0, "origin/feat:my dir/attach_test.go\n", ""),
                             (*Q, "diff", "--name-only"): (0, "my dir/other.go\n", "")})
        self.assertNoRerun(result, "TestAttachFollows (my dir/attach_test.go) is in what the branch changes")

    def test_a_test_missing_from_the_branch(self):
        self.assertNoRerun(self.rerun({(*Q, "grep"): (1, "", "")}), "TestAttachFollows was not found in the branch")


class Stops(Rerun):
    def test_a_check_not_green_on_the_base(self):
        red = (0, json.dumps({"check_runs": [{"name": "test", "conclusion": "failure"}]}), "")
        self.assertNoRerun(self.rerun({("gh", "api"): red}), "test is not green on main")

    def test_no_test_can_be_named(self):
        self.assertNoRerun(self.rerun({("gh", "run", "view", RUN, "--log-failed"): (0, f"{PREFIX}##[error]exit 1\n", "")}),
                           "no failing test could be named in test's log")

    def test_checks_still_running_come_before_every_other_decision(self):
        checks = [{"name": "test", "bucket": "fail", "link": LINK}, {"name": "container", "bucket": "pending", "link": LINK}]
        self.assertNoRerun(self.rerun({("gh", "pr", "checks", "7"): (0, json.dumps(checks), "")}), "checks are still running")

    def test_a_failing_merge_base_is_no_evidence(self):
        self.assertNoRerun(self.rerun({(*Q, "merge-base"): (1, "", "boom")}), "git merge-base failed: boom")

    def test_a_failing_diff_is_no_evidence(self):
        self.assertNoRerun(self.rerun({(*Q, "diff", "--name-only"): (128, "", "boom")}), "git diff failed: boom")

    def test_a_git_grep_that_errors_is_not_a_missing_test(self):
        self.assertNoRerun(self.rerun({(*Q, "grep"): (2, "", "")}), "git grep failed: 2")

    def test_a_failing_rev_parse_is_no_evidence(self):
        self.assertNoRerun(self.rerun({("git", "rev-parse", "origin/main"): (128, "", "")}), "git rev-parse failed: 128")

    def test_a_failing_log_read_is_not_a_missing_test_name(self):
        self.assertNoRerun(self.rerun({("gh", "run", "view", RUN, "--log-failed"): (1, "", "boom")}), "gh run view failed: boom")

    def test_an_answer_without_an_attempt_is_not_attempt_1(self):
        self.assertNoRerun(self.rerun({("gh", "run", "view", RUN, "--json", "attempt"): (0, "{}", "")}),
                           "gh run view failed: no attempt in its answer")

    def test_no_failed_check(self):
        checks = (0, json.dumps([{"name": "test", "bucket": "cancel", "link": LINK}]), "")
        self.assertNoRerun(self.rerun({("gh", "pr", "checks", "7"): checks}), "no failed check")

    def test_a_check_that_is_not_an_actions_run(self):
        checks = (0, json.dumps([{"name": "lint", "bucket": "fail", "link": "https://elsewhere/ci/1"}]), "")
        self.assertNoRerun(self.rerun({("gh", "pr", "checks", "7"): checks}), "lint is not an Actions run")

    def test_a_pull_request_that_is_not_open(self):
        closed = (0, json.dumps({"state": "MERGED", "headRefName": "feat", "baseRefName": "main"}), "")
        self.assertNoRerun(self.rerun({("gh", "pr", "view", "7"): closed}), "the pull request is not open")


class Cli(unittest.TestCase):
    def invoke(self, answers):
        """`factory rerun 7` through main(); `fetch` goes through the same stub as every other git call."""
        with mock.patch.object(factory, "act_git", lambda path, *a: factory.run(["git", *a])):
            return invoke(["rerun", "7"], answers)

    def test_the_line_has_the_number_and_the_exit_code_says_whether_a_rerun_started(self):
        code, out, err, fake = self.invoke(answers({}))
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("#7 rerun: test: TestAttachFollows"), out)
        self.assertTrue(fake.ran("gh", "run", "rerun"))
        closed = (0, json.dumps({"state": "MERGED", "headRefName": "feat", "baseRefName": "main"}), "")
        code, out, err, fake = self.invoke(answers({("gh", "pr", "view", "7"): closed}))
        self.assertEqual((code, out), (1, "#7 no rerun: the pull request is not open\n"))

    def test_a_gh_failure_midway_is_a_no_rerun_line_and_starts_nothing(self):
        code, out, err, fake = self.invoke(answers({("gh", "api"): (1, "", "boom")}))
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("#7 no rerun: gh api ") and out.endswith("failed: boom\n"), out)
        self.assertFalse(fake.ran("gh", "run", "rerun"))


class LogFormats(unittest.TestCase):
    def test_go(self):
        self.assertEqual(factory.failing_tests(GO_LOG), ["TestAttachFollows"])

    def test_a_fail_line_that_is_the_first_of_the_log_carries_the_bom(self):
        self.assertEqual(factory.failing_tests("test\tRun go test\t﻿2026-11-14T16:27:48.8671256Z --- FAIL: TestFirst (1s)\n"), ["TestFirst"])

    def test_a_go_subtest_names_its_parent(self):
        self.assertEqual(factory.failing_tests(f"{PREFIX}    --- FAIL: TestA/sub_1 (0.00s)\n{PREFIX}--- FAIL: TestA (0.00s)\n"), ["TestA"])

    def test_unittest_fail_and_error_once_each(self):
        self.assertEqual(factory.failing_tests(UNITTEST_LOG), ["test_lander_stops", "test_parse"])

    def test_nothing(self):
        self.assertEqual(factory.failing_tests(f"{PREFIX}FAIL\tisshoni/x\t1s\n{PREFIX}exit 1\n"), [])

    def test_pytest_and_jest_logs_name_nothing(self):
        pytest = f"{PREFIX}FAILED tests/test_a.py::test_one - assert 1 == 2\n{PREFIX}=== 1 failed, 3 passed in 0.12s ===\n"
        jest = f"{PREFIX}FAIL src/a.test.js\n{PREFIX}  ● adds numbers\n{PREFIX}Tests: 1 failed, 3 passed\n"
        self.assertEqual((factory.failing_tests(pytest), factory.failing_tests(jest)), ([], []))


if __name__ == "__main__":
    unittest.main()
