import io
import json
import os
import plistlib
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import DISPATCHER, LEAN_SETTINGS, NO_ATTRIBUTION, ROOT, factory, put_access
from test_health import NOW, SchedHarness, iso

CHECKS = {"read": "mcp__metrics__query: errors in the last 10m", "fine": "0 errors", "alarm": "any error", "done": "served reaches 30"}
EVERY = 600
SERVER = {"type": "stdio", "command": "/bin/true"}
OPS_RUN = ("claude", "-p", "--model", "sonnet", "--agents")
COURIER = ("claude", "-p", "-n", "factory-courier", "--model", "haiku")  # pins the courier's argv


class OpsHarness(SchedHarness):
    """The CLI against a faked `claude`: a scripted look writes its evidence files where the hook would."""

    def setUp(self):
        super().setUp()
        self.access = put_access(ROOT, tools=["mcp__metrics__query"])
        self.claude_json = self.home / "claude.json"
        self.claude_json.write_text(json.dumps({"mcpServers": {"metrics": SERVER}}))
        env = mock.patch.dict(os.environ)
        env.start()
        os.environ.pop("CLAUDECODE", None)
        patcher = mock.patch.object(factory, "CLAUDE_JSON", self.claude_json)
        patcher.start()
        for undo in (env.stop, patcher.stop):
            self.addCleanup(undo)
        self.script, self.sessions_run, self.calls = [], 0, []

    def look_answer(self):
        def answer():
            self.sessions_run += 1
            step = {"outcome": "fine", "saw": "0 errors, 412 requests", "cost": 0.1, "evidence": 1, "checks": CHECKS,
                    "rc": 0, **(self.script.pop(0) if self.script else {})}
            if step.get("raise"):
                raise RuntimeError(step["raise"])
            if step.get("hook"):
                step["hook"]()
            sid = f"s{self.sessions_run}"
            folder = self.store / "deliverables" / f"session-{sid}" / "evidence"
            for i in range(step["evidence"]):
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{i}.json").write_text('{"response": "ok"}')
            output = {k: step[k] for k in ("outcome", "saw", "verdict", "checks") if step.get(k)}
            doc = {"structured_output": output, "session_id": sid, "num_turns": 4, "subtype": "success"}
            if step["cost"] is not None:
                doc["total_cost_usd"] = step["cost"]
            return step["rc"], json.dumps(doc), ""
        return answer

    def tick(self, *script, at=None, **kw):
        self.script += script
        answers = {OPS_RUN: self.look_answer(), COURIER: (0, json.dumps({"result": "sent"}), ""), **kw.pop("answers", {})}
        result = self.cli("watch", answers=answers, at=at, **kw)
        self.calls += self.fake.calls
        return result

    def start(self, name="w", every=EVERY, until="6h", budget="5", goal="zero errors, done once served reaches 30"):
        return self.cli("ops", "start", name, goal, "--every", f"{every}s", "--until", until, "--budget", budget)

    def watch(self, name="w"):
        return json.loads((self.store / "ops" / name / "watch.json").read_text())

    def notes(self, name="w"):
        return (self.store / "ops" / name / "notes.md").read_text()

    def ops_records(self):
        return self.records("ops")

    def ops_runs(self):
        return [c for c in self.calls if tuple(c[:5]) == OPS_RUN]

    def courier_lines(self):
        return [c[-1] for c in self.calls if tuple(c[:len(COURIER)]) == COURIER]


