"""The inbox's history and approve items, read by `factory status --json`; the lander's approval stops go there, not to the dispatcher."""
import json
from types import SimpleNamespace
from unittest import mock

from test_decisions import Decisions
from test_factory import ROOT, WT, factory
from test_health import HOUR, NOW, Harness, decided, fired, iso
from test_lander import Loop
from test_overrun import Card

URL = "https://github.com/example/demo/pull/12"
DAY = 24 * HOUR


class History(Decisions):
    def history(self):
        return json.loads(self.as_human("status", "--json", "--cached")[1])["history"]

    def entries(self):
        return [(e["slug"], e["by"], e["text"]) for e in self.history()]


class HistoryTests(History):
    def test_an_answer_is_a_human_entry_with_the_option_text(self):
        rid = self.posted()
        self.assertEqual(self.history(), [])
        self.as_human("decision", "answer", rid, "2")
        self.assertEqual(self.history(), [{"ts": iso(NOW), "slug": "demo-task", "by": "human", "text": "question: picked 2: Add retry now"}])

    def test_a_hand_over_is_an_entry_by_its_role_and_declared_twice_gives_two(self):
        for n in (1, 2):
            self.clock = NOW + n
            self.assertEqual(self.run_in(f"{WT}/demo-task", "state", "handed-over", URL)[0], 0)
        self.assertEqual(self.entries(), [("demo-task", "lead", f"handed over: {URL}")] * 2)

    def test_a_key_decision_is_the_line_of_the_lead_that_filed_it(self):
        self.assertEqual(self.run_in(f"{WT}/demo-task", "feedback", "decide", "Retry lives in the importer.\nWhy: one owner")[0], 0)
        self.assertEqual(self.entries(), [("demo-task", "lead", "Retry lives in the importer. Why: one owner")])

    def test_the_site_managers_extension_pause_and_close_are_entries_and_other_decisions_are_not(self):
        self.seed("firings", [
            fired("a", subject="demo-task"), {**decided("a", "extended", ts=NOW - 3 * HOUR, reason="landing left"), "minutes": 20, "until": iso(NOW + 1200)},
            fired("b", criterion="churning", subject="demo-other"), decided("b", "paused", ts=NOW - 2 * HOUR, reason="the human decides"),
            fired("c", criterion="ended-undeclared", subject="demo-gone"), decided("c", "closed", ts=NOW - HOUR, reason="replaced"),
            fired("d", subject="demo-quiet"), decided("d", "suppressed"),
            fired("e", subject="demo-up"), decided("e", "escalated")])
        self.assertEqual(self.entries(), [("demo-gone", "site-manager", "closed: replaced"),
                                          ("demo-other", "site-manager", "paused: the human decides"),
                                          ("demo-task", "site-manager", f"extended 20 min to {iso(NOW + 1200)[11:16]}Z: landing left")])

    def test_a_leads_or_tech_leads_paused_declaration_is_no_script_entry(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "demo-task", "role": role, "state": "blocked", "waits_on": ["human"], "paused": True, "ts": iso(NOW - HOUR)}
                            for role in ("lead", "tech-lead")])
        self.assertEqual(self.history(), [])

    def test_a_legacy_overrun_pause_with_its_declaration_is_one_paused_entry(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "demo-task", "role": "site-manager", "state": "blocked", "waits_on": ["human"], "paused": True,
                             "ts": iso(NOW - HOUR)}])
        self.seed("firings", [fired("a", criterion="overrun", subject="demo-task"), decided("a", "paused", ts=NOW - HOUR, reason="no progress")])
        self.assertEqual(self.entries(), [("demo-task", "site-manager", "paused: no progress")])

    def test_entries_are_newest_first_and_those_older_than_seven_days_are_left_out(self):
        self.seed("tasks", [{"repo": ROOT, "slug": f"demo-{n}", "role": "lead", "state": "handed-over", "deliverable": f"d{n}", "ts": iso(NOW - age)}
                            for n, age in ((1, 8 * DAY), (2, 6 * DAY), (3, HOUR), (4, 2 * DAY))])
        self.assertEqual([e["slug"] for e in self.history()], ["demo-3", "demo-4", "demo-2"])

    def test_a_hundred_and_one_entries_give_a_hundred(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "demo-task", "role": "lead", "state": "handed-over", "deliverable": f"d{n}", "ts": iso(NOW - 60 * n)}
                            for n in range(1, 102)])
        history = self.history()
        self.assertEqual((len(history), history[0]["text"], history[-1]["text"]), (100, "handed over: d1", "handed over: d100"))


