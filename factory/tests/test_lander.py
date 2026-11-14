import io
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_health
from test_factory import ROOT, WT, FakeRun, factory
from test_health import NOW, iso, register
from test_merge import SESSION, World, pr, review

GREEN = [{"name": "test", "bucket": "pass"}]
PENDING = [{"name": "test", "bucket": "pending"}]
RED = [{"name": "test", "bucket": "fail", "link": "https://x/run/2"}]
COURIER_LINE = re.compile(r"nothing else: (.*)\. Then reply", re.S)


class Sim:
    """GitHub and git in memory: a pull request is behind until a merge of the default branch is pushed into it, and
    every merge moves the default branch under all the others."""

    def __init__(self):
        self.prs, self.merged, self.tip, self.log, self.lines, self.cwd_of = {}, [], 0, [], [], {}

    def add(self, n, behind=True, base="main", approved=True, draft=False, checks=None, after=None, conflicts=(),
            update_conflict=(), local=None, diverged=False, mergeable=("MERGEABLE",), lag=False, stays_behind=False, ahead=False):
        self.prs[n] = {"head": f"h{n}", "base": base, "behind": behind, "approved": approved, "draft": draft,
                       "local": local, "diverged": diverged, "lag": lag, "stale": 0, "shown": None, "stays_behind": stays_behind, "ahead": ahead, "mergeable": list(mergeable),
                       "checks": [GREEN] if checks is None else checks, "after": [GREEN] if after is None else after,
                       "conflicts": list(conflicts), "update_conflict": list(update_conflict)}

    def open(self):
        return [n for n in sorted(self.prs) if n not in self.merged]

    def listed(self, n):
        p = self.prs[n]
        head = p["shown"] if p["stale"] else p["head"]
        p["stale"] = max(0, p["stale"] - 1)
        return {**pr(n, base=p["base"], draft=p["draft"], sha=head), "comments": [],
                "latestReviews": [{"state": "APPROVED", "submittedAt": iso(NOW - 60)}] if p["approved"] else []}

    def events(self, *kinds):
        return [(k, n) for k, n, *_ in self.log if k in kinds]

    def pushes(self):
        return [n for k, n in self.events("push")]

    def lead_lines(self):
        return [line for line in self.lines if line.startswith("LANDER:")]

    def handle(self, a, cwd):
        if a[:3] == ["gh", "pr", "list"]:
            if "--head" not in a:
                return 0, json.dumps([self.listed(n) for n in self.open()]), ""
            branch = a[a.index("--head") + 1]
            if not re.fullmatch(r"worktree-t\d+", branch):
                return None
            n = int(branch.removeprefix("worktree-t"))
            p = self.prs[n]
            return 0, json.dumps([{"state": "MERGED" if n in self.merged else "OPEN", "url": f"https://x/pull/{n}",
                                   "headRefOid": p["head"], "isDraft": p["draft"],
                                   "baseRefName": "main" if n in self.merged else p["base"]}]), ""
        if a[:3] == ["gh", "pr", "checks"]:
            n = int(a[3])
            script = self.prs[n]["checks"]
            answer = script.pop(0) if len(script) > 1 else script[0]
            self.log.append(("checks", n, self.prs[n]["head"], answer is not None and all(c["bucket"] == "pass" for c in answer)))
            return (1, "", "no checks reported") if answer is None else (0, json.dumps(answer), "")
        if a[:3] == ["gh", "pr", "view"]:
            script = self.prs[int(a[3])]["mergeable"]
            return 0, json.dumps({"mergeable": script.pop(0) if len(script) > 1 else script[0]}), ""
        if a[:3] == ["gh", "pr", "merge"]:
            n = int(a[3])
            assert a[-1] == self.prs[n]["head"], "merged a head other than the one whose checks were read"
            self.log.append(("merge", n, a[-1]))
            self.merged.append(n)
            self.tip += 1
            for p in self.prs.values():
                p["behind"] = True
            return 0, "", ""
        if a[:3] == ["gh", "pr", "ready"]:
            n = int(next(x for x in a[3:] if x != "--undo").removeprefix("worktree-t"))
            self.prs[n]["draft"] = "--undo" in a
            self.log.append(("undo" if "--undo" in a else "ready", n))
            return 0, "", ""
        if a[:3] == ["gh", "pr", "edit"]:
            self.prs[int(a[3])]["base"] = a[-1]
            self.log.append(("edit", int(a[3]), a[-1]))
            return 0, "", ""
        if a[:2] == ["gh", "api"]:
            m = re.search(r"pulls/(\d+)/reviews$", a[-1])
            if m:
                n = int(m[1])
                return 0, json.dumps([[review(sha=self.prs[n]["head"], n=n)]]), ""
            if a[-1].endswith("/permission"):
                return 0, json.dumps({"permission": "write"}), ""
        if a[:3] == ["git", "merge-base", "--is-ancestor"] and a[4] in {p["head"] for p in self.prs.values()}:
            return (1 if next(p for p in self.prs.values() if p["head"] == a[4])["behind"] else 0), "", ""
        if a[:3] == ["git", "rev-parse", "origin/main"]:
            return 0, f"tip{self.tip}\n", ""
        if a[:2] == ["git", "merge-tree"]:
            files = next(p for p in self.prs.values() if p["head"] == a[-1])["conflicts"]
            return (1, "tree\n" + "\n".join(files) + "\n\ninfo\n", "") if files else (0, "tree\n", "")
        if cwd and cwd.startswith(WT):
            n = int(os.path.basename(cwd).removeprefix("t"))
            p = self.prs[n]
            if a[:2] == ["git", "log"]:
                return 0, f"{NOW} {p['local'] or p['head']}\n", ""
            if a[:3] == ["git", "rev-parse", f"origin/worktree-t{n}"]:
                return 0, f"{p['head']}\n", ""  # the branch as the remote has it
            if a[:3] == ["git", "rev-parse", "HEAD"]:
                return 0, f"{p['local'] or p['head']}\n", ""
            if a[:3] == ["git", "merge", "--ff-only"]:
                if p["diverged"]:
                    return 1, "", "not possible to fast-forward"
                if not p["ahead"]:
                    p["local"] = None
                self.log.append(("ff", n))
                return 0, "", ""
            if a[:3] == ["git", "merge", "--no-edit"]:
                return (1, "CONFLICT", "") if p["update_conflict"] else (0, "", "")
            if a[:2] == ["git", "diff"]:
                return 0, "\n".join(p["update_conflict"]) + "\n", ""
            if a[:3] == ["git", "merge", "--abort"]:
                self.log.append(("abort", n))
                return 0, "", ""
            if a[:2] == ["git", "push"]:
                self.log.append(("push", n))
                p["shown"], p["stale"] = p["head"], 1 if p["lag"] else 0  # GitHub still lists the old head once
                p["head"], p["behind"], p["checks"] = f"h{n}.{len(self.pushes())}", p["stays_behind"], list(p["after"])
                return 0, "", ""
        if a[:2] == ["claude", "-p"]:
            self.lines.append(COURIER_LINE.search(a[-1])[1])
            return 0, json.dumps({"result": "sent"}), ""
        return None