class StartTests(OpsHarness):
    def test_it_refuses_under_claudecode(self):
        os.environ["CLAUDECODE"] = "1"
        code, _, err = self.start()
        self.assertEqual((code, err), (2, "factory: only the human starts a watch: run this in your own terminal\n"))
        self.assertFalse((self.store / "ops").exists())

    def test_it_refuses_a_bad_name_an_existing_name_and_bad_numbers(self):
        for name in ("Bad", "a_b", "a b", "a/b"):
            self.assertEqual(self.start(name)[0], 2, name)
        self.assertEqual(self.start()[0], 0)
        self.assertIn("a watch named w exists", self.start()[2])
        for kw in ({"every": 0}, {"until": "0m"}, {"until": "2001-01-01T00:00:00Z"}, {"until": "soon"}, {"budget": "0"}, {"budget": "-1"},
                   {"budget": "inf"}, {"budget": "nan"}):
            code, _, err = self.start("v", **kw)
            self.assertEqual((code, (self.store / "ops" / "v").exists()), (2, False), kw)

    def test_it_refuses_without_a_configured_server(self):
        self.claude_json.write_text("{}")
        code, _, err = self.start()
        self.assertEqual(code, 2)
        self.assertIn("the recorded list resolves no server", err)
        self.assertFalse((self.store / "ops").exists())

    def test_it_refuses_a_repository_that_was_not_asked_or_declined(self):
        self.access.unlink()
        self.assertEqual(self.start()[1:], ("", "factory: ops: production access is not recorded for this repository: "
                                               "the dispatcher asks the human (factory ops access)\n"))
        put_access(ROOT, state="declined", words="no prod")
        self.assertEqual(self.start()[1:], ("", "factory: ops: the human declined production access for this repository "
                                               "(2026-11-18T10:40:00Z): do not ask for it\n"))
        self.assertFalse((self.store / "ops").exists())

    def test_it_writes_private_state_and_notes_and_installs_the_job(self):
        code, out, _ = self.start()
        self.assertEqual(code, 0)
        w = self.watch()
        self.assertEqual((w["repo"], w["state"], w["next_look"], w["until"], w["every_seconds"], w["budget_usd"]),
                         (ROOT, "watching", iso(NOW), iso(NOW + 6 * 3600), EVERY, 5.0))
        self.assertIn(w["notes"], out)
        notes = self.notes()
        for text in ("Goal: zero errors, done once served reaches 30", f"Started: {iso(NOW)}", f"Until: {iso(NOW + 6 * 3600)}",
                     f"Every: {EVERY} s", "Budget: $5.00"):
            self.assertIn(text, notes)
        self.assertEqual(stat.S_IMODE((self.store / "ops").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.store / "ops" / "w").stat().st_mode), 0o700)
        for leaf in ("watch.json", "notes.md"):
            self.assertEqual(stat.S_IMODE((self.store / "ops" / "w" / leaf).stat().st_mode), 0o600)
        self.assertTrue(self.plist().exists())

    def test_a_write_never_follows_a_planted_temporary_name(self):
        self.start()
        victim = self.home / "victim"
        victim.write_text("mine")
        (self.store / "ops" / "w" / "watch.json.tmp").symlink_to(victim)
        self.cli("ops", "stop", "w")
        self.assertEqual((victim.read_text(), self.watch()["state"]), ("mine", "stopped"))

    def test_it_never_replaces_an_installed_job(self):
        self.put_plist(marker="mine")
        self.start()
        self.assertEqual(plistlib.loads(self.plist().read_bytes())["marker"], "mine")

    def test_until_may_be_a_time(self):
        self.start(until=iso(NOW + 100))
        self.assertEqual(self.watch()["until"], iso(NOW + 100))


