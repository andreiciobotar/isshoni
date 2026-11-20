"""The site manager decides on overruns and on build runs that keep ending `stopped`, on record."""
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from test_factory import DISPATCHER, ROOT, WT, factory
from test_health import COURIER_LINE, HOUR, NOW, SID, Harness, choice, ev, fired, iso, lead, lead_prompt, pic, reply, row

DISPATCHER_NAME = DISPATCHER | {"name": "dispatcher-repo"}
STARTED = NOW - 130 * 60  # the lead is 10 minutes past its 2 h limit
ASK = "built and reviewed; landing left; the review went round twice; with 20 minutes I land it"


def clock(ts):
    return iso(ts)[11:16] + "Z"


def stop_line(reason, wait=30):
    """The line the script sends the lead when the site manager recommends stopping."""
    return (f"SITE MANAGER: x: stop recommended: the human decides in the inbox; carry on meanwhile. Unanswered in {wait} minutes, "
            f"the script pauses the task. {reason}")


def undecided_line(reason):
    """The line the script sends the lead when nothing could be extended."""
    return f"SITE MANAGER: x: undecided: carry on; your ask is in the human's inbox. {reason}"


PAUSE_LINE = ("SITE MANAGER: x: paused: the human has not answered the stop recommended in the inbox. "
              "Start no build run and end your turn; the answer arrives as INBOX: x.")
CONTINUE = "Continue: 60 more minutes, then hand over or ask again"
OTHER_OPTIONS = ["Cut: hand over what works now, list what is left in the pull request",
                 "Split: hand over the finished part, the rest becomes new tasks",
                 "Abandon: stop work; the dispatcher closes the task"]


def already(minutes):
    return f"Continue: keep going, the limit is already extended by {minutes} minutes"


class Overrun(Harness):
    """A lead `x` in the dispatcher's repository, past its limit, between two full passes."""

    def setUp(self):
        super().setUp()
        factory.write_lander(SimpleNamespace(root=ROOT), {"health": NOW})  # every tick below is not a full pass
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(STARTED + 60)}])
        self.sessions = [DISPATCHER_NAME, lead("x", pid=4242, startedAt=STARTED * 1000)]

    def cli(self, *argv, **kw):
        with mock.patch.object(os, "getcwd", return_value=kw.pop("cwd", f"{WT}/x")):
            return super().cli(*argv, **{"sessions": self.sessions, "entries": ["x"], **kw})

    def ask(self, text=ASK, **kw):
        return self.cli("overrun", text, **kw)

    def decide(self, *decisions, sent=True, fail=False, deaf=()):
        """The answers of the fake `claude`: the site manager's run says `decisions`; a courier run sends, but not to
        a session named in `deaf`."""
        def answer():
            call = self.fake.calls[-1]
            if "--json-schema" not in call:
                if any(f"session named {n}. " in call[-1] for n in deaf):
                    return 1, "", "no such session"
                if "session named x. " in call[-1]:  # the lead's session takes what it is sent
                    lead_prompt(COURIER_LINE.search(call[-1])[1])
                return 0, json.dumps({"result": "sent"}), ""
            return (1, "", "boom") if fail else reply([{"looked": ["factory status"], **d, "id": self.pending()} for d in decisions], sent)
        return {("claude", "-p"): answer}

    def pending(self):
        return next(f["id"] for f in factory.fold(self.records())[0] if f["criterion"] in ("overrun", "run-stopped") and f["decision"] is None)

    def watch(self, *decisions, **kw):
        return self.cli("watch", answers=self.decide(*decisions, **{k: kw.pop(k) for k in ("sent", "fail", "deaf") if k in kw}), **kw)

    def runs(self):
        return [c for c in self.claude_runs() if "--json-schema" in c]

    def lines(self):
        """(name, line) of each message a courier was asked to send."""
        return [(c[-1].split("session named ")[1].split(". ")[0], re.sub(r" \[outbox [0-9a-f]{12}\]$", "", COURIER_LINE.search(c[-1])[1]))
                for c in self.claude_runs() if "--json-schema" not in c]

    def decisions(self):
        return [r for r in self.records() if "decision" in r]

    def assert_card(self, ask, recommend=None, option=CONTINUE, minutes=60):
        """The task is still working, and the inbox holds one overrun card: its ask is `ask`, its first option `option`, and the
        record keeps `minutes` (None: no such key) for a pick of 1 to extend by."""
        self.assertEqual(factory.read_tasks(ROOT)["x"]["state"], "working")
        [d] = factory.open_decisions(ROOT)
        self.assertEqual((d["kind"], d["slug"], d["ask"], d.get("recommend")), ("overrun", "x", ask, recommend))
        self.assertEqual(d["options"], [option, *OTHER_OPTIONS])
        [record] = [r for r in self.records("decisions") if r["id"] == d["id"] and "ask" in r]
        self.assertEqual(record.get("minutes"), minutes)
        self.assertEqual("minutes" in record, minutes is not None)

    def answer(self, rid, n, **kw):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            return self.cli("decision", "answer", rid, str(n), cwd=ROOT, **kw)

    def human_extensions(self):
        return [f for f in factory.extended(SimpleNamespace(root=ROOT)) if f["by"] == "human"]

    def inbox_lines(self):
        return [r["line"] for r in self.records("outbox") if r.get("line", "").startswith("INBOX:")]

    def stopped_runs(self, n, reason=None):
        self.seed("techlead", [{"ts": iso(STARTED + 120 * i), "repo": ROOT, "slug": "x", "kind": "build", "outcome": "stopped",
                                "reason": reason, "cost_usd": 3.0, "seconds": 300} for i in range(1, n + 1)])


class AskTests(Overrun):
    def test_an_ask_is_a_firing_the_site_manager_decides_on_the_next_tick_and_the_script_tells_both(self):
        code, out, _ = self.ask()
        self.assertEqual(code, 0)
        self.assertIn("SITE MANAGER: x", out)
        [ask] = self.records()
        self.assertEqual((ask["criterion"], "escalate" in ask, ask["slug"], ask["subject"], ask["detail"]),
                         ("overrun", False, "x", "x", ASK))
        before = json.loads(self.cli("status", "--json")[1])["rows"][0]
        self.assertTrue(before["overtime"])
        code, out, _ = self.watch({"decision": "extended", "minutes": 20, "reason": "landing left"})
        self.assertEqual(code, 0, out)
        [run] = self.runs()
        [sent] = json.loads(run[-1].partition("\n\n")[2])["firings"]
        self.assertEqual((sent["criterion"], sent["detail"], sent["extended_minutes"], sent["runs"]), ("overrun", ASK, 0, []))
        self.assertNotIn("extension_left_minutes", sent)
        self.assertEqual(sent["limit_left_minutes"], -10)
        until = NOW + 20 * 60
        [d] = self.decisions()
        self.assertEqual((d["id"], d["decision"], d["minutes"], d["from"], d["until"], d["by"], d["reason"], d["looked"]),
                         (ask["id"], "extended", 20, iso(NOW), iso(until), "site-manager", "landing left", ["factory status"]))
        self.assertEqual(self.lines(), [("x", f"SITE MANAGER: x: extended 20 min, limit now {clock(until)}: landing left")])
        self.assertIn({"repo": ROOT, "id": ask["id"], "cleared": True, "ts": iso(NOW)}, self.records())
        after = json.loads(self.cli("status", "--json")[1])["rows"][0]
        self.assertEqual((after["limit"], after["overtime"]), (until - STARTED, False))
        self.assertEqual([(e["minutes"], e["by"], e["reason"]) for e in after["extensions"]], [(20, "site-manager", "landing left")])
        self.assertEqual(before["extensions"], [])
        self.assertIn("(+20m site-manager)", self.cli("status")[1])
        self.watch()
        self.assertEqual(self.runs(), [])

    def test_an_ask_is_refused_while_one_is_pending_and_outside_a_worktree(self):
        self.ask()
        code, _, err = self.ask("again")
        self.assertEqual((code, "pending" in err, len(self.records())), (1, True, 1))
        code, _, err = self.ask(cwd=ROOT)
        self.assertEqual((code, "inside a task worktree" in err), (2, True))
        self.assertEqual(self.ask("  ")[0], 2)

    def test_an_ask_is_cut_at_a_thousand_characters(self):
        self.ask("x" * 1500)
        self.assertEqual(len(self.records()[0]["detail"]), 1000)