class LoopRun(FakeRun):
    def __init__(self, answers, sim):
        super().__init__(answers)
        self.sim, self.cwd = sim, None

    def __call__(self, argv, cwd=None, input=None, timeout=None, env=None, **kw):
        self.cwd = cwd
        return super().__call__(argv, cwd, input, timeout, env, **kw)

    def resolve(self, argv):
        found = self.sim.handle(argv, self.cwd)
        return super().resolve(argv) if found is None else found


class Loop(World):
    CRITERIA = ["lgtm-blocked"]

    def setUp(self):
        super().setUp()
        self.sim = Sim()
        self.reruns = []
        for patcher in (mock.patch.object(test_health, "FakeRun", lambda answers: LoopRun(answers, self.sim)),
                        mock.patch.object(factory, "rerun_line", self.rerun_line)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def rerun_line(self, repo, n):
        """The function behind `factory rerun`: what the test says it answers, and what it did to the checks."""
        self.reruns.append(n)
        started, line, checks = self.rerun_answer
        if started:
            self.sim.prs[int(n)]["checks"] = checks
        return started, line

    rerun_answer = (False, "no rerun: test double", None)

    def go(self, *argv, leads=True, dispatcher=True, state="handed-over", at=None):
        prs = [pr(n) for n in sorted(self.sim.prs)]
        answers, sessions, slugs = self.world(prs, state=state)
        sessions = [s if (i == 0 or not leads) else {**s, "name": os.path.basename(s["cwd"])} for i, s in enumerate(sessions)]
        answers[("osascript",)] = (0, "", "")
        return self.cli(*argv, answers=answers, sessions=sessions if dispatcher else sessions[1:], entries=slugs, at=at)

    def stopped(self, out):
        return [line for line in out.splitlines() if " stopped " in line]

    def lander_json(self):
        return factory.read_lander(SimpleNamespace(root=ROOT))


class MergeInOrderTests(Loop):
    def test_five_ready_pull_requests_cost_five_pushes_one_before_each_merge(self):
        for n in range(1, 6):
            self.sim.add(n)
        code, out, _ = self.go("lander")
        self.assertEqual(code, 0)
        self.assertEqual(self.sim.merged, [1, 2, 3, 4, 5])
        self.assertEqual(self.sim.events("push", "merge"), [e for n in range(1, 6) for e in (("push", n), ("merge", n))])
        self.assertEqual([l for l in out.splitlines() if l.startswith("#") and " merged " in l], [f"#{n} merged https://x/pull/{n}" for n in range(1, 6)])
        self.assertEqual(out.count("brought up to date, waiting for its checks"), 5)

    def test_a_stale_listing_after_a_push_keeps_the_pull_request_in_flight_and_starts_no_cascade(self):
        for n in range(1, 4):
            self.sim.add(n, lag=True)
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.sim.merged), (0, [1, 2, 3]))
        self.assertEqual(self.sim.events("push", "merge"), [e for n in range(1, 4) for e in (("push", n), ("merge", n))])
        self.assertEqual(self.stopped(out), [])

    def test_a_push_that_leaves_the_pull_request_behind_ends_with_that_stop_and_no_second_update(self):
        self.sim.add(1, stays_behind=True)
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.sim.pushes(), self.sim.merged), (1, [1], []))
        self.assertIn("#1 stopped other: still behind after the update (told dispatcher)\n", out)

    def test_a_handed_over_pull_request_in_no_queue_is_sent_back_when_it_conflicts_and_left_alone_otherwise(self):
        self.sim.add(1, approved=False, conflicts=["a.txt"])
        self.sim.add(2, approved=False)
        code, out, _ = self.go("lander")
        self.assertEqual(code, 0)
        self.assertIn("#1 stopped conflict: conflicts with main in a.txt (told lead)\n", out)
        self.assertEqual(self.sim.events("undo"), [("undo", 1)])
        self.assertTrue(self.sim.lead_lines()[0].startswith("LANDER: t1: #1 went back to you: it conflicts with main in a.txt."))
        self.assertEqual([e for e in self.sim.log if e[0] in ("push", "merge", "checks")], [])
        self.sim.lines.clear()
        self.go("lander")
        self.assertEqual(self.sim.lines, [])

    def test_a_stacked_pull_request_in_no_queue_is_not_conflict_checked(self):
        self.sim.add(1, approved=False, behind=False)
        self.sim.add(2, approved=False, base="worktree-t1", conflicts=["a.txt"])
        self.go("lander")
        self.assertEqual((self.sim.lines, self.sim.events("undo")), ([], []))

    def queued(self, *steps):
        """The `queue` records of these steps, without their `ts`."""
        return [{k: v for k, v in r.items() if k != "ts"} for r in self.records("queue") if r["step"] in steps]

    def test_the_queue_records_the_update_with_the_tip_it_merged_in(self):
        self.sim.add(1)
        self.go("lander")
        self.assertEqual(self.queued("updated"), [
            {"repo": ROOT, "slug": "t1", "pr": 1, "head": "h1", "step": "updated", "tip": "tip0"}])

    def test_the_queue_records_a_rerun_with_its_line(self):
        self.sim.add(1, behind=False, checks=[RED])
        self.rerun_answer = (True, "rerun: attempt 2 started", [GREEN])
        self.go("lander")
        self.assertEqual(self.queued("rerun"), [
            {"repo": ROOT, "slug": "t1", "pr": 1, "head": "h1", "step": "rerun", "text": "rerun: attempt 2 started"}])

    def test_the_queue_records_a_send_back_with_who_was_told_and_the_conflicting_files(self):
        self.sim.add(1, conflicts=["a.txt", "b.txt"])
        self.sim.add(2, behind=False, checks=[RED])
        self.go("lander")
        self.assertEqual(sorted(self.queued("sent-back"), key=lambda r: r["pr"]), [
            {"repo": ROOT, "slug": "t1", "pr": 1, "head": "h1", "step": "sent-back", "kind": "conflict",
             "text": "conflicts with main in a.txt, b.txt", "told": "lead", "files": ["a.txt", "b.txt"]},
            {"repo": ROOT, "slug": "t2", "pr": 2, "head": "h2", "step": "sent-back", "kind": "ci",
             "text": "failed or cancelled checks: test; no rerun: test double", "told": "lead"}])
        self.assertEqual(self.queued("rerun"), [])

    def test_the_queue_records_a_stop_with_who_was_told(self):
        self.sim.add(1, base="elsewhere", behind=False)
        self.go("lander")
        [record] = self.queued("stopped")
        self.assertEqual((record["pr"], record["kind"], record["told"], "files" in record), (1, "base", "dispatcher", False))
        self.assertEqual(self.queued("sent-back"), [])

    def test_a_stop_is_reported_once_per_number_head_and_kind(self):
        self.sim.add(1, base="elsewhere", behind=False)
        self.go("lander")
        self.go("lander", at=NOW + 120)
        self.assertEqual(len(self.sim.lines), 1)
        self.assertTrue(self.sim.lines[0].startswith("LANDER: #1 stopped base:"))

    def test_a_red_check_that_rerun_restarts_is_not_sent_back_stays_in_flight_and_merges_when_green(self):
        self.sim.add(1, behind=False, checks=[RED])
        self.sim.add(2)
        self.rerun_answer = (True, "rerun: test: T (f.go) is outside what the branch changes; attempt 2 started", [PENDING, GREEN])
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.stopped(out), self.sim.lead_lines(), self.sim.events("undo")), (0, [], [], []))
        self.assertEqual(self.reruns, ["1"])
        self.assertEqual(self.sim.merged[0], 1)
        self.assertEqual(self.sim.pushes(), [2])  # not before #1 merged: it was in flight

    def test_a_red_check_with_no_rerun_is_sent_back_with_the_reason_in_the_leads_line(self):
        self.sim.add(1, behind=False, checks=[RED])
        self.rerun_answer = (False, "no rerun: already re-run once; looks like a flake: T (f.go)", None)
        code, out, _ = self.go("lander")
        self.assertEqual(code, 1)
        self.assertEqual(self.reruns, ["1"])
        [line] = self.sim.lead_lines()
        self.assertIn("went back to you: red checks: test https://x/run/2; no rerun: already re-run once; looks like a flake: T (f.go).", line)

    def test_a_red_update_is_sent_back_and_the_next_one_is_updated(self):
        for n in range(1, 6):
            self.sim.add(n, after=[RED] if n == 2 else None)
        code, out, _ = self.go("lander")
        self.assertEqual(self.sim.merged, [1, 3, 4, 5])
        self.assertEqual(self.sim.pushes(), [1, 2, 3, 4, 5])
        self.assertEqual(code, 1)
        self.assertIn("#2 stopped ci: failed or cancelled checks: test; no rerun: test double (told lead)\n", out)

    def test_a_conflict_is_sent_back_in_the_first_pass_with_no_push(self):
        for n in range(1, 6):
            self.sim.add(n, conflicts=["a.txt", "b.txt"] if n == 4 else ())
        code, out, _ = self.go("lander")
        self.assertEqual(self.sim.merged, [1, 2, 3, 5])
        self.assertEqual(self.sim.pushes(), [1, 2, 3, 5])
        self.assertLess(out.index("#4 stopped conflict: conflicts with main in a.txt, b.txt"), out.index("#1 merged"))
        [line] = [l for l in self.sim.lead_lines() if "#4" in l]
        self.assertTrue(line.startswith("LANDER: t4: #4 went back to you: it conflicts with main in a.txt, b.txt."))

    def test_every_merge_is_preceded_by_a_passing_checks_read_on_its_head_and_no_model_runs(self):
        for n in range(1, 4):
            self.sim.add(n)
        self.go("lander")
        for k, n, head in [e for e in self.sim.log if e[0] == "merge"]:
            before = self.sim.log[:self.sim.log.index((k, n, head))]
            self.assertEqual([e for e in before if e[0] == "checks" and e[1] == n][-1][2:], (head, True))
        self.assertFalse(any("--agent" in c for c in self.claude_runs()))
        self.assertEqual(self.claude_runs(), [])

    def test_an_up_to_date_green_pull_request_merges_before_a_lower_one_that_is_behind(self):
        self.sim.add(1)
        self.sim.add(2, behind=False)
        self.go("lander")
        self.assertEqual(self.sim.merged, [2, 1])
        self.assertEqual(self.sim.pushes(), [1])

    def test_a_stuck_one_does_not_block_and_merges_by_itself_once_handed_over_again(self):
        self.sim.add(1, behind=False, checks=[RED])
        self.sim.add(2)
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.sim.merged), (1, [2]))
        self.assertEqual(self.sim.events("undo"), [("undo", 1)])
        self.assertEqual(self.sim.lead_lines(), [
            "LANDER: t1: #1 went back to you: red checks: test https://x/run/2; no rerun: test double. Fix it on your branch (merge origin/main in), "
            "re-review what you change, and hand over again: it is then merged by itself."])
        self.assertIn("#1 stopped ci: failed or cancelled checks: test; no rerun: test double (told lead)\n", out)
        self.sim.lines.clear()
        _, out, _ = self.go("lander")
        self.assertEqual((self.sim.lead_lines(), self.stopped(out)), ([], []))
        self.sim.prs[1].update(checks=[GREEN], draft=False)
        self.seed("tasks", [{"repo": ROOT, "slug": "t1", "state": "handed-over", "ts": iso(NOW + 60)}])
        self.go("lander", at=NOW + 120)
        self.assertEqual(self.sim.merged, [2, 1])

    def test_a_conflict_at_the_update_goes_to_the_lead_and_the_pass_goes_on(self):
        self.sim.add(1, update_conflict=["a.txt"])
        self.sim.add(2)
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.sim.merged), (1, [2]))
        self.assertEqual(self.sim.events("abort", "undo"), [("abort", 1), ("undo", 1)])
        self.assertEqual(self.sim.pushes(), [2])
        self.assertIn("#1 stopped conflict: conflicts with main in a.txt (told lead)\n", out)
        self.assertIn("#1 went back to you: it conflicts with main in a.txt.", self.sim.lead_lines()[0])
        self.assertFalse(any("make" in c[:1] for c in self.fake.calls))
        self.assertEqual([c for c in self.claude_runs() if "--agent" in c], [])

    def test_a_stack_waits_for_its_base_then_is_updated_retargeted_and_merged(self):
        self.sim.add(1)
        self.sim.add(2, base="worktree-t1")
        self.go("lander")
        self.assertEqual(self.sim.merged, [1, 2])
        self.assertEqual(self.sim.events("push", "merge", "edit"), [("push", 1), ("merge", 1), ("push", 2), ("edit", 2), ("merge", 2)])