class StopHistoryTests(Card):
    """The history after an overrun is decided: what the records say happened, through the real commands and tick."""

    def entries(self):
        return [(e["slug"], e["by"], e["text"]) for e in json.loads(self.cli("status", "--json", "--cached")[1])["history"]]

    def test_a_stop_recommendation_is_recommended_not_paused(self):
        self.stop()
        self.assertEqual(self.entries(), [("x", "site-manager", "stop recommended: not converging")])

    def test_the_scripts_later_pause_is_one_entry_by_the_script(self):
        self.stop()
        self.tick(30)
        self.tick(31)
        self.assertEqual(self.entries(), [("x", "script", "paused: the human has not answered the stop recommended"),
                                          ("x", "site-manager", "stop recommended: not converging")])

    def test_the_site_managers_pause_on_a_stopped_run_is_one_entry_by_it(self):
        self.stopped_runs(2)
        self.watch({"decision": "paused", "waits_on": ["human"], "reason": "nothing handed over"})
        self.assertEqual(self.state()["state"], "blocked")
        self.assertEqual(self.entries(), [("x", "site-manager", "paused: nothing handed over")])

    def test_a_continue_is_one_entry_the_humans_answer(self):
        [d] = self.post(minutes=45)
        self.assertEqual(self.answer(d["id"], 1)[0], 0)
        self.assertEqual(len(self.human_extensions()), 1)  # the firing exists, and the history leaves it out
        self.assertEqual(self.entries(), [("x", "human", f"overrun: picked 1: {d['options'][0]}")])


class ApproveTests(Loop):
    def approvals(self, **kw):
        return json.loads(self.go("status", "--json", "--cached", **kw)[1])["approvals"]

    def lander_stop(self, head, kind="stale"):
        factory.write_lander(SimpleNamespace(root=ROOT), {**factory.read_lander(SimpleNamespace(root=ROOT)),
                             "stopped": {"1": {"head": head, "kind": kind, "text": "its head changed after the approval", "ts": iso(NOW - 600)}}})

    def test_a_handed_over_ready_unapproved_pull_request_is_a_new_item_since_the_hand_over(self):
        self.assertEqual(self.approvals(), {"as_of": None, "items": []})
        self.sim.add(1, approved=False, behind=False)
        self.go("watch")
        self.assertEqual(self.approvals(), {"as_of": iso(NOW), "items": [
            {"slug": "t1", "number": 1, "url": "https://x/pull/1", "why": "new", "since": iso(NOW - 3600)}]})

    def test_a_draft_is_no_item(self):
        self.sim.add(1, approved=False, draft=True, behind=False)
        self.go("watch")
        self.assertEqual(self.approvals()["items"], [])

    def test_a_task_back_at_work_drops_out_of_status_without_a_new_tick(self):
        self.sim.add(1, approved=False, behind=False)
        self.go("watch")
        self.assertEqual(len(self.approvals()["items"]), 1)
        self.assertEqual(self.approvals(state="working")["items"], [])

    def test_a_stop_at_the_current_head_is_a_stale_item_with_its_text(self):
        self.sim.add(1, behind=False)
        self.lander_stop("h1")
        self.go("watch")
        self.assertEqual(self.approvals()["items"], [{"slug": "t1", "number": 1, "url": "https://x/pull/1", "why": "stale",
                                                      "text": "its head changed after the approval", "since": iso(NOW - 600)}])

    def test_a_stop_at_an_older_head_is_no_item_while_the_pull_request_is_open(self):
        self.sim.add(1, behind=False)
        self.lander_stop("h0")
        with mock.patch.object(factory, "drain"):  # the drain would merge it or drop the stop: only the tick's own write is under test
            self.go("watch")
        self.assertEqual(self.sim.open(), [1])
        self.assertEqual(self.approvals()["items"], [])

    def test_an_approved_pull_request_with_no_stop_is_none(self):
        self.sim.add(1, behind=False)
        self.go("watch")
        self.assertEqual(self.approvals()["items"], [])


class ReportStopTests(Harness):
    def stop(self, kind, line):
        repo = SimpleNamespace(root=ROOT, tasks=[{"path": f"{WT}/t1", "branch": "worktree-t1"}])
        pr = {"number": 1, "headRefName": "worktree-t1", "headRefOid": "h1"}
        told, notified = [], []
        with mock.patch.object(factory, "forward", lambda *a, **k: told.append(a) or "dispatcher"), \
                mock.patch.object(factory, "notify", lambda *a: notified.append(a)), \
                mock.patch.object(factory, "gh_step", lambda *a: None), mock.patch("sys.stdout"):
            factory.report_stop(repo, "disp", pr, kind, line)
        return told, notified, [r["told"] for r in self.records("queue")]

    def test_a_stale_or_unapproved_stop_goes_to_the_inbox_and_nobody_is_told(self):
        for kind in ("stale", "unapproved"):
            with self.subTest(kind):
                told, notified, queue = self.stop(kind, "needs a fresh approval")
                self.assertEqual((told, notified, queue[-1]), ([], [], "inbox"))
        self.assertEqual({r["reason"] for r in self.records() if r.get("decision") == "escalated"}, {"reported by the lander to the inbox"})

    def test_another_stop_is_still_a_lander_line_to_the_dispatcher(self):
        told, _, queue = self.stop("no-ci", "no check is reported")
        self.assertEqual(([a[3:5] for a in told], queue), ([("LANDER", "#1 stopped no-ci: no check is reported")], ["dispatcher"]))
