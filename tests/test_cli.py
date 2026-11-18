import argparse
import contextlib
import gc
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.support import AGENTS, PRICES_PATH, PROJECTS, ROOT, user_prices
from isshoni import cli, metrics, prices as price_file

BASE = ["--projects-dir", PROJECTS, "--agents-dir", AGENTS, "--prices", PRICES_PATH]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class PriceCommandTests(unittest.TestCase):
    RATES = ["--input", "1", "--output", "2.5", "--cache-write-5m", "3", "--cache-write-1h", "4", "--cache-read", "0.5"]

    def test_rates_then_the_cost_of_a_custom_id(self):
        with user_prices() as path:
            rc, out, err = run("price", "vendor/model-x", *self.RATES)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(out.count("\n"), 1)
            for want in (path, "vendor/model-x", "output 2.5", "cache_read 0.5", "USD"):
                self.assertIn(want, out)
            self.assertAlmostEqual(price_file.load().cost("vendor/model-x", {"input_tokens": 1000000, "output_tokens": 1000000}), 3.5)

    def test_like_names_what_it_follows(self):
        with user_prices() as path:
            rc, out, _ = run("price", "vendor/model-x", "--like", "claude-opus-5-5")
            self.assertEqual(rc, 0)
            self.assertIn("(like claude-opus-5-5)", out)
            self.assertIn("input 4,", out)
            self.assertEqual(price_file.load().user, path)

    def test_refusals_exit_2_and_write_nothing(self):
        for argv in (["m"], ["m", "--input", "1"], ["m", "--like", "claude-opus-5-5", "--input", "1"], ["m", "--like", "nothing"],
                     ["m", *self.RATES[:-1], "-1"], ["m", *self.RATES[:-1], "nan"], ["bad\nid", "--like", "claude-opus-5-5"]):
            with self.subTest(argv=argv), user_prices() as path:
                rc, out, err = run("price", *argv)
                self.assertEqual((rc, out), (2, ""))
                self.assertNotEqual(err, "")
                self.assertFalse(os.path.exists(path))

    def test_a_second_command_keeps_the_first_entry(self):
        with user_prices():
            run("price", "a", "--like", "claude-opus-5-5")
            run("price", "b", "--like", "a")
            self.assertEqual(price_file.load().models["b"], price_file.load().models["claude-opus-5-5"])


