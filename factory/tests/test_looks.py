"""The site manager is woken by events and at a pace, is given one picture, can make calls of its own, and is scored per entry."""
import json
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from test_factory import DISPATCHER, ROOT, WT, factory
from test_health import FINISHED, HOUR, NOW, Harness, Home, choice, decided, ev, fired, iso, lead, look, pic, reply, row
from test_merge import pr

ENTRIES = ["overrun", "collision", "review-loops", "unlisted"]
DISPATCH = DISPATCHER | {"name": "dispatcher-repo"}
SHIPPED_ENTRIES = (
    "nearing-limit past-limit quiet blocked-unanswered handover-unacknowledged busy-after-handover review-loops deep-stack "
    "merged-not-cleaned cost-outlier lead-heavy wrong-model overrun run-stopped collision new-spec churning base-merged "
    "ci-red-default ci-red-handover red-waiting ended-undeclared unlisted").split()
LABELS = ["**What:**", "**Why it matters:**", "**What it has looked like:**", "**A good call:**", "**Score (to 2026-11-15):**"]


class Wake(Harness):
    """What wakes the site manager, one event kind at a time."""

    def setUp(self):
        super().setUp()
        self.guidance.write_text("".join(f"## {e}\n" for e in ENTRIES))

    def wake(self, entries=(), state=None, declared=NOW - HOUR, **kw):
        """A watch tick (a full pass) with a live dispatcher and `entries` as task worktrees, `state` declared for x."""
        if state:
            self.seed("tasks", [{"repo": ROOT, "slug": "x", "role": "lead", "state": state, "ts": iso(declared)}])
        sessions = kw.pop("sessions", [DISPATCH, lead("x")])
        return self.cli("watch", sessions=sessions, entries=entries, **kw)

    def only_look(self):
        self.assertEqual(len(self.claude_runs()), 1)
        [looked] = self.looks()
        return looked

    def set_look(self, ts=NOW - 60, **kw):
        (self.store / "looks-2027-01.jsonl").write_text(json.dumps(look(ts, **kw)) + "\n")

    def test_a_tick_with_no_event_runs_nothing_and_writes_no_look(self):
        _, out, _ = self.wake()
        self.assertIn("nothing new", out)
        self.assertEqual((self.claude_runs(), self.looks()), ([], []))

    def test_a_task_the_dispatcher_started(self):
        self.seed("events", [ev("a", "TaskStarted", role="dispatcher", ts=iso(NOW - 30)),
                             ev("b", "TaskStarted", role="lead", ts=iso(NOW - 30)),
                             ev("c", "TaskStarted", role="dispatcher", ts=iso(NOW - 90))])  # before the last look
        self.wake()
        self.assertEqual(self.only_look()["woken_by"], [{"event": "task-started", "slug": "a", "ts": iso(NOW - 30)}])

    def test_every_queue_record(self):
        self.seed("queue", [{"repo": ROOT, "ts": iso(NOW - 30), "step": "sent-back", "slug": "a", "pr": 106, "kind": "conflict", "head": "h"}])
        self.wake()
        self.assertEqual(self.only_look()["woken_by"],
                         [{"event": "queue", "step": "sent-back", "slug": "a", "pr": 106, "kind": "conflict", "ts": iso(NOW - 30)}])

    def test_a_lead_blocking_or_handing_over_but_not_working(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "a", "role": "lead", "state": "blocked", "waits_on": ["b"], "ts": iso(NOW - 30)},
                            {"repo": ROOT, "slug": "c", "role": "lead", "state": "working", "ts": iso(NOW - 20)},
                            {"repo": ROOT, "slug": "d", "role": "lander", "state": "handed-over", "ts": iso(NOW - 10)}])
        self.wake()
        self.assertEqual(self.only_look()["woken_by"],
                         [{"event": "state", "slug": "a", "state": "blocked", "waits_on": ["b"], "ts": iso(NOW - 30)}])

    def test_a_pull_request_that_opened(self):
        answers = {("gh", "pr", "list"): (0, json.dumps([pr(7)]), "")}
        self.set_look(prs=[])
        self.wake(["x"], state="blocked", answers=answers)
        self.assertEqual(self.only_look()["woken_by"], [{"event": "pr-opened", "pr": 7}])
        self.assertEqual(self.looks()[0]["prs"], [7])

    def test_a_pull_request_that_is_gone_unless_the_queue_merged_it(self):
        answers = {("gh", "pr", "list"): (0, "[]", "")}
        self.set_look(prs=[7, 8])
        self.seed("queue", [{"repo": ROOT, "ts": iso(NOW - 30), "step": "merged", "slug": "y", "pr": 8, "head": "h"}])
        self.wake(["x"], state="blocked", answers=answers)
        looked = self.only_look()
        self.assertEqual([e for e in looked["woken_by"] if e["event"] != "queue"], [{"event": "pr-gone", "pr": 7}])
        self.assertEqual(looked["prs"], [])

    def test_no_pull_request_event_without_a_look_to_compare_with(self):
        for path in self.store.glob("looks-*"):
            path.unlink()
        self.wake(["x"], state="blocked", answers={("gh", "pr", "list"): (0, json.dumps([pr(7)]), "")}, at=NOW)
        self.assertEqual([e["event"] for e in self.looks()[0]["woken_by"]], ["pace"])

    def test_a_fact_firing(self):
        self.tick()
        looked = self.only_look()
        self.assertEqual([e["event"] for e in looked["woken_by"]], ["firing"])

    def test_two_events_share_one_run_and_a_second_tick_with_nothing_new_runs_nothing(self):
        self.seed("events", [ev("a", "TaskStarted", role="dispatcher", ts=iso(NOW - 30))])
        self.seed("queue", [{"repo": ROOT, "ts": iso(NOW - 20), "step": "updated", "slug": "a", "pr": 5, "head": "h"}])
        self.wake()
        self.assertEqual([e["event"] for e in self.only_look()["woken_by"]], ["task-started", "queue"])
        _, out, _ = self.wake(at=NOW + 120)
        self.assertIn("nothing new", out)
        self.assertEqual((self.claude_runs(), len(self.looks())), ([], 1))

    def test_pace_needs_a_working_or_blocked_task_and_an_old_look(self):
        for state, age_, looks in (("working", 30 * 60, 1), ("blocked", 30 * 60, 1), ("working", 20 * 60, 0),
                                   ("handed-over", 3 * HOUR, 0), (None, 3 * HOUR, 0)):
            with self.subTest(state=state, look_age=age_):
                for path in self.store.glob("looks-*"):
                    path.unlink()
                for path in [*self.store.glob("tasks*"), *self.store.glob("lander-*")]:
                    path.unlink()
                self.set_look(ts=NOW - age_)
                self.wake(["x"], state=state, declared=NOW - 4 * HOUR)
                self.assertEqual(len(self.claude_runs()), looks)
                if looks:
                    self.assertEqual(self.looks()[-1]["woken_by"], [{"event": "pace"}])

    def test_pace_is_a_full_pass_matter_and_any_look_resets_it(self):
        self.set_look(ts=NOW - 2 * HOUR)
        factory.write_lander(SimpleNamespace(root=ROOT), {"health": NOW - 10})
        self.wake(["x"], state="working")
        self.assertEqual(self.claude_runs(), [])  # between full passes
        self.wake(["x"], at=NOW + 1800)
        self.assertEqual(len(self.claude_runs()), 1)
        self.wake(["x"], at=NOW + 3600 - 120)
        self.assertEqual(len(self.claude_runs()), 0)  # the look of NOW + 1800 is a recent one

    def test_a_look_is_stamped_with_the_ticks_time_and_records_what_the_run_reported(self):
        self.seed("queue", [{"repo": ROOT, "ts": iso(NOW - 30), "step": "merged", "slug": "a", "pr": 5, "head": "h"}])
        self.wake(answers={("claude", "-p"): (0, json.dumps({"session_id": "s1", "total_cost_usd": 0.21, "structured_output": {
            "sent": False, "decisions": [{"entry": "unlisted", "slug": "x", "detail": "d", "decision": "suppressed", "reason": "r", "looked": []}]}}), "")})
        [looked] = self.looks()
        self.assertEqual((looked["ts"], looked["cost_usd"], looked["run"], looked["model"], looked["tasks"], looked["calls"]),
                         (iso(NOW), 0.21, "s1", "opus", 0, 0))  # no row for x: the call is skipped
        self.assertIsInstance(looked["seconds"], int)
        self.assertNotIn("failed", looked)

    def test_a_failed_run_still_writes_its_look(self):
        self.seed("queue", [{"repo": ROOT, "ts": iso(NOW - 30), "step": "merged", "slug": "a", "pr": 5, "head": "h"}])
        code, _, _ = self.wake(answers={("claude", "-p"): (1, "", "boom")})
        self.assertEqual(code, 1)
        [looked] = self.looks()
        self.assertEqual((looked["failed"], looked["cost_usd"]), ("boom", None))
        _, out, _ = self.wake(at=NOW + 120)
        self.assertIn("nothing new", out)  # its events are not retried

    def test_a_record_written_after_the_look_read_the_logs_wakes_the_next_tick(self):
        self.seed("queue", [{"repo": ROOT, "ts": iso(NOW + 30), "step": "sent-back", "slug": "a", "pr": 1, "kind": "conflict", "head": "h"}])
        _, out, _ = self.wake()
        self.assertIn("nothing new", out)
        self.wake(at=NOW + 60)
        self.assertEqual([w["pr"] for w in self.only_look()["woken_by"]], [1])

    def test_without_a_dispatcher_nothing_runs_and_no_look_is_written(self):
        self.seed("events", [ev("a", "TaskStarted", role="dispatcher", ts=iso(NOW - 30))])
        self.wake(sessions=[])
        self.assertEqual((self.claude_runs(), self.looks()), ([], []))


