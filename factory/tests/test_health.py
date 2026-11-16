import io
import json
import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import DISPATCHER, LEAN_SETTINGS, ROOT, WT, FakeRun, factory, repo_answers, stub_popen, task_entry

NOW = 1_800_000_000
HOUR = 3600
SID = "11111111-aaaa"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def register(sid):
    """Register session `sid` as the dispatcher of ROOT."""
    factory.repo_state(ROOT, "dispatcher", "json").write_text(json.dumps({"root": ROOT, "session": sid, "since": "t"}))


def row(slug, **kw):
    return {"slug": slug, "branch": f"worktree-{slug}", "path": f"{WT}/{slug}", "base": "main", "phase": "motion",
            "overtime": False, "nearing": False, "size": None, "limit": 2 * HOUR, "elapsed": None, "last_commit": None, "pr_url": "", "pr_state": "none", "state": None,
            "deliverable": None, "waits_on": None, "paused": None, **kw}


def lead(slug, sid=SID, **kw):
    return {"sessionId": sid, "name": slug, "cwd": f"{WT}/{slug}", "kind": "background", "status": "busy",
            "state": "working", "startedAt": (NOW - 3 * HOUR) * 1000, **kw}


FINISHED = {"status": "idle", "state": "done"}


def pic(**kw):
    sessions = kw.pop("sessions", [DISPATCHER | {"name": "dispatcher-repo"}])
    found, why = (None, "cannot list sessions") if sessions is None else factory.find_dispatcher(sessions, ROOT)
    return {"now": NOW, "repo": SimpleNamespace(root=ROOT, default="main", tasks=[], worktree_path=lambda slug: f"{WT}/{slug}"),
            "dispatcher": found and found["name"],
            "dispatcher_why": why, "sessions": sessions, "rows": [], "declared": {}, "events": [],
            "models": {"coder": "sonnet", "explorer": "haiku", "reviewer": "opus"},
            "gh": lambda *a: [], "monitor": None, "lgtm_from_author": True,
            **kw}


def check(name, p, threshold=None):
    return (factory.CRITERIA.get(name) or factory.FACTS[name])[0](p, threshold)


def ev(slug, event, **kw):
    return {"repo": ROOT, "slug": slug, "event": event, **kw}


