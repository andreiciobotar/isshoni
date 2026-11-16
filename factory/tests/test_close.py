"""A task declared `closed`: `factory close`, the site manager's decision, what `clean` keeps of it, and the wake-up of an
escalation that was acted on and is still true. Real git in a scratch repository; `claude`, `gh` and `lsof` answer from the test."""
import io
import json
import os
import re
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_factory import ROOT, factory, git_task_answers
from test_health import HOUR, NOW, Harness, decided, fired, iso
from test_lander import ScratchRepoBase

ONLY_HERE = "closed, but 1 commit(s) are in neither origin/main, a live task branch nor origin: "
NO_USER = "p1\ncself\nn/dev/null\n"  # lsof always lists its own process


def ago(seconds=3600):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


class CloseBase(ScratchRepoBase):
    """The worktree `t1` of ScratchRepoBase, a dead lead (no session) and an empty store under the scratch HOME."""

    def setUp(self):
        super().setUp()
        self.sessions, self.prs, self.lsof, self.calls, self.rm = [], [], NO_USER, [], (0, "", "")
        patcher = mock.patch.dict(os.environ, {**self.env, "CLAUDE_CODE_SESSION_ID": "sid-d"})
        patcher.start()
        self.addCleanup(patcher.stop)
        real = factory.run

        def answer(argv, **kw):
            self.calls.append(list(argv))
            if argv[:2] == ["claude", "rm"]:
                return self.rm
            if argv[0] == "claude":
                return (0, json.dumps(self.sessions), "") if self.sessions is not None else (1, "", "boom")
            if argv[0] == "lsof":
                return 0, self.lsof, ""
            if argv[0] == "gh":
                return (0, json.dumps(self.prs), "") if argv[:3] == ["gh", "pr", "list"] else (0, "", "")
            return real(argv, **{**kw, "cwd": kw.get("cwd") or str(self.root)})
        for patch in (mock.patch.object(factory, "run", answer), mock.patch.object(factory, "identity_exports", lambda: ({}, None))):
            patch.start()
            self.addCleanup(patch.stop)
        self.declare("t1", "working")

    def declare(self, slug, state, **extra):
        factory.log("tasks", {"ts": extra.pop("ts", ago()), "repo": str(self.root), "slug": slug, "role": "lead", "state": state, **extra})

    def go(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = factory.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def records(self, name):
        return [r for _, path in factory.log_files(name) for r in factory.read_file(path)]

    def states(self):
        return [(r["slug"], r["state"]) for r in self.records("tasks")]

    def event_names(self):
        return [e["event"] for e in self.records("events")]

    def add_task(self, slug, start="main"):
        path = self.root / ".claude" / "worktrees" / slug
        self.git("worktree", "add", "-q", "-b", f"worktree-{slug}", str(path), start)
        return path

    def own_commit(self, message="only here"):
        self.commit(self.wt, "c.txt", f"{message}\n", message)
        return self.git("rev-parse", "HEAD", cwd=self.wt)

    def branches(self):
        return self.git("branch", "--format=%(refname:short)").split()

    def refused(self, *argv):
        """`factory close` that is refused: exit 1, and nothing written or removed."""
        before = self.records("tasks"), self.records("events")
        code, out, err = self.go(*argv)
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual((self.records("tasks"), self.records("events")), before)
        self.assertTrue(self.wt.is_dir())
        self.assertIn("worktree-t1", self.branches())
        return err

    def vanished_lead(self, session=True):
        """A report task whose lead is gone: declared working, no pull request, clean, level with nothing of its own while main
        moved on, the worktree locked as Claude Code locks it, and (unless `session` is false) its finished session listed."""
        self.main_moves("m1.txt", "1\n")
        self.main_moves("m2.txt", "2\n")
        self.git("worktree", "lock", "--reason", "claude session x (pid 1)", str(self.wt))
        self.sessions = [{"id": "sx", "name": "t1", "cwd": str(self.wt), "kind": "background", "state": "done"}] if session else []

    def removal_commands(self):
        return [c for c in self.calls if c[:2] == ["claude", "rm"] or c[:3] == ["git", "worktree", "remove"] or "update-ref" in c]

    def pr(self, state, branch="worktree-t1", head=None, base="main"):
        self.prs.append({"state": state, "url": f"https://x/pull/{len(self.prs) + 1}", "headRefOid": head or self.git("rev-parse", branch),
                         "baseRefName": base, "headRefName": branch, "isDraft": False})


class CloseTests(CloseBase):
    def test_a_dead_superseded_task_is_closed_and_removed(self):
        code, out, _ = self.go("close", "t1", "--reason", "replaced", "--superseded-by", "t1-finish")
        self.assertEqual((code, out), (0, "closed t1: replaced\nremoved t1\n"))
        [closed] = [r for r in self.records("tasks") if r["state"] == "closed"]
        self.assertEqual((closed["slug"], closed["role"], closed["reason"], closed["superseded_by"]), ("t1", "dispatcher", "replaced", "t1-finish"))
        [event] = [e for e in self.records("events") if e["event"] == "TaskClosed"]
        self.assertEqual((event["slug"], event["by"], event["reason"], event["superseded_by"]), ("t1", "dispatcher", "replaced", "t1-finish"))
        self.assertEqual(self.event_names(), ["TaskClosed", "TaskRemoved"])
        self.assertFalse(self.wt.exists())
        self.assertNotIn("worktree-t1", self.branches())

    def test_a_closed_task_that_is_kept_holds_no_slot(self):
        self.own_commit()
        self.assertEqual(json.loads(self.go("status", "--json")[1])["in_motion"], 1)
        self.go("close", "t1", "--reason", "abandoned")
        doc = json.loads(self.go("status", "--json")[1])
        self.assertEqual([(r["slug"], r["state"], r["phase"]) for r in doc["rows"]], [("t1", "closed", "settled")])
        self.assertEqual((doc["in_motion"], doc["settled"]), (0, 1))

    def test_a_commit_in_no_other_branch_and_not_on_the_remote_keeps_the_task_and_names_it(self):
        sha = self.own_commit("the only copy")
        code, out, _ = self.go("close", "t1", "--reason", "abandoned")
        self.assertEqual((code, out), (0, f"closed t1: abandoned\nkept t1: {ONLY_HERE}{sha[:7]} the only copy\n"))
        self.assertEqual(self.states()[-1], ("t1", "closed"))
        self.assertEqual(self.event_names(), ["TaskClosed"])
        self.assertTrue(self.wt.is_dir())
        self.assertEqual(self.git("rev-parse", "worktree-t1"), sha)

    def test_more_than_five_lost_commits_name_five(self):
        for i in range(7):
            self.own_commit(f"c{i}")
        _, out, _ = self.go("close", "t1", "--reason", "abandoned")
        self.assertIn("7 commit(s) are in neither", out)
        self.assertEqual(len(re.findall(r"[0-9a-f]{7} c\d", out)), 5)
        self.assertTrue(out.rstrip().endswith("; and 2 more"))

    def test_the_same_commit_pushed_to_the_remote_lets_the_task_go(self):
        self.own_commit()
        self.git("push", "-q", "origin", "worktree-t1", cwd=self.wt)
        code, out, _ = self.go("close", "t1", "--reason", "abandoned")
        self.assertEqual((code, out), (0, "closed t1: abandoned\nremoved t1\n"))
        self.assertFalse(self.wt.exists())
        self.assertNotIn("worktree-t1", self.branches())

    def test_a_stale_remote_tracking_ref_is_not_the_remote(self):
        sha = self.own_commit()
        self.git("push", "-q", "-u", "origin", "worktree-t1", cwd=self.wt)
        self.git("update-ref", "-d", "refs/heads/worktree-t1", cwd=self.remote)  # deleted there, not pruned here
        self.assertEqual(self.git("rev-parse", "origin/worktree-t1"), sha)
        _, out, _ = self.go("close", "t1", "--reason", "abandoned")
        self.assertIn(f"kept t1: {ONLY_HERE}{sha[:7]} only here", out)
        self.assertTrue(self.wt.is_dir())

    def test_a_remote_that_cannot_be_asked_keeps_the_task(self):
        self.own_commit()
        self.git("remote", "set-url", "origin", str(self.root.parent / "nowhere.git"))
        _, out, _ = self.go("close", "t1", "--reason", "abandoned")
        self.assertIn("kept t1: closed, but cannot ask origin which commits it holds: ", out)
        self.assertTrue(self.wt.is_dir())

    def test_a_commit_in_another_working_task_branch_lets_the_task_go(self):
        self.own_commit()
        other = self.add_task("t2", "worktree-t1")
        self.declare("t2", "working")
        code, out, _ = self.go("close", "t1", "--reason", "taken over by t2")
        self.assertEqual((code, out), (0, "closed t1: taken over by t2\nremoved t1\n"))
        self.assertNotIn("worktree-t1", self.branches())
        self.assertTrue(other.is_dir())
        self.assertEqual(self.git("log", "-1", "--format=%s", "worktree-t2"), "only here")

    def test_a_commit_only_in_another_closed_task_branch_keeps_the_task(self):
        sha = self.own_commit()
        self.add_task("t2", "worktree-t1")
        self.declare("t2", "closed", reason="gone")
        _, out, _ = self.go("close", "t1", "--reason", "abandoned")
        self.assertIn(f"kept t1: {ONLY_HERE}{sha[:7]} only here", out)
        self.assertTrue(self.wt.is_dir())

    def test_an_untracked_file_keeps_the_task(self):
        self.write(self.wt, "notes.txt", "mine\n")
        _, out, _ = self.go("close", "t1", "--reason", "abandoned")
        self.assertEqual(out, "closed t1: abandoned\nkept t1: uncommitted changes\n")
        self.assertTrue((self.wt / "notes.txt").exists())

    def test_an_open_pull_request_keeps_the_task(self):
        self.pr("OPEN")
        _, out, _ = self.go("close", "t1", "--reason", "abandoned")
        self.assertEqual(out, "closed t1: abandoned\nkept t1: closed, but its pull request is open\n")
        self.assertTrue(self.wt.is_dir())

    def test_a_process_in_the_worktree_keeps_a_task_that_was_closed_before(self):
        self.declare("t1", "closed", reason="gone", ts=ago(1800))
        self.lsof = f"p77\ncvim\nn{self.wt}/a.txt\n"
        _, out, _ = self.go("clean")
        self.assertIn("kept t1: closed, but pid 77 (vim) uses it", out)
        self.assertTrue(self.wt.is_dir())

    def test_a_closed_task_is_kept_when_lsof_cannot_say(self):
        self.declare("t1", "closed", reason="gone", ts=ago(1800))
        self.lsof = ""
        _, out, _ = self.go("clean")
        self.assertIn("kept t1: closed, but cannot list open files: lsof printed nothing", out)

    def test_closing_a_closed_task_writes_no_second_record_and_cleans_it(self):
        self.declare("t1", "closed", reason="gone", ts=ago(1800))
        code, out, _ = self.go("close", "t1", "--reason", "again")
        self.assertEqual((code, out), (0, "closed t1: again\nremoved t1\n"))
        self.assertEqual(self.states(), [("t1", "working"), ("t1", "closed")])
        self.assertEqual(self.event_names(), ["TaskRemoved"])
        self.assertFalse(self.wt.exists())


class StackedTests(CloseBase):
    """t1 is merged into main and t2 is open, stacked on it: cleaning t1 would remove t2's base."""

    def setUp(self):
        super().setUp()
        self.pr("MERGED")
        self.add_task("t2")
        self.git("config", "branch.worktree-t2.factoryBase", "worktree-t1")
        self.declare("t2", "working")

    def test_a_merged_task_with_a_task_stacked_on_it_is_kept(self):
        _, out, _ = self.go("clean")
        self.assertIn("kept t1: t2 is stacked on it", out)
        self.assertTrue(self.wt.is_dir())

    def test_a_closed_task_with_a_task_stacked_on_it_is_kept(self):
        code, out, _ = self.go("close", "t1", "--reason", "replaced by t2")
        self.assertEqual((code, out), (0, "closed t1: replaced by t2\nkept t1: t2 is stacked on it\n"))
        self.assertTrue(self.wt.is_dir())
        self.assertIn("worktree-t1", self.branches())

    def test_a_merged_task_with_a_session_is_kept_while_its_stack_is_open(self):
        self.sessions = [{"id": "sx", "name": "t1", "cwd": str(self.wt), "kind": "background", "state": "done"}]
        self.pr("OPEN", branch="worktree-t2")
        _, out, _ = self.go("clean")
        self.assertIn("kept t1: t2 is stacked on it", out)
        self.assertNotIn(["claude", "rm", "sx"], self.calls)
        self.assertTrue(self.wt.is_dir())

    def test_a_merged_task_whose_stack_merged_too_is_removed_through_its_session_without_a_close(self):
        self.sessions = [{"id": "sx", "name": "t1", "cwd": str(self.wt), "kind": "background", "state": "done"}]
        self.pr("MERGED", branch="worktree-t2")
        code, out, _ = self.go("clean")
        self.assertEqual((code, out), (0, "removed t1\nremoved t2\n"))
        self.assertEqual([c for c in self.calls if c[:2] == ["claude", "rm"]], [["claude", "rm", "sx"]])
        self.assertEqual(self.states(), [("t1", "working"), ("t2", "working")])
        self.assertEqual(self.event_names(), ["TaskRemoved", "TaskRemoved"])


class VanishedLeadTests(CloseBase):
    """A report task whose lead is gone, its worktree locked: the dispatcher closes it."""

    def test_a_listed_session_removes_it_with_one_claude_rm_through_the_lock(self):
        self.vanished_lead()
        code, out, _ = self.go("close", "t1", "--reason", "report used")
        self.assertEqual((code, out), (0, "closed t1: report used\nremoved t1\n"))
        self.assertEqual(self.states(), [("t1", "working"), ("t1", "closed")])
        self.assertEqual(self.event_names(), ["TaskClosed", "TaskRemoved"])
        self.assertEqual(self.removal_commands(), [["claude", "rm", "sx"]])

    def test_a_claude_rm_that_fails_keeps_the_closed_task_and_its_worktree(self):
        self.vanished_lead()
        self.rm = (1, "", "no such session")
        code, out, err = self.go("close", "t1", "--reason", "report used")
        self.assertEqual((code, out), (1, "closed t1: report used\n"))
        self.assertEqual(err, "failed t1 (branch worktree-t1): claude rm sx: no such session\n")
        self.assertEqual(self.states(), [("t1", "working"), ("t1", "closed")])
        self.assertEqual(self.event_names(), ["TaskClosed"])
        self.assertEqual(self.removal_commands(), [["claude", "rm", "sx"]])
        self.assertTrue(self.wt.is_dir())
        self.assertIn("worktree-t1", self.branches())

    def test_a_locked_worktree_with_no_session_is_kept_until_unlocked(self):
        self.vanished_lead(session=False)
        code, out, _ = self.go("close", "t1", "--reason", "report used")
        why = f"its worktree is locked (claude session x (pid 1)) and no session is left to remove it through; if nothing uses it, run: git worktree unlock {self.wt}"
        self.assertEqual((code, out), (0, f"closed t1: report used\nkept t1: {why}\n"))
        self.assertEqual(self.event_names(), ["TaskClosed"])
        self.assertEqual(self.removal_commands(), [])
        self.assertTrue(self.wt.is_dir())
        self.assertIn("worktree-t1", self.branches())
        self.git("worktree", "unlock", str(self.wt))
        code, out, _ = self.go("close", "t1", "--reason", "report used")
        self.assertEqual((code, out), (0, "closed t1: report used\nremoved t1\n"))
        self.assertEqual(self.states(), [("t1", "working"), ("t1", "closed")])
        self.assertEqual(self.event_names(), ["TaskClosed", "TaskRemoved"])
        self.assertFalse(self.wt.exists())
        self.assertNotIn("worktree-t1", self.branches())


class RefusalTests(CloseBase):
    def test_a_task_with_an_unfinished_session_is_live(self):
        self.sessions = [{"id": "s1", "name": "t1", "cwd": str(self.wt), "kind": "background", "status": "busy", "state": "working"}]
        self.assertIn("background session s1 is not finished", self.refused("close", "t1", "--reason", "dead"))

    def test_an_idle_lead_with_a_process_is_live(self):
        self.sessions = [{"id": "s1", "name": "t1", "cwd": str(self.wt), "kind": "background", "status": "idle", "state": "done", "pid": 4242}]
        self.assertIn("session s1 has a process (pid 4242), so its lead is live", self.refused("close", "t1", "--reason", "dead"))

    def test_an_interactive_session_is_live(self):
        self.sessions = [{"id": "s1", "cwd": str(self.wt), "kind": "interactive"}]
        self.assertIn("an interactive session is open in it", self.refused("close", "t1", "--reason", "dead"))

    def test_a_held_build_lock_is_live(self):
        with factory.repo_lock(factory.Repo(str(self.root)), "tech-lead-t1") as free:
            self.assertTrue(free)
            err = self.refused("close", "t1", "--reason", "dead")
        self.assertIn("a build or tech lead run holds its lock", err)

    def test_a_process_in_the_worktree_is_live(self):
        self.lsof = f"p77\ncvim\nn{self.wt}/a.txt\n"
        self.assertIn("pid 77 (vim) uses it", self.refused("close", "t1", "--reason", "dead"))

    def test_an_lsof_that_cannot_say_is_live(self):
        self.lsof = ""
        self.assertIn("cannot list open files", self.refused("close", "t1", "--reason", "dead"))

    def test_sessions_that_cannot_be_listed_are_live(self):
        self.sessions = None
        self.assertIn("cannot list sessions", self.refused("close", "t1", "--reason", "dead"))

    def test_an_unplaced_session_is_live(self):
        self.sessions = [{"id": "s9", "kind": "background", "status": "busy", "state": "working"}]
        self.assertIn("session s9 has no working directory", self.refused("close", "t1", "--reason", "dead"))

    def test_a_handed_over_or_settled_task_is_not_closed(self):
        for age, state in enumerate(("handed-over", "acknowledged", "dismissed")):
            with self.subTest(state):
                self.declare("t1", state, ts=ago(1800 - 60 * age), deliverable="https://x/pull/1")
                self.assertIn(f"declared {state}, it finished", self.refused("close", "t1", "--reason", "dead"))

    def test_the_rest_of_the_refusals(self):
        self.assertIn("a reason is required", self.refused("close", "t1", "--reason", "  "))
        self.assertIn("cannot be superseded by itself", self.refused("close", "t1", "--reason", "dead", "--superseded-by", "t1"))
        self.assertIn("close: nowhere: not a task worktree", self.refused("close", "nowhere", "--reason", "dead"))


class StateTests(CloseBase):
    def test_closed_is_not_a_state_a_lead_or_the_dispatcher_can_declare(self):
        for argv in (("state", "closed"), ("ack", "t1", "closed")):
            with self.subTest(argv), redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
                factory.main(list(argv))
            self.assertEqual(stop.exception.code, 2)
            self.assertIn("invalid choice: 'closed'", err.getvalue())
        self.assertEqual(self.states(), [("t1", "working")])

    def test_a_lead_that_comes_back_reopens_a_closed_task(self):
        self.own_commit()
        self.go("close", "t1", "--reason", "abandoned")
        self.assertEqual(self.states()[-1], ("t1", "closed"))
        with mock.patch.object(os, "getcwd", return_value=str(self.wt)):
            self.assertEqual(self.go("state", "working")[0], 0)
        self.assertEqual(self.states()[-1], ("t1", "working"))
        self.assertEqual(json.loads(self.go("status", "--json")[1])["rows"][0]["phase"], "motion")


class SiteManagerTests(CloseBase):
    """`record_decisions` and `record_call` with the dispatcher's lines recorded instead of sent."""

    def setUp(self):
        super().setUp()
        self.told = []
        patcher = mock.patch.object(factory, "forward", lambda repo, fallback, slug, head, said, lead=True: self.told.append((slug, head, said, lead)) or "dispatcher")
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def repo(self):
        return factory.Repo(str(self.root))  # worktrees come and go within a test

    @property
    def p(self):
        return {"repo": self.repo, "dispatcher": "dispatcher-repo"}

    def firing(self, criterion="ended-undeclared", slug="t1"):
        return factory.fire(self.repo, {"criterion": criterion, "slug": slug, "subject": slug, "detail": "d"})

    def decide(self, f, **d):
        result = {"sent": True, "decisions": [{"id": f["id"], "decision": "closed", "reason": "replaced", "looked": [], **d}]}
        with redirect_stdout(io.StringIO()) as out:
            logged = factory.record_decisions(self.p, [f], result, "run1")
        return logged, out.getvalue()

    def folded(self):
        return factory.fold(self.records("firings"))[0]

    def test_a_closed_decision_closes_removes_and_tells_the_dispatcher_nothing_is_needed(self):
        f = self.firing()
        logged, _ = self.decide(f, superseded_by="t1-finish")
        self.assertEqual(logged, 1)
        [rec] = [r for r in self.records("firings") if r.get("decision") == "closed"]
        self.assertEqual((rec["by"], rec["superseded_by"]), ("site-manager", "t1-finish"))
        [closed] = [r for r in self.records("tasks") if r["state"] == "closed"]
        self.assertEqual((closed["role"], closed["reason"], closed["superseded_by"]), ("site-manager", "replaced", "t1-finish"))
        self.assertEqual(self.event_names(), ["TaskClosed", "TaskRemoved"])
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.told, [("t1", "SITE MANAGER", "t1: closed (replaced; superseded by t1-finish) and removed, no answer needed", False)])
        self.assertIn({"ts": mock.ANY, "repo": str(self.root), "id": f["id"], "told": "dispatcher"}, self.records("firings"))

    def test_a_close_that_keeps_the_worktree_is_an_escalation_naming_what_would_be_lost(self):
        sha = self.own_commit("the only copy")
        f = self.firing()
        self.assertEqual(self.decide(f)[0], 1)
        self.assertTrue(self.wt.is_dir())
        self.assertEqual(self.told, [("t1", "ESCALATION", f"{f['id']} ended-undeclared t1: closed (replaced); kept: {ONLY_HERE}{sha[:7]} the only copy", False)])
        self.assertEqual(self.event_names(), ["TaskClosed"])

    def test_a_removal_that_fails_is_an_escalation_pointing_at_the_watch_log(self):
        self.vanished_lead()
        self.rm = (1, "", "no such session")
        f = self.firing()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.decide(f)[0], 1)
        self.assertEqual(self.states()[-1], ("t1", "closed"))
        self.assertEqual(self.event_names(), ["TaskClosed"])
        self.assertTrue(self.wt.is_dir())
        self.assertEqual(self.told, [("t1", "ESCALATION", f"{f['id']} ended-undeclared t1: closed (replaced); kept: its removal failed, see the watch log", False)])

    def test_a_call_of_his_own_closes_on_any_entry(self):
        call = {"entry": "merged-not-cleaned", "slug": "t1", "detail": "head is in main", "decision": "closed", "reason": "replaced",
                "superseded_by": "t1-finish", "looked": []}
        look = {"tasks": [{"slug": "t1"}]}
        with redirect_stdout(io.StringIO()) as out:
            logged = factory.record_decisions(self.p, [], {"sent": True, "decisions": [call]}, "run1", look)
        self.assertEqual(logged, 1)
        [f] = self.folded()
        self.assertEqual((f["criterion"], f["decision"], f["by"], f["cleared"]), ("merged-not-cleaned", "closed", "site-manager", True))
        self.assertIn(f"{f['id']}  merged-not-cleaned  t1  closed", out.getvalue())
        self.assertEqual(self.states()[-1], ("t1", "closed"))
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.told, [("t1", "SITE MANAGER", "t1: closed (replaced; superseded by t1-finish) and removed, no answer needed", False)])

    def test_a_call_of_his_own_that_is_kept_says_so_under_its_entry(self):
        sha = self.own_commit("the only copy")
        call = {"entry": "merged-not-cleaned", "slug": "t1", "detail": "x", "decision": "closed", "reason": "replaced", "looked": []}
        with redirect_stdout(io.StringIO()):
            factory.record_decisions(self.p, [], {"sent": True, "decisions": [call]}, "run1", {"tasks": [{"slug": "t1"}]})
        [f] = self.folded()
        self.assertEqual(self.told, [("t1", "ESCALATION", f"{f['id']} merged-not-cleaned t1: closed (replaced); kept: {ONLY_HERE}{sha[:7]} the only copy", False)])

    def test_a_failure_after_the_declare_is_a_kept_close_the_dispatcher_hears_of(self):
        f = self.firing()
        with mock.patch.object(factory, "list_sessions", side_effect=[[], None]):
            logged, out = self.decide(f)
        why = "cannot list sessions again before removing t1; nothing further removed"
        self.assertEqual(logged, 1)
        self.assertIn(f"kept t1: {why}", out)
        self.assertNotIn("close skipped", out)
        self.assertEqual([r["decision"] for r in self.records("firings") if "decision" in r], ["closed"])
        self.assertEqual(self.states()[-1], ("t1", "closed"))
        self.assertEqual(self.told, [("t1", "ESCALATION", f"{f['id']} ended-undeclared t1: closed (replaced); kept: {why}", False)])

    def test_factory_close_prints_a_failure_after_the_declare_as_kept_and_exits_0(self):
        with mock.patch.object(factory, "list_sessions", side_effect=[[], None]):
            code, out, _ = self.go("close", "t1", "--reason", "dead")
        self.assertEqual((code, out), (0, "closed t1: dead\nkept t1: cannot list sessions again before removing t1; nothing further removed\n"))
        self.assertEqual(self.states()[-1], ("t1", "closed"))

    def test_superseded_by_is_logged_on_a_closed_decision_only(self):
        f = self.firing()
        self.decide(f, decision="suppressed", superseded_by="t1-finish")
        call = {"entry": "merged-not-cleaned", "slug": "t1", "detail": "x", "decision": "suppressed", "reason": "r", "superseded_by": "t1-finish", "looked": []}
        with redirect_stdout(io.StringIO()):
            factory.record_decisions(self.p, [], {"sent": True, "decisions": [call]}, "run1", {"tasks": [{"slug": "t1"}]})
        decisions = [r for r in self.records("firings") if "decision" in r]
        self.assertEqual(len(decisions), 2)
        self.assertTrue(all("superseded_by" not in r for r in decisions))

    def test_a_refused_close_is_printed_and_logs_no_decision(self):
        self.sessions = [{"id": "s1", "name": "t1", "cwd": str(self.wt), "kind": "background", "status": "busy", "state": "working"}]
        f = self.firing()
        logged, out = self.decide(f)
        self.assertEqual(logged, 0)
        self.assertIn("close skipped: close: t1: live: background session s1 is not finished", out)
        self.assertEqual([r for r in self.records("firings") if "decision" in r], [])
        call = {"entry": "merged-not-cleaned", "slug": "t1", "detail": "x", "decision": "closed", "reason": "r", "looked": []}
        with redirect_stdout(io.StringIO()) as out:
            logged = factory.record_decisions(self.p, [], {"sent": True, "decisions": [call]}, "run1", {"tasks": [{"slug": "t1"}]})
        self.assertEqual(logged, 0)
        self.assertEqual((self.states(), self.told, self.event_names()), ([("t1", "working")], [], []))
        self.assertTrue(self.wt.is_dir())

    def test_a_firing_of_another_criterion_cannot_be_answered_with_a_close(self):
        f = self.firing("collision")
        self.assertEqual(self.decide(f)[0], 0)
        self.assertEqual((self.states(), self.told), ([("t1", "working")], []))

    def test_stood_and_reopened_are_settled_on_the_full_pass_and_shown(self):
        self.own_commit()
        self.decide(self.firing())  # kept: the worktree is still there
        factory.settle_closed(self.p)
        self.assertEqual(self.folded()[0]["outcome"], None)
        self.git("push", "-q", "origin", "worktree-t1", cwd=self.wt)
        self.git("worktree", "remove", str(self.wt))
        factory.settle_closed(self.p)
        self.assertEqual(self.folded()[0]["outcome"], "stood")
        self.add_task("t2")
        self.declare("t2", "working", ts=ago(7200))
        self.decide(self.firing(slug="t2"))
        self.declare("t2", "working", ts=iso(time.time() + 60))
        factory.settle_closed(self.p)
        self.assertEqual([f["outcome"] for f in self.folded() if f["decision"] == "closed"], ["stood", "reopened"])
        _, out, _ = self.go("firings")
        self.assertIn("  stood  ", out)
        self.assertIn("  reopened  ", out)
        self.assertIn("closed 2 (stood 1, reopened 1, no outcome 0)", out)
        summary = json.loads(self.go("firings", "--json")[1])["summary"]
        self.assertEqual((summary["closed"], summary["stood"], summary["reopened"]), (2, 1, 1))
        self.assertEqual(summary["entries"]["ended-undeclared"]["closed"], 2)


