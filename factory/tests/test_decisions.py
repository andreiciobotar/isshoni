"""A lead posts a decision for the human to the inbox; the human's pick reaches the lead through the outbox."""
import json
import os
from types import SimpleNamespace
from unittest import mock

from test_factory import ROOT, WT, factory
from test_health import HOUR, NOW, Harness, iso
from test_overrun import Overrun

ASK = "Ship the importer without retry, or add it now? Retry adds a day."
OPTIONS = ("Ship without retry", "Add retry now")


class Decisions(Harness):
    """A lead `demo-task`, declared blocked on the human; `ask` runs as that lead, `answer` as the human."""

    def setUp(self):
        super().setUp()
        self.seed("tasks", [{"repo": ROOT, "slug": "demo-task", "state": "blocked", "ts": iso(NOW - HOUR)}])

    def run_in(self, where, *argv):
        with mock.patch.dict(os.environ), mock.patch.object(os, "getcwd", return_value=where or ROOT):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            if where:
                os.environ["CLAUDE_PROJECT_DIR"] = where
            return self.cli(*argv, entries=["demo-task"])

    def as_lead(self, *argv):
        return self.run_in(f"{WT}/demo-task", "decision", *argv)

    def as_human(self, *argv):
        return self.run_in(None, *argv)

    def ask(self, text=ASK, options=OPTIONS, recommend=None):
        pick = ["--recommend", str(recommend)] if recommend else []
        return self.as_lead("ask", "--kind", "question", text, *[a for o in options for a in ("--option", o)], *pick)

    def posted(self):
        code, out, err = self.ask(recommend=2)
        self.assertEqual((code, err), (0, ""))
        return out.strip()

    def outbox(self):
        return [r["line"] for r in self.records("outbox") if "line" in r]


class AskTests(Decisions):
    def refused(self, run, reason):
        code, out, err = run
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.count("\n"), 1)
        self.assertIn(reason, err)
        self.assertEqual(self.records("decisions"), [])

    def test_outside_a_task_worktree_is_refused(self):
        self.refused(self.run_in(None, "decision", "ask", "--kind", "question", ASK, "--option", "a", "--option", "b"),
                     "inside a task worktree")

    def test_a_task_not_blocked_on_the_human_is_refused(self):
        for waits in ({"state": "working"}, {"state": "blocked", "waits_on": "dispatcher"}):
            self.seed("tasks", [{"repo": ROOT, "slug": "demo-task", "ts": iso(NOW - 60), **waits}])
            self.refused(self.ask(), "factory state blocked human")

    def test_the_limits_are_refused(self):
        self.refused(self.ask(" ".join(["word"] * 61)), "1 to 60 words")
        self.refused(self.ask(options=["one"]), "2 to 4 options")
        self.refused(self.ask(options=["a", "b", "c", "d", "e"]), "2 to 4 options")
        self.refused(self.ask(options=["fine", " ".join(["word"] * 16)]), "1 to 15 words")
        self.refused(self.ask(recommend=3), "--recommend")

    def test_the_limits_are_met_by_60_words_and_15_words(self):
        code, _, err = self.ask(" ".join(["word"] * 60), ["a", " ".join(["word"] * 15)])
        self.assertEqual((code, err), (0, ""))

    def test_an_ask_is_listed_by_list_and_by_status_with_the_fields(self):
        rid = self.posted()
        self.assertRegex(rid, r"^[0-9a-f]{12}$")
        want = {"id": rid, "slug": "demo-task", "kind": "question", "ask": ASK, "options": list(OPTIONS), "recommend": 2,
                "ts": iso(NOW)}
        self.assertEqual(json.loads(self.as_human("decision", "list", "--json")[1]), [want])
        self.assertEqual(json.loads(self.as_human("status", "--json", "--cached")[1])["decisions"], [want])
        text = self.as_human("decision", "list")[1]
        self.assertIn(f"{rid}  demo-task  question\n{ASK}\n  1  Ship without retry\n  2  Add retry now  (recommended)", text)

    def test_whitespace_collapses_and_recommend_is_absent_when_none(self):
        code, out, _ = self.ask("  Ship   it\n now? ", ["a  b", "c"])
        self.assertEqual(code, 0)
        [d] = json.loads(self.as_human("decision", "list", "--json")[1])
        self.assertEqual((d["ask"], d["options"], "recommend" in d), ("Ship it now?", ["a b", "c"], False))


