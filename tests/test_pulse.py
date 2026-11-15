import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from isshoni import cli, kaizen, metrics, pulse
from tests import test_kaizen
from tests.support import AGENTS
from tests.test_kaizen import ALPHA, Store, ev, fresh, loose, pr, reply, run_cli, session

LOCAL = timezone(timedelta(hours=2))
NOW = datetime(2026, 11, 15, 23, 0, tzinfo=LOCAL)  # today: 2026-11-15 local = 2026-11-14T22:00Z up to 2026-11-15T22:00Z
TODAY, YESTERDAY = "2026-11-15", "2026-11-14"


class PulseStore(Store):
    """Tasks merged on given days, a queue log, and no real git or gh: a subprocess fails as if the binary were missing."""

    def setUp(self):
        super().setUp()
        self.events, self.states, self.queued, self.prs = [], [], [], {}
        patch = mock.patch.object(subprocess, "run", side_effect=FileNotFoundError(2, "no such binary"))
        self.run = patch.start()
        self.addCleanup(patch.stop)

    def task(self, slug, number, merged, handed=None, size=None, repo=ALPHA, started="2026-11-01T00:00:00Z"):
        if started:  # None: a task that only has state records
            self.events.append(ev(started, "TaskStarted", repo=repo, slug=slug, **({"size": size} if size else {})))
        if handed:
            self.states.append({"ts": handed, "repo": repo, "slug": slug, "state": "handed-over"})
        self.prs.setdefault(repo, []).append(pr(number, "worktree-" + slug, "2026-11-01T00:00:00Z", merged))

    def queue(self, step, number, ts, head="h0", repo=ALPHA, **kw):
        self.queued.append({"ts": ts, "repo": repo, "slug": None, "pr": number, "head": head, "step": step, **kw})

    def gh_says(self, *replies):
        """gh answers `replies` in turn; git still fails."""
        answers = iter(replies)

        def run(argv, **kw):
            if argv[0] == "git":
                raise FileNotFoundError(2, "no such binary")
            return next(answers)

        self.run.side_effect = run

    def gh_calls(self):
        return [c.args[0][2] for c in self.run.call_args_list if c.args[0][0] == "gh"]

    def seed(self, kind, repo, cache):
        kaizen._write_cache(kaizen.cache_path(self.factory, kind, repo), cache)

    def pulse(self, now=NOW, cached=False, **kw):
        self.log("events-2026-11.jsonl", self.events)
        self.log("tasks-2026-11.jsonl", self.states)
        self.log("queue-2026-11.jsonl", self.queued)
        return self.build(lambda path: self.prs.get(path, []), now=now, cached=cached, **kw)["pulse"]


class DayTests(PulseStore):
    def test_a_day_is_a_calendar_day_in_the_zone_of_now(self):
        self.task("p1", 1, "2026-11-14T21:59:59Z")
        self.task("p2", 2, "2026-11-14T22:00:00Z")
        p = self.pulse()
        self.assertEqual((p["today"]["date"], p["baseline"]["date"]), (TODAY, YESTERDAY))
        self.assertEqual((p["today"]["merged"], p["baseline"]["merged"]), (1, 1))
        utc = self.pulse(now=datetime(2026, 11, 15, 12, tzinfo=timezone.utc))
        self.assertEqual((utc["today"]["merged"], utc["baseline"]["merged"]), (0, 2))

    def test_the_cutoff_does_not_change_today(self):
        self.task("p1", 1, "2026-11-14T23:00:00Z")
        self.assertEqual(self.pulse(cutoff=datetime(2026, 11, 15, tzinfo=timezone.utc))["today"]["merged"], 1)

    def test_the_task_gains_its_stated_size(self):
        self.task("p1", 1, "2026-11-15T08:00:00Z", size="small")
        self.task("p2", 2, "2026-11-15T08:00:00Z")
        self.pulse()
        d = self.build(lambda path: self.prs[path], now=NOW)
        self.assertEqual({t["slug"]: t["size"] for t in d["tasks"]}, {"p1": "small", "p2": None})