class Picture(Harness):
    def setUp(self):
        super().setUp()
        self.guidance.write_text("## overrun\n## unlisted\n")

    def prompt(self, **kw):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "role": "lead", "state": "working", "ts": iso(NOW - 2 * HOUR)}])
        self.seed("queue", [{"repo": ROOT, "ts": iso(NOW - 30), "step": "merged", "slug": "a", "pr": 5, "head": "h"}])
        self.cli("watch", sessions=[DISPATCH, lead("x", pid=100)], entries=["x"], **kw)
        return json.loads(self.claude_runs()[0][-1].partition("\n\n")[2])

    def test_the_body_and_a_row(self):
        self.seed("events", [ev("x", "TaskStarted", ts=iso(NOW - 3 * HOUR))] + [ev("x", "SubagentStart", role="reviewer", ts=iso(NOW - HOUR))] * 2)
        self.seed("techlead", [{"repo": ROOT, "ts": iso(NOW - HOUR), "slug": "x", "kind": "build", "outcome": "stopped", "reason": "r", "cost_usd": 1.5,
                                "seconds": 600}])
        self.seed("firings", [fired("old", criterion="review-loops", ts=NOW - 3 * HOUR), {"ts": iso(NOW - 2 * HOUR), "repo": ROOT, "id": "old",
                                                                                           "decision": "suppressed", "reason": "ordinary", "by": "site-manager"}])
        body = self.prompt()
        self.assertEqual(list(body), ["since", "woken_by", "firings", "tasks", "landed_median_usd"])
        self.assertEqual(body["since"], iso(NOW - 60))
        [task] = body["tasks"]
        self.assertEqual((task["slug"], task["state"], task["reviews"], task["lead"], task["pid"]), ("x", "working", 2, "x", 100))
        self.assertEqual(task["runs"], [{"ts": iso(NOW - HOUR), "outcome": "stopped", "reason": "r", "cost_usd": 1.5, "minutes": 10}])
        self.assertEqual(task["calls"], [{"entry": "review-loops", "decision": "suppressed", "reason": "ordinary", "decided": "2h"}])
        self.assertEqual((task["declared"], task["worktree"], task["limit"]), ("2h", f"{WT}/x", "2h"))
        self.assertNotIn("transcript_tail", task)
        self.assertNotIn("what", json.dumps(body["firings"]))

    def test_a_cost_the_monitor_cannot_give_is_unpriced_not_zero(self):
        [task] = self.prompt()["tasks"]
        self.assertTrue(task["cost"].startswith("unpriced: no monitor"), task)
        self.assertNotIn("cost_usd", task)

    def test_a_priced_row_and_the_median(self):
        (self.store / "settings.toml").write_text('[paths]\nmonitor = "/m/isshoni"\n')
        tasks = [(f"t{i}", float(i), 50.0) for i in range(1, 6)] + [("x", 4.0, 90.0)]
        monitor = json.dumps({"repos": [{"repo_path": ROOT, "tasks": [{"task": s, "cost": {"total": t, "main_pct": m}} for s, t, m in tasks]}]})
        merged = json.dumps([{"headRefName": f"worktree-t{i}"} for i in range(1, 6)])
        body = self.prompt(answers={("/m/isshoni",): (0, monitor, ""), ("gh", "pr", "list", "--state", "merged"): (0, merged, "")})
        self.assertEqual(body["landed_median_usd"], 3.0)
        self.assertEqual((body["tasks"][0]["cost_usd"], body["tasks"][0]["lead_share_pct"]), (4.0, 90.0))

    def test_children_and_models_off_and_stack_and_merged(self):
        ps = "  100     1       20:00 claude --bg\n  101   100       19:00 sleep 600\n  102   100       00:05 sh\n"
        events = [ev("x", "TaskStarted"), ev("x", "PostToolUse", role="coder", model="claude-opus-5-5")]
        base = row("a")
        top = row("x", base="worktree-a", state="handed-over", phase="review", session="working", elapsed=None, limit=2 * HOUR, extensions=[], last_commit=None,
                  dirty=False, ahead=1, behind=0, pr_draft=False, path=f"{WT}/x")
        done = row("m", phase="merged", pr_url="https://pr/1", session="none", limit=HOUR, extensions=[{"minutes": 20}], dirty=False, ahead=0, behind=0, pr_draft=False)
        p = pic(rows=[{**base, "session": "none", "extensions": [], "dirty": None, "ahead": None, "behind": None, "pr_draft": False}, top, done],
                sessions=[lead("x", pid=100)], events=events, declared={"x": {"ts": iso(NOW - 600)}},
                gh=lambda kind, *a: {"mergedAt": iso(NOW - 3 * HOUR)} if kind is dict else [])
        with mock.patch.object(factory, "run", lambda argv, **kw: (0, ps, "")):
            tasks = {t["slug"]: t for t in factory.look_picture(p, None, [], [])["tasks"]}
        self.assertEqual(tasks["x"]["children"], ["101 up 19:00: sleep 600", "102 up 00:05: sh"])
        self.assertEqual(tasks["x"]["models_off"], [{"role": "coder", "model": "claude-opus-5-5", "configured": "sonnet"}])
        self.assertEqual(tasks["x"]["stack"], 2)
        self.assertEqual((tasks["m"]["merged"], tasks["m"]["extended_minutes"]), ("3h", 20))
        self.assertNotIn("children", tasks["a"])

    def test_messages_are_the_queued_or_returned_rows_since_the_declared_time(self):
        def put(rid, ago, **fields):
            return [{"ts": iso(NOW - ago), "repo": ROOT, "id": rid, "slug": "x", **fields}]
        self.seed("outbox", [
            *put("old", 1200, line="L0"), *put("pending", 300, line="L1"), *put("pending", 200, tries=2, why="no"),
            *put("back", 400, line="L2"), *put("back", 100, tries=3, why="gone"), *put("back", 100, end="dispatcher"),
            *put("done", 300, line="L3"), *put("done", 100, end="delivered"),
            {**put("other", 300, line="L4")[0], "slug": "y"}])
        base = row("x", session="none", extensions=[], dirty=None, ahead=None, behind=None, pr_draft=False)
        p = pic(rows=[base], declared={"x": {"ts": iso(NOW - 600)}}, repo=SimpleNamespace(root=ROOT, default="main", tasks=[], worktree_path=lambda s: f"{WT}/{s}"))
        [task] = factory.look_picture(p, None, [], [])["tasks"]
        self.assertEqual(task["messages"], [{"queued": "6m", "tries": 3, "why": "gone", "end": "dispatcher"},
                                            {"queued": "5m", "tries": 2, "why": "no"}])
        self.assertNotIn("L1", json.dumps(task))
        p["declared"] = {}
        [task] = factory.look_picture(p, None, [], [])["tasks"]
        self.assertEqual(len(task["messages"]), 3)

    def test_calls_are_the_ten_newest_of_the_day_newest_first_and_a_lead_call_among_them_still_refuses(self):
        self.seed("firings", [r for i in range(12) for r in (
            fired(f"f{i}", criterion="review-loops" if i == 11 else f"c{i}", subject="x", ts=NOW - 2000 + i), {**decided(f"f{i}", "escalated", ts=NOW - 1000 + i), "to": "lead"})])
        base = row("x", session="none", extensions=[], dirty=None, ahead=None, behind=None, pr_draft=False)
        p = pic(rows=[base], repo=SimpleNamespace(root=ROOT, default="main", tasks=[], worktree_path=lambda s: f"{WT}/{s}"))
        [task] = factory.look_picture(p, None, [], [])["tasks"]
        self.assertEqual([c["entry"] for c in task["calls"]], ["review-loops", *[f"c{i}" for i in range(10, 1, -1)]])
        task = {**task, "lead": "x"}
        with mock.patch.object(factory, "forward") as forward:
            call = {"entry": "review-loops", "slug": "x", "detail": "d", "decision": "escalated", "reason": "r", "to": "lead", "looked": []}
            self.guidance.write_text("## review-loops\n")
            self.assertEqual(factory.record_decisions({**p, "dispatcher": "dispatcher-repo"}, [], {"sent": False, "decisions": [call]}, "r1", {"tasks": [task]}), 0)
        self.assertFalse(forward.called)