class CliTests(unittest.TestCase):
    def test_sessions_json(self):
        rc, out, _ = run("sessions", "--json", *BASE)
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(len(d["sessions"]), 1)
        s = d["sessions"][0]
        self.assertEqual((s["dispatch_count"], s["repeats"], s["review_loops"], s["warn_count"], s["malformed"]), (18, 1, 3, 1, 2))
        self.assertEqual(d["malformed"], 2)

    def test_session_json_full_data(self):
        rc, out, _ = run("session", "fixture", "--json", *BASE)
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["by_agent"]["main"]["total"], 300)
        self.assertEqual(set(d["by_model"]["claude-haiku-5-5"]), {"input", "output", "cache_write", "cache_read", "total", "cost", "unpriced"})
        self.assertEqual(len(d["dispatches"]), 18)
        e2 = next(x for x in d["dispatches"] if x["description"] == "Find config loader again")
        self.assertEqual(e2["flags"], ["repeat"])
        self.assertEqual(e2["repeat_of"], "toolu_E1")
        self.assertEqual(e2["status"], "running")
        self.assertEqual(len(d["warnings"]), 1)
        self.assertEqual(d["malformed"], 2)
        self.assertEqual(d["review_loops"], len(d["review_loop_events"]))
        self.assertEqual([e["via"] for e in d["review_loop_events"]], ["dispatch", "dispatch", "resume"])
        c1 = next(x for x in d["dispatches"] if x["description"] == "Apply fixes")
        self.assertEqual((c1["review_loops_dispatch"], c1["review_loops_resume"]), (1, 1))
        self.assertEqual(c1["flags"], ["review-loop", "review-loop(resume x1)"])

    def test_agents_json(self):
        rc, out, _ = run("agents", "--json", *BASE)
        self.assertEqual(rc, 0)
        d = json.loads(out)
        names = [a["agent"] for a in d["agents"]]
        self.assertEqual(names[0], "main")
        ex = next(a for a in d["agents"] if a["agent"] == "explorer")
        self.assertEqual(ex["dispatches"], 10)
        self.assertEqual(ex["usage"]["output"], 39)
        co = next(a for a in d["agents"] if a["agent"] == "coder")
        self.assertEqual(co["review_loops"], 3)  # 2 dispatch-triggered + 1 resume-triggered
        self.assertEqual(ex["warn_count"], 1)
        self.assertIn("claude-haiku-5-5", d["by_model"])

    def test_tables(self):
        rc, out, _ = run("session", "fixture", *BASE)
        self.assertEqual(rc, 0)
        self.assertIn("repeat,running", out)
        self.assertIn("WARN agent explorer expected model haiku", out)
        rc, out, _ = run("sessions", *BASE)
        self.assertEqual(rc, 0)
        self.assertIn("fixture-", out)
        rc, out, _ = run("agents", *BASE)
        self.assertEqual(rc, 0)
        self.assertIn("general-purpose", out)

    def test_cost_text_and_footer(self):
        for argv in (("sessions",), ("session", "fixture"), ("agents",)):
            rc, out, _ = run(*argv, *BASE)
            self.assertEqual(rc, 0)
            self.assertIn("prices: USD as of 2026-01-01 (%s)" % PRICES_PATH, out)
            self.assertIn("* no price for: claude-sonnet-5-5", out)
        rc, out, _ = run("sessions", *BASE)
        self.assertIn("cost", out.splitlines()[0])
        self.assertIn("$0.0054*", out)
        rc, out, _ = run("session", "fixture", *BASE)
        self.assertIn("cost      total $0.0054*, main $0.0043 (79.4%), subagents $0.0011* (20.6%)", out)
        self.assertIn("unpriced", out)  # By model shows the unpriced model as such
        rc, out, _ = run("agents", *BASE)
        self.assertIn("cost p50", out.splitlines()[0])
        coder = next(ln.split() for ln in out.splitlines() if ln.startswith("coder"))
        self.assertEqual(coder[3:6], ["$0*", "$0*", "$0*"])  # sonnet only: no price, marked everywhere

    def test_no_unpriced_line_when_all_priced(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "all.toml")
            with open(PRICES_PATH) as f, open(path, "w") as g:
                g.write(f.read() + "[models.claude-sonnet-5-5]\ninput = 1\noutput = 1\ncache_write_5m = 1\n"
                        "cache_write_1h = 1\ncache_read = 1\n")
            for argv in (("sessions",), ("session", "fixture"), ("agents",)):
                rc, out, _ = run(*argv, "--projects-dir", PROJECTS, "--agents-dir", AGENTS, "--prices", path)
                self.assertEqual(rc, 0)
                self.assertIn("prices: USD", out)
                self.assertNotIn("no price for", out)
                self.assertNotIn("*", out)

    def test_cost_json_keys(self):
        want = {"currency": "USD", "as_of": "2026-01-01", "source": "test", "path": PRICES_PATH}
        rc, out, _ = run("sessions", "--json", *BASE)
        d = json.loads(out)
        self.assertEqual(d["prices"], want)
        self.assertEqual(set(d["sessions"][0]["cost"]), {"total", "main", "subagents", "main_pct", "subagent_pct",
                                                         "unpriced_tokens", "unpriced_models"})
        self.assertEqual(set(d["repos"][0]["cost"]), set(d["sessions"][0]["cost"]))
        self.assertEqual(d["sessions"][0]["cost"]["unpriced_models"], ["claude-sonnet-5-5"])
        rc, out, _ = run("session", "fixture", "--json", *BASE)
        d = json.loads(out)
        self.assertEqual(d["prices"], want)
        self.assertEqual(d["dispatches"][0]["usage"]["unpriced"], 0)
        rc, out, _ = run("agents", "--json", *BASE)
        d = json.loads(out)
        self.assertEqual(d["prices"], want)
        self.assertEqual(d["unpriced_models"], ["claude-sonnet-5-5"])
        self.assertIn("cost_p90", d["agents"][0])

    def test_default_prices_file_and_bad_file(self):
        rc, out, _ = run("sessions", "--json", "--projects-dir", PROJECTS, "--agents-dir", AGENTS)
        self.assertEqual(json.loads(out)["prices"]["path"], os.path.join(ROOT, "isshoni", "prices.toml"))
        rc, _, err = run("sessions", "--projects-dir", PROJECTS, "--prices", "/nonexistent.toml")
        self.assertEqual(rc, 2)
        self.assertIn("/nonexistent.toml", err)

    def test_no_user_file_gives_todays_output(self):
        with user_prices():
            rc, out, _ = run("sessions", "--projects-dir", PROJECTS, "--agents-dir", AGENTS)
            _, js, _ = run("sessions", "--json", "--projects-dir", PROJECTS, "--agents-dir", AGENTS)
        self.assertIn("prices: USD as of 2026-11-14 (%s)\n" % price_file.DEFAULT_PATH, out)
        self.assertNotIn("user_path", json.loads(js)["prices"])

    def test_user_file_prices_a_model_and_is_named(self):
        with user_prices("[models.claude-sonnet-5-5]\nlike = 'claude-opus-5-5'\n") as path:
            _, flagged, _ = run("sessions", "--json", *BASE)
            _, js, _ = run("sessions", "--json", "--projects-dir", PROJECTS, "--agents-dir", AGENTS)
            _, out, _ = run("sessions", "--projects-dir", PROJECTS, "--agents-dir", AGENTS)
        d = json.loads(js)
        self.assertEqual(d["prices"]["user_path"], path)
        self.assertEqual(d["sessions"][0]["cost"]["unpriced_tokens"], 0)  # the fixtures' sonnet is priced now
        self.assertIn("(%s + %s)" % (price_file.DEFAULT_PATH, path), out)
        self.assertNotIn("user_path", json.loads(flagged)["prices"])  # --prices reads its file alone
        self.assertGreater(json.loads(flagged)["sessions"][0]["cost"]["unpriced_tokens"], 0)

    def test_invalid_user_file_exits_2_naming_it(self):
        with user_prices("currency = 'EUR'\n") as path:
            rc, _, err = run("sessions", "--projects-dir", PROJECTS)
        self.assertEqual(rc, 2)
        self.assertIn(path, err)

    def test_money_format(self):
        usd = lambda x, u=0: cli.money(x, "USD", u)  # noqa: E731
        self.assertEqual([usd(x) for x in (0, 0.0012, 0.00001234, 0.00005, 0.5, 12.345, 1234.5, 12345, 2500000)],
                         ["$0", "$0.0012", "<$0.0001", "$0.0001", "$0.50", "$12.35", "$1234", "$12.3k", "$2.5M"])
        self.assertEqual([usd(x) for x in (0.00994, 0.00995, 99.996, 9999.6, 999960)], ["$0.0099", "$0.01", "$100", "$10.0k", "$1.0M"])
        self.assertEqual(usd(3, 5), "$3.00*")
        self.assertEqual(cli.money(3, "EUR"), "EUR 3.00")
        self.assertEqual(cli.money(0.00001, "EUR", 5), "<EUR 0.0001*")

    def test_usage_errors_exit_2(self):
        for argv in (("session", "nope", *BASE), ("sessions", "--since", "garbage", *BASE),
                     ("sessions", "--projects-dir", "/nonexistent-dir-xyz"), ("bogus",), ()):
            rc, _, _ = run(*argv)
            self.assertEqual(rc, 2, argv)

    def test_since_filters_old_sessions(self):
        rc, out, _ = run("sessions", "--json", "--since", "1h", *BASE)
        self.assertEqual(json.loads(out)["sessions"], [])
        rc, out, _ = run("sessions", "--json", "--since", "2020-01-01", *BASE)
        self.assertEqual(len(json.loads(out)["sessions"]), 1)

    def test_parse_since(self):
        now = datetime(2026, 1, 8, tzinfo=timezone.utc)
        self.assertEqual(cli.parse_since("7d", now), now - timedelta(days=7))
        self.assertEqual(cli.parse_since("2h", now), now - timedelta(hours=2))
        self.assertEqual(cli.parse_since("1w", now), now - timedelta(weeks=1))
        self.assertEqual(cli.parse_since("2026-01-02", now), datetime(2026, 1, 2, tzinfo=timezone.utc))

    def test_formatting(self):
        self.assertEqual(cli.human(1687683), "1.7M")
        self.assertEqual(cli.human(44082), "44.1k")
        self.assertEqual(cli.human(86), "86")
        self.assertEqual(cli.fmt_duration(269000), "4m29s")
        self.assertEqual(cli.fmt_duration(45000), "45s")

    def test_shim_json_is_clean(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "isshoni"), "session", "fixture", "--json", *BASE],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        json.loads(p.stdout)


class LoadAllGcTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(gc.enable if gc.isenabled() else gc.disable)
        self.args = argparse.Namespace(projects_dir=PROJECTS, agents_dir=AGENTS, repeat_threshold=0.5)
        self.prices = price_file.load(PRICES_PATH)

    def test_collector_off_during_load(self):
        seen = []
        real = metrics.analyze

        def analyze(*a):
            seen.append(gc.isenabled())
            return real(*a)

        gc.enable()
        with mock.patch.object(metrics, "analyze", analyze):
            self.assertEqual(len(cli.load_all(self.args, self.prices)), 1)
        self.assertEqual(seen, [False])

    def test_collector_back_on_after_return(self):
        gc.enable()
        cli.load_all(self.args, self.prices)
        self.assertTrue(gc.isenabled())

    def test_collector_back_on_after_exception(self):
        gc.enable()
        with mock.patch.object(metrics, "analyze", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cli.load_all(self.args, self.prices)
        self.assertTrue(gc.isenabled())

    def test_collector_stays_off_when_it_was_off(self):
        gc.disable()
        cli.load_all(self.args, self.prices)
        self.assertFalse(gc.isenabled())


if __name__ == "__main__":
    unittest.main()