class IdleLeadTests(Overrun):
    """A lead whose turn ended but whose process lives is reachable: the decision goes to it."""

    def idle_lead(self, **kw):
        return lead("x", startedAt=STARTED * 1000, pid=4242, status="idle", state="done", **kw)

    def told(self):
        return [r["told"] for r in self.records() if "told" in r]

    def test_an_extension_reaches_an_idle_lead_and_the_firing_records_it(self):
        self.ask()
        self.sessions = [DISPATCHER_NAME, self.idle_lead()]
        self.watch({"decision": "extended", "minutes": 20, "reason": "landing left"})
        until = clock(NOW + 20 * 60)
        self.assertEqual(self.lines(), [("x", f"SITE MANAGER: x: extended 20 min, limit now {until}: landing left")])
        self.assertEqual(self.told(), ["lead"])

    def test_a_stop_recommendation_reaches_an_idle_lead_and_tells_only_it(self):
        self.ask()
        self.sessions = [DISPATCHER_NAME, self.idle_lead()]
        self.watch({"decision": "paused", "waits_on": ["human"], "reason": "the human decides"})
        self.assertEqual(self.lines(), [("x", stop_line("the human decides"))])
        self.assertEqual(self.told(), ["lead"])
        self.assert_card("the human decides", recommend=2)
        self.assertEqual([d["decision"] for d in self.decisions()], ["paused"])

    def test_a_line_the_lead_has_not_taken_sends_the_dispatcher_a_later_notice_for_a_stopped_run_only(self):
        for criterion, decision, heads in (("overrun", "extended", ["SITE MANAGER"]), ("overrun", "paused", ["SITE MANAGER"]),
                                           ("overrun", "escalated", ["SITE MANAGER"]), ("run-stopped", "paused", ["SITE MANAGER", "PAUSED"])):
            f = {"id": "aaa", "slug": "x", "criterion": criterion}
            with self.subTest(criterion=criterion, decision=decision), \
                    mock.patch.object(factory, "forward", side_effect=["queued", "dispatcher"]) as forward, mock.patch.object(factory, "log_firing"):
                factory.tell_decision({"repo": SimpleNamespace(root=ROOT), "dispatcher": "dispatcher-repo"}, f, decision, "why",
                                      minutes=20, until="2026-01-01T10:00:00Z")
            self.assertEqual([c.args[3] for c in forward.call_args_list], heads)
            if len(heads) > 1:
                self.assertIs(forward.call_args_list[1].kwargs["lead"], False)

    def retired(self, resume):
        """A watch tick that extends the limit of a lead the harness retired; `resume` is what `claude --bg --resume` answers."""
        self.ask()
        gone = {k: v for k, v in self.idle_lead().items() if k not in ("pid", "status")} | {"cwd": ROOT, "id": "ab"}
        self.sessions = [DISPATCHER_NAME, gone]
        self.cli("watch", answers={**self.decide({"decision": "extended", "minutes": 20, "reason": "landing left"}),
                                   ("claude", "--bg", "--resume"): self.takes(resume) if resume[0] == 0 else resume})
        return [c for c in self.fake.calls if c[:3] == ["claude", "--bg", "--resume"]]

    def test_a_retired_lead_is_resumed_with_the_line_and_the_dispatcher_hears_nothing(self):
        until = clock(NOW + 20 * 60)
        [resume] = self.retired((0, "backgrounded · ab · x\n", ""))
        self.assertEqual(resume[:4], ["claude", "--bg", "--resume", SID])
        self.assertRegex(resume[4], rf"^SITE MANAGER: x: extended 20 min, limit now {until}: landing left \[outbox [0-9a-f]{{12}}\]$")
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.told(), ["lead"])

    def test_a_retired_lead_that_cannot_be_resumed_is_queued_and_the_dispatcher_hears_nothing(self):
        self.assertEqual(len(self.retired((1, "", "boom"))), 1)
        self.assertEqual((self.lines(), self.told()), ([], ["queued"]))

    def test_nobody_reached_is_recorded(self):
        self.ask()
        self.watch({"decision": "extended", "minutes": 20, "reason": "landing left"}, deaf=["x", "dispatcher-repo"])
        self.assertEqual(self.lines(), [
            ("x", f"SITE MANAGER: x: extended 20 min, limit now {clock(NOW + 20 * 60)}: landing left"),
            ("dispatcher-repo", f"SITE MANAGER: x (not delivered to its lead: no such session): extended 20 min, limit now "
                                f"{clock(NOW + 20 * 60)}: landing left")])
        self.assertEqual(self.told(), ["nobody"])

    def test_task_lead_names_a_lead_live_idle_or_retired(self):
        p = pic(rows=[], sessions=[self.idle_lead()])
        self.assertEqual(factory.task_lead(p, "x")[3], "x")
        self.assertEqual(factory.firing_for_run(p, fired("f1", criterion="overrun") | {"what": ""})["task"]["lead"], "x")
        p = pic(rows=[], sessions=[{k: v for k, v in self.idle_lead().items() if k not in ("pid", "status")} | {"cwd": ROOT}])
        self.assertEqual(factory.task_lead(p, "x")[3], "x")
        self.assertIsNone(factory.task_lead(pic(rows=[], sessions=[]), "x")[3])