class Home(unittest.TestCase):
    """A temporary HOME for the tests that read or write the store or ~/.claude/projects."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "XDG_CONFIG_HOME": "",
                                                "PATH": "/usr/bin:/opt/bin:/home/u/.local/bin:/bin",
                                                "CLAUDE_CODE_SESSION_ID": "sid-d"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = self.home / ".claude" / "factory"
        self.store.mkdir(parents=True)

    def transcript(self, name, mtime, sid=SID):
        path = self.home / ".claude" / "projects" / "proj" / name.format(sid=sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        os.utime(path, (mtime, mtime))
        return path


class CriteriaFileTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "criteria.json")
            path.write_text(text)
            with mock.patch.object(factory, "CRITERIA_PATH", path):
                return factory.load_criteria()

    def entry(self, **kw):
        return json.dumps([{"name": "mix-shift", "job": "digest", "threshold": 5, "what": "w", **kw}])

    def test_the_shipped_file_holds_only_the_five_digest_criteria(self):
        shipped = factory.load_criteria()
        self.assertEqual(sorted(c["name"] for c in shipped), sorted(factory.CRITERIA))
        self.assertEqual(len(shipped), 5)
        self.assertTrue(all(c["job"] == "digest" and c["what"].strip() for c in shipped))

    def test_a_deleted_line_turns_its_criterion_off(self):
        shipped = factory.load_criteria()
        p = pic(rows=[row("x")])
        with mock.patch.dict(factory.CRITERIA, {"mix-shift": (lambda p, t: [("x", "x", "shifted")], True)}):
            self.assertIn("mix-shift", [f["criterion"] for f in factory.evaluate(p, shipped)[0]])
            self.assertNotIn("mix-shift", [f["criterion"] for f in factory.evaluate(p, [c for c in shipped if c["name"] != "mix-shift"])[0]])

    def test_no_watch_threshold_is_left_in_the_file_or_the_facts(self):
        text = (factory.PLUGIN_ROOT / "criteria.json").read_text()
        for gone in ("quiet", "nearing-limit", "past-limit", "review-loops", "deep-stack", "cost-outlier", "wrong-model", "escalate"):
            self.assertNotIn(gone, text)
        self.assertEqual(set(factory.FACTS) & set(factory.CRITERIA), set())
        self.assertEqual({n for n, (_, every) in factory.FACTS.items() if every},
                         {"run-stopped", "collision", "new-spec", "churning", "ended-undeclared"})

    def test_refusals(self):
        for text, why in ((self.entry(name="nope"), "unknown criterion"),
                          (self.entry(threshold=None), "threshold"),
                          (self.entry(threshold=0), "threshold"),
                          (self.entry(threshold=-5), "threshold"),
                          (self.entry(threshold=True), "threshold"),
                          (self.entry(what=" "), "what"),
                          ('{"name": "mix-shift"}', "JSON list"),
                          ("not json", "JSON")):
            with self.subTest(why=why, text=text), self.assertRaises(factory.Fail) as cm:
                self.load(text)
            self.assertIn(why, str(cm.exception))
            self.assertEqual(cm.exception.code, 2)


class CriteriaTests(Home):
    def test_ci_red_default(self):
        def runs(*items):
            return pic(gh=lambda *a: [{"workflowName": w, "status": s, "conclusion": c, "url": f"u/{w}"}
                                      for w, s, c in items])
        red = check("ci-red-default", runs(("ci", "completed", "failure")))
        self.assertEqual(red, [(None, "ci", "failure u/ci")])
        self.assertEqual(check("ci-red-default", runs(("ci", "completed", "success"))), [])
        cancelled_over_failure = runs(("ci", "completed", "cancelled"), ("ci", "completed", "failure"))
        self.assertEqual(check("ci-red-default", cancelled_over_failure), [(None, "ci", "failure u/ci")])
        cancelled_over_success = runs(("ci", "completed", "cancelled"), ("ci", "completed", "success"),
                                      ("ci", "completed", "failure"))
        self.assertEqual(check("ci-red-default", cancelled_over_success), [])
        self.assertEqual(check("ci-red-default", runs(("ci", "in_progress", None), ("ci", "completed", "success"))), [])
        two = check("ci-red-default", runs(("ci", "completed", "success"), ("deploy", "completed", "failure")))
        self.assertEqual([f[1] for f in two], ["deploy"])
        asked = []
        check("ci-red-default", pic(gh=lambda *a: asked.append(a) or []))
        self.assertEqual(asked[0][:5], (list, "run", "list", "--branch", "main"))

    def test_dispatcher_missing(self):
        d = DISPATCHER | {"name": "dispatcher-repo"}
        live = [row("x")]
        cases = {"zero": ([], 1), "one": ([d], 0), "two": ([d, d | {"sessionId": "sid-e"}], 1),
                 "a finished one does not count": ([d, d | {"sessionId": "sid-e", "kind": "background", **FINISHED}], 0),
                 "other names do not count": ([DISPATCHER | {"name": "other"}], 1)}
        for label, (sessions, fires) in cases.items():
            with self.subTest(label):
                found = check("dispatcher-missing", pic(sessions=sessions, rows=live))
                self.assertEqual(len(found), fires)
        self.assertEqual(check("dispatcher-missing", pic(sessions=[], rows=[])), [])
        self.assertEqual(check("dispatcher-missing", pic(sessions=[], rows=[row("x", phase="settled")])), [])
        for phase in ("blocked", "review"):
            self.assertEqual(len(check("dispatcher-missing", pic(sessions=[], rows=[row("x", phase=phase)]))), 1)

    def test_ended_undeclared(self):
        r = {"state": "working", "ts": iso(NOW - HOUR)}
        repo = SimpleNamespace(root=ROOT, default="main", tasks=[{"path": f"{WT}/x", "branch": "worktree-x"}])
        done = lead("x", status="idle", state="done")
        found = check("ended-undeclared", pic(declared={"x": r}, sessions=[done], repo=repo))
        self.assertEqual(found, [("x", "x", "declared working 1h ago; its lead has no live session and no run holds its lock")])
        self.assertEqual(check("ended-undeclared", pic(declared={"x": r}, sessions=[], repo=repo))[0][0], "x")
        for label, declared, sessions in (("a live lead", {"x": r}, [lead("x")]), ("handed over", {"x": r | {"state": "handed-over"}}, [done]),
                                          ("blocked", {"x": r | {"state": "blocked"}}, []), ("no worktree", {"y": r}, [])):
            with self.subTest(label):
                self.assertEqual(check("ended-undeclared", pic(declared=declared, sessions=sessions, repo=repo)), [])

    def test_ended_undeclared_waits_for_a_run_holding_the_tasks_lock(self):
        r = {"state": "working", "ts": iso(NOW - HOUR)}
        repo = SimpleNamespace(root=ROOT, default="main", tasks=[{"path": f"{WT}/x", "branch": "worktree-x"}])
        p = pic(declared={"x": r}, sessions=[], repo=repo)
        self.assertEqual(len(check("ended-undeclared", p)), 1)
        self.assertEqual(os.listdir(self.store), [])  # asking leaves no lock file
        with factory.repo_lock(repo, "tech-lead-x") as free:
            self.assertTrue(free)
            self.assertEqual(check("ended-undeclared", p), [])
        self.assertEqual(len(check("ended-undeclared", p)), 1)

    def test_ended_undeclared_is_not_evaluated_without_sessions_but_only_for_a_working_task(self):
        r = {"state": "working", "ts": iso(NOW - HOUR)}
        repo = SimpleNamespace(root=ROOT, default="main", tasks=[{"path": f"{WT}/x", "branch": "worktree-x"}])
        unplaced = {"id": "s9", "kind": "background", "status": "busy", "state": "working"}
        for sessions in (None, [unplaced]):
            with self.assertRaises(factory.NotEvaluated):
                check("ended-undeclared", pic(declared={"x": r}, sessions=sessions, repo=repo))
        self.assertEqual(check("ended-undeclared", pic(declared={}, sessions=None, repo=repo)), [])

    def test_facts_that_cannot_be_read_are_not_evaluated(self):
        facts = [{"name": n} for n in factory.FACTS]
        handed = {"x": {"state": "working", "ts": iso(NOW - HOUR)}}
        repo = SimpleNamespace(root=ROOT, default="main", remote="origin", tasks=[{"path": f"{WT}/x", "branch": "worktree-x"}])
        firings, skipped = factory.evaluate(pic(sessions=None, rows=None, events=None, models={}, declared=handed, repo=repo), facts)
        self.assertEqual(firings, [])
        names = {s["criterion"] for s in skipped}
        self.assertEqual(names, {"dispatcher-missing", "ended-undeclared", "ci-red-handover", "base-merged"})
        self.assertIn("sessions cannot be listed", {s["criterion"]: s["why"] for s in skipped}["dispatcher-missing"])

    def test_gh_failing_is_not_evaluated(self):
        def broken(*args):
            raise factory.NotEvaluated("gh run list failed: no auth")
        firings, skipped = factory.evaluate(pic(gh=broken, rows=[row("x", phase="review", pr_url="u")]),
                                            [{"name": "ci-red-default"}, {"name": "ci-red-handover"}])
        self.assertEqual(firings, [])
        self.assertEqual({s["criterion"] for s in skipped}, {"ci-red-default", "ci-red-handover"})

    def test_phase_facts_are_not_evaluated_when_phases_cannot_be_trusted(self):
        phase_based = [{"name": n} for n in ("dispatcher-missing", "ci-red-handover", "base-merged")]
        unplaced = {"id": "s9", "kind": "background", "status": "busy", "state": "working"}
        for label, p in (("an unplaced session", pic(rows=[row("x")], sessions=[unplaced])),
                         ("an unknown pull request state", pic(rows=[row("x", pr_state="unknown")]))):
            with self.subTest(label):
                firings, skipped = factory.evaluate(p, phase_based)
                self.assertEqual(firings, [])
                self.assertEqual([s["criterion"] for s in skipped], [c["name"] for c in phase_based])
                self.assertIn("no working directory" if "unplaced" in label else "pull request state is unknown", skipped[0]["why"])
        self.assertEqual(factory.evaluate(pic(rows=[row("x", branch=None, pr_state="unknown")]), phase_based)[1], [])

    def test_ci_red_handover(self):
        r = row("x", phase="review", pr_url="https://pr/1")
        asked = []

        def checks(*a):
            asked.append(a)
            return [{"name": "test", "bucket": "fail"}, {"name": "lint", "bucket": "pass"}]
        found = check("ci-red-handover", pic(rows=[r], gh=checks))
        self.assertEqual(found, [("x", "x", "failed: test https://pr/1")])
        self.assertEqual(asked, [(list, "pr", "checks", "https://pr/1", "--json", "name,bucket")])
        green = pic(rows=[r], gh=lambda *a: [{"name": "t", "bucket": "pass"}, {"name": "p", "bucket": "pending"}])
        self.assertEqual(check("ci-red-handover", green), [])
        nothing = pic(rows=[row("x", phase="motion", pr_url="u"), row("y", phase="review")], gh=checks)
        self.assertEqual(check("ci-red-handover", nothing), [])

    def test_gh_json_reports_failures_wrong_shapes_and_keeps_json_from_a_failing_exit(self):
        repo = SimpleNamespace(root=ROOT)
        for result in ((1, "", "not logged in"), (1, "garbage", "")):
            with mock.patch.object(factory, "run", FakeRun({("gh",): result})), self.assertRaises(factory.NotEvaluated):
                factory.gh_json(repo, list, "pr", "checks", "u")
        for kind, out in ((list, "{}"), (dict, "[]"), (list, "[1]"), (list, "null")):
            with self.subTest(kind=kind, out=out), mock.patch.object(factory, "run", FakeRun({("gh",): (0, out, "")})), \
                    self.assertRaises(factory.NotEvaluated) as cm:
                factory.gh_json(repo, kind, "run", "list")
            self.assertNotIsInstance(cm.exception, factory.NoJson)
        with mock.patch.object(factory, "run", FakeRun({("gh",): (8, '[{"bucket": "fail"}]', "")})):
            self.assertEqual(factory.gh_json(repo, list, "pr", "checks", "u"), [{"bucket": "fail"}])

    def test_a_wrong_shape_from_gh_is_not_evaluated_but_a_pr_without_checks_is_skipped(self):
        repo = SimpleNamespace(root=ROOT, default="main")
        gh = lambda *a: factory.gh_json(repo, *a)  # noqa: E731
        listed = {("gh", "run"): (0, '{"a": 1}', ""), ("gh", "pr", "view"): (0, "[]", ""),
                  ("gh", "pr", "checks"): (1, "", "no checks reported")}
        reviewing = row("x", phase="review", pr_url="https://pr/1")
        with mock.patch.object(factory, "run", FakeRun(listed)):
            _, skipped = factory.evaluate(pic(gh=gh, rows=[reviewing]), [{"name": "ci-red-default"}])
        self.assertEqual({s["criterion"] for s in skipped}, {"ci-red-default"})
        with mock.patch.object(factory, "run", FakeRun(listed)):
            self.assertEqual(check("ci-red-handover", pic(gh=gh, rows=[reviewing])), [])

    def test_last_activity_survives_a_null_start(self):
        r = row("x", last_commit=NOW - 100)
        self.assertEqual(factory.last_activity(pic(), r, lead("x", startedAt=None)), NOW - 100)
        self.assertEqual(factory.last_activity(pic(), row("x"), lead("x", startedAt=None)), None)

    def test_fold_skips_records_without_a_string_id(self):
        records = [{"repo": ROOT, "decision": "escalated"}, {"repo": ROOT, "id": 7, "cleared": True},
                   {"repo": ROOT, "id": ["a"], "criterion": "c"}, fired("a1")]
        folded, misses = factory.fold(records)
        self.assertEqual(([f["id"] for f in folded], misses), (["a1"], []))


    def test_a_failing_pr_checks_call_is_not_evaluated(self):
        repo = SimpleNamespace(root=ROOT, default="main")
        gh = lambda *a: factory.gh_json(repo, *a)  # noqa: E731
        reviewing = row("x", phase="review", pr_url="https://pr/1")
        with mock.patch.object(factory, "run", FakeRun({("gh", "pr", "checks"): (1, "", "API rate limit exceeded")})):
            with self.assertRaises(factory.NotEvaluated):
                check("ci-red-handover", pic(gh=gh, rows=[reviewing]))

    def test_an_exited_dispatcher_does_not_count(self):
        exited = {"sessionId": "sid-d", "name": "dispatcher-repo", "cwd": ROOT, "kind": "background", "state": "done"}
        self.assertEqual(len(check("dispatcher-missing", pic(sessions=[exited], rows=[row("x")]))), 1)

    def test_events_are_read_from_the_month_files_too(self):
        (self.store / "events.jsonl").write_text(json.dumps(ev("x", "TaskStarted", ts=iso(NOW - 9 * HOUR))) + "\n")
        (self.store / "events-2027-01.jsonl").write_text("".join(
            json.dumps(e) + "\n" for e in [ev("x", "SubagentStart", role="reviewer", ts=iso(NOW - HOUR)),
                                           ev("x", "SubagentStart", role="reviewer", ts=iso(NOW - 2 * HOUR)),
                                           ev("y", "SubagentStart", role="reviewer", ts=iso(NOW)) | {"repo": "/other"}]))
        events = factory.repo_log("events", ROOT)
        self.assertEqual([e["event"] for e in events], ["TaskStarted", "SubagentStart", "SubagentStart"])
        self.assertEqual([e["ts"] for e in events], sorted(e["ts"] for e in events))
        self.assertEqual(len(factory.since_start(events, "x")[0]), 2)
        self.assertIsNone(factory.repo_log("nothing", ROOT))

    def test_the_tail_of_a_long_transcript_drops_the_cut_line_and_odd_lines(self):
        entry = lambda i: json.dumps({"timestamp": f"T{i}", "message": {"role": "user", "content": f"m{i}"}})  # noqa: E731
        path = self.transcript("{sid}.jsonl", NOW)
        lines = [entry(i) + " " * 200 for i in range(400)]
        lines[-3] = json.dumps({"timestamp": "TU", "message": {"role": "user", "content": "a\u2028b"}},
                               ensure_ascii=False)
        lines[-2] = "[" * 20000
        path.write_text("\n".join(lines) + "\n")
        tail = factory.transcript_tail(path)
        self.assertEqual(len(tail), 15)
        self.assertEqual(tail[-1], "T399 user: m399")
        self.assertEqual(tail[-2], "TU user: a\u2028b")
        self.assertTrue(all(t.startswith("T") for t in tail))
        size = path.stat().st_size
        self.assertGreater(size, 65536)
        with open(path, "rb") as f:
            f.seek(size - 65536)
            cut = f.read().split(b"\n")[0]
        self.assertNotEqual(cut, b"")


def monitor_out(*tasks, repo=ROOT, **extra):
    """What `isshoni sessions --json` prints: (slug, total, main_pct) per task of one repository."""
    return json.dumps({"repos": [{"repo_path": repo, "tasks": [
        {"task": slug, "cost": {"total": total, "main_pct": main_pct}} for slug, total, main_pct in tasks]}], **extra})


def landed(*slugs):
    return lambda *a: [{"headRefName": f"worktree-{slug}"} for slug in slugs]


BASE = [(f"t{i}", float(i), 50.0) for i in range(1, 6)]  # five landed tasks: median 3.00


class CostTests(unittest.TestCase):
    def ask(self, fn, tasks=(), answer=None, calls=None, gh=None, p=None):
        p = p or pic(monitor="/m/isshoni", rows=[row("x")], gh=gh or landed("t1", "t2", "t3", "t4", "t5"))

        def fake(argv, cwd=None, timeout=None, input=None):
            if calls is not None:
                calls.append((argv, timeout))
            return answer or (0, monitor_out(*BASE, *tasks), "")
        with mock.patch.object(factory, "run", fake):
            return fn(p)

    def test_no_monitor_is_not_evaluated(self):
        with self.assertRaisesRegex(factory.NotEvaluated, 'no monitor: set paths.monitor in ~/.claude/factory/settings.toml'):
            factory.costs(pic(rows=[row("x")]))

    def test_the_monitor_is_run_once_for_the_costs_and_the_median_and_a_failure_is_remembered_too(self):
        calls = []
        p = pic(monitor="/m/isshoni", rows=[row("x")], gh=landed("t1", "t2", "t3", "t4", "t5"))
        self.ask(factory.costs, calls=calls, p=p)
        self.ask(factory.landed_median, calls=calls, p=p)
        self.assertEqual(calls, [(["/m/isshoni", "sessions", "--json"], 120)])
        calls, p = [], pic(monitor="/m/isshoni", rows=[row("x")], gh=landed("t1"))
        for fn in (factory.costs, factory.landed_median):
            with self.assertRaisesRegex(factory.NotEvaluated, "the monitor failed: boom"):
                self.ask(fn, answer=(1, "", "warn\nboom\n"), calls=calls, p=p)
        self.assertEqual(len(calls), 1)

    def test_a_monitor_that_fails_or_answers_oddly_is_not_evaluated(self):
        bad_task = '{"repos": [{"repo_path": "/repo", "tasks": [{"task": "x", "cost": {}}]}]}'
        for answer, why in (((1, "", ""), "exit 1"), ((124, "", "timed out"), "timed out"), ((0, "no", ""), "unexpected"),
                            ((0, "[]", ""), "unexpected"), ((0, '{"repos": [{"repo_path": "/repo"}]}', ""), "unexpected"),
                            ((0, bad_task, ""), "unexpected")):
            with self.subTest(why=why), self.assertRaisesRegex(factory.NotEvaluated, f"the monitor failed: .*{why}"):
                self.ask(factory.costs, answer=answer)

    def test_only_this_repositorys_tasks_count(self):
        other = (0, monitor_out(*BASE, ("x", 99.0, 99.0), repo="/elsewhere"), "")
        with self.assertRaisesRegex(factory.NotEvaluated, "only 0 landed"):
            self.ask(factory.landed_median, answer=other)

    def test_the_median_needs_five_landed_tasks_with_a_cost(self):
        zero = [("t1", 0.0, 50.0), *BASE[1:]]
        for slugs, tasks, n in (((), BASE, 0), (("t1", "t2", "t3", "t4"), BASE, 4),
                                (("t1", "t2", "t3", "t4", "t5"), zero, 4)):
            answer = (0, monitor_out(*tasks), "")
            with self.subTest(slugs=slugs, n=n), self.assertRaisesRegex(
                    factory.NotEvaluated, f"only {n} landed tasks have a cost; 5 are needed for a median"):
                self.ask(factory.landed_median, answer=answer, gh=landed(*slugs))

    def test_the_median_of_five(self):
        self.assertEqual(self.ask(factory.landed_median), (3.0, 5))


class RunTests(unittest.TestCase):
    def test_a_timeout_returns_124(self):
        self.assertEqual(factory.run(["sleep", "5"], timeout=0.1), (124, "", "timed out"))
        self.assertEqual(factory.run(["true"], timeout=5)[0], 0)


class Harness(Home):
    """Runs the CLI against a faked `run`, a temporary store, a fixed clock and its own criteria and role files."""

    CRITERIA = []

    def setUp(self):
        super().setUp()
        self.fake = None
        self.role = self.home / "site-manager.md"
        self.role.write_text("SITE MANAGER RULES")
        self.guidance = self.home / "site-manager-cases.md"
        self.guidance.write_text("SITE MANAGER CASES")
        self.criteria_path = self.home / "criteria.json"
        self.use(*self.CRITERIA)
        (self.store / "looks-2027-01.jsonl").write_text(json.dumps(look(NOW - 60)) + "\n")  # a look a minute ago: no pace is due
        self.clock = NOW
        self.tools = {"git": "/usr/bin/git", "gh": "/opt/bin/gh", "claude": "/home/u/.local/bin/claude",
                      "crontab": "/usr/bin/crontab", "notify-send": "/usr/bin/notify-send"}
        for patcher in (mock.patch.object(factory.sys, "platform", "darwin"),
                        mock.patch.object(factory.shutil, "which", lambda tool: self.tools.get(tool)),
                        mock.patch.object(factory, "FACTORY", Path("/plug/factory/bin/factory")),
                        mock.patch.object(factory, "CRITERIA_PATH", self.criteria_path),
                        mock.patch.object(factory, "review_tick", lambda repo: None),  # these tests do not test the review
                        mock.patch.object(factory, "ROLE_PATH", self.role),
                mock.patch.object(factory, "GUIDANCE_PATH", self.guidance),
                        mock.patch.object(factory, "now", lambda: iso(self.clock)),
                        mock.patch.object(factory.time, "time", lambda: self.clock),
                        mock.patch.object(factory.time, "sleep", lambda seconds: setattr(self, "clock", self.clock + seconds))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use(self, *names):
        shipped = {c["name"]: c for c in factory.load_criteria()}
        self.criteria_path.write_text("[" + ",\n".join(json.dumps(shipped[n]) for n in names) + "]")

    def seed(self, name, records):
        with open(self.store / f"{name}.jsonl", "a") as f:
            f.writelines(json.dumps(r) + "\n" for r in records)

    def records(self, name="firings"):
        return [r for _, path in factory.log_files(name) for r in factory.read_file(path)]

    def cli(self, *argv, answers=None, sessions=(DISPATCHER | {"name": "dispatcher-repo"},), entries=(), at=None):
        base = repo_answers([task_entry(s) for s in entries], sessions)
        base[("claude", "-p")] = reply()
        base[("gh", "pr", "list")] = (0, "[]", "")
        base[("gh", "run", "list")] = (0, "[]", "")
        self.fake = FakeRun({**base, **(answers or {})})
        if at is not None:
            self.clock = at
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(factory, "run", self.fake), redirect_stdout(out), redirect_stderr(err):
            code = factory.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def claude_runs(self):
        return [c for c in self.fake.calls if c[:2] == ["claude", "-p"]]

    def overtime(self, slug="x"):
        return lead(slug, pid=4242, startedAt=int((self.clock - 3 * HOUR) * 1000))

    def tick(self, **kw):
        """One watch tick with task x declared working and its lead gone: `ended-undeclared` fires."""
        kw.setdefault("sessions", [DISPATCHER | {"name": "dispatcher-repo"}])
        kw.setdefault("entries", ["x"])
        if not self.records("tasks"):
            self.seed("tasks", [{"repo": ROOT, "slug": "x", "role": "lead", "state": "working", "ts": iso(NOW - HOUR)}])
        return self.cli("watch", **kw)

    def looks(self):
        return [r for r in self.records("looks") if r["ts"] != iso(NOW - 60)]


def reply(decisions=(), sent=True, cost=0.15):
    doc = {"structured_output": {"sent": sent, "decisions": list(decisions)}}
    return 0, json.dumps({**doc, **({"total_cost_usd": cost} if cost is not None else {})}), ""


def choice(fid, decision="escalated", reason="because"):
    return {"id": fid, "decision": decision, "reason": reason}


def fired(fid, criterion="ended-undeclared", subject="x", ts=NOW - 10 * HOUR, repo=ROOT):
    return {"ts": iso(ts), "repo": repo, "id": fid, "criterion": criterion, "slug": subject, "subject": subject, "detail": "d"}


def look(ts, **kw):
    return {"ts": iso(ts), "repo": ROOT, "woken_by": [], "firings": [], "tasks": 0, "calls": 0, "cost_usd": None, "seconds": 0,
            "run": None, "model": "opus", "prs": [], **kw}


def decided(fid, decision, ts=NOW - HOUR, reason="why"):
    return {"ts": iso(ts), "repo": ROOT, "id": fid, "decision": decision, "reason": reason, "by": "site-manager"}


class HealthTests(Harness):
    def test_table_and_json_write_nothing(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "role": "lead", "state": "working", "ts": iso(NOW - HOUR)}])
        before = {p.name: p.read_bytes() for p in self.store.iterdir()}
        code, out, _ = self.cli("health", entries=["x"])
        self.assertEqual((code, out), (0, "ended-undeclared  x  declared working 1h ago; its lead has no live session and no run holds its lock\n"))
        code, out, _ = self.cli("health", "--json", entries=["x"])
        doc = json.loads(out)
        self.assertEqual([(f["criterion"], f["slug"], f["subject"]) for f in doc["firings"]], [("ended-undeclared", "x", "x")])
        self.assertNotIn("what", doc["firings"][0])
        self.assertEqual(doc["not_evaluated"], [])
        self.assertEqual([t["slug"] for t in doc["tasks"]], ["x"])
        self.assertEqual({p.name: p.read_bytes() for p in self.store.iterdir()}, before)
    def test_nothing_fired_and_not_evaluated_lines(self):
        code, out, _ = self.cli("health")
        self.assertEqual((code, out), (0, "nothing fired\n"))
        code, out, _ = self.cli("health", answers={("claude", "agents"): (1, "", "boom")})
        self.assertEqual(code, 0)
        self.assertIn("not evaluated: dispatcher-missing: sessions cannot be listed", out)

    def test_the_dispatcher_option(self):
        _, out, _ = self.cli("health", "--dispatcher", "boss", entries=["x"])
        self.assertIn("no dispatcher session for /repo", out)
        boss = DISPATCHER | {"name": "boss"}
        _, out, _ = self.cli("health", "--dispatcher", "boss", sessions=[boss], entries=["x"])
        self.assertNotIn("dispatcher-missing", out)

    def test_a_bad_criteria_file_is_a_usage_error(self):
        self.criteria_path.write_text('[{"name": "nope"}]')
        code, _, err = self.cli("watch")
        self.assertEqual(code, 2)
        self.assertIn("unknown criterion", err)
class WatchTests(Harness):
    def test_nothing_fired_starts_no_claude(self):
        code, out, _ = self.tick(sessions=[DISPATCHER | {"name": "dispatcher-repo"}], entries=[])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(f"{iso(NOW)} watch repo\n"))
        self.assertIn("nothing new", out)
        self.assertEqual(self.claude_runs(), [])
        self.assertEqual(self.records(), [])

    def test_the_site_manager_is_given_the_name_the_dispatcher_has_now(self):
        register("sid-d")
        self.tick(sessions=[DISPATCHER | {"name": "renamed", "pid": 7}])
        [argv] = self.claude_runs()
        self.assertIn("The dispatcher's session name is renamed.", argv[-1])

    def test_no_site_manager_runs_when_no_dispatcher_can_be_addressed(self):
        register("sid-d")
        self.tick(sessions=[DISPATCHER | {"name": None, "pid": 7}])
        self.assertEqual(self.claude_runs(), [])
        self.assertIn("ended-undeclared", [r.get("criterion") for r in self.records()])
        self.assertEqual(self.looks(), [])

    def test_a_new_firing_is_logged_and_sent(self):
        code, out, _ = self.tick()
        [record] = self.records()
        self.assertEqual((code, record["criterion"], record["subject"], record["repo"]), (0, "ended-undeclared", "x", ROOT))
        self.assertNotIn("escalate", record)
        self.assertEqual(len(record["id"]), 12)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertIn(f"{record['id']}  ended-undeclared  x  undecided", out)
        [looked] = self.looks()
        self.assertEqual((looked["woken_by"], looked["firings"]), ([{"event": "firing", "id": record["id"], "criterion": "ended-undeclared", "slug": "x"}], [record["id"]]))
    def test_the_site_manager_runs_on_the_configured_model(self):
        p = pic(dispatcher="d", lean_start=False, monitor=None)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.toml"
            path.write_text('[models]\nsite_manager = "claude-opus-4-1"\n')
            with mock.patch.object(factory, "SETTINGS_PATH", path):
                argv = factory.site_manager_argv(p, [])
        self.assertEqual(argv[:4], ["claude", "-p", "--model", "claude-opus-4-1"])

    def test_the_site_manager_can_read_and_cannot_write_with_or_without_lean_start(self):
        read_only = {"Read", "Grep", "Glob", "SendMessage", "ToolSearch",
                     "Bash(factory status)", "Bash(factory status *)", "Bash(factory health)", "Bash(factory health *)",
                     "Bash(factory log *)", "Bash(factory firings)", "Bash(factory firings *)", "Bash(factory feedback)",
                     "Bash(factory feedback --json)", "Bash(ps *)", "Bash(pgrep *)", "Bash(gh pr view *)",
                     "Bash(gh pr checks *)", "Bash(gh pr list *)", "Bash(gh run view *)", "Bash(gh run list *)",
                     "Bash(claude agents --json --all)", "Bash(/m/isshoni sessions *)"}
        for lean in (True, False):
            with self.subTest(lean_start=lean):
                p = pic(dispatcher="d", lean_start=lean, monitor="/m/isshoni")
                with mock.patch.object(factory, "lean_flags", return_value=["--strict-mcp-config"]):
                    argv = factory.site_manager_argv(p, [])
                [tools] = [a for a in argv if a.startswith("--tools=")]
                self.assertEqual(tools, "--tools=Read,Grep,Glob,Bash,SendMessage,ToolSearch")
                allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
                self.assertEqual(allowed, [*factory.SITE_MANAGER_TOOLS, "Bash(/m/isshoni sessions *)"])
                self.assertEqual(set(allowed), read_only)
                denied = argv[argv.index("--disallowedTools") + 1:argv.index("--disallowedTools") + 4]
                self.assertEqual(denied, ["Edit", "Write", "NotebookEdit"])
                self.assertEqual(argv[argv.index("--max-turns") + 1], "30")
                self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1.50")
                self.assertEqual(argv[argv.index("--setting-sources"):][:2], list(factory.SETTINGS_ISOLATION))
                self.assertEqual("--strict-mcp-config" in argv, lean)

    def test_without_a_monitor_there_is_no_monitor_rule(self):
        p = pic(dispatcher="d", lean_start=False, monitor=None)
        argv = factory.site_manager_argv(p, [])
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        self.assertEqual(allowed, list(factory.SITE_MANAGER_TOOLS))

    maxDiff = None

    def test_the_launch_argv(self):
        path = self.transcript("{sid}.jsonl", NOW - 60)
        entries = [{"timestamp": "T0", "message": {"role": "user", "content": "start"}}] + [
            {"timestamp": f"T{i}", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "x" * 400}, {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}}]}}
            for i in range(1, 17)]
        entries.append({"timestamp": "TR", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "ok"}]}]}})
        path.write_text("partial\n" + "".join(json.dumps(e) + "\nnot json\n" for e in entries) + '{"no": "message"}\n')
        self.tick(sessions=[DISPATCHER | {"name": "dispatcher-repo"}, lead("x", **FINISHED)])
        [argv] = self.claude_runs()
        self.assertEqual(argv[:-1], [
            "claude", "-p", "--model", "opus", "--permission-mode", "dontAsk", "--strict-mcp-config", "--settings",
            json.dumps(LEAN_SETTINGS), "--disable-slash-commands", "--tools=Read,Grep,Glob,Bash,SendMessage,ToolSearch",
            "--allowedTools", *factory.SITE_MANAGER_TOOLS, "--disallowedTools", "Edit", "Write", "NotebookEdit",
            *factory.SETTINGS_ISOLATION, "--max-turns", "30", "--max-budget-usd", "1.50",
            "--output-format", "json", "--json-schema", json.dumps(factory.RUN_SCHEMA),
            "--append-system-prompt", "SITE MANAGER RULES\n\nSITE MANAGER CASES"])
        schema = factory.RUN_SCHEMA
        self.assertEqual(schema["required"], ["sent", "decisions"])
        self.assertEqual(schema["properties"]["decisions"]["items"]["required"], ["decision", "reason", "looked"])
        self.assertTrue({"id", "entry", "slug", "detail"} <= set(schema["properties"]["decisions"]["items"]["properties"]))
        self.assertEqual(schema["properties"]["decisions"]["items"]["properties"]["to"]["enum"],
                         ["dispatcher", "lead"])
        self.assertEqual(schema["properties"]["decisions"]["items"]["properties"]["decision"]["enum"],
                         ["escalated", "suppressed", "paused", "extended", "told"])
        head, _, body = argv[-1].partition("\n\n")
        self.assertEqual(head, f"Job: watch. Repo: {ROOT}. The dispatcher's session name is dispatcher-repo.")
        picture = json.loads(body)
        self.assertEqual(list(picture), ["since", "woken_by", "firings", "tasks", "landed_median_usd"])
        [sent] = picture["firings"]
        self.assertEqual(sent["id"], self.records()[0]["id"])
        self.assertEqual((sent["criterion"], sent["slug"], sent["subject"]), ("ended-undeclared", "x", "x"))
        self.assertTrue(sent["detail"])
        for gone in ("what", "escalate", "session_id", "transcript"):
            self.assertNotIn(gone, sent)
        self.assertEqual(sent["task"], {"phase": "motion", "state": "working", "session": "done", "elapsed": "3h",
                                        "last_commit": "?", "dirty": None, "ahead": None, "behind": None,
                                        "pr_state": "none", "pr_url": "", "limit": "2h", "size": None,
                                        "nearing": False, "overtime": False,
                                        "worktree": f"{WT}/x", "branch": "worktree-x", "lead": "x", "pid": None})
        self.assertEqual(sent["transcripts"], [str(path)])
        tail = sent["transcript_tail"]
        self.assertEqual(len(tail), 15)
        self.assertEqual(tail[0][:20], "T3 assistant: " + "x" * 6)
        self.assertEqual(tail[-1], "TR user: [result] ok")
        self.assertEqual(tail[-2], "T16 assistant: " + "x" * 300 + ' [tool Bash] {"cmd": "ls"}')
        self.assertEqual(self.fake.timeouts[-1], 600)

    def test_a_firing_without_a_transcript_or_a_task_carries_no_tail(self):
        self.tick()
        [sent] = json.loads(self.claude_runs()[0][-1].partition("\n\n")[2])["firings"]
        self.assertNotIn("transcript_tail", sent)
        self.assertIn("task", sent)
    def decisions(self):
        return [(r["id"], r["decision"], r["reason"], r["by"]) for r in self.records() if "decision" in r]

    def test_decisions_are_recorded_from_the_runs_output(self):
        self.seeded_run(choice("aaa", "escalated", " red "))
        self.assertEqual(self.decisions(), [("aaa", "escalated", "red", "site-manager")])

    def test_output_lines_show_the_decision_and_the_cost(self):
        self.seed("firings", [fired("aaa")])
        _, out, _ = self.tick(answers={("claude", "-p"): reply([choice("aaa", "suppressed")])})
        self.assertIn("aaa  ended-undeclared  x  suppressed", out)
        self.assertTrue(out.endswith("site manager run cost $0.15\n"))
        self.assertEqual(self.decisions(), [("aaa", "suppressed", "because", "site-manager")])
    def test_no_cost_line_without_a_numeric_cost(self):
        _, out, _ = self.tick(answers={("claude", "-p"): reply(cost=None)})
        self.assertNotIn("cost", out)

    def seeded_run(self, *decisions, sent=True):
        """One tick with firing `aaa` open and undecided."""
        self.seed("firings", [fired("aaa")])
        _, out, _ = self.tick(answers={("claude", "-p"): reply(decisions, sent)})
        return out
    def test_a_suppression_is_recorded_for_any_firing(self):
        self.seeded_run(choice("aaa", "suppressed"))
        self.assertEqual(self.decisions(), [("aaa", "suppressed", "because", "site-manager")])
    def test_sent_false_records_no_escalation(self):
        self.seeded_run(choice("aaa", "escalated"), sent=False)
        self.assertEqual(self.decisions(), [])

    def test_sent_false_still_records_a_suppression(self):
        self.seeded_run(choice("aaa", "suppressed"), sent=False)
        self.assertEqual(self.decisions(), [("aaa", "suppressed", "because", "site-manager")])

    def test_ids_not_sent_malformed_and_repeated_decisions_are_ignored(self):
        self.seeded_run(choice("zzz"), choice("aaa", "maybe"), choice("aaa", "escalated", "  "), "junk",
                        {"id": ["aaa"], "decision": "escalated", "reason": "r"}, {"id": "aaa"},
                        choice("aaa", "escalated", "first"), choice("aaa", "suppressed", "second"))
        self.assertEqual(self.decisions(), [("aaa", "escalated", "first", "site-manager")])

    def test_output_that_is_not_the_expected_json_exits_1_and_records_nothing(self):
        answers = ((0, "", ""), (0, "not json", ""), (0, "[]", ""), (0, "{}", ""),
                   (0, json.dumps({"structured_output": {"sent": True}}), ""),
                   (0, json.dumps({"structured_output": {"sent": True, "decisions": "x"}}), ""),
                   (1, reply([choice("aaa")])[1], "boom"))
        for i, answer in enumerate(answers):
            with self.subTest(answer=answer):
                self.seed("firings", [fired("aaa")])
                code, out, err = self.tick(answers={("claude", "-p"): answer}, at=NOW + i * 1800)
                self.assertEqual(code, 1)
                self.assertIn("aaa  ended-undeclared  x  undecided", out)
                self.assertEqual(self.decisions(), [])
                self.assertEqual(self.looks()[-1]["seconds"] >= 0 and "failed" in self.looks()[-1], True)
                for _, path in factory.log_files("firings"):
                    path.unlink()

    def test_a_run_stopped_by_a_bound_names_it(self):
        stopped = (1, json.dumps({"subtype": "error_max_turns", "is_error": True}), "")
        code, _, err = self.tick(answers={("claude", "-p"): stopped})
        self.assertEqual(code, 1)
        self.assertIn("the site manager run failed (1): error_max_turns: no usable output", err)

    def test_a_site_manager_run_is_usable_with_anthropic_log_in_the_parent(self):
        def claude(argv, **kw):
            noisy = "ANTHROPIC_LOG" in (kw["env"] or os.environ)
            return subprocess.CompletedProcess(argv, 0, ("[log_1] sending request\n" if noisy else "") + reply()[1], "")
        p = pic()
        with mock.patch.dict(os.environ, {"ANTHROPIC_LOG": "debug"}), mock.patch.object(factory.subprocess, "Popen", stub_popen(claude)), \
                mock.patch.object(factory, "site_manager_argv", return_value=["claude", "-p"]), \
                mock.patch.object(factory, "record_decisions", return_value=0) as record, mock.patch.object(factory, "report"), \
                redirect_stdout(io.StringIO()):
            factory.run_site_manager(p, [])
        record.assert_called_once()

    def test_an_open_firing_survives_an_unplaced_session(self):
        self.seed("firings", [fired("aaa")])
        unplaced = {"id": "s9", "kind": "background", "status": "busy", "state": "working"}
        code, out, _ = self.tick(sessions=[DISPATCHER | {"name": "dispatcher-repo"}, unplaced])
        self.assertEqual(code, 0)
        self.assertIn("not evaluated: ended-undeclared: sessions cannot be listed", out)
        self.assertEqual(self.records(), [fired("aaa")])
    def test_an_escalated_firing_is_not_sent_again(self):
        self.tick()
        fid = self.records()[0]["id"]
        self.seed("firings", [decided(fid, "escalated", ts=NOW + 1)])
        self.tick(at=NOW + 1800)
        [argv] = self.claude_runs()  # the pace look: the working task has not been looked at for a health period
        picture = json.loads(argv[-1].partition("\n\n")[2])
        self.assertEqual((picture["woken_by"], picture["firings"]), ([{"event": "pace"}], []))
        self.assertEqual(len(self.records()), 2)
    def test_an_undecided_firing_is_sent_again_under_the_same_id(self):
        self.tick()
        _, out, _ = self.tick(at=NOW + 1800)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertIn(self.records()[0]["id"], out)

    def test_a_suppressed_firing_comes_back_only_after_the_resurface_time(self):
        self.seed("firings", [fired("aaa", ts=NOW - 5 * HOUR), decided("aaa", "suppressed", ts=NOW - 60 * 60,
                                                                       reason="noise")])
        self.tick()
        self.assertEqual(self.claude_runs(), [])
        self.assertEqual(len(self.records()), 2)
        self.tick(at=NOW + 61 * 60 + 1)
        [argv] = self.claude_runs()
        records = self.records()
        self.assertEqual([r.get("cleared") for r in records[2:3]], [True])
        self.assertEqual(records[2]["id"], "aaa")
        self.assertTrue(records[3]["detail"].endswith("; suppressed earlier: noise"))
        self.assertNotEqual(records[3]["id"], "aaa")
        self.assertIn(records[3]["id"], argv[-1])

    def told(self, minutes):
        self.seed("firings", [fired("aaa"), decided("aaa", "escalated", ts=NOW - minutes * 60, reason="stop sleep")
                              | {"to": "lead"}])

    def test_a_lead_told_less_than_the_grace_ago_is_not_fired_again(self):
        self.told(29)
        self.tick()
        self.assertEqual((self.claude_runs(), len(self.records())), ([], 2))

    def test_a_lead_told_longer_than_the_grace_ago_is_fired_again_with_the_marker(self):
        self.told(31)
        self.tick()
        [argv] = self.claude_runs()
        records = self.records()
        self.assertEqual([r.get("cleared") for r in records[2:3]], [True])
        new = records[3]
        self.assertTrue(new["detail"].endswith(f"; lead told at {iso(NOW - 31 * 60)}: stop sleep"), new["detail"])
        self.assertNotEqual(new["id"], "aaa")
        self.assertNotIn("lead_told", new)
        [sent] = json.loads(argv[-1].partition("\n\n")[2])["firings"]
        self.assertEqual((sent["id"], sent["lead_told"]), (new["id"], True))

    def test_a_refired_firing_still_undecided_is_sent_again_with_the_marker(self):
        self.told(31)
        self.tick()
        new = self.records()[3]
        self.tick(at=NOW + 1800)
        [argv] = self.claude_runs()
        [sent] = json.loads(argv[-1].partition("\n\n")[2])["firings"]
        self.assertEqual((sent["id"], sent["lead_told"]), (new["id"], True))

    def test_a_condition_that_ended_is_cleared_and_fires_anew_under_a_new_id(self):
        self.tick()
        first = self.records()[0]["id"]
        self.tick(at=NOW + 1800, sessions=[DISPATCHER | {"name": "dispatcher-repo"}, lead("x")])
        self.assertEqual(self.records()[1], {"ts": iso(NOW + 1800), "repo": ROOT, "id": first, "cleared": True})
        self.tick(at=NOW + 3600)
        self.assertEqual([r["id"] for r in self.records()][0], first)
        again = self.records()[2]
        self.assertNotEqual(again["id"], first)
        self.assertEqual(again["criterion"], "ended-undeclared")

    def test_a_criterion_that_was_not_evaluated_clears_nothing(self):
        self.seed("firings", [fired("aaa")])
        code, out, _ = self.tick(answers={("claude", "agents"): (1, "", "boom")})
        self.assertEqual(code, 0)
        self.assertIn("not evaluated: ended-undeclared", out)
        self.assertEqual(self.records(), [fired("aaa")])
        self.assertEqual(self.claude_runs(), [])

    def test_a_firing_of_another_repo_is_ignored(self):
        self.seed("firings", [fired("zzz", repo="/elsewhere")])
        self.tick()
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertEqual(self.records()[1]["subject"], "x")

    def test_dispatcher_missing_notifies_records_and_runs_no_claude(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(NOW - 2 * HOUR)}])
        answers = {("osascript",): (0, "", "")}
        code, out, _ = self.cli("watch", sessions=[], entries=["x"], answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(self.claude_runs(), [])
        [notice] = [c for c in self.fake.calls if c[0] == "osascript"]
        self.assertEqual(notice[1], "-e")
        self.assertIn('with title "factory: repo"', notice[2])
        self.assertIn("no dispatcher session for /repo", notice[2])
        by_criterion = {r["criterion"]: r for r in self.records() if "criterion" in r}
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertEqual((decision["id"], decision["decision"], decision["by"]),
                         (by_criterion["dispatcher-missing"]["id"], "escalated", "watch"))
        self.assertIn("notified on this machine", decision["reason"])
        self.assertIn("ended-undeclared", by_criterion)
        self.assertIn(f"{by_criterion['ended-undeclared']['id']}  ended-undeclared  x  undecided", out)

    def test_dispatcher_missing_holds_the_others_while_it_fires(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(NOW - 2 * HOUR)}])
        answers = {("osascript",): (0, "", "")}
        self.cli("watch", sessions=[], entries=["x"], answers=answers)
        self.cli("watch", sessions=[], entries=["x"], answers=answers, at=NOW + 1800)
        self.assertFalse(self.fake.ran("osascript"))
        self.assertEqual(self.claude_runs(), [])
        code, _, _ = self.cli("watch", entries=["x"], at=NOW + 3600)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertIn("ended-undeclared", self.claude_runs()[0][-1])
        self.assertNotIn("dispatcher-missing", self.claude_runs()[0][-1])

    def test_a_failed_notification_is_written_to_the_watch_log_and_recorded(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(NOW - 2 * HOUR)}])
        failing = {("osascript",): (1, "", "no display")}
        code, out, _ = self.cli("watch", sessions=[], entries=["x"], answers=failing)
        self.assertEqual(code, 0)
        self.assertIn("notify: factory: repo: no dispatcher session for /repo: run /factory:dispatch in a session there\n", out)
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertEqual((decision["decision"], decision["by"]), ("escalated", "watch"))
        self.assertIn("written to the watch log", decision["reason"])
        self.cli("watch", sessions=[], entries=["x"], answers=failing, at=NOW + 1800)
        self.assertFalse(self.fake.ran("osascript"))

    def test_the_notice_off_macos_uses_notify_send_or_the_log(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(NOW - 2 * HOUR)}])
        with mock.patch.object(factory.sys, "platform", "linux"):
            self.cli("watch", sessions=[], entries=["x"], answers={("notify-send",): (0, "", "")})
            self.assertEqual([c for c in self.fake.calls if c[0] == "notify-send"],
                             [["notify-send", "factory: repo", "no dispatcher session for /repo: run /factory:dispatch in a session there"]])

    def test_without_a_notification_service_the_notice_goes_to_the_log(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(NOW - 2 * HOUR)}])
        self.tools.pop("notify-send")
        with mock.patch.object(factory.sys, "platform", "linux"):
            _, out, _ = self.cli("watch", sessions=[], entries=["x"])
        self.assertFalse(self.fake.ran("notify-send"))
        self.assertIn("notify: factory: repo: no dispatcher session for /repo: run /factory:dispatch in a session there\n", out)
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertIn("written to the watch log", decision["reason"])

    def test_a_timed_out_run_exits_1(self):
        code, out, err = self.tick(answers={("claude", "-p"): (124, "", "timed out")})
        self.assertEqual(code, 1)
        self.assertIn("timed out", err)
        self.assertIn("undecided", out)
        self.assertEqual(self.fake.timeouts[-1], 600)

    def test_a_run_that_cannot_start_exits_1(self):
        code, _, err = self.tick(answers={("claude", "-p"): (127, "", "no such file")})
        self.assertEqual(code, 1)
        self.assertIn("no such file", err)


class RepoWideTests(Harness):
    def test_a_repo_wide_firing_carries_no_task(self):
        red = json.dumps([{"workflowName": "ci", "status": "completed", "conclusion": "failure", "url": "u"}])
        self.tick(entries=[], answers={("gh", "run", "list"): (0, red, "")})
        [sent] = json.loads(self.claude_runs()[0][-1].partition("\n\n")[2])["firings"]
        self.assertEqual((sent["criterion"], sent["slug"]), ("ci-red-default", None))
        self.assertNotIn("task", sent)
        self.assertNotIn("transcript_tail", sent)


class FiringTests(Harness):
    def setUp(self):
        super().setUp()
        self.seed("firings", [fired("abc111"), decided("abc111", "escalated"),
                              fired("abc222"), decided("abc222", "escalated"),
                              fired("def333", criterion="ci-red-default", subject="ci"),
                              decided("def333", "escalated"),
                              fired("sss444"), decided("sss444", "suppressed"),
                              fired("uuu555"), "junk", {"repo": ROOT, "decision": "escalated"}])

    def refused(self, *argv, why):
        code, _, err = self.cli(*argv)
        self.assertEqual(code, 2, err)
        self.assertIn(why, err)

    def outcomes(self):
        return [(r["id"], r["outcome"]) for r in self.records() if "outcome" in r]

    def test_an_outcome_by_prefix_id_and_the_last_one_wins(self):
        self.assertEqual(self.cli("firing", "def", "ignored")[0], 0)
        self.assertEqual(self.records()[-1], {"ts": iso(NOW), "repo": ROOT, "id": "def333", "outcome": "ignored"})
        self.cli("firing", "def", "acted")
        self.assertEqual(self.outcomes(), [("def333", "ignored"), ("def333", "acted")])
        folded, _ = factory.fold([r for r in self.records() if isinstance(r, dict)])
        self.assertEqual([f["outcome"] for f in folded if f["id"] == "def333"], ["acted"])

    def test_refusals(self):
        self.refused("firing", "zzz", "acted", why="no firing matches")
        self.refused("firing", "abc", "acted", why="matches 2 firings")
        self.refused("firing", "sss", "acted", why="was not escalated")
        self.assertEqual(self.outcomes(), [])
        for argv in (("firing", "abc1", "escalated"), ("firing", "abc1", "acted", "--reason", "r")):
            with self.subTest(argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                factory.parse_args(list(argv))

    def test_an_outcome_is_taken_before_the_decision_is_logged_and_counts_once_it_is(self):
        code, out, _ = self.cli("firing", "uuu", "acted")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "uuu555: acted (its decision is not logged yet)")
        self.seed("firings", [decided("uuu555", "escalated", ts=NOW + 30) | {"to": "dispatcher"}])
        folded, _ = factory.fold(self.records())
        self.assertEqual([(f["decision"], f["outcome"]) for f in folded if f["id"] == "uuu555"], [("escalated", "acted")])
        self.assertEqual(json.loads(self.cli("firings", "--json")[1])["summary"]["acted"], 1)

    def test_an_outcome_is_allowed_after_the_firing_cleared(self):
        self.seed("firings", [{"ts": iso(NOW), "repo": ROOT, "id": "def333", "cleared": True}])
        self.assertEqual(self.cli("firing", "def", "acted")[0], 0)

    def test_a_record_without_a_string_id_does_not_crash_the_commands(self):
        self.assertEqual(self.cli("firings")[0], 0)
        self.assertEqual(self.cli("firing", "abc1", "acted")[0], 0)


class MissTests(Harness):
    def setUp(self):
        super().setUp()
        self.seed("firings", [fired("aaa"), decided("aaa", "escalated"),
                              fired("bbb"), decided("bbb", "suppressed"),
                              fired("ccc")])

    def test_a_miss_that_never_fired(self):
        self.assertEqual(self.cli("miss", "a lead sat on a question", "--slug", "x")[0], 0)
        self.assertEqual(self.records()[-1], {"ts": iso(NOW), "repo": ROOT, "miss": "a lead sat on a question",
                                              "slug": "x", "firing": None, "entry": None})

    def test_a_miss_names_a_suppressed_or_undecided_firing_by_prefix(self):
        self.cli("miss", "filter was wrong", "--firing", "bb")
        self.cli("miss", "never sent", "--firing", "cc")
        self.assertEqual([r["firing"] for r in self.records() if "miss" in r], ["bbb", "ccc"])

    def test_refusals(self):
        for argv, why in ((("miss", "t", "--firing", "aaa"), "was escalated"), (("miss", "t", "--firing", "zzz"), "no firing"),
                          (("miss", " "), "empty")):
            with self.subTest(argv):
                code, _, err = self.cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn(why, err)
        self.assertEqual([r for r in self.records() if "miss" in r], [])


EVIDENCE = {"sent_back": [iso(NOW - HOUR)], "files": ["a.txt"], "pr": 7,
            "others": [{"slug": "y", "state": "working", "files": ["a.txt"]}]}


class ChurningTests(Harness):
    def setUp(self):
        super().setUp()
        self.p = pic(declared={"x": {"state": "handed-over", "ts": iso(NOW - HOUR)}}, churn={"x": EVIDENCE})

    def test_the_criterion_fires_for_each_task_with_evidence_and_names_what_it_needs(self):
        [(slug, subject, detail)] = check("churning", self.p)
        self.assertEqual((slug, subject), ("x", "#7"))
        for text in (iso(NOW - HOUR), "a.txt", "y (working) on a.txt"):
            self.assertIn(text, detail)
        self.assertEqual(check("churning", pic(churn={})), [])

    def test_churning_uses_the_ticks_snapshot_and_asks_gh_for_none_of_its_own(self):
        declared = {"x": {"state": "handed-over", "ts": "t"}}
        snap = [{"number": 7, "headRefName": "worktree-x"}]
        with mock.patch.object(factory, "snapshot", side_effect=AssertionError("a second gh pr list")), \
                mock.patch.object(factory, "queue_send_backs", lambda repo, slug: [iso(NOW)]), \
                mock.patch.object(factory, "task_slug", lambda repo, pr: "x"), \
                mock.patch.object(factory, "churn", lambda repo, slug, pr: {"sent_back": [], "files": ["a"], "others": []}):
            self.assertEqual(list(factory.churning(pic(declared=declared, snap=snap))), ["x"])

    def test_it_is_a_fact_of_every_tick(self):
        self.assertTrue(factory.FACTS["churning"][1])
    def test_no_pull_request_list_unless_a_handed_over_task_was_sent_back(self):
        asked = []
        declared = {"x": {"state": "handed-over", "ts": "t"}, "y": {"state": "working", "ts": "t"}}
        p = pic(declared=declared)
        with mock.patch.object(factory, "snapshot", lambda repo: asked.append(1) or []), \
                mock.patch.object(factory, "queue_send_backs", lambda repo, slug: [iso(NOW)] if slug == "y" else []):
            self.assertEqual(check("churning", p), [])
        self.assertEqual(asked, [])
        with mock.patch.object(factory, "snapshot", lambda repo: asked.append(1) or [{"number": 7, "headRefName": "worktree-x"}]), \
                mock.patch.object(factory, "queue_send_backs", lambda repo, slug: [iso(NOW)]), \
                mock.patch.object(factory, "task_slug", lambda repo, pr: "x"), \
                mock.patch.object(factory, "churn", lambda repo, slug, pr: {"sent_back": [], "files": ["a"], "others": []}):
            self.assertEqual(factory.churning(pic(declared=declared)), {"x": {"sent_back": [], "files": ["a"], "others": [], "pr": 7}})
        self.assertEqual(asked, [1])

    def record(self, *decisions, sent=True, criterion="churning"):
        firing = {"id": "aaa", "criterion": criterion, "slug": "x", "subject": "#7", "detail": "d"}
        self.seed("firings", [{"ts": iso(NOW), "repo": ROOT, **{k: v for k, v in firing.items()}}])
        factory.record_decisions(self.p, [firing], {"sent": sent, "decisions": list(decisions)}, "run1")
        return [r for r in self.records() if "decision" in r]

    def paused(self, waits, reason="y lands soon"):
        return {"id": "aaa", "decision": "paused", "reason": reason, "looked": [], "waits_on": waits}

    def test_paused_is_recorded_with_its_waits_on(self):
        [r] = self.record(self.paused(["y"]))
        self.assertEqual((r["decision"], r["waits_on"], r["by"], r["reason"]), ("paused", ["y"], "site-manager", "y lands soon"))

    def test_paused_needs_sent_true_a_churning_firing_and_waits_on_among_the_others(self):
        for waits in (None, [], "y", ["z"], ["y", "z"], [1]):
            with self.subTest(waits=waits):
                self.assertEqual(self.record(self.paused(waits)), [])
        self.assertEqual(self.record(self.paused(["y"]), sent=False), [])
        self.assertEqual(self.record(self.paused(["y"]), criterion="ended-undeclared"), [])

    def test_paused_is_counted_apart_in_firings_and_takes_no_outcome(self):
        self.seed("firings", [fired("ppp", criterion="churning", subject="#7"),
                              decided("ppp", "paused") | {"waits_on": ["y"]}])
        [f] = factory.fold(self.records())[0]
        self.assertEqual((f["decision"], f["waits_on"]), ("paused", ["y"]))
        s = json.loads(self.cli("firings", "--json")[1])["summary"]
        self.assertEqual((s["paused"], s["escalated"], s["suppressed"], s["acted"], s["ignored"], s["precision"]), (1, 0, 0, 0, 0, None))
        self.assertIn("suppressed 0; paused 1; extended 0 (kept 0, overran 0, no outcome 0); missed 0", self.cli("firings")[1])
        code, _, err = self.cli("firing", "ppp", "acted")
        self.assertEqual(code, 2)
        self.assertIn("was not escalated", err)

    def test_a_tick_between_health_passes_finds_the_dispatcher_and_sends_the_firing_to_the_site_manager(self):
        factory.write_lander(SimpleNamespace(root=ROOT), {"health": NOW})
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "handed-over", "ts": iso(NOW - HOUR)}])
        with mock.patch.object(factory, "churning", lambda p: {"x": EVIDENCE}):
            code, out, _ = self.cli("watch", entries=["x"], answers={("claude", "-p"): reply()})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.claude_runs()), 1)
        [fired_row] = [r for r in self.records() if r.get("criterion") == "churning"]
        self.assertEqual((fired_row["slug"], fired_row["subject"]), ("x", "#7"))

    def status_row(self, state, **declared):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": state, "ts": iso(NOW - 10 * 60), **declared}])
        started = int((NOW - 100 * 60) * 1000)
        _, out, _ = self.cli("status", "--json", sessions=[DISPATCHER | {"name": "dispatcher-repo"}, lead("x", startedAt=started)],
                             entries=["x"])
        return json.loads(out)["rows"][0]

    def test_a_working_task_nearing_its_limit_has_no_pause_or_priority(self):
        working = self.status_row("working")
        self.assertEqual((working["nearing"], working["paused"], working["prioritized"]), (True, None, None))

    def test_an_expired_priority_no_drain_has_cleared_yet_is_not_shown(self):
        old = iso(NOW - (factory.RELEASE_HOLD_MINUTES + 1) * 60)
        factory.repo_state(ROOT, "lander", "json").write_text(json.dumps({"prioritized": {"x": old}}))
        self.assertIsNone(self.status_row("working")["prioritized"])

    def test_status_shows_pause_and_priority(self):
        factory.repo_state(ROOT, "lander", "json").write_text(json.dumps({"prioritized": {"x": iso(NOW - 3 * 60)}}))
        r = self.status_row("blocked", waits_on=["y", "z"], paused=True)
        self.assertEqual((r["nearing"], r["waits_on"], r["paused"], r["prioritized"]),
                         (False, "y, z", {"since": iso(NOW - 10 * 60), "waits_on": ["y", "z"]}, {"since": iso(NOW - 3 * 60)}))
        _, out, _ = self.cli("status", sessions=[DISPATCHER | {"name": "dispatcher-repo"}], entries=["x"])
        self.assertIn("paused 10m: waits y, z", out)
        self.assertIn("prioritized 3m", out)

    def test_pauses_lists_each_pause_with_its_send_backs_and_totals(self):
        sent = lambda ts: {"repo": ROOT, "slug": "x", "role": "lander", "state": "working", "ts": iso(ts)}
        self.seed("tasks", [sent(NOW - 5 * HOUR), sent(NOW - 3 * HOUR), sent(NOW - HOUR)])
        self.seed("events", [ev("x", "TaskPaused", ts=iso(NOW - 3 * HOUR + 60), waits_on=["y"]),
                             ev("x", "TaskPrioritized", ts=iso(NOW - 2 * HOUR), waited=["y"], paused_minutes=59),
                             ev("x", "TaskPrioritizedEnd", ts=iso(NOW - 90 * 60), reason="merged", minutes=30),
                             ev("w", "TaskPaused", ts=iso(NOW - 600), waits_on=["y", "z"])])
        code, out, _ = self.cli("pauses")
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], f"x  {iso(NOW - 3 * HOUR + 60)}  59 min  waited y  send-backs before 2, after 1; prioritized 30 min, ended merged")
        self.assertEqual(lines[1], f"w  {iso(NOW - 600)}  still paused  waited y,z  send-backs before 0, after 0")
        self.assertEqual(lines[2], "pauses 2; still paused 1; minutes paused 59; send-backs after a pause 1")


class RoutingTests(Home):
    """Who the site manager's escalation goes to, as `watch` enforces it."""

    def logged(self, sent, *decisions, session=None, result_sent=True, run="r1", slug="x"):
        p = pic(rows=[row("x")], sessions=[DISPATCHER | {"name": "dispatcher-repo"}, *([session] if session else [])])
        firings = [{"id": "aaa", "slug": slug, **sent}]
        with mock.patch.object(factory, "log_firing") as log, mock.patch.object(factory, "forward") as forward:
            factory.record_decisions(p, firings, {"sent": result_sent, "decisions": list(decisions)}, run)
        self.forwarded = [c.args for c in forward.call_args_list]
        return [c.kwargs for c in log.call_args_list]

    def told_lead(self, **kw):
        return {**choice("aaa"), "to": "lead", "looked": ["ps -ax", 7], **kw}

    def test_to_looked_and_run_land_in_the_log(self):
        [rec] = self.logged({}, self.told_lead(), session=lead("x", pid=4242))
        self.assertEqual(rec, {"id": "aaa", "decision": "escalated", "reason": "because", "by": "site-manager",
                               "run": "r1", "looked": ["ps -ax"], "to": "lead"})

    def test_the_script_sends_a_lead_message_live_or_retired_with_the_reason_as_its_text(self):
        for session in (lead("x", pid=4242), lead("x", **FINISHED)):
            self.assertEqual(len(self.logged({}, self.told_lead(), session=session)), 1)
            self.assertEqual([a[2:] for a in self.forwarded], [("x", "SITE MANAGER", "because")])
        self.logged({}, self.told_lead(), session=None)
        self.assertEqual(self.forwarded, [])

    def test_an_escalation_without_to_goes_to_the_dispatcher_and_looked_may_be_missing(self):
        [rec] = self.logged({}, choice("aaa"))
        self.assertEqual((rec["to"], rec["looked"]), ("dispatcher", []))

    def test_a_suppression_logs_looked_and_no_to(self):
        [rec] = self.logged({}, choice("aaa", "suppressed") | {"to": "lead", "looked": ["a"]})
        self.assertEqual((rec["decision"], rec["looked"], "to" in rec), ("suppressed", ["a"], False))

    def test_a_lead_message_is_not_recorded_when_it_cannot_be_one(self):
        live = lead("x")
        for label, sent, session, slug in (
                ("a firing whose lead was told before", {"lead_told": True}, live, "x"),
                ("no task slug", {}, live, None),
                ("a slug that is no task", {}, live, "y"),
                ("no session", {}, None, "x")):
            with self.subTest(label):
                self.assertEqual(self.logged(sent, self.told_lead(), session=session, slug=slug), [])

    def test_an_unknown_destination_and_a_run_that_sent_nothing_are_not_recorded(self):
        self.assertEqual(self.logged({}, self.told_lead(to="human"), session=lead("x")), [])
        self.assertEqual(self.logged({}, self.told_lead(), session=lead("x"), result_sent=False), [])

    def test_the_earlier_rules_stand(self):
        self.assertEqual(len(self.logged({}, choice("aaa", "suppressed"))), 1)
        self.assertEqual(len(self.logged({}, choice("aaa"))), 1)

    def test_a_firing_whose_lead_was_told_can_still_go_to_the_dispatcher(self):
        [rec] = self.logged({"lead_told": True}, choice("aaa"), session=lead("x"))
        self.assertEqual(rec["to"], "dispatcher")

    def test_the_firing_carries_the_lead_worktree_pid_and_transcripts(self):
        main = self.transcript("{sid}.jsonl", NOW)
        sub = self.transcript("{sid}/subagents/a.jsonl", NOW)
        p = pic(rows=[row("x", session="working", dirty=None, ahead=None, behind=None)], sessions=[lead("x", pid=4242)])
        f = {"id": "aaa", "criterion": "c", "slug": "x", "subject": "x", "detail": "d",
             "what": "w"}
        task = factory.firing_for_run(p, f)["task"]
        self.assertEqual((task["lead"], task["worktree"], task["branch"], task["pid"]),
                         ("x", f"{WT}/x", "worktree-x", 4242))
        self.assertEqual(factory.firing_for_run(p, f)["transcripts"], [str(main), str(sub)])
        gone = factory.firing_for_run(pic(rows=[row("x", session="working", dirty=None, ahead=None, behind=None)], sessions=[lead("x", **FINISHED)]), f)
        self.assertEqual((gone["task"]["lead"], gone["task"]["pid"]), ("x", None))

    def test_a_firing_without_a_row_carries_only_worktree_lead_pid_and_transcripts(self):
        main = self.transcript("{sid}.jsonl", NOW)
        f = {"id": "aaa", "criterion": "busy-after-handover", "slug": "x", "subject": "x",
             "detail": "d", "what": "w"}
        out = factory.firing_for_run(pic(rows=None, sessions=[lead("x", pid=4242)]), f)
        self.assertEqual(out["task"], {"worktree": f"{WT}/x", "lead": "x", "pid": 4242})
        self.assertEqual(out["transcripts"], [str(main)])
        gone = factory.firing_for_run(pic(rows=None, sessions=[lead("x", **FINISHED)]), f)
        self.assertEqual(gone["task"], {"worktree": f"{WT}/x", "lead": "x", "pid": None})

    def test_a_lead_message_for_an_every_tick_finding_is_recorded_for_a_lead_live_or_retired(self):
        sent = [{"id": "aaa", "slug": "x", "criterion": "busy-after-handover"}]
        for label, session, recorded in (("live", lead("x", pid=4242), 1), ("retired", lead("x", **FINISHED), 1), ("none", None, 0)):
            with self.subTest(label), mock.patch.object(factory, "log_firing") as log, mock.patch.object(factory, "forward"):
                p = pic(rows=None, sessions=[session] if session else [])
                factory.record_decisions(p, sent, {"sent": True, "decisions": [self.told_lead()]}, "r1")
                self.assertEqual(log.call_count, recorded)


