"""A lead the harness retired is brought back, with the lines it missed, by the factory's script."""
import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import DISPATCHER, ROOT, WT, FakeRun, factory
from test_health import NOW, Harness, ev, iso, lead

# A retired lead as `claude agents --json --all` lists it: no pid, no status, the main checkout as its cwd.
GONE = {"id": "87bf7aa3", "cwd": ROOT, "kind": "background", "startedAt": 1794819536571,
        "sessionId": "87bf7aa3-5533-4a09-988d-13810816af81", "name": "x", "state": "done"}
LIVE = {**GONE, "pid": 7, "status": "idle", "cwd": f"{WT}/x"}
DISPATCHER_SESSION = DISPATCHER | {"name": "dispatcher-repo", "pid": 9}
BACKGROUNDED = (0, "note: woke session 87bf7aa3 with its saved options (-n, --agents)\nbackgrounded · 87bf7aa3 · x\n", "")
SENT = (0, json.dumps({"result": "sent"}), "")
RESUME = ["claude", "--bg", "--resume", GONE["sessionId"]]
COURIER_LINE = re.compile(r"nothing else: (.*)\. Then reply", re.S)


class Outbox(Harness):
    def setUp(self):
        super().setUp()
        self.repo = SimpleNamespace(root=ROOT, tasks=[{"path": f"{WT}/x", "branch": "worktree-x"}],
                                    worktree_path=lambda slug: f"{WT}/{slug}")
        self.cwds = []

    def stage(self, sessions, resume=BACKGROUNDED, courier=SENT):
        """A runner whose answers are the listing, the resume and the courier as given; it notes each call's cwd."""
        self.fake = FakeRun({("claude", "agents"): (0, json.dumps(list(sessions)), ""), ("claude", "--bg", "--resume"): resume,
                             ("claude", "stop"): (0, "", ""), ("claude", "-p"): courier})

        def run(argv, cwd=None, **kw):
            self.cwds.append((argv[:3], cwd))
            return self.fake(argv, cwd=cwd, **kw)
        return run

    def call(self, function, sessions, **stage):
        with mock.patch.object(factory, "run", self.stage(sessions, **stage)):
            return function()

    def forward(self, sessions, said="built", **stage):
        return self.call(lambda: factory.forward(self.repo, None, "x", "TECH LEAD", said), sessions, **stage)

    def drain(self, sessions, **stage):
        return self.call(lambda: factory.drain_outbox(self.repo, None), sessions, **stage)

    def resumes(self):
        return [c for c in self.fake.calls if c[:3] == RESUME[:3]]

    def couriers(self):
        return [(c[-1].split("session named ")[1].split(". ")[0], COURIER_LINE.search(c[-1])[1])
                for c in self.fake.calls if c[:2] == ["claude", "-p"]]