class AnswerTests(Decisions):
    def test_an_answer_queues_the_line_for_the_lead_and_closes_the_decision(self):
        rid = self.posted()
        code, out, err = self.as_human("decision", "answer", rid, "2")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "answered demo-task: 2; the next tick delivers it\n")
        self.assertEqual(self.outbox(), [f"INBOX: demo-task: decision {rid}: the human picked 2: Add retry now"])
        [row] = [r for r in self.records("outbox") if "line" in r]
        self.assertEqual(row["slug"], "demo-task")
        self.assertEqual(json.loads(self.as_human("decision", "list", "--json")[1]), [])

    def test_the_answer_reaches_the_lead_with_its_outbox_id(self):
        rid = self.posted()
        self.as_human("decision", "answer", rid, "2")
        [row] = [r for r in self.records("outbox") if "line" in r]
        put = []
        with mock.patch.object(factory, "reach_lead", lambda repo, sessions, slug, lines: put.extend(lines) or (True, None)):
            factory.deliver(SimpleNamespace(root=ROOT), None, "demo-task")
        self.assertEqual(put, [f"INBOX: demo-task: decision {rid}: the human picked 2: Add retry now [outbox {row['id']}]"])

    def test_a_second_answer_and_a_pick_out_of_range_fail(self):
        rid = self.posted()
        for n in ("0", "3"):
            code, _, err = self.as_human("decision", "answer", rid, n)
            self.assertEqual((code, "1 to 2" in err), (2, True))
        self.assertEqual(self.as_human("decision", "answer", rid, "1")[0], 0)
        code, _, err = self.as_human("decision", "answer", rid, "2")
        self.assertEqual((code, "already answered" in err), (2, True))
        self.assertEqual(len(self.outbox()), 1)

    def test_an_unknown_id_fails(self):
        code, _, err = self.as_human("decision", "answer", "000000000000", "1")
        self.assertEqual((code, "not an open decision" in err), (2, True))

    def test_a_lead_never_answers(self):
        rid = self.posted()
        code, _, err = self.as_lead("answer", rid, "1")
        self.assertEqual((code, "only the human" in err), (2, True))
        self.assertEqual((self.outbox(), len(self.as_human("decision", "list", "--json")[1]) > 2), ([], True))


class CloseTests(Decisions):
    def test_a_later_state_closes_an_unanswered_decision_for_good(self):
        rid = self.posted()
        self.clock += 60
        self.run_in(f"{WT}/demo-task", "state", "working")
        self.assertEqual(json.loads(self.as_human("decision", "list", "--json")[1]), [])
        self.clock += 60
        self.run_in(f"{WT}/demo-task", "state", "blocked", "human")
        self.assertEqual(json.loads(self.as_human("decision", "list", "--json")[1]), [])
        self.assertEqual(self.as_human("decision", "answer", rid, "1")[0], 2)


class Delegation(Overrun):
    """A lead `x` blocked on the human with a posted decision; the human leaves it to the lead or the site manager."""

    def setUp(self):
        super().setUp()
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "blocked", "ts": iso(NOW - 60)}])
        self.rid = self.as_who(True, "decision", "ask", "--kind", "question", ASK, "--option", "a b", "--option", "c", "--recommend", "2")[1].strip()

    def as_who(self, lead, *argv):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            if lead:
                os.environ["CLAUDE_PROJECT_DIR"] = f"{WT}/x"
            return self.cli(*argv, cwd=f"{WT}/x" if lead else ROOT)

    def answer(self, what):
        return self.as_who(False, "decision", "answer", self.rid, what)

    def pending(self):
        return next(f["id"] for f in factory.fold(self.records())[0] if f["criterion"] == "decision" and f["decision"] is None)

    def listed(self):
        return json.loads(self.as_who(False, "decision", "list", "--json")[1])

    def outbox(self):
        return [r["line"] for r in self.records("outbox") if "line" in r]

    def answers(self):
        return [r for r in self.records("decisions") if "ask" not in r]

    def firing(self):
        return next(f for f in factory.fold(self.records())[0] if f["criterion"] == "decision")