class BoundTests(Overrun):
    """An ask the site manager does not decide is extended by the script, by `limits.extension_minutes`."""

    def assert_undecided(self, why=None):
        """The one decision is `extended` by the watch for 60 minutes; the card has no recommendation, the lead's text, and
        the lead is told."""
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["by"], d["minutes"], "to" in d), ("extended", "watch", 60, False))
        self.assertEqual(d["reason"][:len("the site manager did not decide: ")], "the site manager did not decide: ")
        self.assertIn(why or "", d["reason"])
        self.assertEqual(self.lines(), [("x", f"SITE MANAGER: x: extended 60 min, limit now {clock(NOW + 60 * 60)}: {d['reason']}")])
        self.assert_card(ASK, option=already(60), minutes=None)
        return d

    def test_an_extension_over_the_setting_is_decided_extended_by_the_watch_by_the_setting(self):
        self.ask()
        self.watch({"decision": "extended", "minutes": 61, "reason": "too long"})
        self.assert_undecided()
        self.watch()
        self.assertEqual(self.runs(), [])

    def test_an_escalated_overrun_from_the_site_manager_is_not_recorded_and_the_ask_is_decided_undecided(self):
        self.ask()
        self.watch({"decision": "escalated", "to": "dispatcher", "reason": "the human should see this"})
        self.assert_undecided()

    def test_a_second_extension_is_given_though_the_first_used_up_the_setting(self):
        self.seed("firings", [fired("e1", criterion="overrun", ts=STARTED + 600),
                              {"ts": iso(STARTED + 660), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r", "by": "site-manager",
                               "minutes": 60, "until": "u"},
                              {"repo": ROOT, "id": "e1", "cleared": True}])
        self.ask()
        self.watch({"decision": "extended", "minutes": 30, "reason": "more"})
        [d] = [d for d in self.decisions() if d["id"] != "e1"]
        self.assertEqual((d["decision"], d["by"], d["minutes"]), ("extended", "site-manager", 30))
        self.assertEqual((d["from"], d["until"]), (iso(NOW + 50 * 60), iso(NOW + 80 * 60)))  # the first one's 60 minutes ran to NOW + 50 min

    def test_an_extension_before_this_session_does_not_count(self):
        self.seed("firings", [fired("e1", criterion="overrun", ts=STARTED - 600),
                              {"ts": iso(STARTED - 500), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r",
                               "by": "site-manager", "minutes": 55, "until": "u"}])
        self.assertEqual(json.loads(self.cli("status", "--json")[1])["rows"][0]["limit"], 120 * 60)

    def test_a_failed_run_decides_the_ask_once(self):
        self.ask()
        code, _, err = self.watch(fail=True)
        self.assertEqual(code, 1)
        self.assertIn("the site manager run failed", err)
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["by"]), ("extended", "watch"))
        self.assertIn("the site manager run failed", d["reason"])
        self.assertEqual(self.lines(), [("x", f"SITE MANAGER: x: extended 60 min, limit now {clock(NOW + 60 * 60)}: {d['reason']}")])
        self.assert_card(ASK, option=already(60), minutes=None)
        self.watch()
        self.assertEqual(self.runs(), [])

    def test_with_no_task_start_the_ask_is_escalated_by_the_watch_and_a_continue_tries_again(self):
        self.ask()
        self.sessions = [DISPATCHER_NAME, lead("x", pid=4242, startedAt=None)]
        self.watch()
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["by"], "minutes" in d), ("escalated", "watch", False))
        self.assertEqual(self.lines(), [("x", undecided_line(d["reason"]))])
        self.assert_card(ASK)

    def test_with_no_dispatcher_the_human_is_told_on_this_machine(self):
        self.ask()
        self.sessions = [lead("x", pid=4242, startedAt=STARTED * 1000)]
        code, out, _ = self.watch()
        self.assertEqual(code, 0)
        self.assertEqual(self.runs(), [])
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["by"]), ("extended", "watch"))
        self.assertNotIn("notify", out)
        self.assertNotIn("ESCALATION", out)
        self.assertEqual(self.lines(), [("x", f"SITE MANAGER: x: extended 60 min, limit now {clock(NOW + 60 * 60)}: {d['reason']}")])
        self.assert_card(ASK, option=already(60), minutes=None)
        self.watch()
        self.assertEqual(len(self.decisions()), 1)


class MootTests(Overrun):
    """An ask whose task is over (handed over or settled) is closed by the script, and nobody is told."""

    def hand_over(self, state="handed-over", ts=NOW + 30):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": state, "ts": iso(ts)}])

    def assert_moot(self, state="handed-over", ts=NOW + 30):
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["by"], d["reason"]), ("suppressed", "watch", f"{state.replace('-', ' ')} at {iso(ts)}, the ask is moot"))
        self.assertNotIn("to", d)
        self.assertEqual(self.lines(), [])
        self.assertEqual((factory.open_decisions(ROOT), self.records("decisions")), ([], []))
        self.assertNotIn("ESCALATION", self.watch()[1])
        self.assertEqual(self.runs(), [])

    def test_an_ask_of_a_handed_over_lead_is_closed_as_moot_before_the_look(self):
        self.ask()
        self.hand_over()
        self.assertEqual(self.watch()[0], 0)
        self.assertEqual(self.runs(), [])  # no look for a moot ask alone
        self.assertIn({"repo": ROOT, "id": self.records()[0]["id"], "cleared": True, "ts": iso(NOW)}, self.records())
        self.assert_moot()

    def test_each_settled_state_is_named_in_the_reason(self):
        for state in ("acknowledged", "dismissed"):
            with self.subTest(state):
                self.setUp()
                self.ask()
                self.hand_over(state)
                self.watch()
                self.assert_moot(state)

    def test_a_lead_that_declared_working_again_has_a_live_ask(self):
        self.ask()
        self.hand_over()
        self.hand_over("working", NOW + 60)
        self.watch({"decision": "extended", "minutes": 20, "reason": "reopened"})
        self.assertEqual([d["decision"] for d in self.decisions()], ["extended"])
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.lines()[0][0], "x")

    def test_a_live_ask_the_site_manager_leaves_undecided_is_escalated(self):
        self.ask()
        self.hand_over()
        self.hand_over("working", NOW + 60)
        self.watch()
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["by"]), ("extended", "watch"))
        self.assertEqual([r["kind"] for r in self.records("decisions")], ["overrun"])

    def test_a_hand_over_during_the_look_closes_the_undecided_ask_as_moot(self):
        self.ask()
        def look():
            self.hand_over()
            return 1, "", "boom"
        code, out, _ = self.cli("watch", answers={("claude", "-p"): look})
        self.assertEqual(len(self.runs()), 1)
        self.assertNotIn("ESCALATION", out)
        self.assert_moot()