class LookTests(OpsHarness):
    def setUp(self):
        super().setUp()
        self.start()

    def test_a_look_that_is_not_due_runs_nothing(self):
        self.tick()
        self.assertEqual(self.sessions_run, 1)
        self.tick()
        self.assertEqual(self.sessions_run, 1)
        self.tick(at=NOW + EVERY - 1)
        self.assertEqual(self.sessions_run, 1)
        self.tick(at=NOW + EVERY)
        self.assertEqual(self.sessions_run, 2)

    def test_a_due_look_runs_is_recorded_and_noted(self):
        _, out, _ = self.tick()
        [r] = self.ops_records()
        self.assertEqual(set(r), {"ts", "repo", "watch", "tick", "outcome", "saw", "cost_usd", "seconds", "turns", "session", "evidence",
                                  "late_seconds"})
        self.assertEqual((r["repo"], r["watch"], r["tick"], r["outcome"], r["saw"], r["cost_usd"], r["turns"], r["session"],
                          r["evidence"], r["late_seconds"]), (ROOT, "w", 1, "fine", "0 errors, 412 requests", 0.1, 4, "s1", 1, 0))
        self.assertIsInstance(r["seconds"], float)
        w = self.watch()
        self.assertEqual((w["ticks"], w["spent_usd"], w["next_look"], w["state"]), (1, 0.1, iso(NOW + EVERY), "watching"))
        self.assertEqual(w["last_look"], {"ts": iso(NOW), "outcome": "fine", "saw": "0 errors, 412 requests", "late_seconds": 0})
        self.assertIn(f"- {iso(NOW)} fine: 0 errors, 412 requests [evidence: 1 file(s) in {self.store}/deliverables/session-s1/evidence]\n",
                      self.notes())

    def test_the_session_is_started_as_specified(self):
        self.tick()
        [argv] = self.ops_runs()
        self.assertEqual(argv[:4], ["claude", "-p", "--model", "sonnet"])
        agents = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(list(agents), ["factory:ops-explorer"])
        self.assertEqual(agents["factory:ops-explorer"]["mcpServers"], [{"metrics": SERVER}])
        for flag, value in (("--permission-mode", "dontAsk"), ("--plugin-dir", str(factory.PLUGIN_ROOT)),
                            ("--allowedTools", "Agent(factory:ops-explorer)"), ("--max-turns", str(factory.OPS_TICK_TURNS)),
                            ("--max-budget-usd", "0.5"), ("--output-format", "json"),
                            ("--json-schema", json.dumps(factory.OPS_SCHEMA)), ("--settings", json.dumps(LEAN_SETTINGS))):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertIn("--tools=Agent", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--setting-sources"):][:2], list(factory.SETTINGS_ISOLATION))
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], factory.OPS_ROLE.read_text())
        self.assertEqual(self.fake.timeouts[self.fake.calls.index(argv)], factory.OPS_TICK_TIMEOUT)
        self.assertIn("Goal: zero errors, done once served reaches 30", argv[-1])
        self.assertIn("first look", argv[-1])
        self.assertIn("Final look: no", argv[-1])

    def test_without_lean_start_only_the_strict_flag_and_the_attribution_setting_remain(self):
        (self.store / "settings.toml").write_text("[sessions]\nlean_start = false\n")
        self.tick()
        [argv] = self.ops_runs()
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--settings") + 1], json.dumps(NO_ATTRIBUTION))

    def test_a_missed_gap_is_counted_once_and_never_caught_up(self):
        self.tick()
        _, out, _ = self.tick(at=NOW + 4 * EVERY)
        self.assertEqual(self.sessions_run, 2)
        gap = [r for r in self.ops_records() if r["outcome"] == "missed"]
        self.assertEqual(gap, [{"ts": iso(NOW + 4 * EVERY), "repo": ROOT, "watch": "w", "outcome": "missed", "count": 3,
                                "from": iso(NOW + EVERY), "to": iso(NOW + 4 * EVERY)}])
        self.assertEqual((self.watch()["missed"], self.ops_records()[-1]["late_seconds"]), (3, 3 * EVERY))
        self.assertIn(f"3 look(s) missed between {iso(NOW + EVERY)} and {iso(NOW + 4 * EVERY)}: the tick did not run", self.notes())
        self.assertNotIn("notify:", out)

    def test_a_gap_shorter_than_the_tick_is_not_a_miss(self):
        self.cli("ops", "stop", "w")
        self.cli("ops", "start", "f", "goal", "--every", "30s", "--until", "6h", "--budget", "5")
        self.tick()
        self.tick(at=NOW + 120)
        self.assertEqual((self.sessions_run, self.watch("f")["missed"], self.ops_records()[-1]["late_seconds"]), (2, 0, 90))
        self.assertEqual([r for r in self.ops_records() if r["outcome"] == "missed"], [])

    def test_no_evidence_is_could_not_look_whatever_the_model_says(self):
        _, out, _ = self.tick({"outcome": "alarm", "evidence": 0, "verdict": "met"})
        w = self.watch()
        self.assertEqual((w["last_look"]["outcome"], w["last_look"]["saw"], w["state"], w["checks"]),
                         ("could-not-look", "no read succeeded", "watching", None))
        self.assertEqual(self.ops_records()[0]["evidence"], 0)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("notify:")]), 1)
        self.assertEqual(self.courier_lines(), [])

    def test_a_streak_of_could_not_look_notifies_once(self):
        bad = {"evidence": 0}
        notices = [self.tick(step, at=NOW + i * EVERY)[1].count("notify:") for i, step in enumerate((bad, bad, bad, {}, bad))]
        self.assertEqual(notices, [1, 0, 0, 0, 1])

    def test_an_unusable_output_is_could_not_look_with_the_reason(self):
        for i, (step, reason) in enumerate((({"rc": 124}, "timed out"), ({"rc": 1}, "exit 1"))):
            self.tick(step, at=NOW + i * EVERY)
            self.assertEqual(self.watch()["last_look"]["saw"], reason)

    def test_no_server_now_is_could_not_look_without_a_run(self):
        self.claude_json.write_text("{}")
        self.tick()
        self.assertEqual(self.sessions_run, 0)
        self.assertEqual((self.watch()["last_look"]["saw"], self.ops_records()[0]["cost_usd"]), ("the recorded list names no configured server", 0.0))

    def test_a_record_that_went_missing_or_cannot_be_read_is_could_not_look_without_a_run(self):
        self.start()
        self.access.unlink()
        self.tick()
        self.assertTrue(self.watch()["last_look"]["saw"].startswith("production access is not recorded"))
        self.access.write_text("{")
        self.tick(at=NOW + EVERY)
        self.assertEqual((self.sessions_run, self.watch()["last_look"]["outcome"]), (0, "could-not-look"))
        self.assertIn(self.access.name, self.watch()["last_look"]["saw"])

    def test_the_checks_are_stored_once_and_the_next_look_gets_them(self):
        self.tick()
        self.tick({"checks": {**CHECKS, "fine": "changed"}}, at=NOW + EVERY)
        self.assertEqual(self.watch()["checks"], CHECKS)
        self.assertEqual(self.notes().count("## Checks"), 1)
        self.assertIn("- read: mcp__metrics__query: errors in the last 10m", self.notes())
        self.assertIn('"fine": "0 errors"', self.ops_runs()[1][-1])
        self.assertNotIn("first look", self.ops_runs()[1][-1])
        self.assertIn("fine: 0 errors, 412 requests", self.ops_runs()[1][-1])

    def test_checks_that_are_not_whole_are_not_stored(self):
        self.tick({"checks": {"read": "x"}})
        self.assertIsNone(self.watch()["checks"])
        self.assertEqual(self.watch()["last_look"]["saw"], "no checks were set")

    def test_the_prompt_holds_the_last_twenty_entries_only(self):
        for i in range(25):
            self.tick(at=NOW + i * EVERY, answers={})
        prompt = self.ops_runs()[-1][-1]
        self.assertEqual(sum(line.startswith("- 20") for line in prompt.splitlines()), 20)

    def test_an_alarm_informs_once_across_two_alarm_looks(self):
        _, out, _ = self.tick({"outcome": "alarm", "saw": "9 errors"})
        _, out2, _ = self.tick({"outcome": "alarm", "saw": "11 errors"}, at=NOW + EVERY)
        self.assertEqual((self.watch()["state"], self.sessions_run), ("alarmed", 2))
        self.assertEqual([o.count("notify: OPS WATCH w: alarm - 9 errors") for o in (out, out2)], [1, 0])
        self.assertNotIn("11 errors", out2)
        self.assertEqual(len(self.courier_lines()), 1)
        self.assertIn(f"OPS WATCH w: alarm - 9 errors (notes: {self.store}/ops/w/notes.md)", self.courier_lines()[0])
        self.assertIn("state: alarmed", self.notes())
        self.assertEqual(self.watch()["next_look"], iso(NOW + 2 * EVERY))

    def test_a_dispatcher_that_cannot_be_told_is_printed(self):
        _, out, _ = self.tick({"outcome": "alarm"}, answers={COURIER: (1, "", "boom")})
        self.assertIn("ops watch w: no dispatcher was told", out)

    def test_done_ends_with_the_models_verdict(self):
        _, out, _ = self.tick({"outcome": "done", "saw": "served 30", "verdict": "met"})
        w = self.watch()
        self.assertEqual((w["state"], w["verdict"], w["next_look"]), ("ended", "met", None))
        self.assertIn("notify: OPS WATCH w: ended: met - served 30", out)
        self.assertEqual(len(self.courier_lines()), 1)
        self.tick(at=NOW + EVERY)
        self.assertEqual(self.sessions_run, 1)

    def test_a_verdict_that_is_none_of_the_three_is_inconclusive(self):
        self.tick({"outcome": "done", "verdict": "great"})
        self.assertEqual(self.watch()["verdict"], "inconclusive")

    def test_the_final_look_ends_the_watch_and_is_told_it_is_final(self):
        self.cli("ops", "stop", "w")
        self.cli("ops", "start", "f", "goal", "--every", "10s", "--until", "30s", "--budget", "5")
        self.tick(at=NOW + 5)
        self.assertEqual(self.watch("f")["state"], "watching")
        self.assertIn("Final look: no", self.ops_runs()[-1][-1])
        self.tick({"verdict": "not-met"}, at=NOW + 40)
        w = self.watch("f")
        self.assertEqual((w["state"], w["verdict"]), ("ended", "not-met"))
        self.assertIn("Final look: yes", self.ops_runs()[-1][-1])

    def test_the_final_look_without_evidence_is_inconclusive(self):
        self.cli("ops", "stop", "w")
        self.cli("ops", "start", "f", "goal", "--every", "10s", "--until", "30s", "--budget", "5")
        self.tick({"evidence": 0, "verdict": "met"}, at=NOW + 40)
        self.assertEqual((self.watch("f")["state"], self.watch("f")["verdict"]), ("ended", "inconclusive"))
        self.assertIn("the end time came", self.courier_lines()[0])

    def test_the_cap_is_the_remainder_when_that_is_smaller(self):
        self.cli("ops", "stop", "w")
        self.cli("ops", "start", "b", "goal", "--every", "10s", "--until", "6h", "--budget", "0.7")
        self.tick({"cost": 0.4})
        self.tick(at=NOW + 10)
        self.assertEqual([c[c.index("--max-budget-usd") + 1] for c in self.ops_runs()], ["0.5", "0.3"])

    def test_spending_the_budget_ends_it_and_informs(self):
        self.cli("ops", "stop", "w")
        self.cli("ops", "start", "b", "goal", "--every", "10s", "--until", "6h", "--budget", "0.3")
        _, out, _ = self.tick({"cost": 0.3})
        self.assertEqual(self.watch("b")["state"], "out-of-budget")
        self.assertIn("notify: OPS WATCH b: out of budget", out)
        self.assertEqual(len(self.courier_lines()), 1)
        self.tick(at=NOW + 10)
        self.assertEqual(self.sessions_run, 1)

    def test_a_watch_with_nothing_left_runs_no_look(self):
        w = self.watch()
        (self.store / "ops" / "w" / "watch.json").write_text(json.dumps({**w, "spent_usd": 5.0}))
        _, out, _ = self.tick()
        self.assertEqual((self.sessions_run, self.watch()["state"]), (0, "out-of-budget"))
        self.assertIn("notify: OPS WATCH w: out of budget", out)

    def test_an_unpriced_look_is_null_not_zero(self):
        self.tick({"cost": None})
        w = self.watch()
        self.assertEqual((self.ops_records()[0]["cost_usd"], w["spent_usd"], w["unpriced_ticks"]), (None, 0.0, 1))
        self.assertNotIn("reserved_usd", json.loads(self.cli("ops", "list", "--json")[1])["watches"][0])

    def test_an_unpriced_look_counts_its_cap_against_the_budget(self):
        self.cli("ops", "stop", "w")
        self.cli("ops", "start", "b", "goal", "--every", "10s", "--until", "6h", "--budget", "0.75")
        self.tick({"cost": None})
        self.tick({"cost": None}, at=NOW + 10)
        self.assertEqual([c[c.index("--max-budget-usd") + 1] for c in self.ops_runs()], ["0.5", "0.25"])
        w = self.watch("b")
        self.assertEqual((w["state"], w["spent_usd"], w["reserved_usd"]), ("out-of-budget", 0.0, 0.75))
        _, out, _ = self.tick(at=NOW + 20)
        self.assertEqual(self.sessions_run, 2)

    def test_stop_ends_the_looks_and_a_stop_during_a_look_stays(self):
        code, out, _ = self.cli("ops", "stop", "w")
        self.assertEqual((code, out, self.watch()["state"]), (0, "watch w: stopped\n", "stopped"))
        self.assertIn("state: stopped by the human", self.notes())
        self.tick()
        self.assertEqual(self.sessions_run, 0)
        self.assertEqual(self.cli("ops", "stop", "nope")[0], 2)
        self.start("x")
        self.tick({"hook": lambda: self.cli("ops", "stop", "x"), "outcome": "done"})
        self.assertEqual((self.watch("x")["state"], self.watch("x")["verdict"]), ("stopped", None))

    def test_a_stop_during_the_inform_stays(self):
        stop = lambda: (self.cli("ops", "stop", "w"), (0, json.dumps({"result": "sent"}), ""))[1]
        self.tick({"outcome": "alarm"}, answers={COURIER: stop})
        self.assertEqual((self.watch()["state"], self.sessions_run), ("stopped", 1))

    def test_a_watch_stopped_by_an_earlier_look_is_not_looked_at(self):
        self.clock += 1
        self.start("w2")
        self.tick({"hook": lambda: self.cli("ops", "stop", "w2")})
        self.assertEqual((self.sessions_run, self.watch("w2")["state"]), (1, "stopped"))

    def test_the_stop_works_inside_a_session(self):
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(self.cli("ops", "stop", "w")[0], 0)

    def test_the_look_runs_while_the_watch_lock_is_held(self):
        with factory.repo_lock(SimpleNamespace(root=ROOT), "watch") as free:
            self.assertTrue(free)
            _, out, _ = self.tick()
        self.assertIn("a tick is already running", out)
        self.assertEqual(self.sessions_run, 1)

    def test_a_look_in_progress_is_not_started_twice(self):
        with factory.repo_lock(SimpleNamespace(root=ROOT), "ops") as free:
            self.assertTrue(free)
            self.tick()
        self.assertEqual(self.sessions_run, 0)

    def test_one_watchs_failure_does_not_stop_the_next(self):
        self.clock += 1
        self.start("w2")
        _, out, _ = self.tick({"raise": "boom"})
        self.assertIn("ops watch w: RuntimeError: boom", out)
        self.assertEqual(self.sessions_run, 2)
        self.assertEqual({r["watch"] for r in self.ops_records()}, {"w2"})

    def test_another_repository_is_left_alone(self):
        w = self.watch()
        (self.store / "ops" / "w" / "watch.json").write_text(json.dumps({**w, "repo": "/other"}))
        self.tick()
        self.assertEqual(self.sessions_run, 0)