class LevelTests(Loop):
    def test_a_worktree_behind_its_own_remote_branch_is_fast_forwarded_and_the_pull_request_merges(self):
        self.sim.add(1, behind=False, local="old")
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.sim.merged, self.sim.events("ff", "push")), (0, [1], [("ff", 1)]))
        self.assertEqual(self.stopped(out), [])

    def test_a_worktree_ahead_of_its_remote_is_a_handover_stop_not_lag_and_the_next_one_is_updated(self):
        self.sim.add(1, behind=False, local="unpushed", ahead=True)
        self.sim.add(2)
        code, out, _ = self.go("lander")
        self.assertEqual(self.sim.pushes(), [2])
        self.assertEqual(self.sim.merged[0], 2)
        self.assertEqual(self.lander_json()["waiting"]["1"]["kind"], "handover")
        self.assertNotIn("pending", out)

    def test_a_diverged_worktree_is_a_quiet_handover_stop_and_nothing_is_pushed(self):
        self.sim.add(1, behind=False, local="old", diverged=True)
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.sim.merged, self.sim.pushes(), self.stopped(out)), (1, [], [], []))
        self.assertEqual(self.lander_json()["waiting"]["1"]["kind"], "handover")


class WordTests(Loop):
    def word(self):
        return factory.read_word(SimpleNamespace(root=ROOT))

    def test_a_window_is_recorded_and_the_tick_merges_a_handed_over_unapproved_pull_request_until_it_expires(self):
        code, out, _ = self.go("lander", "--window", "30")
        self.assertEqual(code, 0)
        self.assertEqual(out, f"merge window open until {iso(NOW + 1800)}: every handed-over pull request with green checks merges; "
                              "the next tick starts it\n")
        self.assertEqual(self.word()["all"], NOW + 1800)
        self.assertEqual(self.sim.log, [])
        self.sim.add(1, approved=False, behind=False)
        self.go("watch")
        self.assertEqual(self.sim.merged, [1])
        self.sim.add(2, approved=False, behind=False)
        self.sim.add(3, behind=False)
        self.go("watch", at=NOW + 1801)
        self.assertEqual(self.sim.merged, [1, 3])
        self.go("lander", "--window", "30", at=NOW + 2000)
        self.assertEqual(self.go("lander", "--window", "0")[1], "merge window closed\n")
        self.assertEqual(self.word()["all"], 0)

    def test_a_window_with_numbers_or_out_of_range_is_refused(self):
        self.sim.add(1)
        for argv in (("1", "--window", "5"), ("--window", "721"), ("--window", "-1")):
            self.assertEqual(self.go("lander", *argv)[0], 2)

    def test_a_number_that_is_not_open_is_refused_and_nothing_is_recorded(self):
        self.sim.add(1)
        code, _, err = self.go("lander", "9")
        self.assertEqual(code, 2)
        self.assertIn("#9 is not an open pull request", err)
        self.assertEqual(self.word()["prs"], {})

    def test_a_number_while_the_lock_is_held_joins_the_queue_of_the_running_loop(self):
        self.sim.add(7, approved=False, behind=False)
        with factory.repo_lock(SimpleNamespace(root=ROOT), "lander"):
            code, out, _ = self.go("lander", "7")
        self.assertEqual((code, out), (0, "a lander run is already going: it takes everything that is ready (#7 is now in its queue)\n"))
        self.assertEqual(self.sim.merged, [])
        repo = SimpleNamespace(root=ROOT)
        snap = [self.sim.listed(7)]
        with mock.patch.object(factory, "read_tasks", lambda root: {"t7": {"state": "handed-over"}}), \
                mock.patch.object(factory, "task_slug", lambda repo, pr: "t7"):
            self.assertEqual([(p["number"], perm) for p, perm in factory.lander_queue(repo, snap, self.word())], [(7, None)])

    def test_a_held_lock_skips_the_loop_of_a_tick_silently(self):
        self.sim.add(1, behind=False)
        with factory.repo_lock(SimpleNamespace(root=ROOT), "lander"):
            code, out, _ = self.go("watch")
        self.assertEqual((code, self.sim.merged), (0, []))
        self.assertNotIn("lander", out)
        self.go("watch", at=NOW + 120)
        self.assertEqual(self.sim.merged, [1])

    def test_nothing_ready_is_said_once_and_exits_zero(self):
        self.assertEqual(self.go("lander")[:2], (0, "nothing ready\n"))


