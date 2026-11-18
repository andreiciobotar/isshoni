import io
import json
import os
import subprocess
import tempfile
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_factory import BIN, LEAN_SETTINGS, NO_ATTRIBUTION, factory, porcelain
from test_tech_lead import DEFAULT, ISOLATED_SETTINGS, N, OLD, Case, Machine, SLUG, URL

HEAD = "d" * 40
SPEC_TEXT = "the spec"


def built(outcome="built", handover="### Result\nthe pull request " + URL, **extra):
    return 0, json.dumps({"subtype": "success", "is_error": False, "session_id": "sid-b", "total_cost_usd": 3.2,
                          "structured_output": {"outcome": outcome, "handover": handover}, **extra}), ""


class Build(Machine):
    """The tech-lead machine, with the pull request list and the task's recorded size."""

    def __init__(self, *a, pr="OPEN", size="", ahead=0, **kw):
        super().__init__(*a, claude=kw.pop("claude", built()), **kw)
        self.pr, self.size, self.ahead = pr, size, ahead

    def resolve(self, a):
        if a[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps([{"state": self.pr, "url": URL, "headRefOid": HEAD, "baseRefName": DEFAULT, "isDraft": True}]
                                 if self.pr else []), ""
        if a[:3] == ["git", "config", "--get"]:
            if a[3].endswith("factorySize"):
                return (0, self.size + "\n", "") if self.size else (1, "", "")
            return super().resolve(a)
        if a[:3] == ["git", "rev-list", "--count"]:
            return 0, f"{self.ahead}\n", ""
        return super().resolve(a)


class BuildCase(Case):
    def setUp(self):
        super().setUp()
        self.handover = self.spec.with_suffix(".handover.md")
        self.report = self.store / "deliverables" / SLUG / "report.md"

    def build(self, *flags, **machine):
        return self.build_with(Build, *flags, **machine)

    def build_with(self, machine_class, *flags, **machine):
        m = self.use(machine_class(self.root, **machine))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = factory.main(["build", SLUG, "--run", "--dispatcher", "dispatcher-repo", *flags])
        return code, out.getvalue(), err.getvalue(), m

    def only_record(self):
        [r] = self.records("techlead")
        return r


class LaunchTests(BuildCase):
    def test_launch_command(self):
        code, *_, m = self.build("--note", "The spec has a new section.", size="medium")
        [argv] = m.tech_lead_calls()
        role_text = (BIN.parent.parent / "roles" / "tech-lead-build.md").read_text()
        skill = (BIN.parent.parent / "skills" / "coding-taste" / "SKILL.md").read_text().partition("\n---\n")[2]
        role = f"{role_text}\n\n{skill}"
        self.assertIn("\n## Review words\n", role)
        self.assertNotIn("\n\n---", role.removeprefix(role_text))
        self.assertEqual(argv[:-1], [
            "claude", "-p", "--model", "opus", "--agents", json.dumps(factory.agent_definitions(factory.SETTINGS_DEFAULTS, self.root)),
            "--permission-mode", "auto", "--plugin-dir", str(BIN.parent.parent),
            "--worktree", SLUG, "--strict-mcp-config", "--settings", json.dumps(ISOLATED_SETTINGS), f"--tools={factory.LEAD_TOOLS}",
            "--disallowedTools", *factory.BUILD_DENIED,
            "--max-budget-usd", "15.00", "--output-format", "json", "--json-schema", json.dumps(factory.BUILD_SCHEMA),
            "--append-system-prompt", role])
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1])["required"], ["outcome", "handover"])
        self.assertEqual(argv[-1], (
            f"Task slug: {SLUG}. The spec is {self.spec}. The base branch is main, on remote origin. "
            f"The hand-over file is {self.handover}: this is the first run. The lead says: The spec has a new section. "
            "The task's stated size is medium."))
        i = m.calls.index(argv)
        self.assertEqual((m.cwds[i], m.timeouts[i], m.envs[i]["FACTORY_ROLE"], m.groups[i]),
                         (self.root, 5400, "tech-lead", True))
        self.assertIn(["git", "worktree", "unlock", f"{self.root}/.claude/worktrees/{SLUG}"], m.calls)

    def test_without_the_lean_start_the_run_still_gets_the_worktree_and_the_base_ref_in_one_settings(self):
        (self.store / "settings.toml").write_text('[sessions]\nlean_start = false\n')
        *_, m = self.build()
        argv = m.tech_lead_calls()[0]
        i = argv.index("--worktree")
        self.assertEqual((argv[i:i + 4], argv.count("--settings"), "--strict-mcp-config" in argv),
                         (["--worktree", SLUG, "--settings", json.dumps({**NO_ATTRIBUTION, **factory.BASE_REF})], 1, False))
        self.assertEqual(m.cwds[m.calls.index(argv)], self.root)

    def test_the_lock_the_harness_leaves_is_released_when_the_run_fails(self):
        code, *_, m = self.build(claude=(1, "", "boom"))
        self.assertIn(["git", "worktree", "unlock", f"{self.root}/.claude/worktrees/{SLUG}"], m.calls)

    def lock_after_run(self, lock_before):
        """The lock of a real worktree after `run_in_worktree`, whose session locks it as the harness does."""
        with tempfile.TemporaryDirectory() as tmp:
            git = lambda *a: subprocess.run(["git", "-C", tmp, *a], check=True, capture_output=True, text=True).stdout
            git("init", "-q", "-b", "main")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
            wt = os.path.join(os.path.realpath(tmp), ".claude", "worktrees", "w")
            git("worktree", "add", "-q", "-b", "w", wt)
            if lock_before:
                git("worktree", "lock", "--reason", "claude session w (pid 1)", wt)
            git_calls = []

            def session(argv, **kw):
                if not lock_before:
                    git("worktree", "lock", wt)
                return 0, "", ""
            real_run = factory.run

            def run(argv, **kw):
                git_calls.append(argv)
                return session(argv, **kw) if argv[0] == "claude" else real_run(argv, **kw)
            with mock.patch.object(factory, "run", side_effect=run):
                factory.run_in_worktree(factory.Repo(tmp), wt, ["claude", "-p"])
            locks = [l for l in git("worktree", "list", "--porcelain").splitlines() if l.startswith("locked")]
            return locks, [c for c in git_calls if c[:3] == ["git", "worktree", "unlock"]]

    def test_a_lock_that_was_there_before_the_run_is_left_alone_with_its_reason(self):
        locks, unlocks = self.lock_after_run(True)
        self.assertEqual(locks, ["locked claude session w (pid 1)"])
        self.assertEqual(unlocks, [])

    def test_the_lock_the_run_left_is_released(self):
        self.assertEqual(self.lock_after_run(False)[0], [])

    def test_a_failed_unlock_that_is_not_a_missing_lock_is_said(self):
        err = io.StringIO()
        repo = mock.Mock(root=self.root, worktree_path=lambda name: f"{self.root}/.claude/worktrees/{name}")
        repo.git.side_effect = [(0, porcelain((self.root, DEFAULT, False), (f"{self.root}/.claude/worktrees/{SLUG}", "b", False)), ""),
                                (128, "", "fatal: cannot unlock")]
        with mock.patch.object(factory, "run", return_value=(0, "", "")), redirect_stderr(err):
            factory.run_in_worktree(repo, f"{self.root}/.claude/worktrees/{SLUG}", ["claude", "-p"])
        self.assertIn("worktree unlock", err.getvalue())

    def test_a_worktree_outside_the_task_directory_keeps_the_old_start_and_says_it_is_not_isolated(self):
        repo, outside = mock.Mock(root=self.root, worktree_path=lambda name: f"{self.root}/.claude/worktrees/{name}"), f"{self.root}/elsewhere/{SLUG}"
        self.assertEqual(factory.isolated_name(repo, outside), None)
        with mock.patch.object(factory, "session_flags", return_value=["--strict-mcp-config"]):
            self.assertEqual(factory.worktree_flags(None, True), ["--strict-mcp-config"])
        self.assertEqual(factory.worktree_flags(None, False), ["--settings", json.dumps(NO_ATTRIBUTION)])
        err = io.StringIO()
        with mock.patch.object(factory, "run", return_value=(0, "", "")) as run, redirect_stderr(err):
            factory.run_in_worktree(repo, outside, ["claude", "-p"], timeout=1)
        run.assert_called_once_with(["claude", "-p"], cwd=outside, timeout=1)
        self.assertIn("not isolated to its worktree", err.getvalue())

    def test_the_prompt_sends_a_later_run_to_the_hand_over_file_and_says_nothing_of_a_missing_note_or_size(self):
        self.handover.write_text("## Run 1: question (x)\n")
        *_, m = self.build()
        self.assertEqual(m.tech_lead_calls()[0][-1], (
            f"Task slug: {SLUG}. The spec is {self.spec}. The base branch is main, on remote origin. "
            f"The hand-over file is {self.handover}: earlier runs: read it first."))

    def test_a_base_that_has_merged_is_not_named_as_the_base(self):
        with mock.patch.object(factory, "land_base", return_value=("worktree-other", True)):
            *_, m = self.build()
        self.assertIn("The base branch is main (the recorded base worktree-other has merged), on remote origin.",
                      m.tech_lead_calls()[0][-1])

    def test_the_model_and_the_permission_mode_come_from_the_settings(self):
        (self.store / "settings.toml").write_text(
            '[models]\ntech_lead = "sonnet"\ncoder = "haiku"\n[sessions]\nlead_permission_mode = "plan"\nlean_start = false\n')
        *_, m = self.build()
        argv = m.tech_lead_calls()[0]
        self.assertEqual((argv[3], argv[argv.index("--permission-mode") + 1], "--strict-mcp-config" in argv), ("sonnet", "plan", False))
        agents = json.loads(argv[argv.index("--agents") + 1])
        models = {**factory.SETTINGS_DEFAULTS["models"], "coder": "haiku", "tech_lead": "sonnet"}
        self.assertEqual(agents, factory.agent_definitions({**factory.SETTINGS_DEFAULTS, "models": models}, self.root))
        self.assertEqual((agents["factory:coder"]["model"], agents["factory:reviewer"]["model"]), ("haiku", "opus"))
        self.assertEqual(self.only_record()["model"], "sonnet")

    def test_the_command_starts_a_detached_run_and_returns(self):
        self.use(Build(self.root))
        out = io.StringIO()
        with mock.patch.object(factory, "spawn") as spawn, redirect_stdout(out):
            code = factory.main(["build", SLUG, "--note", "again", "--dispatcher", "dispatcher-repo"])
        self.assertEqual((code, out.getvalue()), (0, f"tech lead started for {SLUG}; you will get a TECH LEAD: line when it ends\n"))
        spawn.assert_called_once_with([sys.executable, str(BIN), "build", SLUG, "--run", "--note", "again", "--dispatcher",
                                       "dispatcher-repo"], self.root, self.store / "tech-lead.log")
        self.assertEqual((self.declared(), self.records("techlead")), ([], []))

    def test_without_a_note_or_a_dispatcher_the_run_gets_neither_flag(self):
        self.use(Build(self.root))
        with mock.patch.object(factory, "spawn") as spawn, redirect_stdout(io.StringIO()):
            factory.main(["build", SLUG])
        spawn.assert_called_once_with([sys.executable, str(BIN), "build", SLUG, "--run"], self.root, self.store / "tech-lead.log")

    def test_a_process_that_cannot_start_fails(self):
        self.use(Build(self.root))
        err = io.StringIO()
        with mock.patch.object(factory, "spawn", side_effect=OSError("no fork")), redirect_stderr(err):
            code = factory.main(["build", SLUG])
        self.assertEqual(code, 1)
        self.assertIn("no fork", err.getvalue())

    def test_a_worktree_with_no_git_fails_with_the_line_and_starts_nothing(self):
        wt = f"{self.root}/.claude/worktrees/{SLUG}"
        os.unlink(f"{wt}/.git")
        self.use(Build(self.root))
        with mock.patch.object(factory, "spawn") as spawn, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(factory.main(["build", SLUG]), 2)
        spawn.assert_not_called()
        self.assertEqual(err.getvalue(), f"factory: {SLUG}: the worktree {wt} has no .git, so git there answers from no repository: nothing ran. "
                         "Change nothing and hand back with this line.\n")
        self.assertEqual((self.declared(), self.records("techlead")), ([], []))

    def test_a_missing_spec_or_a_run_that_is_going_fails_in_both_halves_and_starts_nothing(self):
        for run in ([], ["--run"]):
            with self.subTest(run=run):
                self.spec.unlink()
                self.use(Build(self.root))
                with mock.patch.object(factory, "spawn") as spawn, redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(factory.main(["build", SLUG, *run]), 2)
                self.assertIn(f"the spec {self.spec} is missing", err.getvalue())
                self.spec.write_text(SPEC_TEXT)
                with factory.repo_lock(factory.Repo(), f"tech-lead-{SLUG}"), mock.patch.object(factory, "spawn") as spawn, \
                        redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(factory.main(["build", SLUG, *run]), 2)
                spawn.assert_not_called()
                self.assertIn(f"a tech lead run is already going for {SLUG}", err.getvalue())
                self.assertEqual((self.declared(), self.records("techlead"), self.machine.tech_lead_calls()), ([], [], []))