class OutcomeTests(Overrun):
    """On the full pass an extension is kept or overran; the listing and the summary show it."""

    UNTIL = STARTED + 140 * 60

    def extension(self, decided=NOW - 600):
        self.seed("firings", [fired("e1", criterion="overrun", ts=decided - 60),
                              {"ts": iso(decided), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r", "by": "site-manager",
                               "minutes": 20, "from": iso(decided), "until": iso(self.UNTIL)}])

    def full_pass(self, handed_over=None):
        factory.write_lander(SimpleNamespace(root=ROOT), {"health": 0})
        if handed_over:
            self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "handed-over", "ts": iso(handed_over)}])
        return self.cli("watch", at=NOW + 12 * 60)

    def outcome(self):
        return [r["outcome"] for r in self.records() if "outcome" in r]

    def test_handed_over_before_the_time_it_gave_is_kept(self):
        self.extension()
        self.full_pass(handed_over=NOW - 60)
        self.assertEqual(self.outcome(), ["kept"])
        _, out, _ = self.cli("firings")
        self.assertIn(f"overrun  x  extended 20 min until {iso(self.UNTIL)}  kept  open  r", out)
        self.assertIn("extended 1 (kept 1, overran 0, no outcome 0)", out)

    def test_past_the_time_it_gave_or_handed_over_later_is_overran(self):
        self.extension()
        self.full_pass()
        self.assertEqual(self.outcome(), ["overran"])
        self.assertIn("extended 1 (kept 0, overran 1, no outcome 0)", self.cli("firings")[1])
        self.assertIn("overrun: fired 1; escalated 0 (acted 0, ignored 0, no outcome 0); to lead 0; suppressed 0; extended 1; missed 0;", self.cli("firings")[1])
        summary = json.loads(self.cli("firings", "--json")[1])["summary"]
        self.assertEqual((summary["extended"], summary["kept"], summary["overran"]), (1, 0, 1))

    def test_handed_over_after_the_time_it_gave_is_overran(self):
        self.extension(decided=NOW - 4 * 600)
        self.full_pass(handed_over=NOW + 11 * 60)
        self.assertEqual(self.outcome(), ["overran"])

    def test_no_outcome_while_the_time_runs_and_none_is_written_twice(self):
        self.extension(decided=STARTED + 3600)
        factory.write_lander(SimpleNamespace(root=ROOT), {"health": 0})
        self.cli("watch", at=self.UNTIL - 60)
        self.assertEqual(self.outcome(), [])
        self.full_pass()
        self.full_pass()
        self.assertEqual(self.outcome(), ["overran"])

    def test_an_outcome_comes_only_on_the_full_pass(self):
        self.extension()
        self.cli("watch", at=NOW + 12 * 60)
        self.assertEqual(self.outcome(), [])


class ResumeTests(Overrun):
    """The task's start is its TaskStarted event, whatever happened to its lead's session."""

    def setUp(self):
        super().setUp()
        self.seed("events", [ev("x", "TaskStarted", ts=iso(STARTED))])
        self.seed("firings", [fired("e1", criterion="overrun", ts=STARTED + 3500),
                              {"ts": iso(STARTED + 3600), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r",
                               "by": "site-manager", "minutes": 20, "until": "u"}])

    def row(self):
        return json.loads(self.cli("status", "--json")[1])["rows"][0]

    def test_a_lead_resumed_in_a_new_session_keeps_elapsed_limit_and_extended_minutes(self):
        before = self.row()
        self.sessions = [DISPATCHER_NAME, lead("x", pid=4243, startedAt=(NOW - 6 * 60) * 1000)]
        after = self.row()
        self.assertEqual((before["elapsed"], before["limit"], [e["minutes"] for e in before["extensions"]]), (130 * 60, 140 * 60, [20]))
        self.assertEqual({k: after[k] for k in ("elapsed", "limit", "extensions")}, {k: before[k] for k in ("elapsed", "limit", "extensions")})

    def test_with_no_session_listed_the_start_and_the_total_are_still_the_events(self):
        p = {"repo": SimpleNamespace(root=ROOT)}
        self.assertEqual(factory.extension_state(p, "x", None), (STARTED, 20))


class GrantTests(Overrun):
    """An extension of N minutes gives the lead N minutes from the time it is decided, or from its limit when that is later."""

    def started(self, minutes_ago):
        self.sessions = [DISPATCHER_NAME, lead("x", pid=4242, startedAt=(NOW - minutes_ago * 60) * 1000)]
        return NOW - minutes_ago * 60

    def decide_after(self, delay, minutes, reason="landing left"):
        """A watch tick whose site manager run takes `delay` seconds and extends by `minutes`."""
        answers = self.decide({"decision": "extended", "minutes": minutes, "reason": reason})
        inner = answers[("claude", "-p")]

        def answer():
            self.clock += delay * ("--json-schema" in self.fake.calls[-1])
            return inner()
        return self.cli("watch", answers={("claude", "-p"): answer})

    def status_row(self, **kw):
        return json.loads(self.cli("status", "--json", **kw)[1])["rows"][0]

    def previous(self, decided, minutes, **old):
        """An earlier decision on x: `from` is `decided` and `until` the minutes after it, unless `old` says otherwise."""
        self.seed("firings", [fired("e1", criterion="overrun", ts=decided - 60),
                              {"ts": iso(decided), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r", "by": "site-manager",
                               "minutes": minutes, **(old or {"from": iso(decided), "until": iso(decided + minutes * 60)})},
                              {"repo": ROOT, "id": "e1", "cleared": True}])

    def test_an_ask_past_the_limit_counts_from_the_decision_not_from_the_old_limit(self):
        started = self.started(140)  # the limit, 2 h, ended 20 minutes ago
        self.ask()
        self.decide_after(4 * 60, 15)  # the site manager's run took 4 minutes
        [d] = self.decisions()
        self.assertEqual((d["from"], d["until"]), (iso(NOW + 4 * 60), iso(NOW + 19 * 60)))
        self.assertEqual(self.lines()[0][1], f"SITE MANAGER: x: extended 15 min, limit now {clock(NOW + 19 * 60)}: landing left")
        row = self.status_row(at=NOW + 5 * 60)
        self.assertEqual(started + row["limit"], NOW + 19 * 60)
        self.assertIsInstance(row["limit"], int)  # the Go reader decodes an int64
        self.assertFalse(row["overtime"])
        self.assertIn("(+15m site-manager)", self.cli("status")[1])

    def test_an_ask_before_the_limit_keeps_what_was_left(self):
        self.started(110)  # 10 minutes left
        self.ask()
        self.watch({"decision": "extended", "minutes": 15, "reason": "landing left"})
        [d] = self.decisions()
        self.assertEqual((d["from"], d["until"]), (iso(NOW + 10 * 60), iso(NOW + 25 * 60)))

    def test_a_second_extension_counts_from_the_first_ones_until_and_both_are_summed(self):
        started = self.started(140)
        self.previous(NOW - 600, 15)  # from NOW-10m, until NOW+5m
        self.ask()
        self.watch({"decision": "extended", "minutes": 10, "reason": "more"})
        [d] = [d for d in self.decisions() if d["id"] != "e1"]
        self.assertEqual((d["from"], d["until"]), (iso(NOW + 5 * 60), iso(NOW + 15 * 60)))
        self.assertEqual(factory.extension_state({"repo": SimpleNamespace(root=ROOT)}, "x", None)[1:], (25,))
        self.assertEqual(started + self.status_row()["limit"], NOW + 15 * 60)

    def test_an_old_rule_extension_keeps_its_arithmetic_and_a_new_one_follows_it(self):
        started = self.started(150)
        self.previous(NOW - 1800, 20, until="u")  # the limit is 2 h + 20 min: ended 10 minutes ago
        self.assertEqual(self.status_row()["limit"], 140 * 60)
        self.ask()
        self.watch({"decision": "extended", "minutes": 15, "reason": "landing left"})
        [d] = [d for d in self.decisions() if d["id"] != "e1"]
        self.assertEqual((d["from"], d["until"]), (iso(NOW), iso(NOW + 15 * 60)))
        self.assertEqual(started + self.status_row()["limit"], NOW + 15 * 60)

    def limit_left(self, ago):
        self.started(ago)
        self.ask()
        self.watch()
        [sent] = json.loads(self.runs()[-1][-1].partition("\n\n")[2])["firings"]
        return sent["limit_left_minutes"]

    def test_the_site_manager_is_told_how_long_is_left_before_the_limit(self):
        self.assertEqual(self.limit_left(100), 20)

    def test_the_site_manager_is_told_how_long_past_the_limit_as_a_negative_number(self):
        self.assertEqual(self.limit_left(140), -20)


