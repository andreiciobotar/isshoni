import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_health
from test_factory import ROOT, WT, FakeRun, factory
from test_health import NOW, iso
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

    def __call__(self, argv, cwd=None, input=None, timeout=None, env=None):
        self.cwd = cwd
        return super().__call__(argv, cwd, input, timeout, env)

    def resolve(self, argv):
        found = self.sim.handle(argv, self.cwd)
        return super().resolve(argv) if found is None else found


class Loop(World):
    CRITERIA = ["lgtm-blocked"]

    def setUp(self):
        super().setUp()
        self.sim = Sim()
        patcher = mock.patch.object(test_health, "FakeRun", lambda answers: LoopRun(answers, self.sim))
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_a_stop_is_reported_once_per_number_head_and_kind(self):
        self.sim.add(1, base="elsewhere", behind=False)
        self.go("lander")
        self.go("lander", at=NOW + 120)
        self.assertEqual(len(self.sim.lines), 1)
        self.assertTrue(self.sim.lines[0].startswith("LANDER: #1 stopped base:"))

    def test_a_red_update_is_sent_back_and_the_next_one_is_updated(self):
        for n in range(1, 6):
            self.sim.add(n, after=[RED] if n == 2 else None)
        code, out, _ = self.go("lander")
        self.assertEqual(self.sim.merged, [1, 3, 4, 5])
        self.assertEqual(self.sim.pushes(), [1, 2, 3, 4, 5])
        self.assertEqual(code, 1)
        self.assertIn("#2 stopped ci: failed or cancelled checks: test (told lead)\n", out)

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
            "LANDER: t1: #1 went back to you: red checks: test https://x/run/2. Fix it on your branch (merge origin/main in), "
            "re-review what you change, and hand over again: it is then merged by itself."])
        self.assertIn("#1 stopped ci: failed or cancelled checks: test (told lead)\n", out)
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
        with factory.lander_lock(SimpleNamespace(root=ROOT)):
            code, out, _ = self.go("lander", "7")
        self.assertEqual((code, out), (0, "a lander run is already going: it takes everything that is ready (#7 is now in its queue)\n"))
        self.assertEqual(self.sim.merged, [])
        repo = SimpleNamespace(root=ROOT)
        snap = [self.sim.listed(7)]
        with mock.patch.object(factory, "read_tasks", lambda root: {"t7": {"state": "handed-over"}}), \
                mock.patch.object(factory, "task_slug", lambda repo, pr: "t7"):
            self.assertEqual([(p["number"], perm) for p, perm in factory.lander_queue(repo, snap, self.word())], [(7, None)])

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
        real_health, real_drain = factory.watch_health, factory.drain
        with mock.patch.object(factory, "watch_health", lambda *a: order.append("health") or real_health(*a)), \
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
        self.assertEqual(sorted(self.lander_json()), ["health", "stopped", "waiting"])


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
