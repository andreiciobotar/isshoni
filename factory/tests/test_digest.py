import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_factory import ROOT, factory
from test_health import HOUR, NOW, Harness, Home, TimedRun, check, choice, fired, iso, pic, reply

MIX = {"cache_read": 0.6, "output": 0.4}
SHIFTED = {"cache_read": 0.45, "output": 0.3, "cache_write_1h": 0.25}
MONITOR = "/m/isshoni"


def role(total=1.0, calls=10, median=100_000, peak=150_000, tools=10, mix=MIX):
    return {"runs": 1, "models": ["m"], "calls": calls,
            "context": {"first": 1000, "median": median, "peak": peak, "max": peak},
            "cost": {"total": total, **{k: total * mix.get(k, 0) for k in factory.CLASSES}},
            "tools": {"Read": tools} if tools else {}, "dispatches": 0}


def written(product=0, tests=0, lines=10):
    return {k: {"calls": n, "lines": n * lines} for k, n in (("product", product), ("tests", tests), ("docs", 3))}


def task(slug, version="0.3.0", repo_path=ROOT, **roles):
    return {"repo": "repo", "repo_path": repo_path, "task": slug, "version": version, "start": None, "end": None,
            "cost": round(sum(r["cost"]["total"] for r in roles.values()), 6), "unpriced_tokens": 0, "roles": roles}


def lands(*tasks):
    """Tasks as the window holds them: in order, each merged an hour after the one before."""
    return [{**t, "merged": NOW + i * HOUR} for i, t in enumerate(tasks)]


def names(n, prefix):
    return [f"{prefix}{i}" for i in range(1, n + 1)]


def window_pic(current, baseline):
    return pic(window=(lands(*current), lands(*baseline), None))


def flat(prefix, n, **roles):
    return [task(s, **roles) for s in names(n, prefix)]