class ScoreTests(Overrun):
    """The scores count the extensions of the new rule, and say how many of the old rule they leave out."""

    def test_extensions_without_from_are_counted_apart_and_not_scored(self):
        records = []
        for fid, new, outcome in (("o1", False, "kept"), ("o2", False, "overran"), ("n1", True, "kept")):
            records += [fired(fid, criterion="overrun"),
                        {"ts": iso(NOW - HOUR), "repo": ROOT, "id": fid, "decision": "extended", "reason": "r", "by": "site-manager",
                         "minutes": 20, "until": iso(NOW), **({"from": iso(NOW - HOUR)} if new else {})},
                        {"repo": ROOT, "id": fid, "outcome": outcome}]
        self.seed("firings", records)
        s = json.loads(self.cli("firings", "--json")[1])["summary"]
        self.assertEqual((s["extended"], s["kept"], s["overran"], s["old_rule"]), (1, 1, 0, 2))
        self.assertIn("extended 1 (kept 1, overran 0, no outcome 0); 2 before the limit counted from the decision, not scored; ",
                      self.cli("firings")[1])


class ModelTests(Overrun):
    def argv(self):
        return factory.site_manager_argv(pic(dispatcher="d", lean_start=False, monitor=None), [])

    def test_the_system_prompt_is_the_role_then_the_guidance(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "SITE MANAGER RULES\n\nSITE MANAGER CASES")

    def test_the_shipped_guidance_is_there_and_the_default_model_is_opus(self):
        self.assertTrue((factory.PLUGIN_ROOT / "roles" / "site-manager-cases.md").read_text().startswith("# The site manager's reference"))
        self.assertEqual(self.argv()[self.argv().index("--model") + 1], "opus")

    def test_a_settings_file_naming_another_model_is_respected(self):
        (self.store / "settings.toml").write_text('[models]\nsite_manager = "sonnet"\n')
        self.assertEqual(self.argv()[self.argv().index("--model") + 1], "sonnet")


class ValidityTests(Overrun):
    """record_decisions: what is not in the table of who may decide what is not recorded."""

    def logged(self, criterion, decision, **kw):
        p = pic(rows=[row("x")], sessions=self.sessions)
        sent = [{"id": "aaa", "criterion": criterion, "slug": "x"}]
        with mock.patch.object(factory, "log_firing") as log, mock.patch.object(factory, "tell_decision"), \
                mock.patch.object(factory, "declare"), mock.patch.object(factory, "task_event"):
            factory.record_decisions(p, sent, {"sent": True, "decisions": [{**choice("aaa", decision), **kw}]}, "r1")
        return [c.kwargs for c in log.call_args_list]

    def test_an_extension_is_only_for_an_overrun_and_needs_a_positive_integer(self):
        self.assertEqual(len(self.logged("overrun", "extended", minutes=20)), 1)
        for criterion in ("run-stopped", "past-limit", "churning"):
            with self.subTest(criterion):
                self.assertEqual(self.logged(criterion, "extended", minutes=20), [])
        for minutes in (0, -5, 2.5, "20", True, None):
            with self.subTest(minutes=minutes):
                self.assertEqual(self.logged("overrun", "extended", minutes=minutes), [])

    def test_a_pause_for_the_human_is_only_for_an_ask_and_only_for_the_human(self):
        for criterion in ("overrun", "run-stopped"):
            self.assertEqual(len(self.logged(criterion, "paused", waits_on=["human"])), 1, criterion)
            for waits in (["y"], ["human", "y"], "human", []):
                self.assertEqual(self.logged(criterion, "paused", waits_on=waits), [], (criterion, waits))
        for criterion in ("past-limit", "quiet", "churning"):
            self.assertEqual(self.logged(criterion, "paused", waits_on=["human"]), [], criterion)

    def test_a_churning_pause_records_the_decision_and_the_script_sends_nothing(self):
        p = pic(rows=[row("x")], sessions=self.sessions)
        sent = [{"id": "aaa", "criterion": "churning", "slug": "x"}]
        with mock.patch.object(factory, "churning", lambda p: {"x": {"others": [{"slug": "y"}]}}), \
                mock.patch.object(factory, "forward") as forward, mock.patch.object(factory, "log_firing") as log:
            factory.record_decisions(p, sent, {"sent": True, "decisions": [{**choice("aaa", "paused"), "waits_on": ["y"]}]}, "r1")
        self.assertEqual([c.kwargs["decision"] for c in log.call_args_list], ["paused"])
        forward.assert_not_called()

    def test_an_overrun_is_not_suppressed_nor_sent_to_the_lead(self):
        self.assertEqual(self.logged("overrun", "suppressed"), [])
        for to in ("lead", "dispatcher"):
            self.assertEqual(self.logged("overrun", "escalated", to=to), [])
        self.assertEqual(len(self.logged("run-stopped", "escalated", to="dispatcher")), 1)
        self.assertEqual(len(self.logged("run-stopped", "suppressed")), 1)