class ScheduleTests(OpsHarness):
    def setUp(self):
        super().setUp()
        self.put_plist()
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "blocked", "ts": iso(NOW - 2 * 3600)}])
        self.start()

    def orphan_tick(self, **kw):
        return self.tick(sessions=[], entries=["x"], answers={("osascript",): (0, "", "")}, **kw)

    def test_an_active_watch_keeps_the_job_when_the_dispatcher_is_gone(self):
        _, out, _ = self.orphan_tick()
        self.assertTrue(self.plist().exists())
        self.assertNotIn("removing the schedule", out)

    def test_an_active_watch_keeps_the_job_when_the_dispatcher_session_ends(self):
        stdin = json.dumps({"hook_event_name": "SessionEnd", "session_id": "sid-d", "cwd": ROOT, "reason": "other"})
        with mock.patch.object(factory.sys, "stdin", io.StringIO(stdin)):
            self.cli("hook", sessions=[DISPATCHER | {"name": "dispatcher-repo"}])
        self.assertTrue(self.plist().exists())

    def test_a_stopped_watch_does_not(self):
        self.cli("ops", "stop", "w")
        _, out, _ = self.orphan_tick()
        self.assertFalse(self.plist().exists())
        self.assertIn("removing the schedule", out)


class ListTests(OpsHarness):
    def test_the_json_is_exactly_the_contract(self):
        self.start("old", goal="the human's words")
        self.clock += 1
        self.start("new")
        self.cli("ops", "stop", "old")
        self.tick({"saw": "0 errors, 412 requests"})
        doc = json.loads(self.cli("ops", "list", "--json")[1])
        self.assertEqual(list(doc), ["watches"])
        self.assertEqual([w["name"] for w in doc["watches"]], ["new", "old"])
        w = doc["watches"][0]
        self.assertEqual(list(w), ["name", "repo", "goal", "state", "verdict", "started", "until", "every_seconds", "ticks", "missed",
                                   "last_look", "next_look", "spent_usd", "unpriced_ticks", "budget_usd", "notes"])
        types = {"name": str, "repo": str, "goal": str, "state": str, "started": str, "until": str, "every_seconds": int, "ticks": int,
                 "missed": int, "next_look": str, "spent_usd": float, "unpriced_ticks": int, "budget_usd": float, "notes": str}
        for key, kind in types.items():
            self.assertIsInstance(w[key], kind, key)
        self.assertIsNone(w["verdict"])
        self.assertEqual(w["last_look"], {"ts": iso(NOW + 1), "outcome": "fine", "saw": "0 errors, 412 requests", "late_seconds": 0})
        self.assertIsNone(doc["watches"][1]["last_look"])

    def test_the_table_and_an_empty_store(self):
        self.assertEqual(self.cli("ops", "list")[1], "")
        self.start()
        self.tick({"outcome": "done", "saw": "served 30", "verdict": "met"})
        _, out, _ = self.cli("ops", "list", at=NOW + 120)
        [line] = out.splitlines()
        self.assertEqual(line.split()[:2], ["w", "ended"])
        for text in ("(met)", "2m ago done: served 30", "$0.10/$5.00", f"{self.store}/ops/w/notes.md"):
            self.assertIn(text, line)