class WakingTests(Harness):
    """An `ended-undeclared` firing escalated to the dispatcher stays open; one that was acted on and is still true wakes him again."""

    def escalated(self, minutes, outcome=None, to="dispatcher"):
        records = [fired("aaa"), decided("aaa", "escalated", ts=NOW - minutes * 60, reason="start a replacement") | {"to": to}]
        self.seed("firings", records + ([{"ts": iso(NOW - 60), "repo": ROOT, "id": "aaa", "outcome": outcome}] if outcome else []))

    def test_an_escalation_acted_on_and_still_true_is_fired_again_with_the_marker(self):
        self.escalated(121, "acted")
        self.tick()
        [argv] = self.claude_runs()
        records = self.records()
        self.assertEqual([r.get("cleared") for r in records[3:4]], [True])
        new = records[-1]
        self.assertEqual(records[3]["id"], "aaa")
        self.assertTrue(new["detail"].endswith(f"; escalated at {iso(NOW - 121 * 60)}, acted on, still true: start a replacement"), new["detail"])
        self.assertNotEqual(new["id"], "aaa")
        [sent] = json.loads(argv[-1].partition("\n\n")[2])["firings"]
        self.assertEqual(sent["id"], new["id"])

    def test_an_escalation_acted_on_less_than_the_resurface_time_ago_stays_as_it_is(self):
        self.escalated(119, "acted")
        self.tick()
        self.assertEqual((self.claude_runs(), len(self.records())), ([], 3))

    def test_an_ignored_escalation_stays_as_it_is(self):
        self.escalated(300, "ignored")
        self.tick()
        self.assertEqual((self.claude_runs(), len(self.records())), ([], 3))

    def test_an_escalation_with_no_outcome_is_fired_again_with_the_marker(self):
        self.escalated(121)
        self.tick()
        [argv] = self.claude_runs()
        records = self.records()
        self.assertEqual([r.get("cleared") for r in records[2:3]], [True])
        new = records[-1]
        self.assertTrue(new["detail"].endswith(f"; escalated at {iso(NOW - 121 * 60)}, no outcome, still true: start a replacement"), new["detail"])
        self.assertEqual(json.loads(argv[-1].partition("\n\n")[2])["firings"][0]["id"], new["id"])

    def test_an_escalation_with_no_outcome_less_than_the_resurface_time_ago_stays_as_it_is(self):
        self.escalated(119)
        self.tick()
        self.assertEqual((self.claude_runs(), len(self.records())), ([], 2))

    def test_a_closed_decision_whose_worktree_is_gone_stands_on_the_full_pass(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "role": "site-manager", "state": "closed", "ts": iso(NOW - HOUR)}])
        self.seed("firings", [fired("aaa"), decided("aaa", "closed")])
        self.tick(entries=[])
        self.assertEqual([r["id"] for r in self.records() if r.get("outcome") == "stood"], ["aaa"])

    def test_the_schema_and_tools_carry_the_decision_and_the_dry_run(self):
        item = factory.RUN_SCHEMA["properties"]["decisions"]["items"]["properties"]
        self.assertIn("closed", item["decision"]["enum"])
        self.assertEqual(item["superseded_by"]["type"], "string")
        self.assertIn("Bash(factory clean --dry-run)", factory.SITE_MANAGER_TOOLS)


