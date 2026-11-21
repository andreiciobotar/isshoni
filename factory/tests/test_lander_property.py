"""A property test of the lander's queue: random sequences of events, each played against the real `factory lander` in the
in-memory world of test_lander.py, with the invariants checked after every lander run. Failures are shrunk by deleting one
event at a time and printed as a literal that `replay` plays again. Not generated: stacks, the human's word, prioritized
tasks and `not-ours` holds (each needs a second fake as large as this test)."""
import json
import os
import random
import re
import unittest
from unittest import mock

from test_factory import ROOT, WT, factory
from test_health import iso
from test_lander import GREEN, PENDING, RED, Loop, Sim
from test_merge import review

DEFAULT_SEED = 20261121
SEED = int(os.environ.get("LANDER_PROPERTY_SEED", DEFAULT_SEED))
RUNS = int(os.environ.get("LANDER_PROPERTY_RUNS", "30"))
EVENTS = 12  # the most events of a sequence
PRS = 4
STEP = 2000  # seconds between two lander runs: more than the longest patience of a stop (`pending`, 1800)
SLOW = 8  # pending reads of the checks of a slow pull request after a push: more than `wait` makes in one go (WATCH_INTERVAL // POLL)
MUTANT_RUNS = 60  # the sequences a mutant test tries
WEIGHTS = {"handover": 6, "fix": 4, "approve": 4, "commit": 2, "main_moves": 2, "conflict": 2, "red": 2, "green": 1, "pause": 2,
           "resume": 1, "tick": 1}  # "fix": the lead of a task the lander sent back hands over again
ARGS = factory.parse_args(["lander"])  # one parser for every run


def pending(answer):
    return any(c["bucket"] == "pending" for c in answer)