class DelegateTests(Delegation):
    def test_lead_closes_the_decision_and_queues_the_line(self):
        code, out, err = self.answer("lead")
        self.assertEqual((code, err, out), (0, "", "answered x: left to the lead; the next tick delivers it\n"))
        self.assertEqual(self.answers(), [{"ts": iso(NOW), "repo": ROOT, "id": self.rid, "slug": "x", "choice": "lead"}])
        self.assertEqual(self.outbox(), [f"INBOX: x: decision {self.rid}: the human leaves it to you: decide it yourself, "
                                         "record it with factory feedback decide, and carry on"])
        self.assertEqual(self.listed(), [])
        for what in ("1", "lead", "site-manager"):
            self.assertIn("already answered", self.answer(what)[2])

    def test_site_manager_fires_a_firing_and_parks_the_decision(self):
        code, out, err = self.answer("site-manager")
        self.assertEqual((code, err, out), (0, "", "answered x: left to the site manager; it decides on the next tick\n"))
        self.assertEqual(self.answers(), [{"ts": iso(NOW), "repo": ROOT, "id": self.rid, "slug": "x", "choice": "site-manager"}])
        f = self.firing()
        self.assertEqual((f["criterion"], f["slug"], f["subject"], f["detail"]),
                         ("decision", "x", self.rid, f"question: {ASK} Options: 1. a b 2. c. The lead recommends 2."))
        self.assertEqual(self.outbox(), [])
        [d] = self.listed()
        self.assertEqual((d["with"], d["with_since"], "returned" in d), ("site-manager", iso(NOW), False))
        self.assertEqual(json.loads(self.as_who(False, "status", "--json", "--cached")[1])["decisions"], [d])
        code, _, err = self.answer("1")
        self.assertEqual((code, err.strip()), (2, f"factory: decision answer: {self.rid} is with the site manager"))
        self.assertIn("is with the site manager", self.answer("lead")[2])
        self.assertEqual(len(self.answers()), 1)

    def test_a_pending_decision_ask_does_not_block_overrun_nor_count_in_its_duplicate_check(self):
        self.assertEqual(self.answer("site-manager")[0], 0)
        self.assertEqual(self.pending(), self.firing()["id"])
        self.assertEqual(self.ask()[0], 0)
        self.assertEqual(sorted(f["criterion"] for f in factory.fold(self.records())[0] if f["decision"] is None), ["decision", "overrun"])
        code, _, err = self.ask()
        self.assertEqual((code, "an ask for x is pending" in err), (1, True))

    def test_a_pick_outside_the_options_names_the_choices(self):
        for what in ("0", "3", "later", "-1"):
            code, _, err = self.answer(what)
            self.assertEqual((code, err.strip()), (2, "factory: decision answer: the pick is 1 to 2, lead or site-manager"))
        self.assertEqual(self.answers(), [])

    def test_a_kind_outside_delegable_is_the_humans_alone(self):
        self.seed("decisions", [{"ts": iso(NOW), "repo": ROOT, "id": "ap1", "slug": "x", "kind": "approval", "ask": "Merge it?",
                                 "options": ["yes", "no"]}])
        for what in ("lead", "site-manager"):
            code, _, err = self.as_who(False, "decision", "answer", "ap1", what)
            self.assertEqual((code, err.strip()), (2, "factory: decision answer: a decision of kind approval is the human's alone"))
        self.assertEqual((self.outbox(), len(self.answers())), ([], 0))
        self.assertEqual(self.as_who(False, "decision", "answer", "ap1", "1")[0], 0)

    def test_an_overrun_card_is_the_humans_alone(self):
        self.seed("decisions", [{"ts": iso(NOW), "repo": ROOT, "id": "ov1", "slug": "x", "kind": "overrun", "ask": "Over its limit.",
                                 "options": ["Continue", "Cut", "Split", "Abandon"]}])
        for what in ("lead", "site-manager"):
            code, _, err = self.as_who(False, "decision", "answer", "ov1", what)
            self.assertEqual((code, err.strip()), (2, "factory: decision answer: a decision of kind overrun is the human's alone"))
        self.assertEqual((self.outbox(), len(self.answers())), ([], 0))

    def test_a_lead_never_delegates(self):
        for what in ("lead", "site-manager"):
            code, _, err = self.as_who(True, "decision", "answer", self.rid, what)
            self.assertEqual((code, "only the human" in err), (2, True))
        self.assertEqual((self.outbox(), self.answers()), ([], []))