class OwnCalls(Harness):
    def setUp(self):
        super().setUp()
        self.guidance.write_text("".join(f"## {e}\n" for e in ENTRIES))
        self.p = pic(repo=SimpleNamespace(root=ROOT, default="main", tasks=[], worktree_path=lambda s: f"{WT}/{s}"), dispatcher="dispatcher-repo")
        self.look = {"tasks": [{"slug": "x", "lead": "x", "calls": []}, {"slug": "y", "calls": []}]}

    def call(self, *items, look=None):
        with mock.patch.object(factory, "forward", return_value="dispatcher") as forward:
            n = factory.record_decisions(self.p, [], {"sent": False, "decisions": list(items)}, "r1", look or self.look)
        return n, forward

    def own(self, **kw):
        return {"entry": "review-loops", "slug": "x", "detail": "three rounds on a small change", "decision": "escalated", "reason": "it loops",
                "looked": ["git log"], **kw}

    def test_an_escalated_call_is_a_firing_a_decision_a_clearing_and_one_line_with_the_id(self):
        n, forward = self.call(self.own())
        [fire, decision, cleared] = self.records()
        self.assertEqual(n, 1)
        self.assertEqual((fire["criterion"], fire["slug"], fire["subject"], fire["detail"]), ("review-loops", "x", "x", "three rounds on a small change"))
        self.assertEqual((decision["id"], decision["decision"], decision["by"], decision["run"], decision["to"], decision["looked"]),
                         (fire["id"], "escalated", "site-manager", "r1", "dispatcher", ["git log"]))
        self.assertEqual(cleared, {"ts": iso(NOW), "repo": ROOT, "id": fire["id"], "cleared": True})
        forward.assert_called_once_with(self.p["repo"], "dispatcher-repo", "x", "ESCALATION", f"{fire['id']} review-loops x: it loops", lead=False)
        [folded], _ = factory.fold(self.records())
        self.assertEqual((folded["decision"], folded["cleared"], folded["outcome"]), ("escalated", True, None))
        self.assertEqual(self.cli("firing", fire["id"][:6], "acted")[0], 0)  # scored like any other

    def test_a_call_to_the_lead(self):
        n, forward = self.call(self.own(to="lead"))
        self.assertEqual((n, self.records()[1]["to"]), (1, "lead"))
        forward.assert_called_once_with(self.p["repo"], "dispatcher-repo", "x", "SITE MANAGER", "it loops")

    def test_a_suppression_is_recorded_and_sends_nothing(self):
        n, forward = self.call(self.own(decision="suppressed", to="lead"))
        self.assertEqual((n, forward.called, "to" in self.records()[1]), (1, False, False))

    def test_what_is_refused(self):
        for label, item, look in (("an unknown entry", self.own(entry="nope"), None), ("no entry", {**self.own(), "entry": None}, None),
                                  ("a slug that is not in the picture", self.own(slug="z"), None),
                                  ("paused needs a firing", self.own(decision="paused", waits_on=["y"]), None),
                                  ("told needs a firing", self.own(decision="told", tell=["x"]), None),
                                  ("no detail", self.own(detail=" "), None), ("no reason", self.own(reason=""), None),
                                  ("an unknown destination", self.own(to="human"), None),
                                  ("a lead that is not live", self.own(to="lead", slug="y"), None),
                                  ("a lead told of this entry before", self.own(to="lead"),
                                   {"tasks": [{"slug": "x", "lead": "x", "calls": [{"entry": "review-loops", "to": "lead"}]}]})):
            with self.subTest(label):
                n, forward = self.call(item, look=look)
                self.assertEqual((n, forward.called, self.records()), (0, False, []))

    def test_a_lead_told_of_another_entry_can_be_told_again(self):
        look = {"tasks": [{"slug": "x", "lead": "x", "calls": [{"entry": "collision", "to": "lead"}]}]}
        self.assertEqual(self.call(self.own(to="lead"), look=look)[0], 1)

    def test_one_call_per_entry_and_task_in_a_run(self):
        n, forward = self.call(self.own(), self.own(reason="again"), self.own(slug="y"), self.own(entry="unlisted"))
        self.assertEqual((n, forward.call_count, len([r for r in self.records() if "criterion" in r])), (3, 3, 3))

    def test_a_call_and_a_firing_decision_in_one_answer(self):
        self.seed("firings", [fired("aaa")])
        sent = [{"id": "aaa", "criterion": "ended-undeclared", "slug": "x", "subject": "x", "detail": "d"}]
        with mock.patch.object(factory, "forward", return_value="dispatcher"):
            n = factory.record_decisions(self.p, sent, {"sent": True, "decisions": [choice("aaa"), self.own()]}, "r1", self.look)
        self.assertEqual(n, 2)

    def test_an_empty_answer_is_valid(self):
        self.assertEqual(self.call()[0], 0)