class ForwardTests(Outbox):
    def ends(self):
        """line -> how its row ended (None while pending)."""
        records = list(factory.read_log("outbox"))
        lines = {r["id"]: r["line"] for r in records if "line" in r}
        ends = {i: None for i in lines.values()}
        ends.update({lines[r["id"]]: r["end"] for r in records if "end" in r})
        return ends

    def test_an_exited_lead_is_resumed_once_by_id_in_its_worktree_and_the_row_is_delivered(self):
        to = self.forward([DISPATCHER_SESSION, GONE])
        self.assertEqual(to, "lead")
        self.assertEqual(self.resumes(), [[*RESUME, "TECH LEAD: x: built"]])
        self.assertIn((RESUME[:3], f"{WT}/x"), self.cwds)
        self.assertEqual(self.couriers(), [])
        self.assertEqual(self.ends(), {"TECH LEAD: x: built": "delivered"})

    def test_the_resume_argv_is_exactly_flag_free(self):
        self.forward([GONE], said="a")
        self.assertEqual(self.resumes(), [["claude", "--bg", "--resume", "87bf7aa3-5533-4a09-988d-13810816af81", "TECH LEAD: x: a"]])

    def test_a_resume_that_fails_leaves_the_row_pending_and_tells_the_dispatcher_nothing(self):
        for label, answer in (("non-zero", (1, "", "boom\nmore")), ("no backgrounded line", (0, "ok", "")),
                              ("timed out", (124, "", "timed out"))):
            with self.subTest(label):
                self.assertEqual(self.forward([DISPATCHER_SESSION, GONE], resume=answer), "queued")
                self.assertEqual(self.couriers(), [])
                self.assertEqual(set(self.ends().values()), {None})
        self.assertEqual([r["tries"] for r in factory.outbox(self.repo)], [3, 2, 1])

    def test_a_resume_that_started_a_copy_is_stopped_and_counts_as_failed(self):
        copy = (0, "note: the flags you passed started a copy as c0ffee01\nbackgrounded · c0ffee01 · x\n", "")
        self.assertEqual(self.forward([GONE], resume=copy), "queued")
        self.assertIn(["claude", "stop", "c0ffee01"], self.fake.calls)
        self.assertEqual(len(factory.outbox(self.repo)), 1)

    def test_no_session_named_the_slug_goes_to_the_dispatcher_at_once(self):
        self.assertEqual(self.forward([DISPATCHER_SESSION]), "dispatcher")
        self.assertEqual(self.couriers(), [("dispatcher-repo", "TECH LEAD: x (its lead is gone: no session named x): built")])
        self.assertEqual(self.ends(), {"TECH LEAD: x: built": "dispatcher"})

    def test_a_live_lead_gets_a_courier_by_name_and_no_resume(self):
        self.assertEqual(self.forward([DISPATCHER_SESSION, LIVE]), "lead")
        self.assertEqual((self.couriers(), self.resumes()), ([("x", "TECH LEAD: x: built")], []))
        self.assertEqual(self.ends(), {"TECH LEAD: x: built": "delivered"})

    def test_a_courier_that_fails_says_why_in_the_dispatchers_line(self):
        def courier():  # the lead is not reachable, the dispatcher is
            named = self.fake.calls[-1][-1]
            return (0, json.dumps({"result": "failed: No agent named 'x' is reachable\nmore"}), "") if "session named x." in named else SENT
        self.assertEqual(self.forward([DISPATCHER_SESSION, LIVE], courier=courier), "dispatcher")
        self.assertEqual(self.couriers()[1], (
            "dispatcher-repo", "TECH LEAD: x (not delivered to its lead: failed: No agent named 'x' is reachable): built"))
        self.assertEqual(self.ends(), {"TECH LEAD: x: built": "dispatcher"})

    def test_a_courier_that_crashes_says_its_error(self):
        self.assertEqual(self.forward([DISPATCHER_SESSION, LIVE], courier=(1, "", "boom: no token\nmore")), None)
        self.assertEqual(self.couriers()[1][1], "TECH LEAD: x (not delivered to its lead: boom: no token): built")

    def test_a_lead_is_resumed_with_everything_it_missed_oldest_first(self):
        self.assertEqual(self.forward([GONE], said="one", resume=(1, "", "boom")), "queued")
        self.assertEqual(self.forward([GONE], said="two"), "lead")
        self.assertEqual(self.resumes(), [[*RESUME, "TECH LEAD: x: one\nTECH LEAD: x: two"]])
        self.assertEqual(set(self.ends().values()), {"delivered"})


class TickTests(Outbox):
    def test_pending_rows_are_delivered_in_one_resume_oldest_first_newline_joined(self):
        for line in ("L1", "L2"):
            factory.outbox_put(self.repo, "x", line)
        self.drain([GONE])
        self.assertEqual(self.resumes(), [[*RESUME, "L1\nL2"]])
        self.assertEqual(factory.outbox(self.repo), [])

    def test_after_the_third_failure_the_dispatcher_gets_one_lead_gone_line_and_a_fourth_tick_sends_nothing(self):
        for line in ("L1", "L2"):
            factory.outbox_put(self.repo, "x", line)
        for _ in range(2):
            self.drain([DISPATCHER_SESSION, GONE], resume=(1, "", "boom"))
            self.assertEqual(self.couriers(), [])
        self.drain([DISPATCHER_SESSION, GONE], resume=(1, "", "boom"))
        self.assertEqual(self.couriers(), [("dispatcher-repo",
                                            "LEAD GONE: x (its lead is gone: resume failed: boom): 2 line(s) not delivered, oldest first: L1 | L2")])
        self.assertEqual(factory.outbox(self.repo), [])
        self.drain([DISPATCHER_SESSION, GONE], resume=(1, "", "boom"))
        self.assertEqual(self.fake.calls, [])

    def tick(self, sessions=(), drain=None):
        """`factory watch` with the tick's own work stubbed, `watch_tick` noted; (what it printed, whether `watch_tick` ran)."""
        ticked, out = [], io.StringIO()
        patches = [mock.patch.object(factory, "Repo", lambda: self.repo), mock.patch.object(factory, "ops_tick"),
                   mock.patch.object(factory, "watch_tick", lambda args, repo: ticked.append(1)),
                   mock.patch.object(factory, "read_word", lambda repo: {"all": False, "prs": False}),
                   mock.patch.object(factory, "handed_over", lambda repo: False),
                   mock.patch.object(factory, "paused_rows", lambda repo: {}),
                   mock.patch.object(factory, "run", self.stage(sessions))]
        if drain:
            patches.append(mock.patch.object(factory, "drain_outbox", drain))
        with redirect_stdout(out):
            for patch in patches:
                patch.start()
                self.addCleanup(patch.stop)
            factory.cmd_watch(SimpleNamespace(dispatcher=None))
        return out.getvalue(), bool(ticked)

    def test_a_drain_that_raises_does_not_stop_the_tick(self):
        def boom(repo, fallback):
            raise OSError("disk")
        out, ticked = self.tick(drain=boom)
        self.assertEqual((ticked, "outbox: OSError: disk" in out), (True, True))

    def test_the_tick_delivers_the_outbox_before_and_apart_from_the_site_manager(self):
        factory.outbox_put(self.repo, "x", "L1")
        self.assertEqual(self.tick([DISPATCHER_SESSION, LIVE])[1], True)
        self.assertEqual(self.couriers(), [("x", "L1")])