class OverTests(CloseBase):
    """A task whose work is over by the script's own records leaves by itself: merged and handed over (or settled) whatever
    its exited lead's summary says, a handed-over report with a file deliverable. Nothing unpushed or unmerged goes."""

    LEAD = {"id": "sx", "name": "t1", "kind": "background", "state": "blocked"}  # exited: no status, no pid

    def setUp(self):
        super().setUp()
        self.lead(self.LEAD)

    def lead(self, session):
        self.sessions = [{**session, "cwd": str(self.wt), "startedAt": int((time.time() - 3 * 3600) * 1000)}]

    def merged(self, state="handed-over"):
        self.pr("MERGED")
        self.declare("t1", state, deliverable="https://x/pull/1")

    def report(self, deliverable=None):
        out = self.root.parent / "report.md"
        out.write_text("done\n")
        self.declare("t1", "handed-over", deliverable=deliverable or str(out))

    def status(self):
        return json.loads(self.go("status", "--json")[1])

    def found(self):
        repo = factory.Repo(str(self.root))
        return factory.c_merged_not_cleaned({"repo": repo, "sessions": self.sessions, "rows": factory.collect(repo, self.sessions, 2)}, None)

    def kept(self, why):
        code, out, _ = self.go("clean")
        self.assertEqual((code, out), (0, f"kept t1: {why}\n"))
        self.assertEqual(self.removal_commands(), [])
        self.assertTrue(self.wt.is_dir())

    def removed(self):
        code, out, _ = self.go("clean")
        self.assertEqual((code, out), (0, "removed t1\n"))
        self.assertEqual(self.removal_commands(), [["claude", "rm", "sx"]])
        self.assertEqual(self.event_names(), ["TaskRemoved"])

    def test_a_merged_handed_over_task_with_an_exited_lead_that_says_blocked_is_over_and_removed(self):
        for state in ("handed-over", "acknowledged", "dismissed"):
            with self.subTest(state):
                self.merged(state)
                doc = self.status()
                [row] = doc["rows"]
                self.assertEqual((row["phase"], row["overtime"], row["nearing"], row["blocked"], doc["in_motion"]),
                                 ("merged", False, False, None, 0))
        self.removed()
        self.assertEqual([r["state"] for r in self.records("tasks")], ["working", "handed-over", "acknowledged", "dismissed"])

    def test_a_merged_task_declared_working_or_blocked_is_kept_whatever_its_lead_says(self):
        self.pr("MERGED")
        for state in ("working", "blocked"):
            with self.subTest(state):
                self.declare("t1", state, ts=ago(60))
                self.assertEqual(self.status()["rows"][0]["phase"], "motion")
                self.kept("background session sx is not finished (status missing, state blocked)")

    def test_a_merged_handed_over_task_is_kept_while_its_session_has_a_process_or_is_interactive(self):
        self.merged()
        for name, session, why in (("busy", {"status": "busy", "state": "working"},
                                    "background session sx is not finished (status busy, state working)"),
                                   ("process, no status", {"pid": 7},
                                    "background session sx is not finished (status missing, state blocked)"),
                                   ("interactive", {"kind": "interactive"}, "an interactive session is open in it")):
            with self.subTest(name):
                self.lead({**self.LEAD, **session})
                self.assertEqual(self.status()["rows"][0]["phase"], "merged")
                self.kept(why)

    def test_a_merged_handed_over_task_with_a_newer_commit_or_uncommitted_changes_is_kept(self):
        self.merged()
        self.write(self.wt, "notes.txt", "mine\n")
        self.kept("uncommitted changes")
        (self.wt / "notes.txt").unlink()
        sha = self.own_commit("after the merge")
        self.kept("commits newer than its merged pull request")
        self.assertEqual(self.git("rev-parse", "worktree-t1"), sha)

    def test_a_handed_over_report_with_a_file_deliverable_is_removed_without_an_ack(self):
        self.report()
        self.lead({**self.LEAD, "state": "done"})
        self.removed()
        self.assertEqual([r["state"] for r in self.records("tasks")], ["working", "handed-over"])

    def test_a_report_is_kept_for_unpushed_work_uncommitted_changes_a_busy_or_blocked_lead(self):
        self.report()
        self.lead({**self.LEAD, "status": "busy", "state": "working"})
        self.kept("background session sx is not finished (status busy, state working)")
        self.lead(self.LEAD)  # exited and saying blocked: the relaxed rule is for merged tasks only
        self.kept("background session sx is not finished (status missing, state blocked)")
        self.lead({**self.LEAD, "state": "done"})
        self.write(self.wt, "notes.txt", "mine\n")
        self.kept("uncommitted changes")
        (self.wt / "notes.txt").unlink()
        sha = self.own_commit("only here")
        self.kept("settled, but its head commit is on no remote-tracking branch")
        self.assertEqual(self.git("rev-parse", "worktree-t1"), sha)

    def test_a_report_whose_deliverable_is_a_link_or_missing_or_inside_the_worktree_is_kept(self):
        self.lead({**self.LEAD, "state": "done"})
        for deliverable, why in (("https://x/report", "no merged pull request (none)"),
                                 (str(self.root.parent / "gone.md"), "no merged pull request (none)"),
                                 (str(self.wt / "a.txt"), "no merged pull request (none)")):
            with self.subTest(deliverable):
                self.declare("t1", "handed-over", deliverable=deliverable)
                self.kept(why)

    def test_the_merged_not_cleaned_fact_names_what_clean_keeps(self):
        self.merged()
        self.assertEqual(self.found(), [])  # clean removes it
        self.write(self.wt, "notes.txt", "mine\n")
        self.assertEqual(self.found(), [("t1", "t1", "kept: uncommitted changes")])
        self.lead({**self.LEAD, "status": "busy"})
        self.assertEqual(self.found(), [])  # a session at work is somebody at work, dirty or not
        (self.wt / "notes.txt").unlink()
        self.assertEqual(self.found(), [])
        self.lead(self.LEAD)
        self.removed()
        self.assertEqual(self.found(), [])

    def test_the_fact_fires_for_a_report_and_a_closed_task_kept_for_an_unpushed_commit(self):
        self.lead({**self.LEAD, "state": "done"})
        self.report()
        sha = self.own_commit("only here")
        self.assertEqual(self.found(), [("t1", "t1", "kept: settled, but its head commit is on no remote-tracking branch")])
        self.declare("t1", "closed", reason="abandoned")
        self.assertEqual(self.found(), [("t1", "t1", f"kept: {ONLY_HERE}{sha[:7]} only here")])
        self.assertEqual(self.git("rev-parse", "worktree-t1"), sha)