class Evidence(Home):
    """anatomy(p) and the window, through a faked monitor and gh."""

    def p(self, tasks, prs, **answers):
        run = TimedRun({(MONITOR, "anatomy"): answers.get("monitor", (0, json.dumps({"tasks": tasks}), ""))})
        self.run = run
        merged = [{"headRefName": f"worktree-{slug}", "mergedAt": iso(at)} for slug, at in prs]
        return pic(monitor=answers.get("setting", MONITOR), gh=lambda *a: merged), run

    def window(self, p, run):
        with mock.patch.object(factory, "run", run):
            return factory.window(p)

    def test_no_monitor(self):
        p, run = self.p([], [], setting=None)
        with mock.patch.object(factory, "run", run), self.assertRaises(factory.NotEvaluated) as cm:
            factory.anatomy(p)
        self.assertEqual(str(cm.exception),
                         'no monitor: set "monitor" in ~/.claude/factory/settings.json to the path of the isshoni command')

    def test_the_monitor_fails_or_answers_oddly(self):
        for answer, why in (((1, "", "x\nboom"), "the monitor failed: boom"), ((1, "", ""), "the monitor failed: exit 1"),
                            ((0, "not json", ""), "the monitor failed: unexpected output"),
                            ((0, "[]", ""), "the monitor failed: unexpected output"),
                            ((0, '{"tasks": [{"task": "a"}]}', ""), "the monitor failed: unexpected output")):
            with self.subTest(why=why):
                p, run = self.p([], [("a", NOW)], monitor=answer)
                with mock.patch.object(factory, "run", run), self.assertRaises(factory.NotEvaluated) as cm:
                    factory.anatomy(p)
                self.assertEqual(str(cm.exception), why)

    def test_a_role_block_that_lacks_a_field_is_unexpected_output(self):
        for block in ({"cost": {"total": 1}}, {**role(), "tools": None}, {k: v for k, v in role().items() if k != "calls"}):
            with self.subTest(block=sorted(block)):
                p, run = self.p([task("a", lead=block)], [("a", NOW)])
                with mock.patch.object(factory, "run", run), self.assertRaises(factory.NotEvaluated) as cm:
                    factory.anatomy(p)
                self.assertEqual(str(cm.exception), "the monitor failed: unexpected output")

    def test_a_failure_is_remembered(self):
        p, run = self.p([], [], monitor=(1, "", "boom"))
        with mock.patch.object(factory, "run", run):
            for _ in range(2):
                with self.assertRaises(factory.NotEvaluated):
                    factory.anatomy(p)
        self.assertEqual(len(run.calls), 1)

    def test_the_landed_tasks_of_this_repository_with_a_cost_oldest_merge_first(self):
        tasks = [task("late", lead=role(2)), task("early", lead=role(3)), task("free", lead=role(0)),
                 task("open", lead=role(1)), task("elsewhere", repo_path="/other", lead=role(1)),
                 {**task("nowhere", lead=role(1)), "repo_path": None}]
        prs = [("late", NOW - HOUR), ("early", NOW - 2 * HOUR), ("early", NOW - 5 * HOUR), ("free", NOW),
               ("elsewhere", NOW), ("nowhere", NOW)]
        p, run = self.p(tasks, prs)
        with mock.patch.object(factory, "run", run):
            got = factory.anatomy(p)
        self.assertEqual([(t["task"], t["merged"]) for t in got], [("early", NOW - 2 * HOUR), ("late", NOW - HOUR)])
        self.assertEqual(run.calls, [[MONITOR, "anatomy", "--json", "--repo", "repo"]])
        self.assertEqual(run.timeouts, [factory.MONITOR_TIMEOUT])

    def landed(self, n):
        tasks = [task(f"t{i}", lead=role()) for i in range(n)]
        return tasks, [(f"t{i}", NOW - (n - i) * 100 * HOUR) for i in range(n)]

    def test_without_a_marker_the_newest_five_are_current(self):
        p, run = self.p(*self.landed(12))
        current, baseline, last = self.window(p, run)
        self.assertEqual(([t["task"] for t in current], len(baseline), last), ([f"t{i}" for i in range(7, 12)], 7, None))

    def test_a_marker_splits_the_window_at_its_time(self):
        tasks, prs = self.landed(12)
        marker = {"ts": iso(NOW - 4 * 100 * HOUR + 1), "repo": ROOT, "digest": ["old"]}
        (self.store / "firings.jsonl").write_text(json.dumps(marker) + "\n" + json.dumps(
            {**marker, "ts": iso(NOW - 99 * 100 * HOUR), "digest": ["older"]}) + "\n" + json.dumps(
            {**marker, "repo": "/other", "ts": iso(NOW)}) + "\n")
        p, run = self.p(tasks, prs)
        current, baseline, last = self.window(p, run)
        self.assertEqual(([t["task"] for t in current], len(baseline), last), ([f"t{i}" for i in range(9, 12)], 9, marker))

    def test_findings_need_five_before_and_something_since(self):
        p, run = self.p(*self.landed(7))
        with mock.patch.object(factory, "run", run), self.assertRaises(factory.NotEvaluated) as cm:
            check("mix-shift", p, 5)
        self.assertEqual(str(cm.exception), "only 2 landed tasks before this digest's; 5 are needed to compare")
        tasks, prs = self.landed(10)
        (self.store / "firings.jsonl").write_text(json.dumps({"ts": iso(NOW), "repo": ROOT, "digest": []}) + "\n")
        p, run = self.p(tasks, prs)
        with mock.patch.object(factory, "run", run), self.assertRaises(factory.NotEvaluated) as cm:
            check("dearest-task", p, 1.5)
        self.assertEqual(str(cm.exception), "nothing landed since the last digest")


