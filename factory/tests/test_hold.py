"""A task stacked on a base that is still changing is held: no build run starts, a `hold` card mirrors it in the inbox."""
import json
import os
import re
from types import SimpleNamespace
from unittest import mock

from test_factory import ROOT, WT, factory
from test_health import COURIER_LINE, HOUR, NOW, Harness, fired, iso, lead, lead_prompt, reply
from test_overrun import DISPATCHER_NAME

CARD_OPTION = "Release: build now on alp as it stands"


class Hold(Harness):
    """Tasks `alp` and `bet` (stacked on `alp`), both working, in the dispatcher's repository, between two full passes."""

    STACK = {"bet": "alp"}
    ENTRIES = ["alp", "bet"]

    def setUp(self):
        super().setUp()
        factory.write_lander(SimpleNamespace(root=ROOT), {"health": NOW})
        self.entries, self.stack, self.merged, self.told = list(self.ENTRIES), dict(self.STACK), set(), []
        self.started = NOW - 30 * 60
        for slug in self.entries:
            self.declare(slug, "working", NOW - HOUR)

    def declare(self, slug, state, ts):
        self.seed("tasks", [{"repo": ROOT, "slug": slug, "role": "lead", "state": state, "ts": iso(ts)}])

    def move(self, slug, state):
        """The lead of `slug` declares `state`, a minute after the last thing that happened."""
        self.clock += 60
        self.declare(slug, state, self.clock)

    def cli(self, *argv, cwd=ROOT, **kw):
        answers = {("git", "config", "--get", f"branch.worktree-{s}.factoryBase"): (0, f"worktree-{b}\n", "") for s, b in self.stack.items()}
        answers |= {("git", "merge-base", "--is-ancestor", f"worktree-{s}"): (0, "", "") for s in self.merged}
        sessions = [DISPATCHER_NAME, *(lead(s, pid=4242 + i, startedAt=self.started * 1000) for i, s in enumerate(self.entries))]
        with mock.patch.object(os, "getcwd", return_value=cwd):
            return super().cli(*argv, **{"sessions": sessions, "entries": self.entries, "answers": answers | kw.pop("answers", {}), **kw})

    def tick(self, **kw):
        def take():
            call = self.fake.calls[-1]
            if "--json-schema" in call:
                return reply()
            if "session named bet. " in call[-1]:
                lead_prompt(COURIER_LINE.search(call[-1])[1], cwd=f"{WT}/bet")
            return 0, json.dumps({"result": "sent"}), ""
        code, out, err = self.cli("watch", answers={("claude", "-p"): take}, **kw)
        self.assertEqual(code, 0, out + err)
        self.told += [re.sub(r" \[outbox [0-9a-f]{12}\]$", "", COURIER_LINE.search(c[-1])[1]) for c in self.claude_runs() if "--json-schema" not in c]

    def lines(self):
        """Every line the ticks so far sent to a lead."""
        return self.told

    def cards(self):
        return [r for r in self.records("decisions") if r.get("kind") == "hold" and "ask" in r]

    def released(self):
        return [r["released"] for r in self.records("decisions") if "released" in r]

    def build(self, slug="bet", *flags):
        """`factory build`: (code, stderr, the runs it spawned)."""
        spawned = []
        with mock.patch.object(factory, "tech_lead_missing", lambda repo, slug: None), \
                mock.patch.object(factory, "spawn", lambda argv, *a: spawned.append(argv)), \
                mock.patch.object(factory, "build_locked", lambda *a: 0):
            code, _, err = self.cli("build", slug, *flags, cwd=f"{WT}/{slug}")
        return code, err, spawned

    def answer(self, rid, n):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            return self.cli("decision", "answer", rid, str(n))

    def status_row(self, slug="bet", **kw):
        rows = json.loads(self.cli("status", "--json", **kw)[1])["rows"]
        return next(r for r in rows if r["slug"] == slug)