class FiringsTests(Harness):
    def seed_log(self):
        self.seed("firings", [
            fired("a1", ts=NOW - 5 * HOUR), decided("a1", "escalated"), {"repo": ROOT, "id": "a1", "outcome": "acted"},
            fired("a2", ts=NOW - 4 * HOUR), decided("a2", "escalated"), {"repo": ROOT, "id": "a2", "outcome": "ignored"},
            fired("a3", ts=NOW - 3 * HOUR), decided("a3", "escalated"),
            fired("s1", ts=NOW - 2 * HOUR), decided("s1", "suppressed", reason="noise"),
            {**fired("u1", ts=NOW - HOUR), "ts": iso(NOW - HOUR)}, {"repo": ROOT, "id": "u1", "cleared": True},
            "not json", ["a list"],
            {"ts": iso(NOW), "repo": ROOT, "miss": "late", "slug": "x", "firing": "s1", "entry": "ended-undeclared"},
            {"ts": iso(NOW), "repo": ROOT, "miss": "never", "slug": None, "firing": None},
            {"ts": iso(NOW), "repo": ROOT, "miss": "never", "slug": None, "firing": None},
            {"ts": iso(NOW), "repo": "/other", "miss": "other repo", "slug": None, "firing": None}])

    def test_table_and_summary(self):
        self.seed_log()
        code, out, _ = self.cli("firings")
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], f"a1  {iso(NOW - 5 * HOUR)}  ended-undeclared  x  escalated  acted  open  why")
        self.assertEqual(lines[3], f"s1  {iso(NOW - 2 * HOUR)}  ended-undeclared  x  suppressed  -  open  noise")
        self.assertEqual(lines[4], f"u1  {iso(NOW - HOUR)}  ended-undeclared  x  undecided  -  cleared  -")
        self.assertEqual(lines[5], f"miss  {iso(NOW)}  x  s1  late")
        self.assertEqual(lines[-3], "ended-undeclared: fired 5; escalated 3 (acted 1, ignored 1, no outcome 1); to lead 0; suppressed 1; missed 1; precision 0.50")
        self.assertEqual(lines[-2], "unlisted: fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); to lead 0; suppressed 0; missed 2; precision -")
        self.assertEqual(lines[-1], "escalated 3 (acted 1, ignored 1, no outcome 1); to lead 0; suppressed 1; "
                                    "paused 0; extended 0 (kept 0, overran 0, no outcome 0); missed 3 (1 suppressed first, 2 never fired); precision 0.50; recall 0.25; "
                                    "told 0 (acted 0, noise 0, collided 0, no outcome 0, undelivered 0)")

    def test_json(self):
        self.seed_log()
        _, out, _ = self.cli("firings", "--json")
        doc = json.loads(out)
        self.assertEqual([f["id"] for f in doc["firings"]], ["a1", "a2", "a3", "s1", "u1"])
        self.assertEqual(len(doc["misses"]), 3)
        entries = doc["summary"].pop("entries")
        self.assertEqual(entries["ended-undeclared"], {"fired": 5, "to_lead": 0, "escalated": 3, "acted": 1, "ignored": 1, "no_outcome": 1,
                                                       "suppressed": 1, "told": 0, "extended": 0, "paused": 0, "missed": 1, "precision": 0.5})
        self.assertEqual(entries["unlisted"]["missed"], 2)
        self.assertEqual(doc["summary"], {"escalated": 3, "acted": 1, "ignored": 1, "no_outcome": 1, "suppressed": 1,
                                          "paused": 0, "extended": 0, "kept": 0, "overran": 0, "missed": 3, "missed_suppressed": 1, "missed_never_fired": 2,
                                          "precision": 0.5, "recall": 0.25, "told": 0, "told_acted": 0, "told_noise": 0,
                                          "told_collided": 0, "told_no_outcome": 0, "told_undelivered": 0})

    def test_a_lead_message_shows_in_the_table_and_everything_in_the_json(self):
        self.seed("firings", [fired("l1"), decided("l1", "escalated") | {
            "to": "lead", "looked": ["ps -ax"], "run": "r1"}])
        _, out, _ = self.cli("firings")
        self.assertIn("  escalated to lead  -  open  why\n", out)
        [f] = json.loads(self.cli("firings", "--json")[1])["firings"]
        self.assertEqual((f["to"], f["looked"], f["run"]), ("lead", ["ps -ax"], "r1"))

    def test_a_lead_message_is_counted_apart_from_the_dispatchers(self):
        self.seed("firings", [fired("l1"), decided("l1", "escalated") | {"to": "lead"},
                              fired("d1"), decided("d1", "escalated") | {"to": "dispatcher"}])
        s = json.loads(self.cli("firings", "--json")[1])["summary"]
        self.assertEqual((s["escalated"], s["no_outcome"]), (1, 1))
        self.assertIn("escalated 1 (acted 0, ignored 0, no outcome 1); to lead 1; ", self.cli("firings")[1])

    def test_zero_denominators(self):
        code, out, _ = self.cli("firings")
        self.assertEqual(out, "escalated 0 (acted 0, ignored 0, no outcome 0); to lead 0; suppressed 0; "
                              "paused 0; extended 0 (kept 0, overran 0, no outcome 0); missed 0 (0 suppressed first, 0 never fired); precision -; recall -; "
                              "told 0 (acted 0, noise 0, collided 0, no outcome 0, undelivered 0)\n")
        _, out, _ = self.cli("firings", "--json")
        self.assertEqual((json.loads(out)["summary"]["precision"], json.loads(out)["summary"]["recall"]), (None, None))
        self.seed("firings", [{"ts": iso(NOW), "repo": ROOT, "miss": "m", "slug": None, "firing": None}])
        summary = json.loads(self.cli("firings", "--json")[1])["summary"]
        self.assertEqual((summary["precision"], summary["recall"]), (None, 0.0))