class RunStoppedTests(Overrun):
    def test_runs_ending_with_nothing_fire_and_a_pause_for_the_human_holds_the_build(self):
        self.stopped_runs(2)
        code, out, _ = self.watch({"decision": "paused", "waits_on": ["human"], "reason": "two runs, nothing handed over"}, sent=False)
        self.assertEqual(code, 0, out)
        [run] = self.runs()
        [f] = json.loads(run[-1].partition("\n\n")[2])["firings"]
        self.assertEqual((f["criterion"], f["subject"], f["detail"]),
                         ("run-stopped", "x/run2", "no reason; 2 of 2 build runs ended stopped, cost 6.00 USD"))
        self.assertEqual([(r["outcome"], r["minutes"]) for r in f["runs"]], [("stopped", 5)] * 2)
        [mine] = [t for t in factory.read_log("tasks") if t.get("role") == "site-manager"]
        self.assertEqual((mine["state"], mine["paused"], mine["waits_on"], mine["slug"]), ("blocked", True, ["human"], "x"))
        self.assertEqual([e["event"] for e in factory.read_log("events") if e["slug"] == "x" and e["event"] != "UserPromptSubmit"], ["TaskPaused"])
        self.assertEqual(self.lines(), [
            ("x", "SITE MANAGER: x: paused: start no build run and end your turn; the human decides. two runs, nothing handed over"),
            ("dispatcher-repo", "PAUSED: x: paused by the site manager, for the human: two runs, nothing handed over")])
        self.assertEqual(factory.open_decisions(ROOT), [])

    def pause_for_the_human(self, slug):
        self.seed("tasks", [{"repo": ROOT, "slug": slug, "state": "blocked", "paused": True, "waits_on": ["human"], "role": "site-manager",
                             "ts": iso(NOW - HOUR)}])

    def test_a_paused_task_starts_no_build_run_until_the_lead_declares_working(self):
        self.pause_for_the_human("xyz")
        spawned = []
        with mock.patch.object(factory, "tech_lead_missing", lambda repo, slug: None), \
                mock.patch.object(factory, "spawn", lambda argv, *a: spawned.append(argv)):
            code, _, err = self.cli("build", "xyz", entries=["xyz"])
            self.assertEqual((code, spawned), (1, []))
            self.assertIn("paused: site-manager waits on human; factory state working once it is lifted", err)
            self.assertEqual(self.cli("state", "working", entries=["xyz"], cwd=f"{WT}/xyz")[0], 0)
            self.assertEqual(self.cli("build", "xyz", entries=["xyz"])[0], 0)
        self.assertEqual(len(spawned), 1)

    def test_the_script_never_releases_a_pause_for_the_human(self):
        self.pause_for_the_human("x")
        started = []
        with mock.patch.object(factory, "run", self.fake_run()), \
                mock.patch.object(factory, "snapshot", lambda repo: [{"number": 7, "headRefName": "worktree-x"}]), \
                mock.patch.object(factory, "task_slug", lambda repo, pr: "x"), \
                mock.patch.object(factory, "tech_lead_start", lambda *a: started.append(a) or True):
            repo = factory.Repo()
            factory.release_paused(repo, None)
        self.assertEqual(started, [])
        self.assertEqual(factory.read_lander(repo)["prioritized"], {})

    def fake_run(self):
        self.cli("health")  # builds self.fake with the repository answers
        return self.fake

    def test_a_suppression_and_a_message_to_the_lead_are_accepted_and_a_later_run_clears_the_firing(self):
        self.stopped_runs(1, reason="ended without an answer")
        self.watch({"decision": "suppressed", "reason": "first such run, nothing to add"})
        [d] = self.decisions()
        self.assertEqual(d["decision"], "suppressed")
        self.assertEqual(self.lines(), [])
        self.seed("techlead", [{"ts": iso(NOW - 60), "repo": ROOT, "slug": "x", "kind": "build", "outcome": "built", "seconds": 600}])
        self.watch()
        self.assertEqual(self.runs(), [])
        [fired_row] = [r for r in self.records() if r.get("criterion") == "run-stopped"]
        self.assertIn({"repo": ROOT, "id": fired_row["id"], "cleared": True, "ts": iso(NOW)}, self.records())

    def test_a_stopped_run_is_decided_once_even_when_the_task_goes_back_to_working(self):
        self.stopped_runs(1)
        self.watch({"decision": "paused", "waits_on": ["human"], "reason": "nothing handed over"}, sent=False)
        self.watch()  # blocked: the firing is cleared
        self.assertEqual(self.cli("state", "working")[0], 0)
        self.watch()
        self.assertEqual(self.runs(), [])
        self.seed("techlead", [{"ts": iso(NOW - 60), "repo": ROOT, "slug": "x", "kind": "build", "outcome": "stopped"}])
        self.watch({"decision": "suppressed", "reason": "second run, first was paused"})
        [run] = self.runs()
        self.assertEqual(json.loads(run[-1].partition("\n\n")[2])["firings"][0]["subject"], "x/run2")

    def test_a_message_to_the_lead_is_accepted_for_a_stopped_run(self):
        self.stopped_runs(1)
        self.watch({"decision": "escalated", "to": "lead", "reason": "keep every dispatch in the foreground"})
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["to"]), ("escalated", "lead"))

    def test_only_a_working_task_whose_latest_run_stopped_fires(self):
        self.stopped_runs(1)
        self.seed("techlead", [{"ts": iso(NOW - 60), "repo": "/other", "slug": "x", "kind": "build", "outcome": "built"},
                               {"ts": iso(NOW - 50), "repo": ROOT, "slug": "x", "kind": "merge", "outcome": "resolved"}])
        self.assertEqual(factory.c_run_stopped(self.picture(), None)[0][1], "x/run1")
        self.assertEqual(factory.c_run_stopped(self.picture(state="handed-over"), None), [])
        self.seed("techlead", [{"ts": iso(NOW - 40), "repo": ROOT, "slug": "x", "kind": "build", "outcome": "built"}])
        self.assertEqual(factory.c_run_stopped(self.picture(), None), [])

    def picture(self, state="working"):
        repo = SimpleNamespace(root=ROOT, tasks=[{"path": f"{WT}/x"}])
        return pic(repo=repo, declared={"x": {"state": state}})


class Card(Overrun):
    """The task started at STARTED, by its event; the card is posted by the helper or by a watch tick."""

    def setUp(self):
        super().setUp()
        self.seed("events", [ev("x", "TaskStarted", ts=iso(STARTED))])
        self.repo = SimpleNamespace(root=ROOT)

    def post(self, ask=ASK, minutes=45, slug="x", **kw):
        load = factory.load_settings
        with mock.patch.object(factory, "load_settings", lambda: {**load(), "extension_minutes": minutes}):
            factory.post_overrun_card(self.repo, slug, ask, **kw)
        return factory.open_decisions(ROOT)

    def stop(self, recommend=2, reason="not converging"):
        """The lead asks and the site manager recommends stopping, at NOW; the card is posted."""
        self.ask()
        self.watch({"decision": "paused", "waits_on": ["human"], "reason": reason, "recommend": recommend})
        return factory.open_decisions(ROOT)[0]

    def tick(self, minutes):
        """A watch tick `minutes` after the card, in between two full passes: it runs no site manager."""
        factory.write_lander(self.repo, {"health": NOW + minutes * 60})
        return self.cli("watch", at=NOW + minutes * 60, answers=self.decide())

    def state(self):
        return {k: factory.read_tasks(ROOT)["x"].get(k) for k in ("state", "paused", "waits_on")}

    def build(self):
        spawned = []
        with mock.patch.object(factory, "tech_lead_missing", lambda repo, slug: None), mock.patch.object(factory, "validate_slug"), \
                mock.patch.object(factory, "spawn", lambda argv, *a: spawned.append(argv)):
            code, _, err = self.cli("build", "x", entries=["x"])
        return code, err, spawned

    def paused_events(self):
        return [e for e in factory.read_log("events") if e["slug"] == "x" and e["event"] == "TaskPaused"]