class GateTests(Hold):
    def test_a_task_on_a_working_or_blocked_base_starts_no_build_run(self):
        for state in ("working", "blocked"):
            self.move("alp", state)
            code, err, spawned = self.build()
            self.assertEqual((code, spawned), (1, []), state)
            self.assertIn(f"held: base alp is {state}; a build run starts once it hands over, or the human releases bet in the inbox", err)
            self.assertIn("SITE MANAGER: bet or INBOX: bet", err)

    def test_a_base_that_handed_over_lets_the_run_start(self):
        self.move("alp", "handed-over")
        code, err, spawned = self.build()
        self.assertEqual((code, len(spawned)), (0, 1), err)

    def test_an_unstacked_task_is_never_held(self):
        code, err, spawned = self.build("alp")
        self.assertEqual((code, len(spawned)), (0, 1), err)

    def test_the_run_half_is_not_refused(self):
        code, err, spawned = self.build("bet", "--run")
        self.assertEqual((code, spawned), (0, []), err)

    def test_a_pause_is_reported_before_a_hold(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "bet", "state": "blocked", "paused": True, "waits_on": ["human"], "role": "site-manager",
                             "ts": iso(NOW)}])
        self.assertIn("paused:", self.build()[1])

    def test_a_base_that_handed_over_and_merged_holds_nothing_when_it_works_again(self):
        self.move("alp", "handed-over")
        self.merged = {"alp"}
        self.move("alp", "working")
        self.assertEqual(self.build()[0], 0)

    def test_a_working_base_with_no_commit_of_its_own_holds(self):
        self.merged = {"alp"}  # its branch is at the default branch's tip
        code, err, spawned = self.build()
        self.assertEqual((code, spawned), (1, []))
        self.assertIn("held: base alp is working", err)
        self.tick()
        self.assertEqual([c["slug"] for c in self.cards()], ["bet"])

    def test_a_base_merged_into_the_default_branch_or_gone_holds_nothing(self):
        self.move("alp", "handed-over")
        self.merged = {"alp"}
        self.move("alp", "working")
        self.assertEqual(self.build()[0], 0)
        self.merged, self.entries = set(), ["bet"]
        self.assertEqual(self.build()[0], 0)


class CardTests(Hold):
    def test_a_tick_posts_one_card_with_one_option_and_not_a_second(self):
        self.tick()
        self.tick()
        [card] = self.cards()
        self.assertEqual((card["slug"], card["role"], card["base"], card["options"], "recommend" in card),
                         ("bet", "site-manager", "alp", [CARD_OPTION], False))
        self.assertEqual(card["ask"], "bet is held: its base alp is working, so no build run starts. It is released when alp hands over.")
        [d] = factory.open_decisions(ROOT)
        self.assertEqual((d["kind"], d["options"], "base" in d), ("hold", [CARD_OPTION], False))
        self.assertIn(card["id"], self.cli("decision", "list")[1])
        self.assertEqual([x["id"] for x in json.loads(self.cli("status", "--json")[1])["decisions"]], [card["id"]])
        self.assertEqual(self.lines(), [])  # nothing is sent: the lead learns from the gate

    def test_the_status_row_and_the_table_say_what_the_task_is_held_on(self):
        self.assertEqual(self.status_row()["stack_hold"], {"base": "alp", "since": None})
        self.tick()
        row = self.status_row()
        self.assertEqual((row["stack_hold"], row["held"]), ({"base": "alp", "since": iso(NOW)}, None))
        self.assertIsNone(self.status_row("alp")["stack_hold"])
        self.assertRegex(self.cli("status")[1], r"\bbet .*held on alp now\b")

    def test_a_hold_card_stays_open_while_the_task_is_blocked_and_closes_when_it_hands_over(self):
        self.tick()
        self.move("bet", "blocked")
        self.tick()
        self.assertEqual((len(factory.open_decisions(ROOT)), self.released()), (1, []))
        self.move("bet", "handed-over")
        self.tick()
        self.assertEqual((factory.open_decisions(ROOT), self.released(), self.lines()), ([], ["bet is handed-over"], []))


class DelegationTests(Hold):
    """A `choice` that is not an integer delegates the question; it is not a pick and releases nothing."""

    def test_a_string_choice_on_a_hold_card_keeps_the_task_held_and_the_card_open(self):
        for n, who in enumerate(("lead", "site-manager"), 1):
            self.tick()
            card = self.cards()[-1]
            self.seed("decisions", [{"ts": iso(self.clock), "repo": ROOT, "id": card["id"], "slug": "bet", "choice": who}])
            self.tick()
            self.assertEqual(self.build()[0], 1, who)
            self.assertEqual([d["id"] for d in factory.open_decisions(ROOT)], [card["id"]], who)
            self.assertEqual((len(self.cards()), self.released()), (n, ["x"] * (n - 1)), who)
            self.assertEqual(self.lines(), [], who)
            self.seed("decisions", [{"ts": iso(self.clock), "repo": ROOT, "id": card["id"], "slug": "bet", "released": "x"}])