class Scores(Harness):
    def setUp(self):
        super().setUp()
        self.guidance.write_text("".join(f"## {e}\n" for e in ENTRIES))
        self.seed("firings", [fired("aaa", criterion="review-loops"), {"ts": iso(NOW), "repo": ROOT, "id": "aaa", "decision": "suppressed", "reason": "r"}])

    def misses(self):
        return [r for r in self.records() if "miss" in r]

    def test_a_miss_names_an_entry(self):
        self.assertEqual(self.cli("miss", "it stalled", "--entry", "collision", "--slug", "x")[0], 0)
        self.assertEqual(self.misses()[0]["entry"], "collision")

    def test_an_entry_that_is_not_a_heading_is_refused_and_named(self):
        code, _, err = self.cli("miss", "it stalled", "--entry", "nope")
        self.assertEqual(code, 1)
        self.assertIn("overrun, collision, review-loops, unlisted", err)
        self.assertEqual(self.misses(), [])

    def test_the_entry_comes_from_the_firing_unless_given(self):
        self.cli("miss", "filter was wrong", "--firing", "aa")
        self.cli("miss", "filter was wrong", "--firing", "aa", "--entry", "unlisted")
        self.assertEqual([m["entry"] for m in self.misses()], ["review-loops", "unlisted"])

    def test_no_entry_counts_under_unlisted(self):
        self.cli("miss", "who knows")
        self.assertIsNone(self.misses()[0]["entry"])
        entries = json.loads(self.cli("firings", "--json")[1])["summary"]["entries"]
        self.assertEqual(entries["unlisted"]["missed"], 1)

    def test_one_line_per_entry_with_a_record_and_the_same_in_the_json(self):
        self.cli("miss", "a", "--entry", "collision")
        _, out, _ = self.cli("firings")
        lines = [line for line in out.splitlines() if re.match(r"[a-z-]+: fired", line)]
        self.assertEqual(lines, ["collision: fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); to lead 0; suppressed 0; missed 1; precision -",
                                 "review-loops: fired 1; escalated 0 (acted 0, ignored 0, no outcome 0); to lead 0; suppressed 1; missed 0; precision -"])
        self.assertLess(out.index("collision: fired"), out.index("escalated 0 (acted 0, ignored 0, no outcome 0); to lead 0; suppressed 1; paused"))
        entries = json.loads(self.cli("firings", "--json")[1])["summary"]["entries"]
        self.assertEqual(sorted(entries), sorted(ENTRIES))
        self.assertEqual(entries["review-loops"], {"fired": 1, "to_lead": 0, "escalated": 0, "acted": 0, "ignored": 0, "no_outcome": 0,
                                                   "suppressed": 1, "told": 0, "extended": 0, "paused": 0, "closed": 0, "missed": 0, "precision": None})


class ReferenceFile(Home):
    """The file the site manager reads, as shipped."""

    def setUp(self):
        super().setUp()
        self.text = (factory.PLUGIN_ROOT / "roles" / "site-manager-cases.md").read_text()

    def test_every_entry_is_a_heading_with_the_five_parts_in_order(self):
        self.assertTrue(self.text.startswith("# The site manager's reference\n"))
        sections = re.split(r"(?m)^## ", self.text)[1:]
        self.assertEqual(sorted(s.split("\n", 1)[0].strip() for s in sections), sorted(SHIPPED_ENTRIES))
        for section in sections:
            positions = [section.find(label) for label in LABELS]
            self.assertEqual(positions, sorted(positions), section.split("\n", 1)[0])
            self.assertNotIn(-1, positions, section.split("\n", 1)[0])
        self.assertEqual(factory.reference_entries(), [s.split("\n", 1)[0].strip() for s in sections])

    def test_the_system_prompt_is_the_role_then_the_reference(self):
        argv = factory.site_manager_argv(pic(dispatcher="d", lean_start=False, monitor=None), [])
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], f"{factory.ROLE_PATH.read_text()}\n\n{self.text}")


if __name__ == "__main__":
    unittest.main()