class PatienceTests(Loop):
    def test_no_checks_right_after_the_push_are_not_reported_and_the_pull_request_merges_when_they_pass(self):
        self.sim.add(1, after=[None, None, PENDING, GREEN])
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.stopped(out), self.sim.merged), (0, [], [1]))

    def test_mergeability_not_computed_right_after_a_push_stays_quiet_and_the_pull_request_merges(self):
        self.sim.add(1, behind=False, mergeable=["UNKNOWN"] * 5 + ["MERGEABLE"])
        code, out, _ = self.go("lander")
        self.assertEqual((code, self.stopped(out), self.sim.merged), (0, [], [1]))

    def test_checks_pending_past_1800_seconds_are_reported_to_the_dispatcher_once_and_stop_holding_back_the_next(self):
        self.sim.add(1, behind=False, checks=[PENDING])
        self.sim.add(2)
        code, out, _ = self.go("lander")
        self.assertEqual(self.sim.merged[0], 2)
        self.assertEqual(self.stopped(out), ["#1 stopped pending: checks are still running (told dispatcher)"])
        self.assertLess(out.index("#1 stopped"), out.index("#2 merged"))
        self.assertEqual(self.sim.lines, ["LANDER: #1 stopped pending: checks are still running"])
        self.go("lander")
        self.assertEqual(len(self.sim.lines), 1)

    def test_a_handover_is_not_reported_before_600_seconds(self):
        self.sim.add(1, approved=False, behind=False)
        self.go("lander", "1", state="working")
        self.assertEqual(self.sim.lines, [])
        self.assertIn("1", self.lander_json()["waiting"])
        _, out, _ = self.go("lander", "1", state="working", at=NOW + 601)
        self.assertEqual(len(self.stopped(out)), 1)
        self.assertEqual([l for l in self.sim.lines if "stopped handover" in l][0][:34], "LANDER: #1 stopped handover: task ")
        self.assertEqual(self.lander_json()["waiting"], {})