def job_path():
    """The PATH the harness's tools give a job; Python's own directory may be one of theirs (/usr/bin)."""
    return ":".join(dict.fromkeys(["/usr/bin", "/opt/bin", "/home/u/.local/bin",
                                   os.path.dirname(factory.sys.executable), "/bin", "/usr/sbin", "/sbin"]))


class SchedHarness(Harness):
    """Harness with the launchd job of the repository: its label, plist and `launchctl` calls."""

    LABEL = factory.job_label(ROOT)
    LEGACY = "com.claude.factory.watch.repo"

    def cli(self, *argv, answers=None, **kw):
        return super().cli(*argv, answers={("launchctl",): (0, "", ""), **(answers or {})}, **kw)

    def launchctl(self):
        return [c for c in self.fake.calls if c[0] == "launchctl"]

    def plist(self, label=None):
        return self.home / "Library" / "LaunchAgents" / f"{label or self.LABEL}.plist"

    def put_plist(self, label=None, **job):
        self.plist(label).parent.mkdir(parents=True, exist_ok=True)
        self.plist(label).write_bytes(plistlib.dumps({"Label": label or self.LABEL, **job}))


class JobTests(unittest.TestCase):
    def test_a_percent_or_dollar_in_a_path_is_escaped_for_systemd_and_cron(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw.get("input")))
            return 0, "", ""
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}), \
                mock.patch.object(factory, "run", run):
            factory.Systemd().install("job", ["/py", "/a$b/f%y"], "/re%po", {"PATH": "/bin"}, "/lo%g")
            unit = (Path(home) / "systemd" / "user" / "job.service").read_text()
            factory.Cron().install("job", ["/py", "/f%y"], "/re%po", {"PATH": "/bin"}, "/lo%g")
        self.assertIn("WorkingDirectory=/re%%po\n", unit)
        self.assertIn("ExecStart=/py '/a$$b/f%%y'\n", unit)
        self.assertIn("StandardOutput=append:/lo%%g\n", unit)
        self.assertEqual(calls[-1][1], "*/2 * * * * cd /re\\%po && PATH=/bin /py /f\\%y >> /lo\\%g 2>&1 # job\n")

    def test_two_folders_with_one_name_get_two_labels(self):
        a, b = factory.job_label("/a/repo"), factory.job_label("/b/repo")
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^com\.claude\.factory\.watch\.repo-[0-9a-f]{8}$")
        self.assertEqual(a, factory.job_label("/a/repo"))

    def test_the_path_holds_only_what_the_job_needs(self):
        found = {"git": "/usr/bin/git", "gh": "/opt/bin/gh", "claude": "/home/u/.local/bin/claude"}
        with mock.patch.object(factory.shutil, "which", found.get), \
                mock.patch.object(factory.sys, "executable", "/py/bin/python3"), \
                mock.patch.dict(os.environ, {"PATH": "/home/u/.local/bin:/junk:/opt/bin:/usr/bin"}):
            self.assertEqual(factory.job_path(),  # the caller's order, so the job finds the same copies
                             "/home/u/.local/bin:/opt/bin:/usr/bin:/py/bin:/bin:/usr/sbin:/sbin")
            for tool in found:
                with mock.patch.object(factory.shutil, "which", {**found, tool: None}.get):
                    with self.assertRaises(factory.Fail) as raised:
                        factory.job_path()
                    self.assertEqual(str(raised.exception), f"schedule: {tool} is not on PATH")

    def test_the_notifier_asks_the_platform(self):
        with mock.patch.object(factory.sys, "platform", "darwin"):
            self.assertEqual(factory.notify_argv('t"', 'a\\"b'),
                             ["osascript", "-e", 'display notification "a\\\\\\"b" with title "t\\""'])
        with mock.patch.object(factory.sys, "platform", "linux"):
            with mock.patch.object(factory.shutil, "which", {"notify-send": "/usr/bin/notify-send"}.get):
                self.assertEqual(factory.notify_argv("t", "x"), ["notify-send", "t", "x"])
            with mock.patch.object(factory.shutil, "which", lambda tool: None):
                self.assertIsNone(factory.notify_argv("t", "x"))

    def test_the_scheduler_is_chosen_at_run_time(self):
        def chosen(platform, systemd, crontab):
            fake = FakeRun({("systemctl",): (0 if systemd else 1, "", "")})
            with mock.patch.object(factory.sys, "platform", platform), mock.patch.object(factory, "run", fake), \
                    mock.patch.object(factory.shutil, "which", lambda tool: "/usr/bin/crontab" if crontab else None):
                return factory.scheduler().name
        self.assertEqual(chosen("darwin", True, True), "launchd")
        self.assertEqual(chosen("linux", True, True), "systemd")
        self.assertEqual(chosen("linux", False, True), "cron")
        with self.assertRaises(factory.Fail) as raised:
            chosen("linux", False, False)
        self.assertIn("no scheduler here", str(raised.exception))