class StackedFactTests(StackedTests):
    def test_a_merged_task_kept_only_for_a_task_stacked_on_it_is_not_fired(self):
        self.declare("t1", "handed-over", deliverable="https://x/pull/1")
        repo = factory.Repo(str(self.root))
        rows = factory.collect(repo, [], 2)
        [t1] = [r for r in rows if r["slug"] == "t1"]
        self.assertEqual((t1["phase"], factory.keep_rule(repo, t1, rows)), ("merged", ("t2 is stacked on it", True)))
        self.assertEqual(factory.c_merged_not_cleaned({"repo": repo, "sessions": [], "rows": rows}, None), [])


class MergedNotCleanedWatchTests(Harness):
    """The watch's full pass: the fact's firing folds like any other."""

    def answers(self, dirty=""):
        pr = [{"state": "MERGED", "url": "u", "headRefOid": "sha-x", "baseRefName": "main"}]
        return {**git_task_answers("x", NOW, dirty=dirty), ("gh", "pr", "list"): (0, json.dumps(pr), ""),
                ("gh", "pr", "list", "--state", "open"): (0, "[]", "")}

    def setUp(self):
        super().setUp()
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "role": "lead", "state": "handed-over", "ts": iso(NOW - HOUR)}])

    def test_a_merged_task_clean_keeps_is_fired_with_the_reason(self):
        self.tick(answers=self.answers(" M a\n"))
        [f] = [r for r in self.records() if r.get("criterion") == "merged-not-cleaned"]
        self.assertEqual((f["slug"], f["subject"], f["detail"]), ("x", "x", "kept: uncommitted changes"))

    def suppressed(self, minutes):
        self.seed("firings", [fired("aaa", criterion="merged-not-cleaned"), decided("aaa", "suppressed", ts=NOW - minutes * 60)])
        self.tick(answers=self.answers(" M a\n"))
        return self.records()

    def test_a_firing_suppressed_less_than_120_minutes_ago_stays_suppressed(self):
        self.assertEqual(len(self.suppressed(119)), 2)

    def test_a_firing_suppressed_more_than_120_minutes_ago_is_fired_again(self):
        new = self.suppressed(121)[-1]
        self.assertNotEqual(new["id"], "aaa")
        self.assertEqual(new["criterion"], "merged-not-cleaned")
        self.assertIn("; suppressed earlier: why", new["detail"])

    def test_the_firing_is_cleared_when_nothing_keeps_the_task_any_more(self):
        self.seed("firings", [fired("aaa", criterion="merged-not-cleaned")])
        self.tick(answers=self.answers())
        self.assertEqual([r for r in self.records() if r["id"] == "aaa" and "cleared" in r][0]["cleared"], True)
        self.assertEqual([r for r in self.records() if r.get("criterion") == "merged-not-cleaned" and r["id"] != "aaa"], [])


if __name__ == "__main__":
    unittest.main()