class Findings(unittest.TestCase):
    def test_mix_shift(self):
        base = flat("b", 5, lead=role(10))
        cur = flat("c", 5, lead=role(10, mix=SHIFTED))
        self.assertEqual(check("mix-shift", window_pic(cur, base), 5), [
            (None, "cache_write_1h", "cache_write_1h is 25% of cost over the 5 tasks landed since the last digest "
             "(c1, c2, c3, c4, c5), 0% over the 5 before")])
        self.assertEqual(check("mix-shift", window_pic(cur, base), 30), [])
        self.assertEqual(check("mix-shift", window_pic(base, base), 5), [])

    def test_more_than_six_tasks_are_named_six_and_n_more(self):
        found = check("mix-shift", window_pic(flat("c", 9, lead=role(10, mix=SHIFTED)), flat("b", 5, lead=role(10))), 5)
        self.assertIn("over the 9 tasks landed since the last digest (c1, c2, c3, c4, c5, c6 and 3 more), ", found[0][2])

    def test_dearest_task(self):
        base = flat("b", 5, lead=role(5, calls=40, peak=180_000), coder=role(1))
        small = flat("c", 4, lead=role(5, calls=40, peak=180_000), coder=role(1))
        big = task("big", lead=role(15.2, calls=210, peak=301_000), coder=role(1))
        self.assertEqual(check("dearest-task", window_pic([*small, big], base), 1.5), [
            ("big", "big", "cost 16.20, 2.7 times the median of the 5 before (6.00; threshold 1.5 times); mostly the "
             "lead: 15.20 against a median 5.00, 210 calls against 40, peak context 301k against 180k")])
        self.assertEqual(check("dearest-task", window_pic([*small, big], base), 3), [])

    def test_dearest_task_counts_a_role_the_baseline_lacks_as_zero(self):
        base = flat("b", 5, lead=role(5))
        big = task("big", lead=role(5), reviewer=role(10, calls=30, peak=90_000))
        [(slug, subject, detail)] = check("dearest-task", window_pic([big], base), 1.5)
        self.assertEqual((slug, subject), ("big", "big"))
        self.assertTrue(detail.endswith("mostly the reviewer: 10.00 against a median ?, 30 calls against ?, "
                                        "peak context 90k against ?"), detail)

    def test_dearest_task_shows_a_question_mark_for_null_baseline_context(self):
        nulls = {"first": None, "median": None, "peak": None, "max": None}
        base = [task(f"b{i}", lead={**role(5), "context": nulls}) for i in range(1, 6)]
        big = task("big", lead=role(15, peak=150_000))
        [(_, _, detail)] = check("dearest-task", window_pic([big], base), 1.5)
        self.assertTrue(detail.endswith("peak context 150k against ?"), detail)

    def growing(self, current_median=130_000, base_median=100_000, **kw):
        base = flat("b", 5, lead=role(median=base_median, **kw))
        return flat("c", 5, lead=role(median=current_median, **kw)), base

    def test_role_growing_context(self):
        cur, base = self.growing()
        self.assertEqual(check("role-growing", window_pic(cur, base), 20), [
            (None, "lead/context", "the lead's median context per call is 130k over 5 tasks (c1, c2, c3, c4, c5), "
             "100k over the 5 before: +30%")])
        self.assertEqual(check("role-growing", window_pic(cur, base), 30), [])
        self.assertEqual(check("role-growing", window_pic(*self.growing(110_000)), 20), [])

    def test_role_growing_own_calls(self):
        base = flat("b", 5, lead=role(tools=10))
        cur = flat("c", 5, lead=role(tools=15))
        self.assertEqual(check("role-growing", window_pic(cur, base), 20), [
            (None, "lead/own-calls", "the lead's median own tool calls per task is 15 over 5 tasks "
             "(c1, c2, c3, c4, c5), 10 over the 5 before: +50%")])

    def test_a_role_in_fewer_than_three_tasks_on_either_side_is_left_out(self):
        for cur_with, base_with in ((2, 5), (5, 2)):
            cur = [task(f"c{i}", lead=role(), **({"coder": role(median=500_000)} if i <= cur_with else {}))
                   for i in range(1, 6)]
            base = [task(f"b{i}", lead=role(), **({"coder": role(median=100_000)} if i <= base_with else {}))
                    for i in range(1, 6)]
            with self.subTest(cur_with=cur_with, base_with=base_with):
                self.assertEqual(check("role-growing", window_pic(cur, base), 20), [])

    def test_the_lower_bound_of_three_tasks_fires(self):
        def tasks(prefix, n_with, median):
            return [task(f"{prefix}{i}", lead=role(), **({"coder": role(median=median)} if i <= n_with else {}))
                    for i in range(1, 6)]
        found = check("role-growing", window_pic(tasks("c", 3, 500_000), tasks("b", 3, 100_000)), 20)
        self.assertEqual([f[1] for f in found], ["coder/context"])
        self.assertEqual(check("role-growing", window_pic(tasks("c", 2, 500_000), tasks("b", 3, 100_000)), 20), [])

    def test_no_data_is_not_a_measured_zero(self):
        nulls = {"first": None, "median": None, "peak": None, "max": None}
        cur, base = self.versions(4)
        for t in cur:
            t["roles"]["lead"]["context"] = nulls
        [(_, _, detail)] = check("version-shift", window_pic(cur, base), 20)
        self.assertNotIn("lead context", detail)
        cur, base = self.versions(4)
        for t in cur:
            del t["roles"]["lead"]
        [(_, _, detail)] = check("version-shift", window_pic(cur, base), 20)
        self.assertNotIn("lead context", detail)
        base = flat("b", 5, lead=role())
        cur = [task(f"c{i}", lead={**role(), "context": nulls}) for i in range(1, 6)]
        self.assertEqual(check("role-growing", window_pic(cur, base), 20), [])

    def test_a_baseline_median_of_zero_is_skipped(self):
        base = flat("b", 5, lead=role(tools=0))
        cur = flat("c", 5, lead=role(tools=9))
        self.assertEqual(check("role-growing", window_pic(cur, base), 20), [])

    def versions(self, n, version="0.4.0", baseline_version="0.3.0"):
        base = [task(f"b{i}", version=baseline_version, lead=role(5, median=100_000), coder=role(1)) for i in range(1, 6)]
        cur = [task(f"c{i}", version=version, lead=role(2.4, median=100_000), coder=role(1.6)) for i in range(1, n + 1)]
        return cur, base

    def test_version_shift(self):
        cur, base = self.versions(4)
        self.assertEqual(check("version-shift", window_pic(cur, base), 20), [
            (None, "0.4.0", "since plugin 0.4.0 (4 tasks: c1, c2, c3, c4): median task cost 4.00 against 6.00 over the "
             "5 before (-33%); lead share 60% against 83% (-28%)")])
        self.assertEqual(check("version-shift", window_pic(cur, base), 40), [])

    def test_version_shift_edges(self):
        cur, base = self.versions(2)
        self.assertEqual(check("version-shift", window_pic(cur, base), 20), [], "fewer than 3 tasks")
        cur, base = self.versions(4)
        base[0]["version"] = "0.4.0"
        self.assertEqual(check("version-shift", window_pic(cur, base), 20), [], "also in the baseline")
        cur, base = self.versions(4, version=None)
        self.assertEqual(check("version-shift", window_pic(cur, base), 20), [], "no version")
        cur, base = self.versions(4, version="0.3.0")
        self.assertEqual(check("version-shift", window_pic(cur, base), 20), [], "same version")

    def test_version_shift_names_every_measure_that_moved_and_skips_a_zero_baseline(self):
        cur, base = self.versions(3)
        for t in cur:
            t["roles"]["lead"]["context"]["median"] = 150_000
        [(_, _, detail)] = check("version-shift", window_pic(cur, base), 20)
        self.assertTrue(detail.endswith("lead share 60% against 83% (-28%); median lead context 150k against 100k "
                                        "(+50%)"), detail)
        for t in base:
            t["roles"]["lead"]["context"]["median"] = 0
        [(_, _, detail)] = check("version-shift", window_pic(cur, base), 20)
        self.assertNotIn("lead context", detail)

    def authored(self, n_current, n_baseline, tests=False):
        """5 current and 5 baseline tasks; the lead wrote code in the first n of each (as tests when `tests`)."""
        def mk(prefix, n):
            return [task(f"{prefix}{i}", lead={**role(), "written": written(**{"tests" if tests else "product": int(i <= n)})},
                         coder={**role(), "written": written(product=2)}) for i in range(1, 6)]
        return window_pic(mk("c", n_current), mk("b", n_baseline))

    def test_lead_authoring(self):
        self.assertEqual(check("lead-authoring", self.authored(3, 1), 50), [
            (None, "lead", "the lead wrote product code or tests in 3 of the last 5 landed tasks (c1, c2, c3), "
             "1 of the 5 before (60% against 20%); 30 lines of 130 written by all roles")])
        self.assertEqual(check("lead-authoring", self.authored(3, 1, tests=True), 50)[0][1], "lead")

    def test_lead_authoring_below_the_threshold_does_not_fire(self):
        self.assertEqual(check("lead-authoring", self.authored(2, 0), 50), [])
        self.assertEqual(check("lead-authoring", self.authored(0, 5), 50), [])

    def test_lead_authoring_counts_a_task_without_a_lead_as_not_written(self):
        base = [task(f"b{i}", coder={**role(), "written": written(product=2)}) for i in range(1, 6)]
        cur = [task(f"c{i}", coder={**role(), "written": written(product=2)}) for i in range(1, 6)]
        self.assertEqual(check("lead-authoring", window_pic(cur, base), 1), [])

    def test_lead_authoring_with_an_older_monitor_is_not_evaluated(self):
        old = flat("c", 5, lead=role())
        with self.assertRaises(factory.NotEvaluated) as cm:
            check("lead-authoring", window_pic(old, flat("b", 5, lead=role())), 50)
        self.assertEqual(str(cm.exception), "the monitor reports no authoring: update isshoni")