class LeadCommandTests(Outbox):
    def lead_command(self, slug, message="start the build"):
        return self.cli("lead", slug, message, sessions=[DISPATCHER_SESSION], entries=["x"])

    def test_it_records_and_prints_the_queued_line_and_runs_no_claude(self):
        code, out, _ = self.lead_command("x")
        self.assertEqual((code, out), (0, "queued for x: the next tick delivers it\n"))
        self.assertEqual([c for c in self.fake.calls if c[0] == "claude" and c[1] != "plugin"], [])
        self.assertEqual([r["line"] for r in factory.outbox(self.repo)], ["start the build"])

    def test_a_slug_that_is_not_a_task_is_refused(self):
        code, out, err = self.lead_command("nope")
        self.assertEqual((code, out, factory.outbox(self.repo)), (2, "", []))
        self.assertIn("nope is not a task of this repository", err)

    def test_the_next_tick_delivers_it_live_by_courier_and_retired_by_resume(self):
        self.lead_command("x", "one")
        self.drain([DISPATCHER_SESSION, LIVE])
        self.assertEqual((self.couriers(), self.resumes()), ([("x", "one")], []))
        self.lead_command("x", "two")
        self.drain([DISPATCHER_SESSION, GONE])
        self.assertEqual((self.couriers(), self.resumes()), ([], [[*RESUME, "two"]]))


class SiteManagerTests(Outbox):
    def test_task_lead_names_a_retired_lead(self):
        p = {"rows": None, "repo": self.repo, "sessions": [GONE]}
        self.assertEqual(factory.task_lead(p, "x")[3], "x")

    def test_the_role_has_the_script_send_to_a_lead_not_the_model(self):
        role = (Path(factory.__file__).parent.parent / "roles" / "site-manager.md").read_text()
        self.assertIn("the script sends it", role)
        self.assertNotIn("Message a task's lead directly", role)


class ClockTests(Outbox):
    def test_the_start_of_a_task_is_its_task_started_event_not_a_resumed_sessions_start(self):
        self.seed("events", [ev("x", "TaskStarted", ts=iso(NOW - 83 * 60))])
        resumed = lead("x", pid=7, startedAt=(NOW - 6 * 60) * 1000)
        p = {"repo": self.repo}
        self.assertEqual(factory.extension_state(p, "x", resumed)[0], NOW - 83 * 60)
        self.assertEqual(factory.task_start(factory.repo_log("events", ROOT), "x", resumed), NOW - 83 * 60)

    def test_without_the_event_it_is_the_listings_start(self):
        resumed = lead("x", pid=7, startedAt=(NOW - 6 * 60) * 1000)
        self.assertEqual(factory.extension_state({"repo": self.repo}, "x", resumed)[0], NOW - 6 * 60)
        self.assertIsNone(factory.extension_state({"repo": self.repo}, "x", None)[0])

    def test_status_counts_elapsed_from_the_event(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(NOW - 80 * 60)}])
        self.seed("events", [ev("x", "TaskStarted", ts=iso(NOW - 83 * 60))])
        session = lead("x", pid=7, startedAt=(NOW - 6 * 60) * 1000)
        _, out, _ = self.cli("status", "--json", sessions=[DISPATCHER_SESSION, session], entries=["x"])
        self.assertEqual(json.loads(out)["rows"][0]["elapsed"], 83 * 60)
