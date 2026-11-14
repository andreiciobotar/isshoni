import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_factory import BIN, LEAN_SETTINGS, FakeRun, factory, porcelain

SLUG, N, DEFAULT = "task1", 7, "main"
OLD, NEW = "a" * 40, "b" * 40
URL = f"https://x/pull/{N}"
CONFLICT = "it conflicts with main in a.txt"
RED = "red checks: test https://x/run/2"
COURIER_LINE = re.compile(r"nothing else: (.*)\. Then reply", re.S)
ANSWER = {"outcome": "resolved", "note": "Kept both paragraphs in docs/factory.md; make test passes; no reviewer."}


def said(outcome="resolved", note=ANSWER["note"], **extra):
    return 0, json.dumps({"subtype": "success", "is_error": False, "session_id": "sid-tl", "total_cost_usd": 1.42,
                          "structured_output": {"outcome": outcome, "note": note}, **extra}), ""


class Machine(FakeRun):
    """git, gh and claude for one task: `state` is the worktree, `after` what the tech lead's run changes in it."""

    def __init__(self, root, claude=None, after=None, comment=(0, "", ""), lead=True):
        super().__init__({})
        self.root, self.claude, self.after, self.comment = root, claude or said(), after or {}, comment
        self.state = {"head": OLD, "remote": OLD, "dirty": False, "branch": f"worktree-{SLUG}", "ancestor": True, "merged": True, "on_default": False}
        self.cwds, self.groups, self.comments, self.lines, self.prompts, self.lead = [], [], [], [], [], lead

    def __call__(self, argv, cwd=None, input=None, timeout=None, env=None, group=False):
        self.cwds.append(cwd)
        self.groups.append(group)
        return super().__call__(argv, cwd=cwd, input=input, timeout=timeout, env=env)

    def tech_lead_calls(self):
        return [c for c in self.calls if "--append-system-prompt" in c]

    def resolve(self, a):
        s = self.state
        if a[:3] == ["git", "worktree", "list"]:
            return 0, porcelain((self.root, DEFAULT, False), (f"{self.root}/.claude/worktrees/{SLUG}", s["branch"], False)), ""
        if a[:2] == ["git", "symbolic-ref"]:
            return 0, "refs/remotes/origin/main\n", ""
        if a[:3] == ["claude", "plugin", "list"]:
            return 0, json.dumps([{"id": "zeta@m"}, {"id": "factory@factory"}, {"id": "alpha@m"}]), ""
        if a[:2] == ["claude", "agents"]:
            live = {"sessionId": "sid-x", "name": SLUG, "cwd": f"{self.root}/.claude/worktrees/{SLUG}", "kind": "interactive"}
            return 0, json.dumps([live] if self.lead else []), ""
        if a[:2] == ["claude", "-p"] and "--append-system-prompt" in a:
            self.state.update(self.after() if callable(self.after) else self.after)
            return self.claude
        if a[:2] == ["claude", "-p"]:
            self.lines.append(COURIER_LINE.search(a[-1])[1])
            return 0, json.dumps({"result": "sent"}), ""
        if a[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"number": N, "url": URL, "headRefName": s["branch"], "headRefOid": OLD}), ""
        if a[:3] == ["gh", "pr", "comment"]:
            self.comments.append(a[a.index("--body") + 1])
            return self.comment
        if a[:3] == ["gh", "pr", "ready"]:
            return 0, "", ""
        if a[:3] == ["git", "rev-parse", "HEAD"]:
            return 0, s["head"] + "\n", ""
        if a[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, s["branch"] + "\n", ""
        if a[:2] == ["git", "rev-parse"]:
            return 0, s["remote"] + "\n", ""
        if a[:3] == ["git", "status", "--porcelain"]:
            return 0, " M a.txt\n" if s["dirty"] else "", ""
        if a[:3] == ["git", "merge-base", "--is-ancestor"] and a[3] == "HEAD":
            return (0 if s["on_default"] else 1), "", ""
        if a[:3] == ["git", "merge-base", "--is-ancestor"]:
            return (0 if s["merged" if a[3] == "origin/main" else "ancestor"] else 1), "", ""
        if a[:2] == ["git", "fetch"]:
            return 0, "", ""
        return 1, "", "unexpected call"


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name) + "/repo"
        os.makedirs(f"{self.root}/.claude/worktrees/{SLUG}")
        patcher = mock.patch.dict(os.environ, {"HOME": tmp.name, "CLAUDE_CODE_SESSION_ID": "sid-d"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Path(tmp.name, ".claude", "factory")
        self.spec = self.store / "specs" / "repo" / f"{SLUG}.md"
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text("the spec")
        self.machine = Machine(self.root)

    def use(self, machine):
        self.machine = machine
        patcher = mock.patch.object(factory, "run", machine)
        patcher.start()
        self.addCleanup(patcher.stop)
        return machine

    def go(self, evidence=CONFLICT, **machine):
        return self.go_with(Machine, evidence, **machine)

    def go_with(self, machine_class, evidence=CONFLICT, **machine):
        m = self.use(machine_class(self.root, **machine))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = factory.main(["tech-lead", SLUG, str(N), evidence, "--dispatcher", "dispatcher-repo"])
        return code, out.getvalue(), err.getvalue(), m

    def records(self, name):
        return list(factory.read_log(name))

    def declared(self):
        return [(r["state"], r["role"], r.get("deliverable")) for r in self.records("tasks")]

    def pr(self):
        return {"number": N, "url": URL, "headRefName": f"worktree-{SLUG}", "headRefOid": OLD}

    def send_back(self, evidence=CONFLICT, machine=None):
        self.use(machine or Machine(self.root))
        repo = factory.Repo()
        return factory.send_back(repo, "dispatcher-repo", self.pr(), SLUG, evidence), repo


class SendBackTests(Case):
    def test_with_a_spec_and_a_worktree_a_tech_lead_is_started_and_nobody_is_told(self):
        with mock.patch.object(factory, "spawn") as spawn:
            to, repo = self.send_back()
        self.assertEqual(to, "tech lead")
        spawn.assert_called_once_with([sys.executable, str(BIN), "tech-lead", SLUG, str(N), CONFLICT, "--dispatcher",
                                       "dispatcher-repo"], self.root, self.store / "tech-lead.log")
        self.assertEqual(self.machine.lines, [])
        self.assertEqual(self.declared(), [("working", "lander", None)])
        self.assertTrue(self.machine.ran("gh", "pr", "ready", "--undo"))
        self.assertEqual(self.records("techlead"), [])

    def test_without_a_dispatcher_name_the_run_gets_no_dispatcher_flag(self):
        self.use(Machine(self.root))
        with mock.patch.object(factory, "spawn") as spawn:
            factory.send_back(factory.Repo(), None, self.pr(), SLUG, CONFLICT)
        spawn.assert_called_once_with([sys.executable, str(BIN), "tech-lead", SLUG, str(N), CONFLICT], self.root,
                                      self.store / "tech-lead.log")

    def test_without_a_spec_the_lead_is_told_as_before_and_an_unstarted_record_says_why(self):
        self.spec.unlink()
        with mock.patch.object(factory, "spawn") as spawn:
            to, _ = self.send_back()
        spawn.assert_not_called()
        self.assertEqual(to, "lead")
        self.assertEqual(len(self.machine.lines), 1)
        self.assertTrue(self.machine.lines[0].startswith(f"LANDER: {SLUG}: #{N} went back to you: {CONFLICT}."))
        [r] = self.records("techlead")
        self.assertEqual((r["outcome"], r["pr"], r["slug"], r["role"], r["model"], r["cost_usd"], r["session"]),
                         ("unstarted", N, SLUG, "tech-lead", "opus", None, None))
        self.assertIn(str(self.spec), r["reason"])

    def test_a_process_that_cannot_start_falls_back_to_the_lead(self):
        with mock.patch.object(factory, "spawn", side_effect=OSError("no fork")):
            to, _ = self.send_back()
        self.assertEqual((to, len(self.machine.lines)), ("lead", 1))
        [r] = self.records("techlead")
        self.assertEqual(r["outcome"], "unstarted")
        self.assertIn("no fork", r["reason"])

    def test_a_run_that_is_going_falls_back_to_the_lead(self):
        self.use(Machine(self.root))
        repo = factory.Repo()
        with factory.lander_lock(repo, f"tech-lead-{SLUG}") as free, mock.patch.object(factory, "spawn") as spawn:
            self.assertTrue(free)
            to = factory.send_back(repo, "dispatcher-repo", self.pr(), SLUG, CONFLICT)
        spawn.assert_not_called()
        self.assertEqual(to, "lead")
        self.assertIn("already going", self.records("techlead")[0]["reason"])

    def test_red_checks_at_the_head_the_tech_lead_pushed_go_to_the_lead_but_a_conflict_there_does_not(self):
        factory.log_tech_lead(mock.Mock(root=self.root), SLUG, N, CONFLICT, "resolved", head_after=OLD)
        with mock.patch.object(factory, "spawn") as spawn:
            to, _ = self.send_back(RED)
            self.assertEqual(to, "lead")
            spawn.assert_not_called()
            self.assertEqual(self.records("techlead")[-1]["reason"], "red at the head the tech lead pushed")
            to, _ = self.send_back(CONFLICT)
            self.assertEqual(to, "tech lead")
            spawn.assert_called_once()

    def test_red_checks_at_another_head_start_a_tech_lead(self):
        factory.log_tech_lead(mock.Mock(root=self.root), SLUG, N, CONFLICT, "resolved", head_after=NEW)
        with mock.patch.object(factory, "spawn") as spawn:
            to, _ = self.send_back(RED)
        self.assertEqual(to, "tech lead")
        spawn.assert_called_once()


class RunTests(Case):
    def test_launch_command(self):
        code, *_, m = self.go()
        [argv] = m.tech_lead_calls()
        role = (BIN.parent.parent / "roles" / "tech-lead.md").read_text()
        self.assertEqual(argv[:-1], [
            "claude", "-p", "--model", "opus", "--permission-mode", "dontAsk", "--plugin-dir", str(BIN.parent.parent),
            "--strict-mcp-config", "--settings", json.dumps(LEAN_SETTINGS),
            "--tools=Bash,Read,Edit,Write,Grep,Glob,Agent,Skill,ToolSearch",
            "--allowedTools", "Read", "Edit", "Write", "Grep", "Glob", "Agent", "Skill", "ToolSearch", "Bash(git *)",
            "Bash(make *)", "Bash(gh pr view *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)", "Bash(gh run view *)",
            "--disallowedTools", "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push *--force*)",
            "Bash(git push * +*)", "Bash(git push *:*)", "Bash(git push * -f*)", "Bash(git push *--delete*)",
            "Bash(git push *--mirror*)", "Bash(git push *--all*)", "Bash(git rebase*)", "Bash(git commit --amend*)", "Bash(git worktree*)",
            "--max-budget-usd", "5.00", "--output-format", "json", "--json-schema", json.dumps(factory.TECH_LEAD_SCHEMA),
            "--append-system-prompt", role])
        self.assertEqual(argv[-1], (
            f"Task slug: {SLUG}. Pull request: #{N} {URL}. It came back from the merge queue: {CONFLICT}. "
            f"The spec is {self.spec}. The default branch is main, on remote origin. The pull request came back at {OLD}."
            "\n\nEarlier tech lead runs on this pull request:\nnone"))
        i = m.calls.index(argv)
        self.assertEqual((m.cwds[i], m.timeouts[i], m.envs[i]["FACTORY_ROLE"], m.groups[i]),
                         (f"{self.root}/.claude/worktrees/{SLUG}", 1800, "tech-lead", True))

    def test_the_model_comes_from_the_settings(self):
        (self.store / "settings.json").write_text('{"tech_lead_model": "sonnet"}')
        *_, m = self.go()
        self.assertEqual(m.tech_lead_calls()[0][3], "sonnet")
        self.assertEqual(self.records("techlead")[0]["model"], "sonnet")

    def test_a_setting_that_is_not_a_model_is_refused(self):
        (self.store / "settings.json").write_text('{"tech_lead_model": ""}')
        code, _, err, m = self.go()
        self.assertEqual(code, 2)
        self.assertIn("tech_lead_model must be a non-empty string", err)
        self.assertEqual(m.tech_lead_calls(), [])

    def test_resolved_comments_hands_over_and_tells_nobody(self):
        code, out, _, m = self.go(after={"head": NEW, "remote": NEW})
        self.assertEqual(code, 0)
        self.assertEqual(m.comments, [f"Tech lead: {ANSWER['note']}"])
        self.assertEqual(m.lines, [])
        self.assertEqual(self.declared(), [("working", "tech-lead", None), ("handed-over", "tech-lead", URL)])
        [r] = self.records("techlead")
        self.assertEqual({k: r[k] for k in ("repo", "slug", "role", "model", "session", "pr", "evidence", "outcome", "reason",
                                            "note", "noted", "head_before", "head_after", "cost_usd")},
                         {"repo": self.root, "slug": SLUG, "role": "tech-lead", "model": "opus", "session": "sid-tl", "pr": N,
                          "evidence": CONFLICT, "outcome": "resolved", "reason": None, "note": ANSWER["note"],
                          "noted": True, "head_before": OLD, "head_after": NEW, "cost_usd": 1.42})
        self.assertIsInstance(r["seconds"], int)
        self.assertEqual(out, f"#{N} tech lead resolved: {ANSWER['note']}\n")

    def test_a_failed_comment_is_recorded_and_does_not_stop_the_hand_over(self):
        code, *_ = self.go(after={"head": NEW, "remote": NEW}, comment=(1, "", "boom"))
        self.assertEqual((code, self.declared()[-1][0], self.records("techlead")[0]["noted"]), (0, "handed-over", False))

    def test_a_cost_that_is_missing_stays_null(self):
        rc, out, err = said()
        doc = json.loads(out)
        del doc["total_cost_usd"]
        self.go(claude=(rc, json.dumps(doc), err), after={"head": NEW, "remote": NEW})
        self.assertIsNone(self.records("techlead")[0]["cost_usd"])

    def edit_spec(self):
        self.spec.write_text("another spec")
        return {"head": NEW, "remote": NEW}

    def test_each_failed_check_stops_the_run_and_names_itself(self):
        cases = {
            "the worktree is not clean": {"head": NEW, "remote": NEW, "dirty": True},
            "the worktree is not on": {"head": NEW, "remote": NEW, "branch": "elsewhere"},
            "not an ancestor of HEAD": {"head": NEW, "remote": NEW, "ancestor": False},
            "origin/main is not merged in": {"head": NEW, "remote": NEW, "merged": False},
            "HEAD is not origin/worktree-task1": {"head": NEW},
            "the run pushed the task onto the default branch": {"head": NEW, "remote": NEW, "on_default": True},
            "the spec changed": self.edit_spec,
        }
        for reason, after in cases.items():
            with self.subTest(reason=reason):
                self.spec.write_text("the spec")
                code, out, _, m = self.go(after=after)
                self.assertEqual(code, 1)
                r = self.records("techlead")[-1]
                self.assertEqual(r["outcome"], "stopped")
                self.assertIn(reason, r["reason"])
                self.assertEqual(r["note"], ANSWER["note"])
                self.assertTrue(m.comments[0].startswith("Tech lead: stopped, the lead decides. "))
                [line] = m.lines
                self.assertTrue(line.startswith(f"TECH LEAD: {SLUG}: #{N} came back from the merge queue ({CONFLICT}) and is yours: "))
                self.assertIn(reason, line)
                self.assertNotIn("handed-over", [s for s, *_ in self.declared()][-1:])
                self.assertTrue(out.startswith(f"#{N} tech lead stopped: "))

    def test_a_failed_check_tells_the_lead_the_reason_and_the_note(self):
        *_, m = self.go(after={"head": NEW, "remote": NEW, "dirty": True})
        self.assertIn(f"is yours: the worktree is not clean. {ANSWER['note'].rstrip('.')}. The task is working", m.lines[0])

    def test_a_failing_pull_request_read_is_a_stopped_record_and_a_line_to_the_lead(self):
        class Unreadable(Machine):
            def resolve(self, a):
                return (1, "", "boom") if a[:3] == ["gh", "pr", "view"] else super().resolve(a)
        code, out, _, m = self.go_with(Unreadable)
        self.assertEqual((code, m.tech_lead_calls(), self.declared()), (1, [], []))
        [r] = self.records("techlead")
        self.assertEqual((r["outcome"], r["pr"], r["cost_usd"], r["note"]), ("stopped", N, None, None))
        self.assertTrue(r["reason"].startswith("the run failed: "))
        self.assertIn("boom", r["reason"])
        [line] = m.lines
        self.assertTrue(line.startswith(f"TECH LEAD: {SLUG}: #{N} came back from the merge queue ({CONFLICT}) and is yours: the run failed: "))
        self.assertTrue(out.startswith(f"#{N} tech lead stopped: the run failed: "))

    def test_a_failure_after_the_task_was_declared_working_still_tells_the_lead(self):
        with mock.patch.object(factory, "to_draft", side_effect=KeyError("headRefName")):
            code, _, _, m = self.go()
        self.assertEqual((code, self.declared(), len(m.lines)), (1, [("working", "tech-lead", None)], 1))
        self.assertIn("KeyError", self.records("techlead")[0]["reason"])

    def test_escalate_tells_the_lead_and_the_task_stays_working(self):
        note = "main routes every call through Identity; whose identity the new command uses is not in the spec."
        code, out, _, m = self.go(claude=said("escalate", note))
        self.assertEqual(code, 1)
        self.assertEqual(m.comments, [f"Tech lead: stopped, the lead decides. {note}"])
        self.assertEqual(m.lines, [
            f"TECH LEAD: {SLUG}: #{N} came back from the merge queue ({CONFLICT}) and is yours: {note.rstrip('.')}. The task is "
            "working (a draft); the worktree is at aaaaaaa, clean, pushed. Resolve it as your own change and hand over again."])
        self.assertEqual(self.declared(), [("working", "tech-lead", None)])
        [r] = self.records("techlead")
        self.assertEqual((r["outcome"], r["reason"], r["note"], r["noted"]), ("escalated", None, note, True))

    def test_a_timeout_a_budget_stop_and_a_garbled_answer_each_say_what_happened(self):
        budget = (1, json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "session_id": "sid-tl",
                                 "total_cost_usd": 5.02}), "")
        cases = [((124, "", "timed out"), "reached 30 minutes", None), (budget, "reached 5 USD", 5.02),
                 ((0, "not json", ""), "ended without an answer", None), ((1, "", "crash"), "ended without an answer", None),
                 ((0, json.dumps({"structured_output": {"outcome": "maybe", "note": "x"}}), ""), "ended without an answer", None)]
        for claude, reason, cost in cases:
            with self.subTest(reason=reason, claude=claude[:2]):
                code, _, _, m = self.go(claude=claude)
                r = self.records("techlead")[-1]
                self.assertEqual((code, r["outcome"], r["reason"], r["cost_usd"]), (1, "stopped", reason, cost))
                self.assertEqual(len(m.lines), 1)
                self.assertIn(f"is yours: {reason}.", m.lines[0])

    def test_earlier_notes_on_this_pull_request_are_in_the_prompt(self):
        repo = mock.Mock(root=self.root)
        factory.log_tech_lead(repo, SLUG, N, CONFLICT, "resolved", note="Kept both\nparagraphs.", head_after=NEW)
        factory.log_tech_lead(repo, SLUG, N, CONFLICT, "unstarted", reason="no spec")
        factory.log_tech_lead(repo, SLUG, N + 1, CONFLICT, "resolved", note="another pull request")
        factory.log_tech_lead(repo, "other-task", N, CONFLICT, "resolved", note="another task")
        *_, m = self.go()
        prompt = m.tech_lead_calls()[0][-1]
        [line] = prompt.split("pull request:\n")[1].splitlines()
        self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ resolved: Kept both paragraphs\.$")

    def test_a_second_run_while_one_is_going_fails_and_does_nothing(self):
        self.use(Machine(self.root))
        with factory.lander_lock(factory.Repo(), f"tech-lead-{SLUG}"):
            code, _, err, m = self.go()
        self.assertEqual(code, 2)
        self.assertIn(f"a tech lead run is already going for {SLUG}", err)
        self.assertEqual((m.tech_lead_calls(), self.declared(), self.records("techlead")), ([], [], []))

    def test_without_a_spec_nothing_is_declared(self):
        self.spec.unlink()
        code, _, err, m = self.go()
        self.assertEqual(code, 2)
        self.assertIn("is missing", err)
        self.assertEqual((m.tech_lead_calls(), self.declared()), ([], []))


class RunGroupTests(unittest.TestCase):
    def test_a_timeout_kills_what_the_child_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp, "pid")
            rc, *_ = factory.run(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout=1, group=True)
            self.assertEqual(rc, 124)
            pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            for _ in range(50):
                os.kill(pid, 0)
                time.sleep(0.1)


class TagsTests(unittest.TestCase):
    def role(self, env, payload=None):
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": f"/repo/.claude/worktrees/{SLUG}", **env}):
            if "FACTORY_ROLE" not in env:
                os.environ.pop("FACTORY_ROLE", None)
            return factory.tags({"session_id": "s", "model": "m", **(payload or {})})["role"]

    def test_factory_role_names_a_main_session_and_nothing_else(self):
        self.assertEqual(self.role({"FACTORY_ROLE": "tech-lead"}), "tech-lead")
        self.assertEqual(self.role({}), "lead")
        self.assertEqual(self.role({"FACTORY_ROLE": "tech-lead"}, {"agent_id": "a1", "agent_type": "factory:coder"}), "coder")
        self.assertEqual(self.role({"FACTORY_ROLE": "tech-lead"}, {"agent_id": "a1"}), "subagent")


if __name__ == "__main__":
    unittest.main()