class ScheduleTests(SchedHarness):
    def schedule(self, *argv, platform="darwin", answers=None, path="/plug/factory/bin/factory"):
        with mock.patch.object(factory.sys, "platform", platform), mock.patch.object(factory, "FACTORY", Path(path)):
            return self.cli("schedule", *argv, answers=answers)

    def test_install_writes_the_plist_and_replaces_the_job(self):
        code, out, err = self.schedule("install")
        self.assertEqual((code, err, out), (0, "", f"installed {self.LABEL} (launchd)\n"))
        job = plistlib.loads(self.plist().read_bytes())
        log = str(self.store / "watch.log")
        self.assertEqual(job, {"Label": self.LABEL,
                               "ProgramArguments": [factory.sys.executable, "/plug/factory/bin/factory", "watch"],
                               "WorkingDirectory": ROOT, "StartInterval": 120,
                               "EnvironmentVariables": {"PATH": job_path()},
                               "StandardOutPath": log, "StandardErrorPath": log})
        uid = os.getuid()
        self.assertEqual(self.launchctl(), [["launchctl", "bootout", f"gui/{uid}/{self.LABEL}"],
                                            ["launchctl", "bootstrap", f"gui/{uid}", str(self.plist())]])

    def test_install_with_a_failing_bootstrap(self):
        code, _, err = self.schedule("install", answers={("launchctl", "bootstrap"): (5, "", "Bootstrap failed")})
        self.assertEqual(code, 1)
        self.assertIn("Bootstrap failed", err)

    def test_install_from_a_worktree_warns(self):
        _, _, err = self.schedule("install", path=f"{WT}/y/factory/bin/factory")
        self.assertIn("task worktree", err)
        _, _, err = self.schedule("install")
        self.assertNotIn("worktree", err)

    def test_install_drops_the_job_of_the_old_label_that_belongs_to_this_repository(self):
        self.put_plist(self.LEGACY, WorkingDirectory=ROOT)
        other = "com.claude.factory.watch.other"
        self.put_plist(other, WorkingDirectory="/elsewhere")
        self.schedule("install")
        self.assertFalse(self.plist(self.LEGACY).exists())
        self.assertTrue(self.plist(other).exists())
        uid = os.getuid()
        self.assertEqual(self.launchctl()[0], ["launchctl", "bootout", f"gui/{uid}/{self.LEGACY}"])
        self.assertTrue(self.plist().exists())

    def test_install_without_a_tool_the_job_needs_fails(self):
        self.tools.pop("gh")
        code, _, err = self.schedule("install")
        self.assertEqual(code, 2)
        self.assertIn("gh is not on PATH", err)
        self.assertFalse(self.plist().exists())

    def test_remove_unlinks_the_plist_before_it_boots_the_job_out(self):
        code, out, _ = self.schedule("remove")
        self.assertEqual((code, out), (0, f"nothing installed: {self.LABEL}\n"))
        self.schedule("install")
        seen = []
        code, out, _ = self.schedule("remove", answers={
            ("launchctl", "bootout"): lambda: seen.append(self.plist().exists()) or (0, "", "")})
        self.assertEqual((code, out, seen), (0, f"removed {self.LABEL}\n", [False]))

    def test_remove_also_drops_the_old_label(self):
        self.put_plist(self.LEGACY, WorkingDirectory=ROOT)
        _, out, _ = self.schedule("remove")
        self.assertEqual(out, f"removed {self.LABEL}\n")
        self.assertFalse(self.plist(self.LEGACY).exists())

    def test_status(self):
        _, out, _ = self.schedule("status")
        self.assertEqual(out, f"{self.LABEL}: launchd job absent, loaded\n")
        self.schedule("install")
        _, out, _ = self.schedule("status", answers={("launchctl", "print"): (113, "", "not found")})
        self.assertEqual(out, f"{self.LABEL}: launchd job present, not loaded\n")
        self.assertEqual(self.launchctl()[-1], ["launchctl", "print", f"gui/{os.getuid()}/{self.LABEL}"])