class CriteriaFile(unittest.TestCase):
    def load(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "criteria.json")
            path.write_text(json.dumps([{"name": "mix-shift", "escalate": "judgment", "threshold": 5, "what": "w", **kw}]))
            with mock.patch.object(factory, "CRITERIA_PATH", path):
                return factory.load_criteria()

    def test_job_is_watch_or_digest(self):
        self.assertEqual(len(self.load()), 1)
        self.assertEqual(len(self.load(job="digest")), 1)
        for job in ("monthly", None, 1):
            with self.subTest(job=job), self.assertRaises(factory.Fail) as cm:
                self.load(job=job)
            self.assertIn("job must be one of watch, digest", str(cm.exception))

    def test_the_shipped_digest_entries(self):
        shipped = {c["name"]: c for c in factory.load_criteria() if c.get("job") == "digest"}
        self.assertEqual({k: v["threshold"] for k, v in shipped.items()},
                         {"mix-shift": 5, "dearest-task": 1.5, "role-growing": 20, "version-shift": 20,
                          "lead-authoring": 50})
        self.assertEqual({v["escalate"] for v in shipped.values()}, {"judgment"})
        self.assertEqual({c["name"] for c in factory.job_criteria(factory.load_criteria(), "watch")}
                         & set(shipped), set())