class InboxCardTests(Card):
    """An overrun the human decides is a card in the inbox, and the human's `Continue` extends the limit."""

    def test_the_helper_posts_the_card_and_leaves_the_task_working(self):
        [d] = self.post()
        self.assertEqual(self.state(), {"state": "working", "paused": None, "waits_on": None})
        self.assertEqual([e["event"] for e in factory.read_log("events") if e["slug"] == "x"], ["TaskStarted"])
        self.assertEqual((d["kind"], d["ask"], "recommend" in d), ("overrun", ASK, False))
        self.assertEqual(d["options"], ["Continue: 45 more minutes, then hand over or ask again", *OTHER_OPTIONS])
        [ask] = [r for r in self.records("decisions") if "ask" in r]
        self.assertEqual(ask["minutes"], 45)

    def test_a_card_for_a_limit_already_extended_names_the_minutes_and_keeps_none(self):
        [d] = self.post(recommend=1, granted=20)
        self.assertEqual((d["options"], d["recommend"]), ([already(20), *OTHER_OPTIONS], 1))
        [ask] = [r for r in self.records("decisions") if "ask" in r]
        self.assertNotIn("minutes", ask)

    def test_an_ask_of_70_words_is_cut_to_60_ending_in_an_ellipsis_and_one_of_60_is_whole(self):
        words = [f"w{i}" for i in range(70)]
        [d] = self.post("  ".join(words))
        self.assertEqual(d["ask"], " ".join(words[:59]) + " …")
        self.assertEqual(len(d["ask"].split()), 60)
        factory.log("decisions", {"repo": ROOT, "id": d["id"], "slug": "x", "choice": 4, "ts": iso(NOW)})
        self.assertEqual(self.post(" ".join(words[:60]))[0]["ask"], " ".join(words[:60]))

    def test_continue_extends_by_the_minutes_on_the_card_as_a_new_firing_by_the_human(self):
        self.seed("firings", [fired("e1", criterion="overrun", ts=NOW - 3600),
                              {"ts": iso(NOW - 3500), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r", "by": "site-manager",
                               "minutes": 5, "from": iso(NOW - 3500), "until": iso(NOW - 3200)}])
        [d] = self.post(minutes=45)
        code, out, err = self.answer(d["id"], 1)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "answered x: 1; the next tick delivers it\n")
        [f] = self.human_extensions()
        self.assertEqual((f["criterion"], f["minutes"], f["decided"], f["cleared"]), ("overrun", 45, iso(NOW), True))
        self.assertEqual((f["from"], f["until"]), (iso(NOW), iso(NOW + 45 * 60)))  # 10 minutes past the limit: from now
        self.assertEqual(f["detail"], f"the human picked continue: decision {d['id']}")
        self.assertEqual(STARTED + json.loads(self.cli("status", "--json")[1])["rows"][0]["limit"], NOW + 45 * 60)
        self.assertEqual(factory.extension_state({"repo": self.repo}, "x", None)[1:], (50,))
        self.assertEqual(self.inbox_lines(), [f"INBOX: x: decision {d['id']}: the human picked 1: {d['options'][0]}"])

    def test_continue_is_granted_though_the_earlier_extensions_were_large(self):
        self.seed("firings", [fired("e1", criterion="overrun", ts=NOW - 3600),
                              {"ts": iso(NOW - 3500), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r", "by": "site-manager",
                               "minutes": 55, "from": iso(NOW - 3500), "until": iso(NOW - 200)}])
        [d] = self.post(minutes=45)
        self.assertEqual(self.answer(d["id"], 1)[0], 0)
        [f] = self.human_extensions()
        self.assertEqual((f["minutes"], f["until"]), (45, iso(NOW + 45 * 60)))

    def test_continue_before_the_limit_runs_from_the_limit(self):
        self.seed("events", [ev("x", "TaskStarted", ts=iso(NOW - 110 * 60))])  # 10 minutes left
        [d] = self.post(minutes=15)
        self.assertEqual(self.answer(d["id"], 1)[0], 0)
        [f] = self.human_extensions()
        self.assertEqual((f["from"], f["until"]), (iso(NOW + 10 * 60), iso(NOW + 25 * 60)))

    def test_continue_without_a_task_start_is_answered_but_the_limit_is_not_extended(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "z", "state": "blocked", "ts": iso(NOW - 60)}])
        rid = factory.post_decision(self.repo, "z", "site-manager", "overrun", "a?", ["Continue: 1", "b"], minutes=1)
        code, out, _ = self.answer(rid, 1)
        self.assertEqual(code, 0)
        self.assertIn("the limit of z was not extended", out)
        self.assertEqual(self.human_extensions(), [])
        self.assertEqual(len(self.inbox_lines()), 1)

    def test_cut_split_and_abandon_send_the_line_and_extend_nothing(self):
        for n in (2, 3, 4):
            [d] = self.post()
            self.assertEqual(self.answer(d["id"], n)[0], 0)
            self.assertEqual(self.inbox_lines()[-1], f"INBOX: x: decision {d['id']}: the human picked {n}: {d['options'][n - 1]}")
        self.assertEqual((self.human_extensions(), len(self.inbox_lines())), ([], 3))

    def test_a_lead_cannot_post_an_overrun_card(self):
        with self.assertRaises(SystemExit) as refused:
            self.cli("decision", "ask", "--kind", "overrun", "a?", "--option", "b", "--option", "c")
        self.assertEqual(refused.exception.code, 2)
        self.assertEqual(self.records("decisions"), [])