class SystemdTests(SchedHarness):
    ANSWERS = {("systemctl",): (0, "", ""), ("loginctl",): (0, "Linger=yes\n", "")}

    def schedule(self, *argv, answers=None):
        with mock.patch.object(factory.sys, "platform", "linux"):
            return self.cli("schedule", *argv, answers={**self.ANSWERS, **(answers or {})})

    def systemctl(self):
        return [c[1:] for c in self.fake.calls if c[0] == "systemctl"]

    def unit(self, kind):
        return self.home / ".config" / "systemd" / "user" / f"{self.LABEL}.{kind}"

    def test_install_writes_the_units_and_enables_the_timer(self):
        code, out, err = self.schedule("install")
        self.assertEqual((code, err, out), (0, "", f"installed {self.LABEL} (systemd)\n"))
        log = self.store / "watch.log"
        path = job_path()
        self.assertEqual(self.unit("service").read_text(),
                         f"[Unit]\nDescription=factory watch {ROOT}\n\n[Service]\nType=oneshot\n"
                         f"WorkingDirectory={ROOT}\nEnvironment=\"PATH={path}\"\n"
                         f"ExecStart={factory.sys.executable} /plug/factory/bin/factory watch\n"
                         f"StandardOutput=append:{log}\nStandardError=append:{log}\n")
        self.assertEqual(self.unit("timer").read_text(),
                         f"[Unit]\nDescription=factory watch {ROOT}\n\n[Timer]\nOnActiveSec=120\n"
                         "OnUnitActiveSec=120\n\n[Install]\nWantedBy=timers.target\n")
        self.assertEqual(self.systemctl(), [["--user", "show-environment"], ["--user", "daemon-reload"],
                                            ["--user", "enable", "--now", f"{self.LABEL}.timer"]])
        self.assertEqual([c[0] for c in self.fake.calls if c[0] == "launchctl"], [])

    def test_install_honors_xdg_config_home_and_says_what_linger_means(self):
        xdg = self.home / "xdg"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            _, out, _ = self.schedule("install", answers={("loginctl",): (0, "Linger=no\n", "")})
        self.assertTrue((xdg / "systemd" / "user" / f"{self.LABEL}.timer").exists())
        self.assertFalse(self.unit("timer").exists())
        self.assertIn("loginctl enable-linger", out.splitlines()[1])
        self.assertEqual(len(out.splitlines()), 2)

    def test_a_failing_enable_exits_1(self):
        code, _, err = self.schedule("install", answers={("systemctl", "--user", "enable"): (1, "", "no bus")})
        self.assertEqual(code, 1)
        self.assertIn("no bus", err)

    def test_remove_and_status(self):
        _, out, _ = self.schedule("remove")
        self.assertEqual(out, f"nothing installed: {self.LABEL}\n")
        self.schedule("install")
        _, out, _ = self.schedule("status")
        self.assertEqual(out, f"{self.LABEL}: systemd job present, loaded\n")
        _, out, _ = self.schedule("status", answers={("systemctl", "--user", "is-active"): (3, "inactive", "")})
        self.assertEqual(out, f"{self.LABEL}: systemd job present, not loaded\n")
        _, out, _ = self.schedule("remove")
        self.assertEqual(out, f"removed {self.LABEL}\n")
        self.assertEqual(self.systemctl()[-2:], [["--user", "disable", "--now", f"{self.LABEL}.timer"],
                                                 ["--user", "daemon-reload"]])
        self.assertFalse(self.unit("timer").exists() or self.unit("service").exists())