class SiteManagerTests(Delegation):
    def setUp(self):
        super().setUp()
        self.answer("site-manager")

    def test_picked_records_the_pick_and_queues_the_line_for_the_lead(self):
        code, out, _ = self.watch({"decision": "picked", "option": 1, "reason": "the brief wants it small"})
        self.assertEqual(code, 0, out)
        [run] = self.runs()
        [sent] = json.loads(run[-1].partition("\n\n")[2])["firings"]
        self.assertEqual((sent["criterion"], sent["slug"]), ("decision", "x"))
        self.assertIn("Options: 1. a b 2. c.", sent["detail"])
        self.assertEqual(self.answers()[-1], {"ts": iso(NOW), "repo": ROOT, "id": self.rid, "slug": "x", "choice": 1,
                                              "by": "site-manager", "reason": "the brief wants it small"})
        self.assertEqual(self.outbox(), [f"INBOX: x: decision {self.rid}: the site manager picked 1 for the human: a b. the brief wants it small"])
        self.assertEqual(self.listed(), [])
        [d] = [r for r in self.records() if r.get("decision") == "picked"]
        self.assertEqual((d["option"], d["by"]), (1, "site-manager"))
        self.assertTrue(self.firing()["cleared"])
        self.watch()
        self.assertEqual(self.runs(), [])

    def test_a_pick_out_of_range_or_not_an_integer_is_skipped_and_the_watch_returns_the_decision(self):
        for option in (0, 3, "1", True, None):
            with self.subTest(option=option):
                self.setUp()
                self.watch({"decision": "picked", "option": option, "reason": "r"})
                self.assertEqual([r["choice"] for r in self.answers() if "choice" in r], ["site-manager"])
                self.assertEqual(self.outbox(), [])
                self.assertEqual((self.firing()["decision"], self.firing()["by"]), ("returned", "watch"))

    def test_the_other_words_are_skipped_for_a_decision(self):
        for word in ("extended", "escalated", "suppressed", "paused", "told", "closed"):
            with self.subTest(word=word):
                self.setUp()
                self.watch({"decision": word, "minutes": 5, "waits_on": ["human"], "to": "dispatcher", "reason": "r"})
                self.assertEqual([r["choice"] for r in self.answers() if "choice" in r], ["site-manager"])
                self.assertEqual(self.firing()["by"], "watch")

    def test_returned_opens_it_for_the_human_again_and_messages_nobody(self):
        self.watch({"decision": "returned", "reason": "it turns on a taste call"})
        self.assertEqual(self.answers()[-1], {"ts": iso(NOW), "repo": ROOT, "id": self.rid, "slug": "x",
                                              "returned": "it turns on a taste call", "by": "site-manager"})
        [d] = self.listed()
        self.assertEqual((d["returned"], "with" in d, "with_since" in d), ("it turns on a taste call", False, False))
        self.assertEqual((self.outbox(), self.lines()), ([], []))
        self.assertEqual(self.answer("2")[0], 0)
        self.assertEqual(self.outbox(), [f"INBOX: x: decision {self.rid}: the human picked 2: c"])

    def test_a_returned_decision_can_be_left_to_the_site_manager_again(self):
        self.watch({"decision": "returned", "reason": "unsure"})
        self.clock += 60
        self.assertEqual(self.answer("site-manager")[0], 0)
        [d] = self.listed()
        self.assertEqual((d["with"], d["with_since"], "returned" in d), ("site-manager", iso(NOW + 60), False))

    def test_a_decision_left_undecided_is_returned_by_the_watch_once(self):
        self.watch()
        why = "the site manager did not decide: its decision was missing or not valid"
        self.assertEqual((self.firing()["decision"], self.firing()["by"], self.firing()["reason"], self.firing()["cleared"]),
                         ("returned", "watch", why, True))
        self.assertEqual(self.answers()[-1], {"ts": iso(NOW), "repo": ROOT, "id": self.rid, "slug": "x", "by": "watch", "returned": why})
        [d] = self.listed()
        self.assertNotIn("with", d)
        self.assertEqual((self.outbox(), self.lines()), ([], []))
        self.watch()
        self.assertEqual(self.runs(), [])

    def test_a_failed_run_returns_the_decision(self):
        self.assertEqual(self.watch(fail=True)[0], 1)
        self.assertEqual((self.firing()["decision"], "the site manager run failed" in self.firing()["reason"]), ("returned", True))
        self.assertIn("returned", self.listed()[0])

    def test_a_lead_that_declared_working_before_the_tick_makes_it_moot(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(NOW + 30)}])
        self.assertEqual(self.watch()[0], 0)
        self.assertEqual(self.runs(), [])
        f = self.firing()
        self.assertEqual((f["decision"], f["by"], f["cleared"]), ("suppressed", "watch", True))
        self.assertEqual((self.outbox(), self.listed(), len(self.answers())), ([], [], 1))


class OtherCriterionTests(Overrun):
    def test_picked_and_returned_are_skipped_for_an_overrun_ask(self):
        for word in ({"decision": "picked", "option": 1}, {"decision": "returned"}):
            with self.subTest(word=word["decision"]):
                self.setUp()
                self.ask()
                self.watch({**word, "reason": "r"})
                self.assertEqual([(d["decision"], d["by"]) for d in self.decisions()], [("extended", "watch")])
                self.assertEqual([r for r in self.records("decisions") if "choice" in r or "returned" in r], [])