class Truth(Sim):
    """Sim with what the property test needs: the reviews carry the head that was approved; a push by the lander makes a
    merge commit that git would have made (so the real `approved_heads` carries an approval across it), a lead's commit a
    head with one parent; and the facts the invariants are judged by, recorded at the moment each happens."""

    def __init__(self):
        super().__init__()
        self.approved_on, self.state, self.first = {}, {}, {}  # n -> head the human approved; n -> task state; n -> first hand-over
        self.parents, self.under = {}, {}  # lander head -> (old head, tip it merged); lead's head -> the head it was committed on
        self.red, self.slow, self.leads = set(), set(), 0
        self.merges, self.updates, self.sent, self.mark = [], [], [], 0  # of this lander run (its first entry in `log`): the facts at each merge, update push and send-back

    def after(self, n):
        return [PENDING] * (SLOW if n in self.slow else 1) + [RED if n in self.red else GREEN]

    def lead_head(self, n):
        self.leads += 1
        new = f"h{n}c{self.leads}"
        self.under[new] = self.prs[n]["head"]
        return new

    def row(self, n, state, at, **extra):
        self.state[n] = state
        return {"repo": ROOT, "slug": f"t{n}", "state": state, "ts": iso(at), **extra}

    def apply(self, event, at):
        """Play `event` at `at`; the task rows it writes. One that does not apply to the world as it is does nothing."""
        kind, *args = event
        n = args[0] if args else None
        if kind == "main_moves":
            self.tip += 1
            for m in self.open():
                self.prs[m]["behind"] = True
            return []
        if kind == "handover" and n not in self.prs:
            self.add(n, behind=args[1], approved=False)
            if args[2]:
                self.slow.add(n)
            self.first[n] = at
            return [self.row(n, "handed-over", at)]
        if n not in self.open():
            return []
        p = self.prs[n]
        if kind == "handover" and self.state[n] == "working":
            p.update(conflicts=[], mergeable=["MERGEABLE"], draft=False, checks=[GREEN], head=self.lead_head(n))
            self.red.discard(n)
            return [self.row(n, "handed-over", at)]
        if kind == "approve":
            self.approved_on[n], p["approved"] = p["head"], True
        elif kind == "commit":
            p.update(head=self.lead_head(n), checks=[RED] if n in self.red else [GREEN])
        elif kind == "conflict":
            p.update(conflicts=["a.txt"], behind=True, mergeable=["CONFLICTING"])
        elif kind == "red":
            self.red.add(n)
            p["checks"] = [RED]
        elif kind == "green":
            self.red.discard(n)
            p["checks"] = [GREEN]
        elif kind == "pause":
            return [self.row(n, "blocked", at, paused=True, waits_on=["human"])]
        elif kind == "resume" and self.state[n] == "blocked":
            return [self.row(n, "handed-over", at)]
        return []

    def reached(self, n, head):
        """Whether `head` is what the human approved, or that and clean merges of the default branch by the lander."""
        while head not in (None, self.approved_on.get(n)) and head in self.parents:
            head = self.parents[head][0]
        return head is not None and head == self.approved_on.get(n)

    def green(self, n, head):
        reads = [e[3] for e in self.log[self.mark:] if e[0] == "checks" and e[1] == n and e[2] == head]
        return reads[-1] if reads else not pending(self.prs[n]["checks"][0]) and all(c["bucket"] == "pass" for c in self.prs[n]["checks"][0])

    def handle(self, a, cwd):
        found = self.truthfully(a)
        if found:
            return found
        if a[:3] == ["gh", "pr", "merge"]:
            n, p = int(a[3]), self.prs[int(a[3])]
            self.merges.append({"n": n, "head": a[-1], "approved": self.reached(n, a[-1]), "behind": p["behind"],
                                "green": self.green(n, a[-1]), "draft": p["draft"],
                                "conflict": bool(p["conflicts"]) or p["mergeable"][0] == "CONFLICTING",
                                "handed": self.state[n] == "handed-over"})
        elif a[:3] == ["gh", "pr", "ready"] and "--undo" in a:
            n = int(next(x for x in a[3:] if x != "--undo").removeprefix("worktree-t"))
            self.sent.append({"n": n, "before": [m for m in self.open() if m != n and self.state[m] != "blocked" and self.first[m] < self.first[n]]})
            self.state[n] = "working"
        elif a[:2] == ["git", "push"] and cwd and cwd.startswith(WT):
            n = int(os.path.basename(cwd).removeprefix("t"))
            self.updates.append({"n": n, "waiting": [m for m in self.open() if m != n and self.prs[m]["head"] in self.parents
                                                    and pending(self.prs[m]["checks"][0])]})
            self.prs[n]["after"] = self.after(n)
            found = super().handle(a, cwd)
            self.parents[self.prs[n]["head"]] = (self.prs[n]["shown"], f"tip{self.tip}")
            return found
        return super().handle(a, cwd)

    def truthfully(self, a):
        """The answers Sim does not give as the modelled world would: the approval path, and git's view of the heads."""
        last = a[-1]
        if a[:2] == ["gh", "api"]:
            if m := re.search(r"pulls/(\d+)/reviews$", last):
                n = int(m[1])
                return 0, json.dumps([[review(sha=self.approved_on[n], n=n)] if n in self.approved_on else []]), ""
            if re.search(r"issues/\d+/comments$", last):
                return 0, "[[]]", ""
            if m := re.search(r"pulls/(\d+)/commits$", last):
                return 0, json.dumps([[{"sha": self.prs[int(m[1])]["head"]}]]), ""
            if m := re.search(r"/commits/(\S+)$", last):  # a commit's change is its own: no two commits make the same
                parents = [{"sha": x} for x in self.parents.get(m[1], (self.under.get(m[1], f"p-{m[1]}"),))]
                return 0, json.dumps({"sha": m[1], "parents": parents, "files": [{"filename": "f", "status": "modified", "patch": f"@@\n+{m[1]}"}]}), ""
        if a[:4] == ["git", "rev-list", "--parents", "-n"]:
            return 0, " ".join([last, *self.parents.get(last, (self.under.get(last, f"p-{last}"),))]) + "\n", ""
        if a[:2] == ["git", "rev-parse"] and last.endswith("^{tree}"):
            head = last.removesuffix("^{tree}")
            return 0, "T:" + "+".join(self.parents.get(head, (head,))) + "\n", ""
        if a[:3] == ["git", "merge-base", "--is-ancestor"] and a[3].startswith("tip"):  # every tip a push merged is on the default branch
            return 0, "", ""
        if a[:3] == ["git", "merge-tree", "--write-tree"] and "--name-only" not in a:  # the merge of two heads that merged cleanly
            return 0, "T:" + "+".join(a[3:]) + "\n", ""
        return None