class SinceTests(PulseStore):
    """The command takes no `now`: times are relative to the real clock."""

    def setUp(self):
        super().setUp()
        self.start = pulse._midnight(date.today() - timedelta(days=1), None)

    def at(self, hours):
        return (self.start + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def reviewed(self, slug, hours, rename=None):
        """A session of `slug` with one review, its lead call `hours` after the start of yesterday (a file of its own when renamed)."""
        with mock.patch.object(test_kaizen, "DAY", self.at(hours)[:10]):
            session(self.projects, ALPHA, slug, self.at(hours)[:-1] + ".000Z", 1000000, [("reviewer", "claude-opus-5-5", 1, "10:20", "10:25")])
        if rename:
            os.rename(os.path.join(self.projects, "p-" + slug), os.path.join(self.projects, rename))

    def kaizen(self, *argv):
        self.log("events-2026-11.jsonl", self.events)
        self.log("tasks-2026-11.jsonl", self.states)
        args = ("--factory-dir", self.factory, "--projects-dir", self.projects, "--prices", os.path.join(os.path.dirname(self.factory), "prices.toml"),
                "--agents-dir", AGENTS, "--json")
        with mock.patch.object(kaizen, "gh_prs", side_effect=fresh(lambda path: self.prs[path])):
            return json.loads(run_cli("kaizen", *argv, *args)[1])

    def test_a_session_before_yesterday_of_a_task_merged_yesterday_is_loaded(self):
        self.task("p1", 1, self.at(13), self.at(12), started=self.at(-7))
        self.reviewed("p1", -6)  # ended long before the cutoff and before yesterday
        for since in ((), ("--since", "1h")):
            self.assertEqual(self.kaizen(*since)["pulse"]["baseline"]["first_pass"]["unstated"], {"yes": 1, "of": 1}, since)

    def test_a_round_before_yesterday_still_counts_so_the_task_is_not_first_pass(self):
        self.task("p1", 1, self.at(13), self.at(12), started=self.at(-7))
        self.reviewed("p1", -6, rename="p-early")
        self.reviewed("p1", 12)
        for since in ((), ("--since", "1h")):
            self.assertEqual(self.kaizen(*since)["pulse"]["baseline"]["first_pass"]["unstated"], {"yes": 0, "of": 1}, since)

    def test_a_session_older_than_what_the_figures_need_is_not_loaded(self):
        self.task("p1", 1, self.at(13), self.at(12), started=self.at(-7))
        self.task("old", 2, self.at(-100), self.at(-101), started=self.at(-120))  # merged long ago: its sessions are not needed
        self.reviewed("p1", -6, rename="p-early")
        self.reviewed("old", -110)
        loose(self.projects, "disp", ALPHA, [(self.at(-90), "claude-fable-5-2", 1)], title="dispatcher-alpha")
        self.assertEqual(self.loaded(), {"p-early", "p-old", "p-disp"})
        self.assertEqual(self.loaded("--since", "1h"), {"p-early"})  # the old task's session and the old dispatcher's are not read

    def loaded(self, *argv):
        """The project directories of the sessions the command kept for analysis."""
        with mock.patch.object(metrics, "analyze", wraps=metrics.analyze) as analyze:
            self.kaizen(*argv)
        return {c.args[0].project_dir for c in analyze.call_args_list}

    def test_a_task_without_a_start_makes_everything_load_so_since_changes_nothing(self):
        self.task("p1", 1, self.at(13), self.at(12), started=None)
        self.reviewed("p1", -6, rename="p-early")
        self.reviewed("p1", 12)
        self.assertEqual(self.kaizen()["pulse"]["baseline"]["first_pass"]["unstated"], {"yes": 0, "of": 1})
        self.assertEqual(self.kaizen("--since", "1h")["pulse"], self.kaizen()["pulse"])

    def test_since_never_changes_the_pulse(self):
        def early():
            self.task("p1", 1, self.at(13), self.at(12), started=self.at(-7))
            self.reviewed("p1", -6)

        def two_rounds():
            self.task("p1", 1, self.at(13), self.at(12), started=self.at(-7))
            self.reviewed("p1", -6, rename="p-early")
            self.reviewed("p1", 12)

        def no_start():
            self.task("p1", 1, self.at(13), self.at(12), started=None)
            self.reviewed("p1", -6, rename="p-early")
            self.reviewed("p1", 12)

        def plain():
            self.task("p1", 1, self.at(13), self.at(12))
            self.task("p2", 2, self.at(30), self.at(29), size="small")
            self.reviewed("p2", 28)
            loose(self.projects, "disp", ALPHA, [(self.at(20), "claude-fable-5-2", 1000)], title="dispatcher-alpha")

        for shape in (early, two_rounds, no_start, plain):
            with self.subTest(shape.__name__):
                self.events, self.states, self.prs = [], [], {}
                shutil.rmtree(self.projects)
                os.makedirs(self.projects)
                shape()
                everything = self.kaizen()["pulse"]
                self.assertGreater(everything["baseline"]["merged"], 0)
                self.assertEqual(self.kaizen("--since", "1h")["pulse"], everything)

    def test_load_from_is_the_earliest_of_the_cutoff_the_baseline_and_the_starts_of_tasks_merged_since(self):
        cutoff, baseline = datetime(2026, 11, 15, 6, tzinfo=timezone.utc), datetime(2026, 11, 8, tzinfo=timezone.utc)
        started = datetime(2026, 11, 3, tzinfo=timezone.utc)
        self.assertEqual(pulse.sessions_from(cutoff, baseline, []), baseline)
        self.assertEqual(pulse.sessions_from(cutoff, baseline, [started]), started)
        self.assertIsNone(pulse.sessions_from(None, baseline, [started]))
        self.assertIsNone(pulse.sessions_from(cutoff, baseline, [started, None]))
        self.assertEqual(pulse.sessions_from(datetime(2026, 11, 1, tzinfo=timezone.utc), baseline, [started]), datetime(2026, 11, 1, tzinfo=timezone.utc))

    def test_the_baseline_of_the_range_is_loaded_back_to_its_first_midnight(self):
        self.task("p1", 1, self.at(13), self.at(12), started=self.at(-7))
        first = date.today() - timedelta(days=3)  # the baseline's first day; the cutoff is its local noon, which no clock change touches
        midnight = pulse._midnight(first, None)
        cutoff = datetime(first.year, first.month, first.day, 12).astimezone().isoformat()
        hours = lambda moment: (moment - self.start) / timedelta(hours=1)
        loose(self.projects, "in", ALPHA, [(self.at(hours(midnight) + 1), "claude-fable-5-2", 1)], title="dispatcher-alpha")  # an hour into that day, before the task started and before the cutoff
        loose(self.projects, "out", ALPHA, [(self.at(hours(midnight) - 1), "claude-fable-5-2", 1)], title="dispatcher-alpha")
        self.assertEqual(self.loaded("--since", cutoff), {"p-in"})


class BaselineTests(PulseStore):
    """Today is 2026-11-15 (local, +02:00). The cutoffs are the range selector's."""

    def baseline(self, since=None):
        p = self.pulse(cutoff=None if since is None else NOW - since)
        return p["baseline"]["date"], p["baseline"]["to"], p["baseline"]["days"], [d["date"] for d in p["days"]]

    def test_the_baseline_is_the_whole_days_of_the_range_without_today(self):
        self.assertEqual(self.baseline(timedelta(hours=24))[:3], ("2026-11-14", "2026-11-14", 1))
        self.assertEqual(self.baseline(timedelta(days=7))[:3], ("2026-11-08", "2026-11-14", 7))
        self.assertEqual(self.baseline(timedelta(days=30))[:3], ("2026-10-16", "2026-11-14", 30))

    def test_a_cutoff_later_than_yesterday_still_has_yesterday(self):
        self.assertEqual(self.baseline(timedelta(hours=1))[:3], ("2026-11-14", "2026-11-14", 1))

    def test_without_a_cutoff_it_starts_on_the_earliest_merge_or_yesterday(self):
        self.assertEqual(self.baseline()[:3], ("2026-11-14", "2026-11-14", 1))  # nothing merged
        self.task("p1", 1, "2026-11-10T08:00:00Z")
        self.task("p2", 2, "2026-11-15T08:00:00Z")
        self.assertEqual(self.baseline()[:3], ("2026-11-10", "2026-11-14", 5))

    def test_the_days_are_oldest_first_with_today_last_and_one_more_than_the_baseline(self):
        _, _, days, dates = self.baseline(timedelta(days=7))
        self.assertEqual((len(dates), dates[0], dates[-1], dates == sorted(dates)), (days + 1, "2026-11-08", TODAY, True))

    def test_the_baseline_is_pooled_not_a_mean_of_the_days(self):
        self.task("a1", 1, "2026-11-13T08:40:00Z", "2026-11-13T08:00:00Z")
        for n, slug, handed, merged in ((2, "b1", "08:00", "08:10"), (3, "b2", "09:00", "09:20"), (4, "b3", "10:00", "10:30")):
            self.task(slug, n, "2026-11-14T%s:00Z" % merged, "2026-11-14T%s:00Z" % handed)
        loose(self.projects, "disp", ALPHA, [("2026-11-13T10:00:00.000Z", "claude-fable-5-2", 1000000)], title="dispatcher-alpha")  # 10.0 spent on the 13th
        p = self.pulse(cutoff=NOW - timedelta(days=7))
        self.assertEqual([d["cost"]["per_merged"] for d in p["days"][-3:-1]], [10.0, 0.0])  # the 13th and the 14th
        self.assertEqual((p["baseline"]["merged"], p["baseline"]["cost"]["per_merged"]), (4, 2.5))  # 10.0 over 4, not the mean of 10.0 and 0.0
        self.assertEqual(p["baseline"]["handover"]["to_merge"], {"p50_s": 1500, "p90_s": 2220, "n": 4})  # 2400 600 1200 1800 over both days

    def test_the_send_backs_are_summed_over_the_days_the_log_covers(self):
        self.queue("updated", 9, "2026-11-12T10:00:00Z")  # the log starts on the 12th
        self.queue("sent-back", 2, "2026-11-13T08:00:00Z", head="b")
        self.queue("sent-back", 3, "2026-11-14T08:00:00Z", head="c")
        self.queue("updated", 3, "2026-11-14T09:00:00Z", head="c")
        p = self.pulse(cutoff=NOW - timedelta(days=7))
        self.assertEqual((p["baseline"]["handover"]["sent_back"], p["baseline"]["handover"]["sent_back_unchanged"]), (2, 1))
        self.assertEqual([d["handover"]["sent_back"] for d in p["days"][:3]], [None, None, None])  # the 8th to the 10th
        self.assertEqual(p["days"][-3]["handover"]["sent_back"], 1)  # the 13th

    def test_today_without_a_merged_pull_request_has_no_figure_and_no_verdict(self):
        self.task("a1", 1, "2026-11-14T08:00:00Z")
        p = self.pulse(cutoff=NOW - timedelta(days=7))
        self.assertEqual((p["today"]["merged"], p["today"]["cost"]["per_merged"], p["today"]["conflicts"]["per_merged"]), (0, None, None))
        self.assertEqual(p["verdicts"], {"cost": None, "first_pass": None, "approval_to_merge": None, "conflicts": None})


class VerdictTests(unittest.TestCase):
    def test_few_below_five_pull_requests_on_either_side(self):
        self.assertEqual([pulse._verdict((1.0, n), (10.0, 80), True) for n in (4, 5)], ["few", "better"])
        self.assertEqual([pulse._verdict((1.0, 80), (10.0, n), True) for n in (4, 5)], ["few", "better"])

    def test_same_within_ten_percent_of_the_baseline_and_both_zero_is_same(self):
        self.assertEqual([pulse._verdict((t, 9), (10.0, 9), True) for t in (11.0, 11.5, 9.0, 8.5)], ["same", "worse", "same", "better"])
        self.assertEqual(pulse._verdict((0.0, 9), (0.0, 9), True), "same")

    def test_a_higher_first_pass_is_better_and_no_value_is_null(self):
        self.assertEqual([pulse._verdict((t, 9), (0.5, 9), False) for t in (0.8, 0.2)], ["better", "worse"])
        self.assertEqual([pulse._verdict(a, b, True) for a, b in (((None, 0), (1.0, 9)), ((1.0, 9), (None, 0)))], [None, None])

    def test_the_four_verdicts_come_from_the_figures(self):
        def day(merged, cost, yes, of, p50, conflict):
            return {"merged": merged, "cost": {"per_merged": cost}, "first_pass": {"small": {"yes": yes, "of": of}},
                    "handover": {"approval_to_merge": {"p50_s": p50, "n": of}}, "conflicts": {"per_merged": conflict, "prs": merged}}

        self.assertEqual(pulse._verdicts(day(5, 5.0, 5, 5, 900, 0.0), day(50, 10.0, 25, 50, 600, 0.0)),
                         {"cost": "better", "first_pass": "better", "approval_to_merge": "worse", "conflicts": "same"})
        self.assertEqual(pulse._verdicts(day(0, None, 0, 0, None, None), day(50, 10.0, 25, 50, 600, 0.0)),
                         {"cost": None, "first_pass": None, "approval_to_merge": None, "conflicts": None})


class ZoneTests(unittest.TestCase):
    def test_the_bounds_of_a_day_follow_the_machines_zone_at_that_date(self):
        old = os.environ.get("TZ")
        self.addCleanup(lambda: (os.environ.__setitem__("TZ", old) if old else os.environ.pop("TZ", None), time.tzset()))
        os.environ["TZ"] = "America/New_York"  # the clock went back on 2026-11-01
        time.tzset()
        starts = [pulse._midnight(date(2026, 11, d), None) for d in (1, 2, 3)]
        self.assertEqual([s.strftime("%m-%dT%H") for s in starts], ["11-01T04", "11-02T05", "11-03T05"])  # the first day is 25 hours long


class CostTests(PulseStore):
    def test_everything_spent_on_the_day_over_the_pull_requests_merged_on_it(self):
        self.task("a1", 1, "2026-11-15T08:40:00Z")
        with mock.patch.object(test_kaizen, "DAY", YESTERDAY):  # the coder's dispatch is yesterday's spend
            session(self.projects, ALPHA, "a1", "2026-11-15T08:00:05.000Z", 1000000, [("coder", "claude-haiku-5-5", 2000000, "10:30", "10:35")])
        loose(self.projects, "disp", ALPHA, [("2026-11-15T10:00:00.000Z", "claude-fable-5-2", 100000),
                                             ("2026-11-15T11:00:00.000Z", "claude-sonnet-5-5", 500000)], title="dispatcher-alpha")
        loose(self.projects, "site", ALPHA, [("2026-11-15T10:30:00.000Z", "claude-haiku-5-5", 1000000)],
              prompt="Job: watch. Repo: %s. The dispatcher's session name is dispatcher-alpha." % ALPHA)
        loose(self.projects, "mine", ALPHA, [("2026-11-15T10:30:00.000Z", "claude-fable-5-2", 1000000)], title="a person's own")
        loose(self.projects, "other", "/r/other", [("2026-11-15T10:30:00.000Z", "claude-fable-5-2", 1000000)], title="dispatcher-other")
        p = self.pulse()
        self.assertEqual(p["today"]["cost"], {"tasks": 10.0, "dispatcher": 1.0, "site_manager": 1.0, "unpriced_tokens": 500000, "per_merged": 12.0})
        self.assertEqual(p["baseline"]["cost"], {"tasks": 2.0, "dispatcher": 0.0, "site_manager": 0.0, "unpriced_tokens": 0, "per_merged": None})

    def test_no_merged_pull_request_is_a_null_cost_not_zero(self):
        p = self.pulse()
        self.assertEqual(p["today"]["cost"], {"tasks": 0.0, "dispatcher": 0.0, "site_manager": 0.0, "unpriced_tokens": 0, "per_merged": None})


class FirstPassTests(PulseStore):
    def test_by_stated_size_and_anything_else_is_unstated(self):
        for slug, number, size, rounds in (("p1", 1, "small", 1), ("p2", 2, "huge", 2), ("p3", 3, None, 1), ("p4", 4, "medium", None)):
            self.task(slug, number, "2026-11-15T08:00:00Z", size=size)
            if rounds:
                session(self.projects, ALPHA, slug, "2026-11-01T00:00:05.000Z", 0, [("reviewer", "claude-opus-5-5", 1, "10:20", "10:25")] * rounds)
        want = {"small": {"yes": 1, "of": 1}, "medium": {"yes": 0, "of": 0}, "large": {"yes": 0, "of": 0}, "unstated": {"yes": 1, "of": 2}}
        self.assertEqual(self.pulse()["today"]["first_pass"], want)
        self.assertEqual(list(self.pulse()["baseline"]["first_pass"]), ["small", "medium", "large", "unstated"])


class HandoverTests(PulseStore):
    def test_p90_to_merge_and_its_split_at_the_approval(self):
        for slug, n, handed, merged in (("h1", 1, "08:00", "08:10"), ("h2", 2, "09:00", "09:20"), ("h3", 3, "10:00", "10:30"),
                                        ("h4", 4, "11:00", "11:10"), ("h5", 5, "12:00", "11:50"), ("h6", 6, None, "11:00"),
                                        ("h7", 7, "13:00", "13:05")):
            self.task(slug, n, "2026-11-15T%s:00Z" % merged, handed and "2026-11-15T%s:00Z" % handed)
        self.queue("merged", 1, "2026-11-15T08:10:00Z", approved_at="2026-11-15T08:04:00Z")  # approval from the queue
        self.queue("merged", 3, "2026-11-15T10:30:00Z", approved_at=None)  # no approval was required: no lookup, not in the split
        self.queue("merged", 4, "2026-11-15T11:10:00Z", approved_at="2026-11-15T11:20:00Z")  # approved after the merge: not in the split
        self.seed("approvals", ALPHA, {"2": "2026-11-15T09:15:00Z", "7": None})  # from the backfill; 7 was looked up, none found
        h = self.pulse()["today"]["handover"]
        self.assertEqual(h["to_merge"], {"p50_s": 600, "p90_s": 1560, "n": 5})  # 600 1200 1800 600 300; h5 handed over after the merge, h6 never
        self.assertEqual((h["to_approval"], h["approval_to_merge"]), ({"p50_s": 570, "p90_s": 834, "n": 2}, {"p50_s": 330, "p90_s": 354, "n": 2}))
        self.assertEqual(h["approvals_backfilled"], 1)
        self.assertEqual(self.gh_calls(), [])  # every approval was known

    def test_nothing_handed_over_is_null_not_zero(self):
        self.task("p1", 1, "2026-11-15T08:00:00Z")
        h = self.pulse()["today"]["handover"]
        self.assertEqual((h["to_merge"], h["to_approval"], h["approval_to_merge"]), ({"p50_s": None, "p90_s": None, "n": 0},) * 3)

    def test_the_backfill_is_asked_once_for_a_pull_request_the_queue_did_not_merge_and_then_cached(self):
        self.task("p1", 1, "2026-11-15T08:10:00Z", "2026-11-15T08:00:00Z")
        self.gh_says(reply([{"state": "APPROVED", "submitted_at": "2026-11-15T08:05:00Z"}]))
        h = self.pulse()["today"]["handover"]
        self.assertEqual((self.gh_calls(), h["approvals_backfilled"], h["to_approval"]),
                         (["repos/{owner}/{repo}/pulls/1/reviews?per_page=100"], 1, {"p50_s": 300, "p90_s": 300, "n": 1}))
        self.assertEqual(pulse.read_approvals(self.factory, ALPHA), {"1": "2026-11-15T08:05:00Z"})
        self.run.reset_mock()
        self.assertEqual(self.pulse()["today"]["handover"]["to_approval"], {"p50_s": 300, "p90_s": 300, "n": 1})
        self.assertEqual(self.gh_calls(), [])

    def test_a_failed_lookup_is_not_cached_and_is_tried_again(self):
        self.task("p1", 1, "2026-11-15T08:10:00Z", "2026-11-15T08:00:00Z")
        h = self.pulse()["today"]["handover"]
        self.assertEqual((h["to_approval"], h["to_merge"], pulse.read_approvals(self.factory, ALPHA)), ({"p50_s": None, "p90_s": None, "n": 0}, {"p50_s": 600, "p90_s": 600, "n": 1}, {}))
        self.run.reset_mock()
        self.pulse()
        self.assertEqual(self.gh_calls(), ["repos/{owner}/{repo}/pulls/1/reviews?per_page=100"])

    def test_cached_runs_nothing_and_a_missing_approval_is_unknown(self):
        self.task("p1", 1, "2026-11-15T08:10:00Z", "2026-11-15T08:00:00Z")
        h = self.pulse(cached=True)["today"]["handover"]
        self.assertEqual((h["to_approval"]["n"], h["to_merge"]["n"], self.run.called), (0, 1, False))


class BackfillTests(unittest.TestCase):
    MERGED = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)

    def ask(self, reviews, comments):
        answers = {"reviews": reviews, "comments": comments}
        run = lambda argv, **kw: reply(answers["reviews" if "/reviews" in argv[2] else "comments"])  # noqa: E731
        with tempfile.TemporaryDirectory() as d, mock.patch.object(subprocess, "run", side_effect=run):
            return pulse.backfill("/r/x", d, 5, self.MERGED)

    def test_the_newest_approving_review_at_or_before_the_merge(self):
        review = lambda state, at: {"state": state, "submitted_at": at}  # noqa: E731
        self.assertEqual(self.ask([review("APPROVED", "2026-11-15T08:00:00Z"), review("APPROVED", "2026-11-15T08:30:00Z"),
                                   review("COMMENTED", "2026-11-15T08:40:00Z"), review("APPROVED", "2026-11-15T09:30:00Z")], []),
                         "2026-11-15T08:30:00Z")

    def test_without_a_review_the_newest_lgtm_of_a_person(self):
        said = lambda body, at, kind="User": {"body": body, "created_at": at, "user": {"type": kind}}  # noqa: E731
        comments = [said(" LGTM\n", "2026-11-15T08:00:00Z"), said("lgtm", "2026-11-15T08:10:00Z", "Bot"), said("lgtm, but", "2026-11-15T08:20:00Z"),
                    said("lgtm", "2026-11-15T09:10:00Z"), {"body": "lgtm", "created_at": "2026-11-15T08:25:00Z"}]
        self.assertEqual(self.ask([], comments), "2026-11-15T08:00:00Z")
        self.assertIsNone(self.ask([], comments[1:]))

    def test_a_failed_call_raises_and_is_logged(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(subprocess, "run", return_value=reply(rc=1, err="HTTP 502")):
            with self.assertRaises(kaizen.GhError):
                pulse.backfill("/r/x", d, 5, self.MERGED)
            with open([os.path.join(d, f) for f in os.listdir(d)][0]) as f:
                self.assertEqual(json.loads(f.readline())["error"], "HTTP 502")


class SentBackTests(PulseStore):
    def test_send_backs_that_changed_nothing_are_those_whose_next_record_has_the_same_head(self):
        self.queue("updated", 9, "2026-11-14T23:00:00Z")  # the log starts on today, local
        self.queue("sent-back", 1, "2026-11-15T08:00:00Z", head="a")
        self.queue("sent-back", 2, "2026-11-15T08:10:00Z", head="b")
        self.queue("sent-back", 3, "2026-11-15T08:20:00Z", head="c")
        self.queue("merged", 3, "2026-11-15T08:25:00Z", head="c", repo="/r/elsewhere")  # another repository's pull request 3
        self.queue("updated", 1, "2026-11-15T09:00:00Z", head="a")  # same head: it changed nothing
        self.queue("updated", 2, "2026-11-15T09:10:00Z", head="x")  # a different head
        p = self.pulse()
        self.assertEqual((p["queue_since"], p["today"]["handover"]["sent_back"], p["today"]["handover"]["sent_back_unchanged"]),
                         ("2026-11-14T23:00:00Z", 3, 1))  # pull request 3: nothing came after

    def test_before_the_log_it_is_unknown_not_zero(self):
        self.queue("updated", 9, "2026-11-14T23:00:00Z")
        yesterday = self.pulse()["baseline"]["handover"]  # that day ended at 2026-11-14T22:00Z
        self.assertEqual((yesterday["sent_back"], yesterday["sent_back_unchanged"]), (None, None))
        self.queued = [dict(self.queued[0], ts="2026-11-14T10:00:00Z")]  # the log starts during yesterday
        again = self.pulse()
        self.assertEqual((again["baseline"]["handover"]["sent_back"], again["today"]["handover"]["sent_back"]), (0, 0))

    def test_without_a_queue_log_everything_about_it_is_unknown(self):
        p = self.pulse()
        self.assertEqual((p["queue_since"], p["today"]["handover"]["sent_back"], p["baseline"]["handover"]["sent_back_unchanged"]), (None, None, None))


class ConflictFigureTests(PulseStore):
    def base(self, conflicted, *files):
        return {"sha": "0" * 40, "conflicted": conflicted, "files": list(files)}

    def test_pulled_from_the_cache_and_a_pull_request_git_cannot_tell_shows_its_error(self):
        for slug, n in (("c1", 1), ("c2", 2), ("c3", 3)):
            self.task(slug, n, "2026-11-15T08:00:00Z")
        self.seed("merges", ALPHA, {"m1": {"pr": 1, "base_merges": [self.base(True, "a.go"), self.base(False)]}, "m2": {"pr": 2, "base_merges": []}})
        c = self.pulse()["today"]["conflicts"]
        self.assertEqual({k: v for k, v in c.items() if k != "error"}, {"prs": 2, "base_merges": 2, "conflicted": 1, "per_merged": 0.5})
        self.assertIn("no such binary", c["error"])
        self.assertEqual(sorted(pulse.read_merges(self.factory, ALPHA)), ["m1", "m2"])  # the failure is not cached

    def test_cached_runs_nothing_and_a_pull_request_not_in_the_cache_is_not_in_prs(self):
        self.task("c1", 1, "2026-11-15T08:00:00Z")
        self.task("c3", 3, "2026-11-15T08:00:00Z")
        self.seed("merges", ALPHA, {"m1": {"pr": 1, "base_merges": [self.base(True, "a.go")]}})
        c = self.pulse(cached=True)["today"]["conflicts"]
        self.assertEqual((c, self.run.called), ({"prs": 1, "base_merges": 1, "conflicted": 1, "per_merged": 1.0, "error": None}, False))
        none = self.pulse(cached=True)["baseline"]["conflicts"]
        self.assertEqual(none, {"prs": 0, "base_merges": 0, "conflicted": 0, "per_merged": None, "error": None})


class TextTests(PulseStore):
    def test_a_heading_and_a_line_per_figure_with_today_and_the_baseline_and_a_dash_for_unknown(self):
        self.task("p1", 1, "2026-11-15T08:10:00Z", "2026-11-15T08:00:00Z")
        self.task("p2", 2, "2026-11-15T09:10:00Z", "2026-11-15T09:00:00Z")
        session(self.projects, ALPHA, "p1", "2026-11-15T08:00:05.000Z", 2000000, [("reviewer", "claude-opus-5-5", 1, "10:20", "10:25")])
        self.seed("merges", ALPHA, {"m1": {"pr": 1, "base_merges": [self.base(True)]}, "m2": {"pr": 2, "base_merges": []}})
        self.assertEqual(cli._pulse_lines(self.pulse(cached=True), "USD"), [
            "pulse: today 2026-11-15 against yesterday (2026-11-14)",
            "all-in cost per merged PR: today $10.00 (n=2), baseline - (n=0): -",
            "first pass: today 100% (n=1), baseline - (n=0): -",
            "approval to merge median: today - (n=0), baseline - (n=0): -",
            "conflicted base merges per merged PR: today 0.50 (n=2), baseline - (n=0): -"])

    def test_the_verdict_word_and_the_baseline_days_of_a_range(self):
        self.task("p1", 1, "2026-11-15T08:10:00Z", "2026-11-15T08:00:00Z")
        self.task("b1", 2, "2026-11-10T08:10:00Z", "2026-11-10T08:00:00Z")
        self.queue("merged", 1, "2026-11-15T08:10:00Z", approved_at="2026-11-15T08:04:00Z")
        self.queue("merged", 2, "2026-11-10T08:10:00Z", approved_at="2026-11-10T08:01:00Z")
        lines = cli._pulse_lines(self.pulse(cached=True, cutoff=NOW - timedelta(days=7)), "USD")
        self.assertEqual((lines[0], lines[3]), ("pulse: today 2026-11-15 against the 7 days before (2026-11-08 to 2026-11-14)",
                                                "approval to merge median: today 6m00s (n=1), baseline 9m00s (n=1): few"))

    def base(self, conflicted):
        return {"sha": "0" * 40, "conflicted": conflicted, "files": []}


GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


class GitTests(PulseStore):
    """A scratch repository: a branch that merges the base three times (a conflict resolved by hand, a clean merge, a clean
    merge with an extra edit) and a side branch, all merged back as pull request 7."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        self.edit("f.txt", "base\n"), self.commit("base")
        self.git("checkout", "-q", "-b", "feature")
        self.edit("f.txt", "feature\n"), self.commit("feature work")
        self.git("checkout", "-q", "main")
        self.edit("f.txt", "main\n"), self.commit("main edits f")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "main", check=False)  # conflicts in f.txt
        self.edit("f.txt", "resolved\n"), self.git("add", "f.txt"), self.git("commit", "-q", "--no-edit")
        self.git("checkout", "-q", "main")
        self.edit("g.txt", "g\n"), self.commit("main edits g")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "main", "-m", "clean base merge")
        self.git("checkout", "-q", "main")
        self.edit("h.txt", "h\n"), self.commit("main edits h")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-commit", "--no-ff", "main")
        self.edit("extra.txt", "by hand\n"), self.git("add", "extra.txt"), self.git("commit", "-q", "-m", "clean merge, one more edit")
        self.git("checkout", "-q", "-b", "side")
        self.edit("side.txt", "s\n"), self.commit("side work")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-ff", "side", "-m", "a worktree merged back")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #7 from x/feature")
        self.git("update-ref", "refs/remotes/origin/HEAD", "main")
        self.task("t7", 7, "2026-11-15T08:00:00Z", repo=self.repo)
        self.run.side_effect = self.run_real

    def run_real(self, argv, **kw):
        return REAL_RUN(argv, **kw)

    def git(self, *args, check=True):
        REAL_RUN(["git", "-C", self.repo, *args], capture_output=True, text=True, env=GIT_ENV, check=check)

    def edit(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def commit(self, message):
        self.git("add", "-A"), self.git("commit", "-q", "-m", message)

    def test_conflicted_means_a_conflict_in_the_remerge_not_any_edit_and_other_merges_are_not_base_merges(self):
        c = self.pulse()["today"]["conflicts"]
        self.assertEqual(c, {"prs": 1, "base_merges": 3, "conflicted": 1, "per_merged": 1.0, "error": None})
        (entry,) = pulse.read_merges(self.factory, self.repo).values()
        self.assertEqual((entry["pr"], [(b["conflicted"], b["files"]) for b in entry["base_merges"]]),
                         (7, [(False, []), (False, []), (True, ["f.txt"])]))
        self.assertTrue(all(len(b["sha"]) == 40 for b in entry["base_merges"]))

    def test_the_second_build_runs_no_git_and_a_cached_build_reads_the_cache(self):
        first = self.pulse()["today"]["conflicts"]
        self.run.reset_mock()
        self.assertEqual(self.pulse()["today"]["conflicts"], first)
        self.assertEqual(self.pulse(cached=True)["today"]["conflicts"], first)
        self.assertFalse(self.run.called)

    def test_cached_on_an_empty_cache_runs_no_git(self):
        self.assertEqual(self.pulse(cached=True)["today"]["conflicts"]["prs"], 0)
        self.assertFalse(self.run.called)

    def test_a_failure_shows_and_is_not_cached(self):
        self.run.side_effect = subprocess.TimeoutExpired("git", 10)
        c = self.pulse()["today"]["conflicts"]
        self.assertEqual((c["prs"], c["error"].startswith("git unavailable: Command 'git' timed out")), (0, True))
        self.assertEqual(pulse.read_merges(self.factory, self.repo), {})
        self.run.side_effect = self.run_real
        self.assertEqual(self.pulse()["today"]["conflicts"]["prs"], 1)
        self.assertEqual({c.kwargs["timeout"] for c in self.run.call_args_list}, {pulse.GIT_TIMEOUT})

    def test_a_color_setting_does_not_hide_the_conflict(self):
        for key, value in (("color.ui", "always"), ("color.diff", "always"), ("diff.noprefix", "true")):
            self.git("config", key, value)
        self.assertEqual(self.pulse()["today"]["conflicts"]["conflicted"], 1)
        (entry,) = pulse.read_merges(self.factory, self.repo).values()
        self.assertEqual([b["files"] for b in entry["base_merges"]], [[], [], ["f.txt"]])

    def test_a_pull_request_merged_another_way_is_not_in_prs(self):
        self.task("t8", 8, "2026-11-15T09:00:00Z", repo=self.repo)  # no `Merge pull request #8` commit
        c = self.pulse()["today"]["conflicts"]
        self.assertEqual((c["prs"], c["error"]), (1, None))


REAL_RUN = subprocess.run


if __name__ == "__main__":
    unittest.main()