class RunOnTests(Card):
    """An overrun never holds the task: whatever the site manager says, it keeps working."""

    def test_an_extension_is_a_continue_card_and_the_pick_extends_nothing_again(self):
        self.ask()
        self.watch({"decision": "extended", "minutes": 20, "reason": "landing left"})
        self.assertEqual(self.state()["state"], "working")
        self.assert_card("landing left", recommend=1, option=already(20), minutes=None)
        self.assertEqual(self.lines(), [("x", f"SITE MANAGER: x: extended 20 min, limit now {clock(NOW + 20 * 60)}: landing left")])
        [d] = factory.open_decisions(ROOT)
        limit = json.loads(self.cli("status", "--json")[1])["rows"][0]["limit"]
        self.assertEqual(self.answer(d["id"], 1)[0], 0)
        self.assertEqual((self.human_extensions(), len(self.decisions())), ([], 1))
        self.assertEqual(json.loads(self.cli("status", "--json")[1])["rows"][0]["limit"], limit)
        self.assertEqual(self.inbox_lines(), [f"INBOX: x: decision {d['id']}: the human picked 1: {already(20)}"])

    def test_a_stop_recommendation_is_a_card_with_its_pick_and_the_lead_carries_on(self):
        self.ask()
        self.watch({"decision": "paused", "waits_on": ["human"], "reason": "not converging", "recommend": 3})
        self.assertEqual(self.state(), {"state": "working", "paused": None, "waits_on": None})
        self.assert_card("not converging", recommend=3)
        self.assertEqual(self.lines(), [("x", stop_line("not converging"))])
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["waits_on"], d["by"]), ("paused", ["human"], "site-manager"))
        self.assertEqual(self.paused_events(), [])

    def test_a_stop_card_says_option_2_without_a_valid_recommendation(self):
        for recommend in (None, 1, 7):
            with self.subTest(recommend=recommend):
                self.ask()
                self.watch({"decision": "paused", "waits_on": ["human"], "reason": "slow", **({"recommend": recommend} if recommend else {})})
                self.assertEqual(factory.open_decisions(ROOT)[0]["recommend"], 2)

    def test_a_stop_card_unanswered_for_the_wait_pauses_the_task_once(self):
        self.stop()
        code, out, _ = self.tick(29)
        self.assertEqual((code, self.state()["state"], self.lines(), self.runs(), self.paused_events()), (0, "working", [], [], []))
        self.assertEqual(self.tick(30)[0], 0)
        self.assertEqual(self.state(), {"state": "blocked", "paused": True, "waits_on": ["human"]})
        self.assertEqual((self.lines(), self.runs(), len(self.paused_events())), ([("x", PAUSE_LINE)], [], 1))
        code, err, spawned = self.build()
        self.assertEqual((code, spawned), (1, []))
        self.assertIn("paused: site-manager waits on human", err)
        [d] = factory.open_decisions(ROOT)
        self.assertEqual((d["kind"], d["recommend"]), ("overrun", 2))
        self.tick(31)
        self.assertEqual((self.lines(), len(self.paused_events()), len(self.decisions())), ([], 1, 1))

    def test_the_pause_comes_on_a_tick_that_runs_the_site_manager_too(self):
        self.stop()
        self.stopped_runs(1)
        self.watch({"decision": "suppressed", "reason": "first such run"}, at=NOW + 31 * 60)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.state()["state"], "blocked")
        self.assertEqual([n for n, line in self.lines() if line == PAUSE_LINE], ["x"])

    def test_the_wait_is_the_setting(self):
        (self.store / "settings.toml").write_text("[limits]\noverrun_stop_wait_minutes = 5\n")
        self.stop()
        self.assertEqual(self.lines(), [("x", stop_line("not converging", wait=5))])
        self.tick(4)
        self.assertEqual(self.state()["state"], "working")
        self.tick(5)
        self.assertEqual(self.state()["state"], "blocked")

    def test_an_answered_stop_card_is_never_paused(self):
        d = self.stop()
        self.assertEqual(self.answer(d["id"], 3)[0], 0)
        self.tick(40)
        self.assertEqual((self.state()["state"], self.lines(), self.paused_events()),
                         ("working", [("x", f"INBOX: x: decision {d['id']}: the human picked 3: {d['options'][2]}")], []))

    def test_a_stop_card_whose_task_handed_over_is_never_paused(self):
        self.stop()
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "handed-over", "ts": iso(NOW + 60)}])
        self.tick(40)
        self.assertEqual((self.state()["state"], self.lines(), self.paused_events(), factory.open_decisions(ROOT)), ("handed-over", [], [], []))

    def test_an_undecided_card_is_not_a_stop_card_and_is_never_paused(self):
        self.post()  # the card posted when nothing could be extended: `minutes`, no recommendation
        self.tick(40)
        self.assertEqual((self.state()["state"], self.lines(), self.paused_events()), ("working", [], []))

    def test_an_extended_decision_is_recommend_1_whatever_the_site_manager_adds(self):
        self.ask()
        self.watch({"decision": "extended", "minutes": 20, "reason": "landing left", "recommend": 3})
        self.assert_card("landing left", recommend=1, option=already(20), minutes=None)

    def test_a_pick_is_capped_at_the_extension_setting_lowered_after_the_card(self):
        [d] = self.post(minutes=60)
        load = factory.load_settings
        with mock.patch.object(factory, "load_settings", lambda: {**load(), "extension_minutes": 20}):
            code, out, _ = self.answer(d["id"], 1)
        self.assertEqual(code, 0)
        self.assertNotIn("not extended", out)
        self.assertIn("was extended by 20 minutes, not 60: limits.extension_minutes is 20", out)
        [f] = self.human_extensions()
        self.assertEqual(f["minutes"], 20)

    def test_continue_after_the_pause_extends_by_the_human_and_the_leads_working_lets_the_build_start(self):
        d = self.stop()
        self.tick(31)
        self.assertEqual(self.state()["state"], "blocked")
        self.assertEqual(self.answer(d["id"], 1, at=NOW + 32 * 60)[0], 0)
        [f] = self.human_extensions()
        self.assertEqual((f["minutes"], f["by"]), (60, "human"))
        self.assertEqual(self.inbox_lines(), [f"INBOX: x: decision {d['id']}: the human picked 1: {d['options'][0]}"])
        self.assertEqual(self.build()[0], 1)
        self.assertEqual(self.cli("state", "working")[0], 0)
        code, _, spawned = self.build()
        self.assertEqual((code, len(spawned)), (0, 1))
        self.tick(60)
        self.assertEqual((self.state()["state"], self.paused_events()[1:]), ("working", []))

    def test_continue_before_the_pause_extends_the_same_way_and_no_pause_follows(self):
        d = self.stop()
        self.assertEqual(self.answer(d["id"], 1, at=NOW + 10 * 60)[0], 0)
        [f] = self.human_extensions()
        self.assertEqual((f["minutes"], f["by"]), (60, "human"))
        self.tick(40)
        self.assertEqual((self.state()["state"], self.lines(), self.paused_events()),
                         ("working", [("x", f"INBOX: x: decision {d['id']}: the human picked 1: {d['options'][0]}")], []))

    def test_a_lead_that_declares_working_without_an_answer_is_paused_again(self):
        self.stop()
        self.tick(31)
        self.assertEqual(self.cli("state", "working")[0], 0)
        self.tick(32)
        self.assertEqual((self.state()["state"], len(self.paused_events())), ("blocked", 2))


class OpenRuleTests(Card):
    """An overrun card is open while its task works or waits, until it is answered, replaced or the task hands over."""

    def test_a_second_card_for_the_task_closes_the_first(self):
        first = self.post("one")[0]["id"]
        [d] = self.post("two")
        self.assertNotEqual(d["id"], first)
        self.assertEqual([d["ask"]], ["two"])

    def test_a_card_is_open_while_working_and_after_a_block_and_closed_by_a_hand_over(self):
        self.assertEqual([d["ask"] for d in self.post()], [ASK])
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "blocked", "waits_on": ["dispatcher"], "ts": iso(NOW + 60)}])
        self.assertEqual([d["ask"] for d in factory.open_decisions(ROOT)], [ASK])
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "handed-over", "ts": iso(NOW + 120)}])
        self.assertEqual(factory.open_decisions(ROOT), [])

    def test_a_question_card_is_still_closed_by_a_task_that_works(self):
        factory.post_decision(self.repo, "x", "lead", "question", "which?", ["a", "b"])
        self.assertEqual(factory.open_decisions(ROOT), [])


class FlagTests(Card):
    """From the 10th overrun card in a row that no pick has followed, the ask says so, counted over the repository."""

    FLAG = "overruns in a row unanswered: limits or sizing look structurally wrong. "

    def cards(self, n):
        return [self.post(f"ask {i}", slug="xy"[i % 2])[-1]["id"] for i in range(n)]

    def test_the_tenth_card_across_two_tasks_says_so_and_the_ninth_does_not(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "y", "state": "working", "ts": iso(NOW - 60)}])
        self.cards(8)
        self.assertEqual(self.post("ask 8", slug="y")[-1]["ask"], "ask 8")
        tenth = self.post("ask 9", slug="x")[-1]
        self.assertEqual(tenth["ask"], f"10 {self.FLAG}ask 9")
        self.assertEqual(self.post("ask 10", slug="y")[-1]["ask"], f"11 {self.FLAG}ask 10")

    def test_a_pick_on_an_overrun_card_restarts_the_count(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "y", "state": "working", "ts": iso(NOW - 60)}])
        ids = self.cards(11)
        factory.log("decisions", {"repo": ROOT, "id": ids[-1], "slug": "y", "choice": 2, "ts": iso(NOW)})
        self.assertEqual(self.post("after")[-1]["ask"], "after")

    def test_a_flagged_ask_is_still_cut_to_60_words(self):
        self.cards(9)
        [d] = self.post(" ".join(f"w{i}" for i in range(70)))[-1:]
        words = d["ask"].split()
        self.assertEqual((len(words), words[:2], words[-1]), (60, ["10", "overruns"], "…"))


if __name__ == "__main__":
    unittest.main()