class Rig(Loop):
    """A fresh store and a fresh Truth, and the lander run against them once per event."""

    def setUp(self):
        super().setUp()
        self.sim = Truth()
        patcher = mock.patch.object(factory, "parse_args", lambda argv: ARGS)  # building the parser costs as much as the lander run
        patcher.start()
        self.addCleanup(patcher.stop)

    def lander(self):
        sim = self.sim
        sim.merges, sim.updates, sim.sent, sim.mark = [], [], [], len(sim.log)
        for p in sim.prs.values():  # checks that were pending when the last run ended have finished since
            p["checks"] = [c for c in p["checks"] if not pending(c)] or [p["checks"][-1]]
        self.go("lander", at=self.clock + STEP, seed_tasks=False)
        return self.broken()

    def event(self, event):
        self.seed("tasks", self.sim.apply(event, self.clock + 1))
        return self.lander()

    def broken(self):
        """(invariant, why) of the first invariant the last run broke, else None."""
        sim = self.sim
        for m in sim.merges:
            if not m["approved"]:
                return "approved-head", f"#{m['n']} merged at {m['head']}, but the human approved {sim.approved_on.get(m['n'])}"
            bad = [word for word, yes in (("behind", m["behind"]), ("a draft", m["draft"]), ("conflicting", m["conflict"]),
                                          ("not green", not m["green"]), ("not handed over", not m["handed"])) if yes]
            if bad:
                return "tip-and-green", f"#{m['n']} merged at {m['head']} while it was {', '.join(bad)}"
        for s in sim.sent:
            if s["before"]:
                return "front-only", f"#{s['n']} was sent back while #{s['before'][0]} stood before it in the line"
        for u in sim.updates:
            if u["waiting"]:
                return "one-update", f"#{u['n']} was updated while #{u['waiting'][0]} waits for its checks on the head the lander pushed"

    def choose(self, rng):
        """A random event that applies to the world as it is, so that few are wasted; the kinds are weighted by WEIGHTS."""
        sim = self.sim
        live = sim.open()
        new = [n for n in range(1, PRS + 1) if n not in sim.prs]
        works = {"handover": new, "fix": [n for n in live if sim.state[n] == "working"], "main_moves": [0], "tick": [0], "commit": live,
                 "approve": [n for n in live if sim.approved_on.get(n) != sim.prs[n]["head"]],
                 "conflict": [n for n in live if not sim.prs[n]["conflicts"]],
                 "red": [n for n in live if n not in sim.red], "green": [n for n in live if n in sim.red],
                 "pause": [n for n in live if sim.state[n] != "blocked"], "resume": [n for n in live if sim.state[n] == "blocked"]}
        kinds = [k for k in WEIGHTS if works[k]]
        kind = rng.choices(kinds, [WEIGHTS[k] for k in kinds])[0]
        n = rng.choice(works[kind])
        if kind in ("handover", "fix"):  # a pull request opens (behind or not, slow checks or not), or its lead hands over again
            return ("handover", n, rng.random() < 0.6, rng.random() < 0.3) if kind == "handover" else ("handover", n, False, False)
        return (kind,) if n == 0 else (kind, n)

    def play(self, events, rng=None):
        """(events played, (invariant, why) of the first invariant broken or None): `events`, or with `rng` that many
        random ones, then the rounds."""
        played = []
        for i in range(len(events) if rng is None else rng.randint(4, EVENTS)):
            played.append(events[i] if rng is None else self.choose(rng))
            if failure := self.event(played[-1]):
                return played, failure
        return played, self.rounds()

    def rounds(self):
        """The cooperating human and leads: each round, every task sent back and not paused is fixed and handed over again,
        every head of a task handed over and not paused that carries no approval is approved, then the lander runs."""
        sim = self.sim
        for _ in range(4 * len(sim.prs) + 4):
            live = [n for n in sim.open() if sim.state[n] != "blocked"]
            if not live:
                return None
            moved = False
            for n in live:
                if sim.state[n] == "working":
                    self.seed("tasks", sim.apply(("handover", n, False, False), self.clock + 1))
                    moved = True
            for n in live:
                if sim.state[n] == "handed-over" and sim.approved_on.get(n) != sim.prs[n]["head"]:
                    sim.apply(("approve", n), self.clock + 1)
                    moved = True
            if failure := self.lander():
                return failure
            if not (moved or sim.merges or sim.sent or sim.updates):
                return "progress", f"a round changed nothing while {', '.join(f'#{n}' for n in live)} stayed open and not paused"
        return "progress", "the rounds did not end"