class OutcomeTests(BuildCase):
    def test_built_with_every_check_passing_tells_the_lead_and_declares_nothing_further(self):
        code, out, _, m = self.build()
        self.assertEqual(code, 0)
        self.assertEqual(m.lines, [f"TECH LEAD: {SLUG}: build run 1 built. Read {self.handover} (the last section)."])
        self.assertEqual(self.declared(), [("working", "tech-lead", None)])
        self.assertEqual(out, f"{SLUG} build run 1 built\n")
        self.assertEqual((m.pushes, m.comments), ([], []))
        r = self.only_record()
        self.assertEqual({k: r[k] for k in ("kind", "slug", "role", "model", "session", "pr", "evidence", "outcome", "reason",
                                            "note", "noted", "head_before", "head_after", "cost_usd")},
                         {"kind": "build", "slug": SLUG, "role": "tech-lead", "model": "opus", "session": "sid-b", "pr": None,
                          "evidence": "build", "outcome": "built", "reason": None, "note": None, "noted": False,
                          "head_before": OLD, "head_after": "a" * 40,
                          "cost_usd": 3.2})
        self.assertIsInstance(r["seconds"], int)

    def test_each_failed_check_makes_a_built_answer_stopped_and_names_itself(self):
        cases = {"the worktree is not clean": ({"dirty": True}, "OPEN"),
                 f"the worktree is not on worktree-{SLUG}": ({"branch": "elsewhere"}, "OPEN"),
                 "the spec changed": (self.edit_spec, "OPEN"),
                 f"worktree-{SLUG} has no open pull request and no report at {self.report}": ({}, None)}
        for reason, (after, pr) in cases.items():
            with self.subTest(reason=reason):
                self.spec.write_text(SPEC_TEXT)
                code, _, _, m = self.build(after=after, pr=pr)
                r = self.records("techlead")[-1]
                self.assertEqual((code, r["outcome"], r["reason"]), (1, "stopped", reason))
                self.assertIn(f"build run {len(self.records('techlead'))} stopped: {reason}. Read", m.lines[0])
                self.assertIn("## Run %d: stopped" % len(self.records("techlead")), self.handover.read_text())

    def test_a_built_run_records_the_number_of_the_pull_request_at_the_worktree_head_once(self):
        key = f"branch.worktree-{SLUG}.factoryPr"
        recorded = lambda m: [c for c in m.calls if c[:2] == ["git", "config"] and key in c and "--get" not in c]
        with self.subTest("at the head"):
            code, *_, m = self.build(after={"head": HEAD})
            self.assertEqual((code, recorded(m)), (0, [["git", "config", key, str(N)]]))
        with self.subTest("another value is not rewritten"):
            code, *_, m = self.build(after={"head": HEAD}, config={key: "99"})
            self.assertEqual((code, recorded(m)), (0, []))
        with self.subTest("a head that differs"):
            code, *_, m = self.build()
            self.assertEqual((code, recorded(m)), (0, []))

    def edit_spec(self):
        self.spec.write_text("another spec")
        return {}

    def test_a_pull_request_that_cannot_be_read_is_said_so_and_not_called_missing(self):
        class Unreadable(Build):
            def resolve(self, a):
                return (1, "", "boom") if a[:3] == ["gh", "pr", "list"] else super().resolve(a)
        code, *_ = self.build_with(Unreadable)
        self.assertEqual((code, self.only_record()["reason"]), (1, f"cannot check for a pull request on worktree-{SLUG}"))

    def test_a_closed_pull_request_is_not_an_open_one(self):
        self.write_report()
        code, *_ = self.build(pr="MERGED")
        self.assertEqual((code, self.only_record()["reason"]), (1, f"worktree-{SLUG} has no open pull request"))

    def write_report(self):
        self.report.parent.mkdir(parents=True)
        self.report.write_text("the report")

    def test_a_report_with_no_pull_request_and_no_commit_ahead_is_built(self):
        self.write_report()
        code, *_ = self.build(pr=None)
        self.assertEqual((code, self.only_record()["outcome"], self.only_record()["reason"]), (0, "built", None))

    def test_a_report_with_a_commit_ahead_and_no_pull_request_is_stopped(self):
        self.write_report()
        code, _, _, m = self.build(pr=None, ahead=1)
        self.assertEqual((code, self.only_record()["reason"]),
                         (1, f"worktree-{SLUG} has commits and no pull request: a report task commits nothing"))
        self.assertIn(["git", "rev-list", "--count", "origin/main..HEAD"], [c[-4:] for c in m.calls])

    def test_question_and_stopped_are_taken_as_they_are_without_the_checks(self):
        for outcome in ("question", "stopped"):
            with self.subTest(outcome=outcome):
                code, _, _, m = self.build(claude=built(outcome, "### Questions\nwhich one?"), after={"dirty": True}, pr=None)
                r = self.records("techlead")[-1]
                self.assertEqual((code, r["outcome"], r["reason"]), (1, outcome, None))
                self.assertTrue(m.lines[-1].startswith(f"TECH LEAD: {SLUG}: build run {len(self.records('techlead'))} {outcome}. Read"))

    def test_a_timeout_a_budget_stop_and_a_garbled_answer_each_say_what_happened(self):
        budget = (1, json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "session_id": "sid-b",
                                 "total_cost_usd": 15.02}), "")
        cases = [((124, "", "timed out"), "reached 90 minutes", None), (budget, "reached 15 USD", 15.02),
                 ((0, "not json", ""), "ended without an answer", None), ((1, "", "crash"), "ended without an answer", None),
                 ((0, json.dumps({"structured_output": {"outcome": "maybe", "handover": "x"}}), ""), "ended without an answer", None),
                 ((0, json.dumps({"structured_output": {"outcome": "built"}}), ""), "ended without an answer", None)]
        for claude, reason, cost in cases:
            with self.subTest(reason=reason, claude=claude[:2]):
                code, _, _, m = self.build(claude=claude)
                r = self.records("techlead")[-1]
                self.assertEqual((code, r["outcome"], r["reason"], r["cost_usd"]), (1, "stopped", reason, cost))
                self.assertIn(f"stopped: {reason}. Read", m.lines[-1])

    def test_a_failure_after_the_lock_is_a_stopped_record_a_section_and_a_line(self):
        with mock.patch.object(factory, "build_argv", side_effect=OSError("no role file")):
            code, out, _, m = self.build()
        self.assertEqual(code, 1)
        r = self.only_record()
        self.assertEqual((r["kind"], r["outcome"], r["pr"], r["evidence"]), ("build", "stopped", None, "build"))
        self.assertIn("the run failed: OSError: no role file", r["reason"])
        self.assertIn("build run 1 stopped: the run failed: OSError: no role file. Read", m.lines[0])
        self.assertRegex(self.handover.read_text().splitlines()[0], r"^## Run 1: stopped \(.*, unpriced USD, 0 min, head a{7}\)$")
        self.assertIn("Reason: the run failed: OSError: no role file\n\nNo hand-over was written.", self.handover.read_text())
        self.build(claude=built())
        self.assertIn("## Run 2: built", self.handover.read_text())

    def test_a_record_that_cannot_be_written_still_tells_the_lead(self):
        with mock.patch.object(factory, "build_argv", side_effect=OSError("x")), \
                mock.patch.object(factory, "log_tech_lead", side_effect=OSError("disk full")):
            code, out, _, m = self.build()
        self.assertEqual((code, len(m.lines)), (1, 1))
        self.assertIn("the record could not be written: disk full", out)


