"""A stretch of 2026-11-15 (09:00Z-15:30Z) replayed tick by tick from a sanitised copy of the real records: what wakes the site
manager, what it is given, what it costs. The model is a fake that answers every look with a cost; `gh` and the session list are
scripted from the records. See fixtures/replay-2026-11-15/README."""
import calendar
import fcntl
import json
import time
from collections import Counter
from pathlib import Path

from test_factory import DISPATCHER, ROOT, factory
from test_health import Harness, lead, iso
from test_merge import pr

FIXTURE = Path(__file__).parent / "fixtures" / "replay-2026-11-15"
STEP = 120  # the watch interval
LOGS = ("events", "tasks", "queue", "techlead", "firings")
FACTS = ("run-stopped", "ended-undeclared")  # fired by the script from the other records, not replayed from the stored firings
DISPATCH = DISPATCHER | {"name": "dispatcher-repo"}
OWN_CALL = {"entry": "unlisted", "slug": "lander-agent-file-current", "decision": "escalated", "looked": ["gh pr view 106"],
            "detail": "sent back by the queue for a conflict, its approval will not carry over",
            "reason": "a conflict send-back always costs the approval; the dispatcher should expect a second one"}


def epoch(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def load(name):
    return [json.loads(line) | {"repo": ROOT} for line in (FIXTURE / f"{name}.jsonl").read_text().splitlines()]


def tick_of(ts):
    """The first tick at or after the record time `ts`."""
    return -(-epoch(ts) // STEP) * STEP


class Replay(Harness):
    def setUp(self):
        super().setUp()
        for path in self.store.glob("looks-*"):
            path.unlink()
        self.guidance.write_text((factory.PLUGIN_ROOT / "roles" / "site-manager-cases.md").read_text())
        self.logs = {name: load(name) for name in LOGS}
        stored = {r["id"] for r in self.logs["firings"] if r.get("criterion") in FACTS}
        self.logs["firings"] = [r for r in self.logs["firings"] if r["id"] not in stored]
        self.start, self.end = tick_of("2026-11-15T09:00:00Z"), tick_of("2026-11-15T15:30:00Z")
        self.born, self.gone = {}, {}
        for r in sorted([*self.logs["events"], *self.logs["tasks"]], key=lambda r: r["ts"]):
            if r.get("event") == "TaskRemoved":
                self.gone[r["slug"]] = epoch(r["ts"])
            elif r["slug"] not in self.gone:
                self.born.setdefault(r["slug"], epoch(r["ts"]))
        self.pr_opened = {}  # pull request number -> (slug, first declared in a deliverable)
        for r in self.logs["tasks"]:
            if r.get("deliverable"):
                self.pr_opened.setdefault(int(r["deliverable"].rsplit("/", 1)[1]), (r["slug"], epoch(r["ts"])))
        self.pr_closed = {r["pr"]: epoch(r["ts"]) for r in self.logs["queue"] if r["step"] == "merged"}
        self.builds = [(r["slug"], epoch(r["ts"]) - r["seconds"], epoch(r["ts"])) for r in self.logs["techlead"]]
        self.lead_gone = {"kaizen-review-role": epoch("2026-11-15T13:33:49Z")}  # its lead ended right after starting the build run
        self.held, self.reported, self.prompts, self.couriers = {}, [], {}, []
        self.addCleanup(lambda: [f.close() for f in self.held.values()])

    def worktrees(self, t):
        return [s for s, born in self.born.items() if born <= t and self.gone.get(s, t + 1) > t]

    def sessions(self, t):
        return [DISPATCH, *(lead(s, sid=f"sid-{s}", startedAt=self.born[s] * 1000) for s in self.worktrees(t)
                            if t < self.lead_gone.get(s, t + 1))]

    def pull_requests(self, t):
        return [pr(n) | {"headRefName": f"worktree-{slug}"} for n, (slug, opened) in sorted(self.pr_opened.items())
                if opened <= t and self.pr_closed.get(n, t + 1) > t]

    def hold_locks(self, t):
        """The `tech-lead-<slug>` lock is held while a build run of the slug runs: from its end minus its seconds to its record."""
        running = {slug for slug, start, end in self.builds if start <= t < end}
        for slug in running - set(self.held):
            path = factory.repo_state(ROOT, f"tech-lead-{slug}", "lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.held[slug] = open(path, "a")
            fcntl.flock(self.held[slug], fcntl.LOCK_EX | fcntl.LOCK_NB)
        for slug in set(self.held) - running:
            self.held.pop(slug).close()

    def write_records(self, t):
        for name in LOGS:
            due = [r for r in self.logs[name] if epoch(r["ts"]) <= t]
            self.logs[name] = self.logs[name][len(due):]
            self.seed(name, due)

    def model(self):
        """The site manager of the fake: it suppresses every firing it is given, makes one call of its own when the queue sent
        a task back, reports a cost; anything that is not a watch look (the courier of a message) says `sent`."""
        argv = self.fake.calls[-1]
        if "--json-schema" not in argv:
            self.couriers.append(argv[-1])
            return 0, json.dumps({"result": "sent"}), ""
        head, _, body = argv[-1].partition("\n\n")
        if not head.startswith("Job: watch"):
            return 0, json.dumps({"total_cost_usd": None, "structured_output": {"sent": False, "decisions": []}}), ""
        picture = json.loads(body)
        self.prompts[iso(self.clock)] = picture
        decisions = [{"id": f["id"], "decision": "suppressed", "reason": "ordinary", "looked": []} for f in picture["firings"]]
        if any(w.get("step") == "sent-back" and w["slug"] == OWN_CALL["slug"] for w in picture["woken_by"]):
            decisions.append(OWN_CALL)
        self.reported.append(round(0.05 + 0.01 * (len(self.reported) % 9), 2))
        return 0, json.dumps({"session_id": f"run-{len(self.reported)}", "total_cost_usd": self.reported[-1],
                              "structured_output": {"sent": False, "decisions": decisions}}), ""

    def replay(self):
        """One watch tick per two minutes; the ticks that looked, as {tick: look}."""
        looked = {}
        for t in range(self.start, self.end + 1, STEP):
            self.write_records(t)
            self.hold_locks(t)
            before = len(self.looks())
            code, out, err = self.cli("watch", sessions=self.sessions(t), entries=self.worktrees(t), at=t, answers={
                ("claude", "-p"): self.model,
                ("gh", "pr", "list", "--state", "open"): (0, json.dumps(self.pull_requests(t)), "")})
            self.assertEqual(code, 0, (iso(t), out, err))
            self.assertEqual(len(self.looks()) - before, int("nothing new" not in out), (iso(t), out))
            if len(self.looks()) > before:
                looked[t] = self.looks()[-1]
        return looked

    def test_the_stretch(self):
        started = time.monotonic()
        looked = self.replay()
        seconds = time.monotonic() - started
        ticks = range(self.start, self.end + 1, STEP)
        wakes = Counter(w["event"] for look in looked.values() for w in look["woken_by"])
        print(f"\nreplay: {len(ticks)} ticks, {len(looked)} looks, {len(ticks) - len(looked)} ticks without one, {seconds:.1f} s; "
              f"events {dict(wakes)}; looks by kind "
              f"{dict(Counter(k for look in looked.values() for k in {w['event'] for w in look['woken_by']}))}")
        print("firings that woke him:", dict(Counter(self.criteria(looked))))
        for name, check in (("every record that wakes him", self.check_records), ("quiet ticks", self.check_quiet),
                            ("pulse-band-visual", self.check_stopped_runs), ("kaizen-review-role", self.check_ended),
                            ("his own call", self.check_own_call), ("cost", self.check_cost)):
            with self.subTest(name):
                check(looked)
        self.assertLess(seconds, 20)

    def criteria(self, looked):
        return [w["criterion"] for look in looked.values() for w in look["woken_by"] if w["event"] == "firing"]

    def woken(self, looked, t, **match):
        return [w for w in looked.get(t, {"woken_by": []})["woken_by"] if all(w.get(k) == v for k, v in match.items())]

    def check_records(self, looked):
        starts = [r for r in self.logs_all("events") if r["event"] == "TaskStarted"]
        self.assertTrue(starts)
        for r in starts:
            self.assertEqual(self.woken(looked, tick_of(r["ts"]), event="task-started", slug=r["slug"]), [
                {"event": "task-started", "slug": r["slug"], "ts": r["ts"]}], r)
        for r in self.logs_all("queue"):
            self.assertEqual([w["step"] for w in self.woken(looked, tick_of(r["ts"]), event="queue", slug=r["slug"], pr=r["pr"])].count(r["step"]),
                             1, r)
        for n, (slug, opened) in self.pr_opened.items():
            self.assertTrue(self.woken(looked, tick_of(iso(opened)), event="pr-opened", pr=n), (n, slug))

    def logs_all(self, name):
        return load(name)

    def check_quiet(self, looked):
        """No look on a tick without an event, and a look only for a pace when the last look is a health period old."""
        recorded = {tick_of(r["ts"]) for name in ("queue", "events") for r in self.logs_all(name)}
        for t in range(self.start, self.end + 1, STEP):
            if t not in looked:
                self.assertNotIn(t, recorded, iso(t))
        times = sorted(looked)
        for t, look in looked.items():
            self.assertTrue(look["woken_by"], iso(t))
            for w in look["woken_by"]:
                if "ts" in w:  # a record of the log, from the tick before this one
                    self.assertEqual(tick_of(w["ts"]), t, w)
            if [w["event"] for w in look["woken_by"]] == ["pace"]:
                earlier = [u for u in times if u < t]
                self.assertTrue(earlier and t - earlier[-1] >= factory.HEALTH_SECONDS, iso(t))

    def check_stopped_runs(self, looked):
        stopped = [r for r in self.logs_all("techlead") if r["slug"] == "pulse-band-visual" and r["outcome"] == "stopped"]
        runs = [r for r in self.logs_all("techlead") if r["slug"] == "pulse-band-visual"]
        fired = {r["subject"]: r for r in self.records() if r.get("criterion") == "run-stopped" and r["slug"] == "pulse-band-visual"}
        for r in stopped:
            n = runs.index(r) + 1
            t = tick_of(r["ts"])
            [wake] = self.woken(looked, t, event="firing", criterion="run-stopped", slug="pulse-band-visual")
            self.assertEqual(wake["id"], fired[f"pulse-band-visual/run{n}"]["id"])
            [row] = [x for x in self.prompts[iso(t)]["tasks"] if x["slug"] == "pulse-band-visual"]
            self.assertEqual([x["cost_usd"] for x in row["runs"]], [x["cost_usd"] for x in runs[:n]])
            self.assertEqual(row["runs"][-1]["outcome"], "stopped")
        self.assertEqual(len(stopped), len(fired))

    def check_ended(self, looked):
        [fire] = [r for r in self.records() if r.get("criterion") == "ended-undeclared"]
        self.assertEqual(fire["slug"], "kaizen-review-role")
        first = tick_of("2026-11-15T14:54:10Z")  # its build run's lock is free after this record, the lead went at 13:33:49Z
        self.assertEqual(fire["ts"], iso(first))
        self.assertEqual(self.woken(looked, first, event="firing", criterion="ended-undeclared", slug="kaizen-review-role"),
                         [{"event": "firing", "id": fire["id"], "criterion": "ended-undeclared", "slug": "kaizen-review-role"}])
        self.assertLess(first, epoch("2026-11-15T15:22:43Z"))  # the live store's firing, 47 minutes after the last sign of life

    def check_own_call(self, looked):
        [call] = [r for r in self.records() if r.get("criterion") == "unlisted"]
        [decision] = [r for r in self.records() if r["id"] == call["id"] and r.get("decision")]
        self.assertEqual((call["slug"], call["detail"], decision["decision"], decision["by"], decision["to"]),
                         (OWN_CALL["slug"], OWN_CALL["detail"], "escalated", "site-manager", "dispatcher"))
        line = f"ESCALATION: {call['id']} unlisted {OWN_CALL['slug']}: {OWN_CALL['reason']}"
        self.assertEqual(len([c for c in self.couriers if line in c]), 1)
        self.assertEqual(decision["ts"], call["ts"])
        _, out, _ = self.cli("firings")
        [entry] = [x for x in out.splitlines() if x.startswith("unlisted: ")]
        self.assertEqual(entry, "unlisted: fired 1; escalated 1 (acted 0, ignored 0, no outcome 1); to lead 0; suppressed 0; missed 0; precision -")

    def check_cost(self, looked):
        self.assertTrue(self.reported)
        self.assertEqual(len(self.reported), len(looked))
        self.assertAlmostEqual(sum(look["cost_usd"] for look in looked.values()), sum(self.reported))
        self.assertEqual(list(self.prompts), [iso(t) for t in looked])


if __name__ == "__main__":
    import unittest
    unittest.main()
