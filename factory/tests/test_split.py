"""A lead offers part of its work as a split; the human answers it, or a standing answer does; the watch tick starts it."""
import json
from unittest import mock

from test_decisions import Decisions
from test_factory import ROOT, WT, factory, task_entry
from test_health import HOUR, NOW, iso

ASK = "The importer's retry is separable: it needs the queue change first."
BRIEF = "Add retry to the importer.\nExpected size: Small"
LAUNCH = {("claude", "--bg"): (0, "backgrounded · 96dc4869 · demo-retry\n", ""), ("git", "ls-remote"): (2, "", "")}
FAILING = {**LAUNCH, ("claude", "--bg"): (1, "", "no luck")}
OTHER = "/other"


class Splits(Decisions):
    """The parent `demo-task` is blocked on the human, so a question can be asked too; a split declares no state and ignores it."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(factory, "copy_commit", return_value=None), mock.patch.object(factory, "task_event")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def split(self, new="demo-retry", ask=ASK, *extra, brief=BRIEF, entries=("demo-task",), answers=None):
        path = self.home / f"{new}.md"
        if brief is not None:
            path.write_text(brief)
        with mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": f"{WT}/demo-task"}), mock.patch.object(factory.os, "getcwd", return_value=f"{WT}/demo-task"):
            return self.cli("split", new, "--brief-file", str(path), *extra, ask, entries=entries, answers=answers)

    def posted_split(self, **kw):
        code, out, err = self.split(**kw)
        self.assertEqual((code, err), (0, ""))
        return out.strip()

    def tick(self, settings=None, answers=LAUNCH):
        if settings is not None:
            (self.store / "settings.toml").write_text(settings)
        return self.cli("watch", entries=["demo-task"], answers=answers)

    def listed(self):
        return json.loads(self.as_human("decision", "list", "--json")[1])

    def standing(self):
        return {s["kind"]: s for s in json.loads(self.as_human("status", "--json", "--cached")[1])["standing"]}

    def started(self):
        return [r["started"] for r in self.records("decisions") if "started" in r]

    def launched(self):
        return [c for c in self.fake.calls if c[:2] == ["claude", "--bg"]]

    def always_records(self):
        return [(r["kind"], r["always"], r["by"]) for r in self.records("decisions") if "always" in r]


class SplitCommandTests(Splits):
    def test_it_logs_the_record_prints_the_id_and_lists_the_card_without_the_brief(self):
        rid = self.posted_split(ask="  The importer's retry   is separable:\nit needs the queue change first. ")
        self.assertRegex(rid, r"^[0-9a-f]{12}$")
        [r] = [r for r in self.records("decisions") if "ask" in r]
        self.assertEqual({k: r[k] for k in ("id", "slug", "role", "kind", "ask", "options", "new", "brief")},
                         {"id": rid, "slug": "demo-task", "role": "lead", "kind": "split", "ask": ASK,
                          "options": ["Start demo-retry", "Do not start it"], "new": "demo-retry", "brief": BRIEF})
        self.assertNotIn("base", r)
        self.assertEqual(self.listed(), [{"id": rid, "slug": "demo-task", "kind": "split", "ask": ASK, "ts": iso(NOW),
                                          "options": ["Start demo-retry", "Do not start it"], "always": 1}])

    def test_it_asks_neither_github_nor_the_session_list(self):
        self.posted_split()
        self.split("demo-two")
        self.assertEqual({c[0] for c in self.fake.calls}, {"git"})

    def test_the_base_is_recorded_and_must_be_an_open_task(self):
        code, _, err = self.split("demo-stack", ASK, "--base", "demo-gone")
        self.assertEqual((code, err), (2, "factory: --base demo-gone is not an open task\n"))
        self.assertEqual(self.split("demo-stack", ASK, "--base", "demo-task")[0], 0)
        self.assertEqual([r["base"] for r in self.records("decisions") if "base" in r], ["demo-task"])

    def refused(self, run, reason):
        code, out, err = run
        self.assertEqual((code, out, err.count("\n"), reason in err), (2, "", 1, True), err)
        self.assertEqual([r for r in self.records("decisions") if "ask" in r], [])

    def test_each_refusal_is_one_line_and_logs_nothing(self):
        self.refused(self.split("Bad_Slug"), "invalid slug")
        self.refused(self.split(brief=""), "the brief is empty")
        self.refused(self.split(brief=None, new="demo-nobrief"), "cannot read brief file")
        self.refused(self.split(ask=" "), "the ask is 1 to 60 words")
        self.refused(self.split(ask=" ".join(["word"] * 61)), "the ask is 1 to 60 words")
        self.refused(self.split(entries=("demo-task", "demo-retry")), "worktree already exists")
        self.refused(self.split(answers={("git", "rev-parse"): (0, "abc\n", "")}), "branch already exists: worktree-demo-retry")

    def test_outside_a_task_worktree_is_refused(self):
        path = self.home / "b.md"
        path.write_text(BRIEF)
        code, _, err = self.as_human("split", "demo-retry", "--brief-file", str(path), ASK)
        self.assertEqual((code, "inside a task worktree" in err), (2, True))

    def test_a_second_split_of_a_slug_is_refused_while_the_first_is_pending_and_accepted_once_declined(self):
        rid = self.posted_split()
        self.refused_again()
        self.as_human("decision", "answer", rid, "1")
        self.refused_again()
        self.assertEqual(self.as_human("decision", "answer", rid, "2")[0], 0)
        self.assertEqual(self.split()[0], 0)

    def refused_again(self):
        code, _, err = self.split()
        self.assertEqual((code, "neither started nor declined" in err), (2, True))

    def test_a_started_split_frees_the_slug_for_the_check_but_the_worktree_check_then_holds(self):
        rid = self.posted_split()
        self.as_human("decision", "answer", rid, "1")
        self.tick()
        self.assertEqual(self.started(), ["demo-retry"])
        self.assertEqual(self.split(entries=("demo-task", "demo-retry"))[0], 2)


class OpenRuleTests(Splits):
    def test_a_split_is_open_with_the_parent_working_and_gone_after_answer_2_with_the_parent_told(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "demo-task", "state": "working", "ts": iso(NOW)}])
        rid = self.posted_split()
        self.assertEqual([d["id"] for d in self.listed()], [rid])
        self.assertEqual(self.as_human("decision", "answer", rid, "2")[0], 0)
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.outbox(), ["INBOX: demo-task: split demo-retry declined by the human: it is not started"])

    def test_after_answer_1_it_is_picked_and_waits_answer_2_is_accepted_and_answer_1_is_refused(self):
        rid = self.posted_split()
        code, out, _ = self.as_human("decision", "answer", rid, "1")
        self.assertEqual((code, out), (0, "answered demo-task: 1; the next tick delivers it\n"))
        self.assertEqual(self.outbox(), [])
        [d] = self.listed()
        self.assertEqual((d["id"], d["picked"]), (rid, 1))
        code, _, err = self.as_human("decision", "answer", rid, "1")
        self.assertEqual((code, "already answered" in err), (2, True))
        self.assertEqual(self.as_human("decision", "answer", rid, "2")[0], 0)
        self.assertEqual((self.listed(), len(self.outbox())), ([], 1))

    def test_a_split_is_never_left_to_the_lead_or_the_site_manager(self):
        rid = self.posted_split()
        for to in ("lead", "site-manager"):
            self.assertEqual(self.as_human("decision", "answer", rid, to)[2], "factory: decision answer: a decision of kind split is the human's alone\n")
        self.assertEqual([d["id"] for d in self.listed()], [rid])

    def test_a_started_split_is_gone(self):
        rid = self.posted_split()
        self.as_human("decision", "answer", rid, "1")
        self.tick()
        self.assertEqual((self.listed(), self.started()), ([], ["demo-retry"]))

    def test_a_split_stays_open_when_its_parent_declares_another_state(self):
        rid = self.posted_split()
        self.seed("tasks", [{"repo": ROOT, "slug": "demo-task", "state": "handed-over", "ts": iso(NOW + 5)}])
        self.assertEqual([d["id"] for d in self.listed()], [rid])


class TickTests(Splits):
    def test_an_unanswered_split_is_answered_by_standing_started_and_the_parent_told(self):
        rid = self.posted_split()
        code, out, err = self.tick()
        self.assertEqual(code, 0, err)
        [answer] = [r for r in self.records("decisions") if "choice" in r]
        self.assertEqual((answer["id"], answer["choice"], answer["by"]), (rid, 1, "standing"))
        [launch] = self.launched()
        self.assertIn(BRIEF, launch[-1])
        self.assertIn("worktree-demo-retry", " ".join(launch) + " ".join(" ".join(c) for c in self.fake.calls))
        [done] = [r for r in self.records("decisions") if "started" in r]
        self.assertEqual((done["id"], done["slug"], done["started"]), (rid, "demo-task", "demo-retry"))
        self.assertEqual(self.outbox(), ["INBOX: demo-task: split demo-retry started"])
        self.assertEqual(self.listed(), [])
        self.assertEqual(json.loads(factory.repo_state(ROOT, "splits", "json").read_text())["waits"], {})

    def test_at_the_cap_it_is_answered_not_started_and_the_wait_is_the_note_until_a_slot_frees(self):
        rid = self.posted_split()
        self.tick('[limits]\nworktree_cap = 1\nreserve_percent = 0\n')
        self.assertEqual((self.launched(), self.started()), ([], []))
        wait = json.loads(factory.repo_state(ROOT, "splits", "json").read_text())["waits"][rid]
        self.assertEqual(wait, "at the cap of 1 tasks in motion: demo-task (blocked)")
        [d] = self.listed()
        self.assertEqual((d["picked"], d["note"]), (1, f"starts by itself when it can: {wait} (always: start it)"))
        self.assertEqual(self.outbox(), [])
        self.tick('[limits]\nworktree_cap = 5\n')
        self.assertEqual((len(self.launched()), self.started(), self.listed()), (1, ["demo-retry"], []))
        self.assertEqual(json.loads(factory.repo_state(ROOT, "splits", "json").read_text())["waits"], {})

    def test_the_tick_start_holds_the_reserve_as_factory_new_does(self):
        self.posted_split()
        self.tick('[limits]\nworktree_cap = 3\nreserve_percent = 34\n')
        [d] = self.listed()
        _, _, err = self.cli("new", "demo-retry", "--brief", "b", entries=["demo-task"], answers=LAUNCH)
        self.assertEqual(err.strip(), "factory: " + d["note"].removeprefix("starts by itself when it can: ").removesuffix(" (always: start it)"))
        self.assertIn("held in reserve for critical work", err)
        self.assertEqual(self.launched(), [])

    def test_a_split_of_a_closed_parent_is_not_answered_and_the_note_says_so(self):
        rid = self.posted_split()
        self.seed("tasks", [{"repo": ROOT, "slug": "demo-task", "state": "closed", "ts": iso(NOW + 5)}])
        self.tick()
        [d] = self.listed()
        self.assertEqual((d["id"], "picked" in d, d["note"]), (rid, False, "its parent demo-task was closed: start it or not is yours"))
        self.assertEqual([r for r in self.records("decisions") if "choice" in r], [])
        self.assertEqual(self.as_human("decision", "answer", rid, "1")[0], 0)
        self.tick()
        self.assertEqual(self.started(), ["demo-retry"])

    def test_with_standing_off_the_card_stays_and_the_humans_answer_starts_it_by_the_human(self):
        self.assertEqual(self.as_human("decision", "always", "split", "off")[1], "split: asks again from now\n")
        rid = self.posted_split()
        self.tick()
        self.assertEqual((len(self.listed()), self.started()), (1, []))
        self.assertEqual(self.as_human("decision", "answer", rid, "1")[0], 0)
        self.tick()
        self.assertEqual(self.started(), ["demo-retry"])
        started = [e for e in self.history() if e["slug"] == "demo-retry"]
        self.assertEqual([e["by"] for e in started], ["human"])

    def history(self):
        return json.loads(self.as_human("status", "--json", "--cached")[1])["history"]

    def failed_start(self):
        patcher = mock.patch.object(factory, "start_task", wraps=factory.start_task)
        self.attempts = patcher.start()
        self.addCleanup(patcher.stop)
        rid = self.posted_split()
        for _ in range(3):
            self.tick(answers=FAILING)
        return rid

    def test_a_failed_start_is_tried_once_logged_and_the_card_is_open_again_with_the_reason(self):
        rid = self.failed_start()
        self.assertEqual(self.attempts.call_count, 1)
        [failed] = [r for r in self.records("decisions") if "failed" in r]
        self.assertEqual((failed["id"], failed["slug"], failed["failed"]), (rid, "demo-task", "claude did not start the session"))
        [d] = self.listed()
        self.assertEqual((d["id"], "picked" in d, d["note"]),
                         (rid, False, "the start failed: claude did not start the session; pick Start to try again"))
        self.assertEqual((self.started(), self.outbox()), ([], []))
        self.assertIn(("demo-retry", "script", "start from a split of demo-task failed: claude did not start the session"),
                      [(e["slug"], e["by"], e["text"]) for e in self.history()])

    def test_the_humans_answer_after_a_failed_start_gives_exactly_one_more_attempt(self):
        rid = self.failed_start()
        self.assertEqual(self.as_human("decision", "answer", rid, "1")[0], 0)
        for _ in range(3):
            self.tick(answers=FAILING)
        self.assertEqual(self.attempts.call_count, 2)
        self.assertEqual(len([r for r in self.records("decisions") if "failed" in r]), 2)
        self.assertEqual(self.as_human("decision", "answer", rid, "1")[0], 0)
        self.tick()
        self.assertEqual((self.attempts.call_count, self.started()), (3, ["demo-retry"]))

    def test_a_refusal_building_the_launch_argv_is_a_failed_start_not_a_wait(self):
        rid = self.posted_split()
        with mock.patch.object(factory, "build_launch_argv", side_effect=factory.Fail("no such agent definition")):
            self.tick()
        [failed] = [r for r in self.records("decisions") if "failed" in r]
        self.assertEqual((failed["id"], failed["failed"]), (rid, "no such agent definition"))
        self.assertEqual(json.loads(factory.repo_state(ROOT, "splits", "json").read_text())["waits"], {})

    def test_no_dispatcher_is_a_wait(self):
        rid = self.posted_split()
        self.as_human("decision", "answer", rid, "1")
        self.cli("watch", entries=["demo-task"], answers=LAUNCH, sessions=())
        self.assertRegex(json.loads(factory.repo_state(ROOT, "splits", "json").read_text())["waits"][rid], r"^no dispatcher session: ")
        self.assertEqual(self.started(), [])

    def test_a_split_that_waits_does_not_hold_back_the_next_one(self):
        first = self.posted_split()
        second = self.posted_split(new="demo-two")
        self.as_human("decision", "answer", first, "1")
        self.as_human("decision", "answer", second, "1")
        with mock.patch.object(factory, "start_task", side_effect=[factory.Fail("at the cap"), None]):
            self.tick()
        self.assertEqual(self.started(), ["demo-two"])
        self.assertEqual(json.loads(factory.repo_state(ROOT, "splits", "json").read_text())["waits"], {first: "at the cap"})


class AlwaysTests(Splits):
    def test_always_is_refused_for_a_question_and_for_a_splits_second_option(self):
        rid = self.posted()
        code, _, err = self.as_human("decision", "answer", rid, "1", "--always")
        self.assertEqual((code, "a question decision has none" in err), (2, True))
        sid = self.posted_split()
        code, _, err = self.as_human("decision", "answer", sid, "2", "--always")
        self.assertEqual((code, "a split decision has 1" in err), (2, True))
        self.assertEqual((self.always_records(), [r for r in self.records("decisions") if "choice" in r]), ([], []))

    def test_always_is_refused_for_a_lead_or_site_manager_answer(self):
        code, _, err = self.as_human("decision", "answer", self.posted(), "lead", "--always")
        self.assertEqual((code, "decision answer: --always goes with an option number" in err), (2, True))
        self.assertEqual((self.always_records(), [r for r in self.records("decisions") if "choice" in r]), ([], []))

    def test_always_on_a_split_answers_with_the_flag_and_logs_it(self):
        sid = self.posted_split()
        self.assertEqual(self.as_human("decision", "answer", sid, "1", "--always")[0], 0)
        self.assertEqual(self.always_records(), [("split", True, "human")])

    def test_uses_count_across_repositories(self):
        self.seed("decisions", [{"ts": iso(NOW - 1), "repo": OTHER, "id": "s90", "slug": "demo-far", "kind": "split", "ask": ASK,
                                 "options": ["Start x", "Do not start it"], "new": "x", "brief": BRIEF},
                                {"ts": iso(NOW), "repo": OTHER, "id": "s90", "slug": "demo-far", "choice": 1, "by": "standing"}])
        self.assertEqual(self.standing()["split"]["uses"], 1)

    def test_always_is_refused_in_a_task_worktree_and_for_a_kind_without_one(self):
        code, _, err = self.as_lead("always", "split", "off")
        self.assertEqual((code, "only the human" in err, self.always_records()), (2, True, []))
        code, _, err = self.as_human("decision", "always", "question", "on")
        self.assertEqual((code, "no standing answer" in err), (2, True))

    def test_status_carries_the_standing_list_in_table_order(self):
        self.assertEqual(list(self.standing().values()), [{"kind": "split", "label": "start it", "on": True, "uses": 0}])
        self.assertEqual([s["kind"] for s in json.loads(self.as_human("status", "--json")[1])["standing"]], ["split"])


class HistoryTests(Splits):
    def entries(self):
        return [(e["slug"], e["by"], e["text"]) for e in json.loads(self.as_human("status", "--json", "--cached")[1])["history"]]

    def test_a_started_split_is_one_entry_with_the_undo_and_answer_1_is_none(self):
        self.posted_split()
        self.tick()
        self.assertEqual(self.entries(), [("demo-retry", "standing",
                                           "started from a split of demo-task; undo: factory close demo-retry --reason '<why>'")])

    def test_a_declined_split_is_an_entry_by_the_human(self):
        rid = self.posted_split()
        self.as_human("decision", "answer", rid, "2")
        self.assertEqual(self.entries(), [("demo-task", "human", "split: picked 2: Do not start it")])

    def test_the_always_records_are_entries(self):
        self.as_human("decision", "always", "split", "on")
        self.assertEqual(self.entries(), [("", "human", "always start it on split")])
        self.as_human("decision", "always", "split", "off")
        self.assertIn(("", "human", "asks again on split"), self.entries())

    def test_a_non_numeric_answer_is_its_own_entry_beside_a_split_and_breaks_neither(self):
        rid = self.posted()
        self.seed("decisions", [{"ts": iso(NOW), "repo": ROOT, "id": rid, "slug": "demo-task", "choice": "lead"}])
        split = self.posted_split()
        self.assertEqual(self.entries(), [("demo-task", "human", "question: left to the lead")])
        self.assertEqual([d["id"] for d in self.listed()], [split])
        self.assertEqual(self.as_human("decision", "answer", split, "2")[0], 0)
        self.assertEqual(self.listed(), [])
        self.assertIn(("demo-task", "human", "split: picked 2: Do not start it"), self.entries())