class ReleaseTests(Hold):
    def test_a_base_that_hands_over_releases_the_card_and_tells_the_lead_once(self):
        self.tick()
        self.move("alp", "handed-over")
        self.tick()
        self.tick()
        self.assertEqual(factory.open_decisions(ROOT), [])
        self.assertEqual(self.released(), ["base alp handed over"])
        self.assertEqual(self.lines(), ["SITE MANAGER: bet: released: base alp handed over after 1 minutes held; your limit moved by them. Run factory build bet."])
        self.assertEqual(self.build()[0], 0)

    def test_a_base_merged_or_gone_releases_the_card(self):
        self.tick()
        self.move("alp", "handed-over")
        self.merged = {"alp"}
        self.move("alp", "working")
        self.tick()
        self.assertEqual(self.released(), ["base alp merged"])
        self.assertEqual(len(self.lines()), 1)
        self.merged = set()
        self.move("bet", "handed-over")
        self.move("bet", "working")
        self.tick()  # held again: alp is still working
        self.entries = ["bet"]
        self.tick()
        self.assertEqual(self.released(), ["base alp merged", "base alp is gone"])

    def test_a_base_in_any_other_state_releases_the_card_by_name(self):
        self.tick()
        self.move("alp", "acknowledged")
        self.tick()
        self.assertEqual(self.released(), ["base alp is acknowledged"])
        self.assertEqual(len(self.lines()), 1)

    def test_the_human_pick_closes_the_card_and_holds_nothing_until_the_base_hands_over_and_changes_again(self):
        self.tick()
        [card] = self.cards()
        code, out, err = self.answer(card["id"], 1)
        self.assertEqual(code, 0, err)
        self.assertEqual([r["line"] for r in self.records("outbox") if r.get("line", "").startswith("INBOX:")],
                         [f"INBOX: bet: decision {card['id']}: the human picked 1: {CARD_OPTION}"])
        self.assertEqual(factory.open_decisions(ROOT), [])
        self.assertEqual(self.build()[0], 0)  # alp is still working
        self.tick()
        self.assertEqual((len(self.cards()), self.released()), (1, []))
        self.move("alp", "handed-over")
        self.tick()
        self.move("alp", "working")
        self.assertIsNotNone(self.status_row()["stack_hold"])
        self.assertEqual(self.build()[0], 1)
        self.tick()
        self.assertEqual(len(self.cards()), 2)
        self.assertEqual(len(factory.open_decisions(ROOT)), 1)

    def test_a_stack_is_released_from_the_bottom(self):
        self.entries, self.stack = ["alp", "bet", "cat"], {"bet": "alp", "cat": "bet"}
        self.declare("cat", "working", NOW - HOUR)
        self.tick()
        self.assertEqual(sorted(c["slug"] for c in self.cards()), ["bet", "cat"])
        self.assertEqual((self.build("bet")[0], self.build("cat")[0]), (1, 1))
        self.move("alp", "handed-over")
        self.tick()
        self.assertEqual((self.build("bet")[0], self.build("cat")[0]), (0, 1))
        self.assertEqual([d["slug"] for d in factory.open_decisions(ROOT)], ["cat"])
        self.move("bet", "handed-over")
        self.tick()
        self.assertEqual(self.build("cat")[0], 0)
        self.assertEqual(factory.open_decisions(ROOT), [])


class LimitTests(Hold):
    """A task's limit grows by the seconds it was held; the base of the checks is the limit with no card."""

    def setUp(self):
        super().setUp()
        self.limit = factory.task_limit(None, factory.load_settings()["task_time_limit_hours"])
        self.started = NOW - self.limit + 60  # a minute from its limit when the card is posted
        self.before = self.status_row()
        self.assertEqual((self.before["limit"], self.before["overtime"]), (self.limit, False))

    def test_the_seconds_held_move_the_limit_and_the_task_is_not_overtime_by_waiting(self):
        self.tick()
        row = self.status_row(at=NOW + 300)
        self.assertEqual((row["limit"], row["elapsed"], row["overtime"]), (self.limit + 300, self.limit + 240, False))
        self.move("alp", "handed-over")  # released: the credit stays, the clock goes on
        self.tick()
        row = self.status_row(at=NOW + 600)
        self.assertEqual((row["limit"], row["overtime"]), (self.limit + 360, True))  # held from the card at NOW to the release at NOW + 360

    def test_an_unheld_task_has_no_credit(self):
        row = self.status_row(at=NOW + 300)
        self.assertEqual((row["limit"], row["overtime"]), (self.limit, True))

    def test_an_extension_with_from_granted_during_the_hold_counts_only_the_seconds_held_after_it(self):
        self.tick()
        decided = NOW + 100
        self.seed("firings", [fired("e1", criterion="overrun", subject="bet", ts=decided - 60),
                              {"ts": iso(decided), "repo": ROOT, "id": "e1", "decision": "extended", "reason": "r", "by": "site-manager",
                               "minutes": 20, "from": iso(decided), "until": iso(self.started + self.limit + 100 + 20 * 60)},
                              {"repo": ROOT, "id": "e1", "cleared": True}])
        row = self.status_row(at=NOW + 300)
        self.assertEqual(row["limit"], self.limit + 100 + 20 * 60 + 200)
        self.assertFalse(row["overtime"])
        self.seed("events", [{"ts": iso(self.started), "repo": ROOT, "slug": "bet", "event": "TaskStarted"}])
        with mock.patch.object(factory, "run", self.fake), mock.patch.object(os, "getcwd", return_value=ROOT):
            given = factory.grant({"repo": factory.Repo(), "rows": None}, "bet", 15)
        begins = self.started + row["limit"]
        self.assertEqual(given, (iso(begins), iso(begins + 15 * 60)))