class ToldTests(Loop):
    def test_without_a_live_lead_the_dispatcher_hears_it_did_not_reach_its_lead(self):
        self.sim.add(1, behind=False, checks=[RED])
        self.go("lander", leads=False)
        [line] = self.sim.lead_lines()
        self.assertTrue(line.startswith("LANDER: t1 (not delivered to its lead): #1 went back to you: red checks"))

    def test_the_dispatcher_is_found_by_its_registration_and_told_by_its_current_name(self):
        self.sim.add(1, behind=False, checks=[RED])
        register("sid-d")
        answers, sessions, slugs = self.world([pr(1)])
        sessions = [{"sessionId": "sid-d", "name": "renamed", "cwd": ROOT, "kind": "interactive", "pid": 7}, *sessions[1:]]
        self.cli("lander", answers=answers, sessions=[s for s in sessions if s.get("name") != "t1"], entries=slugs)
        [line] = [c[-1] for c in self.claude_runs()]
        self.assertIn("session named renamed.", line)

    def test_without_a_dispatcher_either_the_human_is_notified(self):
        self.sim.add(1, behind=False, checks=[RED])
        _, out, _ = self.go("lander", leads=False, dispatcher=False)
        self.assertEqual(self.sim.lines, [])
        self.assertTrue(self.fake.ran("osascript"))
        self.assertIn("(told human)", out)
        self.assertEqual([r["decision"] for r in self.records() if "decision" in r], ["escalated"])

    def test_a_stop_is_a_firing_of_lgtm_blocked_logged_as_escalated_by_the_lander(self):
        self.sim.add(1, behind=False, checks=[RED])
        self.go("lander")
        [fired] = [r for r in self.records() if "criterion" in r]
        self.assertEqual((fired["criterion"], fired["subject"], fired["escalate"]), ("lgtm-blocked", "#1/ci", "always"))
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertEqual((decision["decision"], decision["by"]), ("escalated", "lander"))


class TickTests(Loop):
    def test_health_runs_before_the_loop_and_the_loop_ends_at_the_deadline_with_one_in_flight(self):
        self.sim.add(1, after=[PENDING])
        order = []
        real_health, real_drain = factory.watch_tick, factory.drain
        with mock.patch.object(factory, "watch_tick", lambda *a: order.append("health") or real_health(*a)), \
                mock.patch.object(factory, "drain", lambda *a, **kw: order.append("drain") or real_drain(*a, **kw)), \
                mock.patch.object(factory, "watch_snapshot", wraps=factory.watch_snapshot) as cheap:
            _, out, _ = self.go("watch")
        self.assertEqual(order, ["health", "drain"])
        self.assertEqual(self.sim.merged, [])
        self.assertGreaterEqual(self.clock, NOW + factory.HEALTH_SECONDS)
        self.assertLess(self.clock, NOW + factory.HEALTH_SECONDS + factory.POLL)
        self.assertGreaterEqual(cheap.call_count, 8)  # the tick's own, then one per WATCH_INTERVAL of the loop
        self.sim.prs[1]["checks"] = [GREEN]
        _, out, _ = self.go("watch")
        self.assertEqual(self.sim.merged, [1])
        self.assertIn("#1 merged https://x/pull/1\n", out)

    def test_the_draft_sync_putting_an_in_flight_pull_request_back_to_draft_does_not_start_a_cascade(self):
        self.sim.add(1, after=[PENDING] * 30 + [GREEN])
        self.sim.add(2)
        self.go("watch")
        self.assertIn(("undo", 1), self.sim.log)  # the cheap part flipped it while its checks ran
        self.assertEqual(self.sim.events("push", "merge", "undo")[:3], [("push", 1), ("undo", 1), ("merge", 1)])
        self.assertEqual(self.sim.merged[0], 1)

    def test_a_waiting_record_does_not_fire_lgtm_blocked_but_a_reported_one_does(self):
        repo = SimpleNamespace(root=ROOT)
        p = {"repo": repo, "rows": [], "gh": lambda kind, *a: [pr(1)]}
        record = {"head": "sha-1", "kind": "pending", "since": NOW}
        factory.write_lander(repo, {"stopped": {}, "waiting": {"1": record}, "health": 0})
        self.assertEqual(factory.c_lgtm_blocked(p, None), [])
        factory.write_lander(repo, {"stopped": {"1": {**record, "text": "t", "ts": "x"}}, "waiting": {}, "health": 0})
        self.assertEqual([f[1] for f in factory.c_lgtm_blocked(p, None)], ["#1/pending"])

    def test_the_file_is_written_whole_and_leaves_no_temporary(self):
        self.sim.add(1, behind=False)
        self.go("lander")
        self.assertEqual(sorted(p.name for p in self.store.glob("lander-*")),
                         sorted(f"lander-repo-{factory.repo_state(ROOT, 'lander', 'json').name[-13:-5]}.{e}"
                                for e in ("json", "lock")))
        self.assertEqual(sorted(self.lander_json()), ["health", "prioritized", "stopped", "waiting"])