class CronTests(SchedHarness):
    OTHER = "0 5 * * * backup --all"

    def schedule(self, *argv, crontab="", answers=None):
        answers = {("systemctl",): (1, "", "no bus"), ("crontab", "-l"): (0, crontab, ""),
                   ("crontab", "-"): (0, "", ""), **(answers or {})}
        with mock.patch.object(factory.sys, "platform", "linux"):
            return self.cli("schedule", *argv, answers=answers)

    def written(self):
        return [i for c, i in zip(self.fake.calls, self.fake.inputs) if c == ["crontab", "-"]]

    def test_install_appends_one_marked_line_and_keeps_the_others(self):
        old = f"*/30 * * * * stale # {self.LABEL}"
        code, out, err = self.schedule("install", crontab=f"{self.OTHER}\n{old}\n")
        self.assertEqual((code, err, out), (0, "", f"installed {self.LABEL} (cron)\n"))
        path = job_path()
        self.assertEqual(self.written(), [f"{self.OTHER}\n*/2 * * * * cd {ROOT} && PATH={path} {factory.sys.executable} "
                                          f"/plug/factory/bin/factory watch "
                                          f">> {self.store / 'watch.log'} 2>&1 # {self.LABEL}\n"])

    def test_install_without_a_crontab_yet(self):
        code, _, _ = self.schedule("install", answers={("crontab", "-l"): (1, "", "no crontab for u")})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.written()[0].splitlines()), 1)

    def test_a_failing_crontab_exits_1(self):
        code, _, err = self.schedule("install", answers={("crontab", "-"): (1, "", "bad time")})
        self.assertEqual(code, 1)
        self.assertIn("bad time", err)

    def test_remove_and_status(self):
        line = f"*/30 * * * * x # {self.LABEL}"
        _, out, _ = self.schedule("remove", crontab=f"{self.OTHER}\n")
        self.assertEqual((out, self.written()), (f"nothing installed: {self.LABEL}\n", []))
        _, out, _ = self.schedule("remove", crontab=f"{self.OTHER}\n{line}\n")
        self.assertEqual((out, self.written()), (f"removed {self.LABEL}\n", [f"{self.OTHER}\n"]))
        _, out, _ = self.schedule("remove", crontab=f"{line}\n")
        self.assertEqual(self.written()[-1], "")
        _, out, _ = self.schedule("status", crontab=f"{line}\n")
        self.assertEqual(out, f"{self.LABEL}: cron job present, loaded\n")
        _, out, _ = self.schedule("status", crontab=f"{self.OTHER}\n")
        self.assertEqual(out, f"{self.LABEL}: cron job absent, not loaded\n")

    def test_without_any_scheduler_it_says_so(self):
        self.tools.pop("crontab")
        code, _, err = self.schedule("status")
        self.assertEqual(code, 2)
        self.assertIn("no scheduler here", err)


