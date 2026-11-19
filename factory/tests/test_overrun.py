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
        self.assertEqual((sent["criterion"], sent["detail"], sent["extension_left_minutes"], sent["extended_minutes"], sent["runs"]),
                         ("overrun", ASK, 60, 0, []))
        self.assertEqual(sent["limit_left_minutes"], -10)
        until = NOW + 20 * 60
        [d] = self.decisions()
        self.assertEqual((d["id"], d["decision"], d["minutes"], d["from"], d["until"], d["by"], d["reason"], d["looked"]),
                         (ask["id"], "extended", 20, iso(NOW), iso(until), "site-manager", "landing left", ["factory status"]))
        self.assertEqual(self.lines(), [
            ("x", f"SITE MANAGER: x: extended 20 min, limit now {clock(until)}: landing left"),
            ("dispatcher-repo", f"SITE MANAGER: x: extended 20 min to {clock(until)}, no answer needed: landing left")])
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
        self.assertEqual(self.lines(), [
            ("x", f"SITE MANAGER: x: extended 20 min, limit now {until}: landing left"),
            ("dispatcher-repo", f"SITE MANAGER: x: extended 20 min to {until}, no answer needed: landing left")])
        self.assertEqual(self.told(), ["lead"])

    def test_a_pause_reaches_an_idle_lead_and_the_dispatcher_gets_its_line(self):
        self.ask()
        self.sessions = [DISPATCHER_NAME, self.idle_lead()]
        self.watch({"decision": "paused", "waits_on": ["human"], "reason": "the human decides"})
        self.assertEqual(self.lines(), [
            ("x", "SITE MANAGER: x: paused: start no build run and end your turn; the human decides. the human decides"),
            ("dispatcher-repo", "PAUSED: x: paused by the site manager, for the human: the human decides")])
        self.assertEqual(self.told(), ["lead"])

    def retired(self, resume):
        """A watch tick that extends the limit of a lead the harness retired; `resume` is what `claude --bg --resume` answers."""
        self.ask()
        gone = {k: v for k, v in self.idle_lead().items() if k not in ("pid", "status")} | {"cwd": ROOT, "id": "ab"}
        self.sessions = [DISPATCHER_NAME, gone]
        self.cli("watch", answers={**self.decide({"decision": "extended", "minutes": 20, "reason": "landing left"}),
                                   ("claude", "--bg", "--resume"): self.takes(resume) if resume[0] == 0 else resume})
        return [c for c in self.fake.calls if c[:3] == ["claude", "--bg", "--resume"]]

    def test_a_retired_lead_is_resumed_with_the_line_and_the_dispatcher_hears_the_extension(self):
        until = clock(NOW + 20 * 60)
        [resume] = self.retired((0, "backgrounded · ab · x\n", ""))
        self.assertEqual(resume[:4], ["claude", "--bg", "--resume", SID])
        self.assertRegex(resume[4], rf"^SITE MANAGER: x: extended 20 min, limit now {until}: landing left \[outbox [0-9a-f]{{12}}\]$")
        self.assertEqual(self.lines(), [("dispatcher-repo", f"SITE MANAGER: x: extended 20 min to {until}, no answer needed: landing left")])
        self.assertEqual(self.told(), ["lead"])

    def test_a_retired_lead_that_cannot_be_resumed_is_queued_and_the_dispatcher_hears_nothing_yet(self):
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
    def test_an_extension_beyond_the_ceiling_is_decided_escalated_by_the_watch(self):
        self.ask()
        self.watch({"decision": "extended", "minutes": 61, "reason": "too long"})
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["by"], d["to"]), ("escalated", "watch", "dispatcher"))
        self.assertIn("the site manager did not decide", d["reason"])
        self.assertEqual(self.lines(), [
            ("x", f"SITE MANAGER: x: passed to the human: declare factory state blocked human and wait. {d['reason']}"),
            ("dispatcher-repo", f"ESCALATION: x: overrun undecided, the human decides: {ASK}")])
        self.watch()
        self.assertEqual(self.runs(), [])

    def test_the_ceiling_counts_what_was_granted_before(self):
        self.seed("firings", [fired("e1", criterion="overrun", ts=STARTED + 600),
                              {"ts": iso(STARTED + 660), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r", "by": "site-manager",
                               "minutes": 40, "until": "u"},
                              {"repo": ROOT, "id": "e1", "cleared": True}])
        self.ask()
        self.watch({"decision": "extended", "minutes": 30, "reason": "more"})
        self.assertEqual([d["decision"] for d in self.decisions() if d["id"] != "e1"], ["escalated"])
        self.assertEqual(json.loads(self.cli("status", "--json")[1])["rows"][0]["limit"], 160 * 60)

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
        self.assertEqual((d["decision"], d["by"]), ("escalated", "watch"))
        self.assertIn("the site manager run failed", d["reason"])
        self.assertEqual([n for n, _ in self.lines()], ["x", "dispatcher-repo"])
        self.watch()
        self.assertEqual(self.runs(), [])

    def test_with_no_dispatcher_the_human_is_told_on_this_machine(self):
        self.ask()
        self.sessions = [lead("x", pid=4242, startedAt=STARTED * 1000)]
        code, out, _ = self.watch()
        self.assertEqual(code, 0)
        self.assertEqual(self.runs(), [])
        [d] = self.decisions()
        self.assertEqual((d["decision"], d["by"]), ("escalated", "watch"))
        self.assertIn(f"overrun undecided, the human decides: {ASK}", out)
        self.assertEqual([n for n, _ in self.lines()], ["x"])
        self.watch()
        self.assertEqual(len(self.decisions()), 1)


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
        self.assertEqual(factory.extension_state(p, "x", None), (STARTED, 20, 40))


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

    def test_a_second_extension_counts_from_the_first_ones_until_and_the_ceiling_falls_by_both(self):
        started = self.started(140)
        self.previous(NOW - 600, 15)  # from NOW-10m, until NOW+5m
        self.ask()
        self.watch({"decision": "extended", "minutes": 10, "reason": "more"})
        [d] = [d for d in self.decisions() if d["id"] != "e1"]
        self.assertEqual((d["from"], d["until"]), (iso(NOW + 5 * 60), iso(NOW + 15 * 60)))
        self.assertEqual(factory.extension_state({"repo": SimpleNamespace(root=ROOT)}, "x", None)[1:], (25, 35))
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
        self.assertEqual(self.logged("overrun", "escalated", to="lead"), [])
        self.assertEqual(len(self.logged("overrun", "escalated", to="dispatcher")), 1)
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


if __name__ == "__main__":
    unittest.main()