def fresh(events, rng=None):
    """What `Rig.play` gives, in a world of its own."""
    rig = Rig("play")
    rig.setUp()
    try:
        return rig.play(events, rng)
    finally:
        rig.doCleanups()


def shrink(events, name):
    """`events` without the events whose deletion leaves the invariant `name` still broken, one at a time."""
    again = True
    while again:
        again = False
        for i in range(len(events)):
            shorter = events[:i] + events[i + 1:]
            if (failure := fresh(shorter)[1]) and failure[0] == name:
                events, again = shorter, True
                break
    return events


class Failure(AssertionError):
    def __init__(self, name, why, events, seed, index, was):
        self.name, self.events = name, events
        super().__init__(
            f"lander invariant `{name}` broken (seed {seed}, run {index}): {why}\n"
            f"shortest sequence ({len(events)} events, shrunk from {was}), replayed by `replay`:\n"
            f"replay({events!r})\n"
            f"reproduce, from factory/tests with the NOCONFIG and INSESSION variables of the Makefile:\n"
            f"LANDER_PROPERTY_SEED={seed} LANDER_PROPERTY_RUNS={index + 1} python3 -m unittest test_lander_property")


def replay(events):
    """Play `events` as the driver does; raises AssertionError naming the invariant and why if one breaks."""
    if failure := fresh(events)[1]:
        raise AssertionError("lander invariant `%s` broken: %s" % failure)


def hunt(seed, runs):
    """The Failure of the first of `runs` random sequences of seed `seed` that breaks an invariant, shrunk; else None."""
    for index in range(runs):
        events, failure = fresh((), random.Random(f"{seed}/{index}"))
        if failure:
            return Failure(failure[0], failure[1], shrink(events, failure[0]), seed, index, len(events))


class PropertyTests(unittest.TestCase):
    """The queue on random sequences, then each invariant shown to bite: a planted bug must be found by the same driver and
    shrunk to at most 6 events."""

    def test_the_queue_holds_its_invariants_on_random_sequences(self):
        failure = hunt(SEED, RUNS)
        if failure:
            raise failure

    def bites(self, name, **patch):
        with mock.patch.multiple(factory, **patch):
            failure = hunt(DEFAULT_SEED, MUTANT_RUNS)
        self.assertIsNotNone(failure, f"no sequence of {MUTANT_RUNS} broke {name}")
        self.assertEqual(failure.name, name, str(failure))
        self.assertLessEqual(len(failure.events), 6, str(failure))

    def test_a_front_that_is_paused_is_not_skipped_and_nothing_moves(self):  # the deadlock of #258
        self.bites("progress", paused_rows=lambda repo: {})

    def test_a_pull_request_that_is_not_the_front_is_sent_back_all_the_same(self):
        self.bites("front-only", in_line=lambda stops, front: stops)

    def test_an_approval_is_carried_across_a_commit_of_the_lead(self):
        real = factory.approved_heads

        def carried(p, pr):
            parents = p["repo"].git("rev-list", "--parents", "-n", "1", pr["headRefOid"])[1].split()
            return real(p, pr) + parents[1:2]
        self.bites("approved-head", approved_heads=carried)

    def test_a_red_pull_request_merges(self):
        self.bites("tip-and-green", checks_gate=lambda p, n: None)

    def test_a_second_pull_request_is_updated_while_the_first_waits_for_its_checks(self):
        self.bites("one-update", held=lambda state, stops: set())


if __name__ == "__main__":
    unittest.main()