class ScratchRepoBase(unittest.TestCase):
    """Real git in a scratch repository: a main checkout, its remote and one task worktree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name).resolve()
        self.env = {**os.environ, "HOME": str(tmp), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.remote, self.root, self.wt = tmp / "remote.git", tmp / "repo", tmp / "repo" / ".claude" / "worktrees" / "t1"
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=tmp)
        self.git("init", "-q", "-b", "main", str(self.root), cwd=tmp)
        self.write(self.root, "a.txt", "one\ntwo\nthree\n")
        self.write(self.root, "b.txt", "b\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "main")
        self.git("remote", "set-head", "origin", "main")
        self.git("worktree", "add", "-q", "-b", "worktree-t1", str(self.wt), "main")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, env=self.env, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, root, name, text):
        (root / name).write_text(text)

    def commit(self, root, name, text, message):
        self.write(root, name, text)
        self.git("add", name, cwd=root)
        self.git("commit", "-q", "-m", message, cwd=root)

    def task_commit(self, name, text):
        self.commit(self.wt, name, text, f"task {name}")
        self.git("push", "-q", "-u", "origin", "worktree-t1", cwd=self.wt)

    def main_moves(self, name, text):
        self.commit(self.root, name, text, f"main {name}")
        self.git("push", "-q", "origin", "main")

    def call(self, function):
        repo = SimpleNamespace(root=str(self.root), remote="origin", default="main",
                               git=lambda *a, path=None: self.run_git(path or self.root, *a))
        task = {"path": str(self.wt), "branch": "worktree-t1"}
        with mock.patch.dict(os.environ, self.env), mock.patch.object(factory, "land_busy", lambda *a: None), \
                mock.patch.object(factory, "identity_exports", lambda: ({}, None)):
            return function(repo, task), repo

    def level(self):
        return self.call(factory.level)[0]

    def update(self):
        return self.call(factory.update_branch)

    def run_git(self, path, *args):
        p = subprocess.run(["git", *args], cwd=path, env=self.env, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr


class UpdateTests(ScratchRepoBase):
    def test_a_clean_update_is_a_merge_commit_pushed_with_no_make_and_no_comment(self):
        self.task_commit("a.txt", "one\ntwo\nthree changed\n")
        self.main_moves("b.txt", "b moved\n")
        old = self.git("rev-parse", "HEAD", cwd=self.wt)
        result, _ = self.update()
        self.assertIsNone(result)
        self.assertEqual(self.git("rev-parse", "worktree-t1", cwd=self.remote), self.git("rev-parse", "HEAD", cwd=self.wt))
        self.assertEqual(self.git("rev-parse", "HEAD^1", cwd=self.wt), old)
        self.assertEqual(self.git("rev-parse", "HEAD^2", cwd=self.wt), self.git("rev-parse", "origin/main", cwd=self.wt))

    def test_a_conflict_is_aborted_nothing_is_pushed_and_the_files_are_named(self):
        self.task_commit("a.txt", "one\ntwo\ntask\n")
        self.main_moves("a.txt", "one\ntwo\nmain\n")
        old = self.git("rev-parse", "HEAD", cwd=self.wt)
        result, repo = self.update()
        self.assertEqual(result, ("conflict", "conflicts with main in a.txt"))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt), old)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.wt), "")
        self.assertEqual(self.git("rev-parse", "worktree-t1", cwd=self.remote), old)
        head = {"headRefOid": old}
        self.assertEqual(factory.conflicting_files(repo, head), ["a.txt"])

    def test_the_read_only_check_sees_a_clean_merge_and_changes_nothing(self):
        self.task_commit("a.txt", "one\ntwo\nthree changed\n")
        self.main_moves("b.txt", "b moved\n")
        head = self.git("rev-parse", "HEAD", cwd=self.wt)
        repo = SimpleNamespace(remote="origin", default="main", git=lambda *a, path=None: self.run_git(self.root, *a))
        self.assertEqual(factory.conflicting_files(repo, {"headRefOid": head}), [])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt), head)


class RealLevelTests(ScratchRepoBase):
    def test_a_remote_branch_ahead_is_fast_forwarded_and_a_diverged_one_is_a_reason(self):
        self.task_commit("a.txt", "one\ntwo\ntask\n")
        other = self.root.parent / "other"
        self.git("clone", "-q", str(self.remote), str(other), cwd=self.root.parent)
        self.git("checkout", "-q", "worktree-t1", cwd=other)
        self.commit(other, "b.txt", "elsewhere\n", "pushed from elsewhere")
        self.git("push", "-q", "origin", "worktree-t1", cwd=other)
        self.assertIsNone(self.level())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt), self.git("rev-parse", "worktree-t1", cwd=self.remote))
        self.commit(self.wt, "b.txt", "local\n", "local only")
        self.commit(other, "b.txt", "again\n", "elsewhere again")
        self.git("push", "-q", "origin", "worktree-t1", cwd=other)
        self.assertEqual(self.level(), "its worktree and origin/worktree-t1 have diverged")
        self.assertEqual(self.git("status", "--porcelain", cwd=self.wt), "")


class ChurnTests(ScratchRepoBase):
    """Real git, three task worktrees: t1 conflicts with main in a.txt; t2 changes a.txt (and is open), t3 only b.txt."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.root.parent), "CLAUDE_CODE_SESSION_ID": "sid"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task_commit("a.txt", "one\ntwo\ntask\n")
        self.main_moves("a.txt", "one\ntwo\nmain\n")
        self.trees = {"t1": self.wt}
        for slug, name, text in (("t2", "a.txt", "ONE\ntwo\nthree\n"), ("t3", "b.txt", "b3\n")):
            tree = self.root / ".claude" / "worktrees" / slug
            self.git("worktree", "add", "-q", "-b", f"worktree-{slug}", str(tree), "main")
            self.commit(tree, name, text, slug)
            self.trees[slug] = tree
        self.repo = SimpleNamespace(root=str(self.root), remote="origin", default="main", worktree_path=lambda slug: str(self.trees[slug]),
                                    tasks=[{"path": str(t), "branch": f"worktree-{s}"} for s, t in self.trees.items()],
                                    git=lambda *a, path=None: self.run_git(path or self.root, *a))
        for slug in self.trees:
            self.declare(slug, "working")
        self.declare("t1", "handed-over")

    def declare(self, slug, state, role="lead", **kw):
        factory.declare(self.repo, slug, role, state, **kw)

    def pr(self, slug="t1", number=1, approved=False):
        head = self.git("rev-parse", f"worktree-{slug}")
        return {"number": number, "headRefName": f"worktree-{slug}", "baseRefName": "main", "headRefOid": head, "url": f"u/{number}",
                "latestReviews": [{"state": "APPROVED", "submittedAt": "2026-01-01T00:00:00Z"}] if approved else [], "comments": []}

    def land(self, slug):
        self.git("merge", "-q", "--no-edit", f"worktree-{slug}")
        self.git("push", "-q", "origin", "main")

    def lander(self):
        return factory.read_lander(self.repo)

    def events(self, kind):
        return [e for e in factory.read_log("events") if e["event"] == kind]

    def test_no_evidence_before_a_send_back_one_is_enough_and_the_others_are_named_with_their_files(self):
        self.assertIsNone(factory.churn(self.repo, "t1", self.pr()))
        self.declare("t1", "working", "lander")
        evidence = factory.churn(self.repo, "t1", self.pr())
        self.assertEqual((evidence["files"], evidence["others"], len(evidence["sent_back"])),
                         (["a.txt"], [{"slug": "t2", "state": "working", "files": ["a.txt"]}], 1))

    def test_no_evidence_without_a_conflict_or_without_an_open_task_on_the_same_files(self):
        self.declare("t1", "working", "lander")
        self.declare("t2", "dismissed")
        self.assertIsNone(factory.churn(self.repo, "t1", self.pr()))  # t2 is not open; t3 changes b.txt only
        self.declare("t2", "working")
        self.land("t2")  # merged: not open any more
        self.assertIsNone(factory.churn(self.repo, "t1", self.pr()))
        self.declare("t3", "working", "lander")
        self.assertIsNone(factory.churn(self.repo, "t3", self.pr("t3")))  # t3 does not conflict

    def decision(self, decision, waits=None, ts=None, slug="t1"):
        factory.log("firings", {"ts": ts or factory.now(), "repo": str(self.root), "id": "f1", "criterion": "churning",
                                "escalate": "judgment", "slug": slug, "subject": "#1", "detail": "d"})
        factory.log("firings", {"ts": ts or factory.now(), "repo": str(self.root), "id": "f1", "decision": decision, "reason": "r",
                                "by": "site-manager", **({"waits_on": waits} if waits else {})})

    def test_a_pause_decision_holds_while_a_waited_task_is_open_and_only_since_the_last_hand_over(self):
        self.decision("paused", ["t2"])
        self.assertEqual(factory.paused_for(self.repo, "t1"), ["t2"])
        self.land("t2")
        self.assertEqual(factory.paused_for(self.repo, "t1"), [])
        self.declare("t2", "working")
        self.assertEqual(factory.churning_decision(self.repo, "t1")["decision"], "paused")
        time.sleep(1.1)
        self.declare("t1", "handed-over")
        self.assertIsNone(factory.churning_decision(self.repo, "t1"))

    def settle_once(self, since_ago=0):
        pr = self.pr("t1", 2)
        told = []
        if since_ago:
            factory.write_lander(self.repo, {**self.lander(), "waiting": {"2": {"head": pr["headRefOid"], "kind": "conflict",
                                                                               "since": time.time() - since_ago}}})
        with mock.patch.object(factory, "report_stop", lambda *a: told.append(a)):
            factory.settle(self.repo, None, {2: (pr, "conflict", "conflicts with main in a.txt")})
        return told

    def test_a_churning_conflict_waits_for_the_decision_up_to_the_patience_then_is_reported(self):
        self.declare("t1", "working", "lander")
        self.declare("t1", "handed-over")
        self.assertEqual(self.settle_once(), [])
        self.assertIn("2", self.lander()["waiting"])
        self.assertEqual(self.settle_once(since_ago=factory.CHURN_PATIENCE - 20), [])
        self.assertEqual(len(self.settle_once(since_ago=factory.CHURN_PATIENCE + 1)), 1)

    def test_a_decision_a_first_send_back_or_no_overlap_reports_at_once(self):
        self.assertEqual(len(self.settle_once()), 1)  # no earlier send-back
        self.declare("t1", "working", "lander")
        self.declare("t1", "handed-over")
        self.decision("suppressed")
        factory.write_lander(self.repo, {"stopped": {}, "waiting": {}, "health": 0, "prioritized": {}})
        self.assertEqual(len(self.settle_once()), 1)

    def test_a_conflict_that_other_tasks_do_not_share_is_not_held(self):
        self.declare("t1", "working", "lander")
        self.declare("t2", "dismissed")
        self.assertEqual(len(self.settle_once()), 1)

    def paused(self, waits=("t2",)):
        self.declare("t1", "blocked", "tech-lead", waits_on=list(waits), paused=True)

    def release(self, started=True):
        told, starts = [], []
        with mock.patch.object(factory, "snapshot", lambda repo: [self.pr()]), \
                mock.patch.object(factory, "tech_lead_start", lambda *a: starts.append(a) or started), \
                mock.patch.object(factory, "tell_lead", lambda *a: told.append(a)):
            factory.release_paused(self.repo, "disp")
        return starts, told

    def test_a_pause_is_released_once_when_no_waited_task_is_open_and_not_before(self):
        self.paused()
        self.assertEqual(self.release(), ([], []))  # t2 is working
        self.land("t2")
        [start], told = self.release()
        self.assertEqual((start[3], start[4]), ("t1", "the tasks it waited for (t2) have landed: merge origin/main in once and land"))
        self.assertEqual(told, [])
        self.assertEqual(self.release(), ([], []))  # once
        self.assertIn("t1", self.lander()["prioritized"])
        [e] = self.events("TaskPrioritized")
        self.assertEqual((e["slug"], e["waited"]), ("t1", ["t2"]))

    def test_without_a_tech_lead_the_lead_is_told_and_a_vanished_waited_task_counts_as_landed(self):
        self.paused(["ghost"])
        starts, [told] = self.release(started=False)
        self.assertEqual(len(starts), 1)
        self.assertEqual(told[3:5], ("LANDER", "the way is clear: ghost have landed. Declare factory state working, merge origin/main in once, "
                                              "re-review what the merge changed and hand over."))

    def test_a_leads_own_block_is_not_released(self):
        self.land("t2")
        self.declare("t1", "blocked", waits_on="t2")
        self.declare("t3", "blocked", waits_on=["t2"])
        self.assertEqual(self.release(), ([], []))
        self.assertEqual(self.lander()["prioritized"], {})

    def test_an_expired_hold_does_not_release_the_same_pause_again(self):
        self.paused()
        self.land("t2")
        self.assertEqual(len(self.release()[0]), 1)
        self.prioritize(ago=factory.RELEASE_HOLD_MINUTES * 60 + 5)
        self.declare("t3", "handed-over")
        with mock.patch.object(factory, "forward", lambda *a, **k: None):
            factory.end_priorities(self.repo, "disp")
        self.assertEqual(self.lander()["prioritized"], {})
        self.assertEqual(self.release(), ([], []))  # still blocked and paused, but already released once
        self.assertEqual(len(self.events("TaskPrioritized")), 1)
        self.assertEqual(self.queue(), [1])  # the queue serves the others again
        time.sleep(1.1)
        self.paused()  # a new pause is released again
        self.assertEqual(len(self.release()[0]), 1)
        self.assertEqual(len(self.events("TaskPrioritized")), 2)

    def test_a_tick_with_only_a_paused_task_runs_the_loop(self):
        self.paused()
        self.declare("t2", "working")
        ran = []
        with mock.patch.object(factory, "Repo", lambda: self.repo), mock.patch.object(factory, "watch_tick", lambda *a: None), \
                mock.patch.object(factory, "drain", lambda *a, **k: ran.append(a)):
            self.assertEqual(factory.cmd_watch(SimpleNamespace(dispatcher="d")), 0)
        self.assertEqual(len(ran), 1)

    def test_a_merge_ends_the_priority_of_its_task_in_the_pass(self):
        self.prioritize()
        pr = self.pr()
        with mock.patch.object(factory, "attempt", lambda *a: None), mock.patch.object(factory, "run_clean", lambda: 0), \
                mock.patch.object(factory, "pull_main", lambda repo: None), redirect_stdout(io.StringIO()):
            self.assertTrue(factory.lander_pass(self.repo, None, {}, [pr], [(pr, None)], {})[0])
        self.assertEqual(self.lander()["prioritized"], {})
        self.assertEqual(self.events("TaskPrioritizedEnd")[-1]["reason"], "merged")

    def test_the_holder_and_its_end_are_named(self):
        self.prioritize()
        [(slug, end)] = factory.queue_holders(self.repo).items()
        self.assertEqual(slug, "t1")
        self.assertAlmostEqual(end, time.time() - 60 + factory.RELEASE_HOLD_MINUTES * 60, delta=5)

    def prioritize(self, slug="t1", ago=60):
        state = self.lander()
        state["prioritized"][slug] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))
        factory.write_lander(self.repo, state)

    def queue(self):
        snap = [self.pr("t3", 1, approved=True), self.pr("t1", 2, approved=True)]
        return [pr["number"] for pr, _ in factory.lander_queue(self.repo, snap, {"all": 0, "prs": {}})]

    def test_the_queue_serves_only_the_prioritized_task_while_the_hold_lasts(self):
        self.declare("t3", "handed-over")
        self.declare("t1", "working")
        self.assertEqual(self.queue(), [1])  # t1 is working: nothing of t1's, t3 first by number
        self.declare("t1", "handed-over")
        self.assertEqual(self.queue(), [1, 2])
        self.prioritize()
        self.assertEqual(self.queue(), [2])  # t3 handed over after the release, and waits
        self.prioritize(ago=factory.RELEASE_HOLD_MINUTES * 60 + 5)
        self.assertEqual(self.queue(), [1, 2])

    def test_a_prioritized_task_not_handed_over_leaves_the_queue_empty(self):
        self.declare("t3", "handed-over")
        for state in ("working", "blocked"):  # a lead's pause ends when it declares working, which can take a while
            with self.subTest(state):
                self.declare("t1", state)
                self.prioritize()
                self.assertEqual(self.queue(), [])

    def test_priority_ends_with_its_event_when_merged_gone_or_expired(self):
        told = []
        for slug, setup, reason in (("t3", lambda: self.land("t3"), "merged"), ("t2", lambda: self.declare("t2", "dismissed"), "gone"),
                                    ("t1", lambda: None, "expired")):
            with self.subTest(reason):
                self.prioritize(slug, ago=factory.RELEASE_HOLD_MINUTES * 60 + 5 if reason == "expired" else 60)
                setup()
                with mock.patch.object(factory, "forward", lambda *a, **k: told.append(a)):
                    factory.end_priorities(self.repo, "disp")
                self.assertNotIn(slug, self.lander()["prioritized"])
                self.assertEqual(self.events("TaskPrioritizedEnd")[-1]["reason"], reason)
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0][4], "t1 (handed-over) was served first for 24 minutes and has not merged; the queue treats every pull request alike again")

    def test_a_merge_and_a_send_back_for_another_stop_end_the_priority_but_a_conflict_does_not(self):
        self.prioritize()
        self.assertTrue(factory.end_priority(self.repo, "t1", "sent-back"))
        self.assertFalse(factory.end_priority(self.repo, "t1", "sent-back"))
        self.assertEqual([e["reason"] for e in self.events("TaskPrioritizedEnd")], ["sent-back"])
        self.prioritize()
        pr = self.pr()
        with mock.patch.object(factory, "send_back", lambda *a: "lead"), redirect_stdout(io.StringIO()), mock.patch.object(factory, "fire", lambda r, f: {"id": "i"}), mock.patch.object(factory, "evidence", lambda *a: "e"), \
                mock.patch.object(factory, "log_firing", lambda *a, **k: None):
            factory.report_stop(self.repo, None, pr, "conflict", "conflicts with main in a.txt")
            self.assertIn("t1", self.lander()["prioritized"])
            factory.report_stop(self.repo, None, pr, "ci", "failed or cancelled checks: test")
        self.assertNotIn("t1", self.lander()["prioritized"])


class EnvTests(unittest.TestCase):
    def printed(self, tty):
        class Out(io.StringIO):
            def isatty(self):
                return tty
        out = Out()
        exports = {"GH_TOKEN": "tok en", "GIT_TERMINAL_PROMPT": "0"}
        with mock.patch.object(factory, "identity_exports", lambda: (exports, None)), redirect_stdout(out):
            self.assertEqual(factory.cmd_env(None), 0)
        return out.getvalue().splitlines()

    def test_the_token_is_masked_on_a_terminal(self):
        self.assertEqual(self.printed(True), ["export GH_TOKEN=***", "export GIT_TERMINAL_PROMPT=0"])

    def test_the_token_is_printed_when_stdout_is_not_a_terminal(self):
        self.assertEqual(self.printed(False), ["export GH_TOKEN='tok en'", "export GIT_TERMINAL_PROMPT=0"])


if __name__ == "__main__":
    unittest.main()