class DrainTests(unittest.TestCase):
    def setUp(self):  # drain reads the watch lock's mtime from the store
        store = tempfile.TemporaryDirectory()
        self.addCleanup(store.cleanup)
        patcher = mock.patch.object(factory, "STORE", Path(store.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_ticking_drain_gives_every_pass_to_the_ops_looks(self):
        passes = []

        def snapshot(repo):
            passes.append(1)
            raise factory.NotEvaluated("stop here")
        for tick, expected in ((mock.Mock(), 1), (None, 0)):
            ops = mock.Mock()
            with mock.patch.object(factory, "ops_tick", ops), mock.patch.object(factory, "snapshot", snapshot), \
                    mock.patch.object(factory, "end_priorities"), mock.patch.object(factory, "release_paused"):
                with redirect_stdout(io.StringIO()):
                    factory.drain(mock.Mock(root="/none"), None, tick=tick)
            self.assertEqual(ops.call_count, expected)

    def test_the_interval_counts_from_the_start_of_a_tick_not_its_end(self):
        clock, began = [0], []

        def snapshot(repo):
            clock[0] += 50
            return []

        def tick():
            began.append(clock[0])
            clock[0] += 100

        with mock.patch.object(factory.time, "time", lambda: clock[0]), mock.patch.object(factory, "ops_tick"), \
                mock.patch.object(factory, "snapshot", snapshot), mock.patch.object(factory, "end_priorities"), \
                mock.patch.object(factory, "release_paused"), mock.patch.object(factory, "gather"), \
                mock.patch.object(factory, "tasks"), mock.patch.object(factory, "ensure_stacks"), \
                mock.patch.object(factory, "lander_queue", return_value=[]), mock.patch.object(factory, "read_word"), \
                mock.patch.object(factory, "queue_holders", return_value={}), \
                mock.patch.object(factory, "lander_pass", return_value=([1], [], set())):
            factory.drain(mock.Mock(root="/none"), None, tick=tick, deadline=500)
        self.assertEqual(began[:2], [150, 300])  # counted from the end, the second would begin at 400


if __name__ == "__main__":
    unittest.main()