class HookTests(SchedHarness):
    DISPATCHER_SESSION = DISPATCHER | {"name": "dispatcher-repo"}

    def hook(self, event, *, sessions=(DISPATCHER_SESSION,), cwd=ROOT, answers=None, entries=(), **payload):
        stdin = json.dumps({"hook_event_name": event, "session_id": "sid-d", "cwd": cwd, **payload})
        with mock.patch.object(factory.sys, "stdin", io.StringIO(stdin)):
            result = self.cli("hook", sessions=sessions, answers=answers, entries=entries)
        self.assertEqual(result, (0, "", ""))

    def listed(self):
        return self.fake.ran("claude", "agents")

    def test_a_prompt_of_the_dispatcher_installs_the_job_when_there_is_none(self):
        self.hook("UserPromptSubmit", prompt="hi")
        self.assertTrue(self.plist().exists())
        self.assertEqual(plistlib.loads(self.plist().read_bytes())["ProgramArguments"][-2:],
                         ["/plug/factory/bin/factory", "watch"])
        self.assertTrue(self.fake.ran("launchctl", "bootstrap"))

    def test_a_registered_session_installs_the_job_whatever_its_name_and_removes_it_when_it_ends(self):
        register("sid-d")
        renamed = [DISPATCHER | {"name": "any name", "cwd": "/elsewhere", "pid": 7}]
        self.hook("SessionStart", source="startup", sessions=renamed)
        self.assertTrue(self.plist().exists())
        self.hook("SessionEnd", reason="other", sessions=renamed)
        self.assertFalse(self.plist().exists())

    def test_a_registered_session_that_is_no_longer_listed_ends_the_job_by_its_id(self):
        register("sid-d")
        self.put_plist()
        self.hook("SessionEnd", reason="other", sessions=[], session_id="sid-other")
        self.assertTrue(self.plist().exists())
        self.hook("SessionEnd", reason="other", sessions=[], session_title="not the old-style name")
        self.assertFalse(self.plist().exists())

    def test_a_registered_session_without_a_process_still_ends_the_job(self):
        register("sid-d")
        self.put_plist()
        self.hook("SessionEnd", reason="other", sessions=[DISPATCHER | {"name": "custom"}])
        self.assertFalse(self.plist().exists())

    def test_a_session_that_is_not_the_registered_one_starts_nothing(self):
        register("sid-other")
        self.hook("UserPromptSubmit", prompt="hi",
                  sessions=[self.DISPATCHER_SESSION, DISPATCHER | {"sessionId": "sid-other", "name": "boss", "pid": 7}])
        self.assertFalse(self.plist().exists())

    def test_a_prompt_does_not_list_sessions_while_the_job_is_there(self):
        self.put_plist()
        self.hook("UserPromptSubmit", prompt="hi")
        self.hook("SessionStart", source="compact")
        self.assertFalse(self.listed())
        self.assertFalse(self.fake.ran("launchctl", "bootstrap"))

    def test_startup_and_resume_replace_the_job(self):
        for source in ("startup", "resume"):
            with self.subTest(source):
                self.put_plist(StartInterval=1)
                self.hook("SessionStart", source=source)
                self.assertTrue(self.listed())
                self.assertEqual(plistlib.loads(self.plist().read_bytes())["StartInterval"], 120)

    def test_a_session_that_is_not_the_dispatcher_starts_nothing(self):
        other = [DISPATCHER | {"name": "someone"}]
        elsewhere = [DISPATCHER | {"name": "dispatcher-repo", "cwd": "/elsewhere"}]
        cases = {"another name": dict(sessions=other), "another repository": dict(sessions=elsewhere),
                 "not listed": dict(sessions=[]), "a subagent": dict(agent_id="a1"),
                 "a task worktree": dict(cwd=f"{WT}/x"), "not a repository": dict(
                     answers={("git", "worktree", "list"): (128, "", "fatal: not a git repository")}),
                 "another event": dict(event="Stop")}
        for what, kw in cases.items():
            with self.subTest(what):
                self.hook(kw.pop("event", "UserPromptSubmit"), **kw)
                self.assertFalse(self.plist().exists())
                self.assertFalse(self.fake.ran("launchctl", "bootstrap"))

    def test_the_last_session_ending_removes_the_job(self):
        self.put_plist()
        self.hook("SessionEnd", reason="other")
        self.assertFalse(self.plist().exists())
        self.assertTrue(self.fake.ran("launchctl", "bootout"))

    def test_the_last_session_ending_keeps_the_job_while_a_task_is_handed_over(self):
        self.put_plist()
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "handed-over", "ts": iso(NOW - HOUR)}])
        self.hook("SessionEnd", reason="other", entries=["x"])
        self.assertTrue(self.plist().exists())

    def test_an_ending_session_that_is_no_longer_listed_is_known_by_its_title(self):
        self.put_plist()
        self.hook("SessionEnd", sessions=[], session_title="someone")
        self.assertTrue(self.plist().exists())
        self.hook("SessionEnd", sessions=[], session_title="dispatcher-repo")
        self.assertFalse(self.plist().exists())

    def test_a_clear_does_not_end_the_dispatcher(self):
        self.put_plist()
        self.hook("SessionEnd", reason="clear")
        self.assertTrue(self.plist().exists())

    def test_a_job_that_is_present_but_not_loaded_is_installed_again(self):
        self.put_plist(StartInterval=1)
        self.hook("UserPromptSubmit", answers={("launchctl", "print"): (113, "", "not found")})
        self.assertEqual(plistlib.loads(self.plist().read_bytes())["StartInterval"], 120)

    def test_another_live_dispatcher_keeps_the_job(self):
        twin = DISPATCHER | {"name": "dispatcher-repo", "sessionId": "sid-2", "cwd": f"{ROOT}/sub"}
        self.put_plist()
        self.hook("SessionEnd", sessions=[self.DISPATCHER_SESSION, twin])
        self.assertTrue(self.plist().exists())

    def test_sessions_that_are_not_live_dispatchers_of_this_repository_do_not_keep_the_job(self):
        done = DISPATCHER | {"name": "dispatcher-repo", "sessionId": "sid-2", "kind": "background", "state": "done"}
        away = DISPATCHER | {"name": "dispatcher-repo", "sessionId": "sid-3", "cwd": "/elsewhere"}
        self.put_plist()
        self.hook("SessionEnd", sessions=[self.DISPATCHER_SESSION, done, away])
        self.assertFalse(self.plist().exists())

    def test_a_failing_install_is_swallowed(self):
        self.hook("UserPromptSubmit", answers={("launchctl", "bootstrap"): (5, "", "Bootstrap failed")})
        self.tools.pop("gh")
        self.hook("UserPromptSubmit")

    def test_the_event_is_still_logged(self):
        self.hook("UserPromptSubmit", prompt="hi")
        [month] = self.store.glob("events-*.jsonl")
        self.assertEqual([json.loads(line)["event"] for line in month.read_text().splitlines()], ["UserPromptSubmit"])


class OrphanTests(SchedHarness):
    """The tick that finds no dispatcher session takes the job away."""

    def setUp(self):
        super().setUp()
        self.put_plist()
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "blocked", "ts": iso(NOW - 2 * HOUR)}])

    def watch(self, sessions, entries=("x",), **kw):
        return self.cli("watch", sessions=sessions, entries=list(entries), **kw)

    def test_with_live_tasks_it_notifies_once_and_removes_the_job(self):
        notice = {("osascript",): (0, "", "")}
        _, out, _ = self.watch([], answers=notice)
        self.assertFalse(self.plist().exists())
        self.assertIn(f"no dispatcher session for {ROOT}: removing the schedule", out)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "osascript"]), 1)
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertIn("notified on this machine", decision["reason"])
        self.watch([], answers=notice, at=NOW + 1800)
        self.assertFalse(self.fake.ran("osascript"))

    def test_without_tasks_it_removes_silently(self):
        _, out, _ = self.watch([], entries=[])
        self.assertFalse(self.plist().exists())
        self.assertFalse(self.fake.ran("osascript"))
        self.assertIn("nothing new", out)
        self.assertEqual([r for r in self.records() if "decision" in r], [])

    def test_it_does_not_start_the_site_manager(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "working", "ts": iso(NOW - HOUR)}])
        _, out, _ = self.watch([])
        self.assertEqual(self.claude_runs(), [])
        self.assertIn("ended-undeclared", out)
        self.assertFalse(self.plist().exists())

    def test_a_task_handed_over_keeps_the_job(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "handed-over", "ts": iso(NOW - HOUR)}])
        _, out, _ = self.watch([])
        self.assertTrue(self.plist().exists())
        self.assertNotIn("removing the schedule", out)
        self.watch([], at=NOW + 1800, entries=[])  # its worktree is gone
        self.assertFalse(self.plist().exists())

    def test_it_stays_while_the_missing_dispatcher_could_not_be_judged(self):
        unplaced = {"sessionId": "u", "kind": "interactive"}
        _, out, _ = self.watch([unplaced])
        self.assertIn("not evaluated: dispatcher-missing", out)
        self.assertNotIn("removing the schedule", out)
        self.assertTrue(self.plist().exists())

    def test_it_stays_when_the_sessions_cannot_be_listed(self):
        _, out, _ = self.watch([], answers={("claude", "agents"): (1, "", "boom")})
        self.assertTrue(self.plist().exists())
        self.assertNotIn("removing the schedule", out)

    def test_it_stays_while_a_dispatcher_lives(self):
        _, out, _ = self.watch([DISPATCHER | {"name": "dispatcher-repo"}])
        self.assertTrue(self.plist().exists())
        self.assertNotIn("removing the schedule", out)

    def test_a_dispatcher_of_another_repository_does_not_count(self):
        self.watch([DISPATCHER | {"name": "dispatcher-repo", "cwd": "/elsewhere"}], entries=[])
        self.assertFalse(self.plist().exists())

    def test_two_dispatchers_fire_the_missing_dispatcher_and_the_job_stays(self):
        twin = DISPATCHER | {"name": "dispatcher-repo", "sessionId": "sid-2"}
        self.watch([DISPATCHER | {"name": "dispatcher-repo"}, twin], answers={("osascript",): (0, "", "")})
        [notice] = [c for c in self.fake.calls if c[0] == "osascript"]
        self.assertIn("2 sessions named dispatcher-repo", notice[2])
        self.assertTrue(self.plist().exists())


if __name__ == "__main__":
    unittest.main()