class DigestCase(Harness):
    CRITERIA = ["mix-shift"]

    def world(self, base=5, cur=5, marker_age=None, monitor=True, shift=True):
        """Answers for a monitor and gh with `base` landed tasks, then `cur` more; a marker `marker_age` hours old."""
        tasks, prs = [], []
        for i in range(base + cur):
            slug = f"b{i + 1}" if i < base else f"c{i - base + 1}"
            mix = SHIFTED if shift and i >= base else MIX
            at = NOW - 100 * HOUR + i * HOUR if i < base else NOW - 10 * HOUR + (i - base) * HOUR
            tasks.append(task(slug, lead=role(10, mix=mix)))
            prs.append({"headRefName": f"worktree-{slug}", "mergedAt": iso(at)})
        if marker_age is not None:
            self.seed("firings", [{"ts": iso(NOW - marker_age * HOUR), "repo": ROOT, "digest": ["old"]}])
        if monitor:
            (self.store / "settings.json").write_text(json.dumps({"monitor": MONITOR}))
        return {(MONITOR, "anatomy"): (0, json.dumps({"tasks": tasks}), ""),
                ("gh", "pr", "list", "--state", "merged"): (0, json.dumps(prs), "")}

    def markers(self):
        return [r for r in self.records() if "digest" in r]

    def use(self, *names):
        shipped = {c["name"]: c for c in json.loads((factory.PLUGIN_ROOT / "criteria.json").read_text())}
        self.criteria_path.write_text(json.dumps([shipped[n] for n in names]))

    def judge(self):
        return lambda: reply([choice(next(r["id"] for r in self.records() if "criterion" in r))])

    def test_a_dry_run_prints_the_findings_and_writes_nothing(self):
        answers = self.world()
        before = {p.name: p.read_bytes() for p in self.store.iterdir()}
        code, out, _ = self.cli("digest", "--dry-run", answers=answers)
        doc = json.loads(out)
        self.assertEqual((code, doc["not_evaluated"]), (0, []))
        [f] = doc["firings"]
        self.assertEqual((f["criterion"], f["subject"], f["slug"]), ("mix-shift", "cache_write_1h", None))
        self.assertEqual(self.claude_runs(), [])
        self.assertEqual({p.name: p.read_bytes() for p in self.store.iterdir()}, before)

    def test_a_digest_fires_asks_decides_clears_and_marks(self):
        code, out, _ = self.cli("digest", answers={**self.world(), ("claude", "-p"): self.judge()})
        self.assertEqual(code, 0)
        fired, decision, cleared, marker = self.records()
        self.assertEqual((fired["criterion"], fired["subject"], fired["escalate"]), ("mix-shift", "cache_write_1h", "judgment"))
        self.assertEqual((decision["id"], decision["decision"], decision["by"]), (fired["id"], "escalated", "site-manager"))
        self.assertEqual(cleared, {"ts": iso(NOW), "repo": ROOT, "id": fired["id"], "cleared": True})
        self.assertEqual(marker, {"ts": iso(NOW), "repo": ROOT, "digest": names(5, "c")})
        [folded], _ = factory.fold(self.records())
        self.assertEqual((folded["decision"], folded["cleared"]), ("escalated", True))
        self.assertIn(f"{fired['id']}  mix-shift  cache_write_1h  escalated", out)
        [argv] = self.claude_runs()
        head, _, body = argv[-1].partition("\n\n")
        self.assertEqual(head, f"Job: digest. Repo: {ROOT}. The dispatcher's session name is dispatcher-repo.")
        doc = json.loads(body)
        self.assertEqual(doc["window"], {"current": names(5, "c"), "baseline": 5, "since": None})
        self.assertEqual([f["id"] for f in doc["findings"]], [fired["id"]])
        self.assertEqual(doc["findings"][0]["subject"], "cache_write_1h")
        self.assertEqual(self.fake.timeouts[-1], factory.WATCH_TIMEOUT)

    def test_a_digest_with_no_dispatcher_to_address_is_skipped(self):
        code, out, _ = self.cli("digest", answers=self.world(), sessions=[])
        self.assertEqual((code, out), (0, "digest skipped: no dispatcher session for /repo: run /factory:dispatch in a session there\n"))
        self.assertEqual((self.claude_runs(), self.records()), ([], []))

    def test_the_next_window_starts_at_the_marker(self):
        self.cli("digest", answers={**self.world(), ("claude", "-p"): self.judge()})
        self.clock = NOW + HOUR
        _, out, _ = self.cli("digest", answers=self.world(base=10, cur=0))
        self.assertIn("not evaluated: mix-shift: nothing landed since the last digest", out)

    def test_an_unusable_run_clears_the_firings_and_leaves_no_marker(self):
        code, _, err = self.cli("digest", answers={**self.world(), ("claude", "-p"): (1, "", "boom")})
        self.assertEqual(code, 1)
        self.assertIn("the site manager run failed (1): boom", err)
        fired, cleared = self.records()
        self.assertEqual(cleared["id"], fired["id"])
        self.assertTrue(cleared["cleared"])
        self.assertEqual(self.markers(), [])

    def test_nothing_fired_but_evaluated_writes_the_marker(self):
        code, out, _ = self.cli("digest", answers=self.world(shift=False))
        self.assertEqual((code, self.claude_runs()), (0, []))
        self.assertIn("digest: nothing to report", out)
        self.assertEqual([m["digest"] for m in self.markers()], [names(5, "c")])
        self.assertEqual(len(self.records()), 1)

    def test_nothing_evaluated_writes_no_marker(self):
        code, out, _ = self.cli("digest", answers=self.world(monitor=False))
        self.assertEqual(code, 0)
        self.assertIn("not evaluated: mix-shift: no monitor: set", out)
        self.assertIn("digest: nothing to report", out)
        self.assertEqual(self.records(), [])

    def test_health_and_watch_evaluate_watch_criteria_only(self):
        self.use("past-limit", "mix-shift")
        code, out, _ = self.cli("health", "--json")
        self.assertEqual(json.loads(out), {"firings": [], "not_evaluated": []})
        code, out, _ = self.cli("health")
        self.assertNotIn("mix-shift", out)
        _, out, _ = self.cli("watch")
        self.assertNotIn("not evaluated: mix-shift", out)
        self.assertEqual(out.count("digest skipped: no monitor"), 1)

    def test_a_tick_clears_a_digest_firing_a_killed_digest_left_open(self):
        self.seed("firings", [fired("aaa", criterion="mix-shift", escalate="judgment", subject="cache_read")])
        self.cli("watch", answers=self.world(marker_age=1))
        [folded], _ = factory.fold(self.records())
        self.assertTrue(folded["cleared"])

    def test_a_due_tick_with_too_few_before_prints_four_lines_and_marks_nothing(self):
        self.use("mix-shift", "dearest-task", "role-growing", "version-shift")
        _, out, _ = self.cli("watch", answers=self.world(base=3, cur=5))
        self.assertEqual(out.count("not evaluated:"), 4)
        self.assertIn("only 3 landed tasks before this digest's", out)
        self.assertEqual((self.markers(), self.claude_runs()), ([], []))

    # ---- the watch tick's cadence

    def tick_digest(self, **kw):
        answers = kw.pop("answers", None) or self.world(**kw)
        return self.cli("watch", answers={**answers, ("claude", "-p"): self.judge()})

    def test_a_tick_runs_the_digest_when_none_was_ever_made(self):
        code, out, _ = self.tick_digest()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertEqual(len(self.markers()), 1)

    def test_a_tick_is_not_due_before_a_day_and_costs_nothing(self):
        answers = self.world(marker_age=23)
        _, out, _ = self.cli("watch", answers=answers)
        self.assertNotIn("digest", out)
        self.assertFalse(self.fake.ran(MONITOR))
        self.assertEqual(self.claude_runs(), [])

    def test_a_tick_is_due_after_a_day(self):
        self.tick_digest(marker_age=25)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertEqual(len(self.markers()), 2)

    def test_fewer_than_five_current_is_silent_and_unmarked(self):
        _, out, _ = self.tick_digest(cur=4, marker_age=25)
        self.assertNotIn("digest", out)
        self.assertNotIn("not evaluated", out)
        self.assertEqual((self.claude_runs(), len(self.markers())), ([], 1))

    def test_a_missing_monitor_skips_with_one_line_per_tick(self):
        _, out, _ = self.cli("watch")
        self.assertEqual(out.count("digest skipped: no monitor: set"), 1)
        self.assertEqual(self.records(), [])

    def test_a_failing_monitor_skips_too(self):
        answers = {**self.world(), (MONITOR, "anatomy"): (1, "", "boom")}
        _, out, _ = self.cli("watch", answers=answers)
        self.assertIn("digest skipped: the monitor failed: boom\n", out)
        self.assertEqual(self.markers(), [])

    def test_an_orphaned_tick_makes_no_digest(self):
        _, out, _ = self.cli("watch", sessions=[], answers=self.world())
        self.assertNotIn("digest", out)
        self.assertFalse(self.fake.ran(MONITOR))

    def test_a_tick_that_fires_dispatcher_missing_makes_no_digest(self):
        self.use("dispatcher-missing", "mix-shift")
        d = {"sessionId": "sid-d", "name": "dispatcher-repo", "cwd": ROOT, "kind": "interactive"}
        _, out, _ = self.cli("watch", sessions=[d, {**d, "sessionId": "sid-e"}], entries=["x"],
                             answers=self.world())
        self.assertIn("dispatcher-missing", out)
        self.assertFalse(self.fake.ran(MONITOR))

    def test_a_failed_digest_leaves_the_ticks_own_firings_handled(self):
        self.use("past-limit", "mix-shift")
        answers = {**self.world(), ("claude", "-p"): [reply(), (1, "", "boom")]}
        code, out, err = self.cli("watch", answers=answers, entries=["x"],
                                  sessions=[{"sessionId": "sid-d", "name": "dispatcher-repo", "cwd": ROOT,
                                             "kind": "interactive"}, self.overtime()])
        self.assertEqual(code, 1)
        self.assertIn("the site manager run failed (1): boom", err)
        self.assertIn("past-limit  x  undecided", out)
        self.assertEqual([(r["criterion"]) for r in self.records() if "criterion" in r], ["past-limit", "mix-shift"])
        self.assertEqual(self.markers(), [])
        folded, _ = factory.fold(self.records())
        self.assertEqual([(f["criterion"], f["cleared"]) for f in folded], [("past-limit", False), ("mix-shift", True)])


if __name__ == "__main__":
    unittest.main()