class HandOverFileTests(BuildCase):
    HEADER = r"^## Run %d: %s \(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ, %s USD, \d+ min, head %s\)$"

    def test_runs_are_numbered_across_runs_and_each_section_has_its_header_and_text(self):
        self.build(claude=built("question", "### Questions\nwhich one?"))
        self.build(claude=built("built", "### Result\ndone"))
        text = self.handover.read_text()
        first, second = re.split(r"(?m)^(?=## Run )", text)[1:]
        self.assertRegex(first.splitlines()[0], self.HEADER % (1, "question", "3.20", "a" * 7))
        self.assertRegex(second.splitlines()[0], self.HEADER % (2, "built", "3.20", "a" * 7))
        self.assertEqual(first.splitlines()[1:], ["", "### Questions", "which one?", ""])
        self.assertEqual(second.splitlines()[1:], ["", "### Result", "done", ""])
        self.assertEqual([self.records("techlead")[i]["outcome"] for i in (0, 1)], ["question", "built"])

    def test_a_reason_gets_its_own_line_and_an_empty_hand_over_the_fallback_text(self):
        self.build(claude=built("built", "  "), pr=None)
        first = self.handover.read_text().splitlines()
        self.assertRegex(first[0], self.HEADER % (1, "stopped", "3.20", "a" * 7))
        self.assertEqual(first[1:4], [f"Reason: worktree-{SLUG} has no open pull request and no report at {self.report}", "",
                                      "No hand-over was written."])

    def test_a_cost_that_is_missing_is_unpriced_in_the_header_and_null_in_the_record(self):
        rc, out, err = built()
        doc = json.loads(out)
        del doc["total_cost_usd"]
        self.build(claude=(rc, json.dumps(doc), err))
        self.assertRegex(self.handover.read_text().splitlines()[0], self.HEADER % (1, "built", "unpriced", "a" * 7))
        self.assertIsNone(self.only_record()["cost_usd"])

    def test_the_head_in_the_header_is_the_head_after_the_run(self):
        self.build(after={"head": HEAD})
        self.assertRegex(self.handover.read_text().splitlines()[0], self.HEADER % (1, "built", "3.20", "d" * 7))
        r = self.only_record()
        self.assertEqual((r["head_before"], r["head_after"]), ("a" * 40, HEAD))


class MergeRecordTests(unittest.TestCase):
    def test_a_merge_record_says_so_and_a_build_one_can_say_otherwise(self):
        records = []
        with mock.patch.object(factory, "log", lambda name, rec: records.append(rec)), \
                mock.patch.object(factory, "record", lambda *a: {}):
            repo = mock.Mock(root="/r")
            factory.log_tech_lead(repo, {"models": {"tech_lead": "opus"}}, "x", 7, "e", "resolved")
            factory.log_tech_lead(repo, {"models": {"tech_lead": "opus"}}, "x", None, "build", "built", kind="build")
        self.assertEqual([r["kind"] for r in records], ["merge", "build"])


if __name__ == "__main__":
    unittest.main()
