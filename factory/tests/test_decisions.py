"""A lead posts a decision for the human to the inbox; the human's pick reaches the lead through the outbox."""
import json
import os
from unittest import mock

from test_factory import ROOT, WT, factory
from test_health import HOUR, NOW, Harness, iso

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
