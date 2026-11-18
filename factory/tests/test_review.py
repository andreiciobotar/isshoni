import contextlib
import io
import json
import os
import re
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest import mock

from test_factory import RealReviewStarted, factory, put_access, stub_popen

TARGET = "me/widgets"
ISSUE_URL = f"https://github.com/{TARGET}/issues/41"
REFUSED = "GraphQL: Resource not accessible by integration (createIssue)"
BODY = {h: f"{h} text" for h in ("Problem", "What the role lacked", "Proposal", "Evidence", "Number")}


def ago(hours):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))


def proposal_file(kind="proposal", issue=None, title="Reviewer gets the fact list", rung="information", role="reviewer",
                  cites="[3fa9c1d2e4b6, 77aa01b2c3d4]", sections=None):
    names = {"proposal": list(BODY), "evidence": ["Evidence"], "check": ["Number"]}[kind] if sections is None else sections
    head = [f"kind: {kind}", f"issue: {issue}" if issue else None, f"title: {title}   # proposal only" if kind == "proposal" else None,
            f"rung: {rung}" if kind == "proposal" else None, f"role: {role}", f"cites: {cites}"]
    return "---\n" + "\n".join(h for h in head if h) + "\n---\n" + "".join(f"## {n}\n{BODY[n]}\n" for n in names)


class Store(unittest.TestCase):
    """A scratch home with a store and a repository root; the clock is the real one, records are placed relative to it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.root = str(self.tmp / "repo")
        os.makedirs(self.root)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = self.tmp / ".claude" / "factory"

    def put(self, name, hours_ago, **fields):
        factory.log(name, {"ts": ago(hours_ago), **fields})

    def settings_file(self, text):
        self.store.mkdir(parents=True, exist_ok=True)
        (self.store / "settings.toml").write_text(text)

    def reviews(self):
        return list(factory.read_log("reviews"))


class DueTests(Store):
    def decide(self):
        return factory.review_decide(factory.load_settings())[0]

    def test_with_no_record_a_review_is_due(self):
        self.put("tasks", 1, slug="a")
        self.assertTrue(self.decide())

    def test_a_record_of_any_outcome_younger_than_the_period_is_not_due(self):
        self.put("tasks", 0.1, slug="a")
        for outcome in ("ran", "skipped", "failed"):
            with self.subTest(outcome=outcome):
                self.put("reviews", 0.5, outcome=outcome)
                self.assertFalse(self.decide())

    def test_an_older_record_is_due(self):
        self.put("reviews", 1.5, outcome="failed")
        self.put("tasks", 0.1, slug="a")
        self.assertTrue(self.decide())

    def test_zero_hours_turns_it_off(self):
        self.settings_file("[kaizen]\nreview_hours = 0\n")
        self.put("tasks", 0.1, slug="a")
        self.assertFalse(self.decide())
        self.assertEqual(self.reviews(), [])

    def test_the_period_is_the_setting(self):
        self.settings_file("[kaizen]\nreview_hours = 3\n")
        self.put("reviews", 2, outcome="ran")
        self.put("tasks", 0.1, slug="a")
        self.assertFalse(self.decide())


class ActivityTests(Store):
    def test_a_record_after_the_last_run_that_counts(self):
        for name in ("tasks", "feedback", "queue"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
                factory.log("reviews", {"ts": ago(3), "outcome": "ran"})
                factory.log(name, {"ts": ago(1)})
                self.assertTrue(factory.review_decide(factory.load_settings())[0])

    def test_a_record_before_the_last_run_does_not(self):
        self.put("tasks", 5, slug="a")
        self.put("reviews", 3, outcome="ran")
        self.assertFalse(factory.review_decide(factory.load_settings())[0])
        self.assertEqual([r["outcome"] for r in self.reviews()], ["ran", "skipped"])

    def test_only_ops_firings_gh_and_reviews_records_is_one_skip_per_period(self):
        self.put("reviews", 3, outcome="ran")
        for name in ("ops", "firings", "gh"):
            self.put(name, 1)
        self.put("reviews", 2, outcome="failed")  # a failed run does not move `since`
        go, line = factory.review_decide(factory.load_settings())
        self.assertFalse(go)
        self.assertRegex(line, r"^skipped: no activity since ")
        [*_, skipped] = self.reviews()
        self.assertEqual(skipped["outcome"], "skipped")
        self.assertTrue(skipped["reason"].startswith("no activity since "))
        self.assertEqual(len(self.reviews()), 3)
        self.assertFalse(factory.review_decide(factory.load_settings())[0])
        self.assertEqual(len(self.reviews()), 3)

    def test_with_no_run_the_window_is_the_last_24_hours(self):
        self.put("tasks", 30, slug="old")
        self.assertFalse(factory.review_decide(factory.load_settings())[0])
        self.assertEqual(len(self.reviews()), 1)

    def test_with_no_run_a_record_of_the_last_24_hours_counts(self):
        self.put("tasks", 20, slug="new")
        self.assertTrue(factory.review_decide(factory.load_settings())[0])


class TickTests(Store):
    def tick(self):
        with mock.patch.object(factory, "spawn") as spawn, mock.patch.object(factory, "run", side_effect=AssertionError("waited")):
            factory.review_tick(SimpleNamespace(root=self.root))
        return spawn

    def test_a_due_review_is_started_once_without_waiting(self):
        self.put("tasks", 1, slug="a")
        spawn = self.tick()
        spawn.assert_called_once_with([mock.ANY, str(factory.FACTORY), "review", "--run"], self.root,
                                      self.store / "review.log")

    def test_nothing_starts_while_the_lock_is_held(self):
        self.put("tasks", 1, slug="a")
        with factory.file_lock(self.store / "review.lock") as free:
            self.assertTrue(free)
            self.tick().assert_not_called()

    def test_nothing_starts_when_it_is_not_due_or_idle(self):
        self.tick().assert_not_called()
        self.assertEqual([r["outcome"] for r in self.reviews()], ["skipped"])
        self.tick().assert_not_called()
        self.assertEqual(len(self.reviews()), 1)

    def test_a_test_that_reaches_the_real_spawn_of_a_review_fails(self):
        self.put("tasks", 1, slug="a")
        with self.assertRaises(RealReviewStarted):
            factory.review_tick(SimpleNamespace(root=self.root))

    def test_a_failing_tick_prints_and_goes_on(self):
        self.put("tasks", 1, slug="a")
        out = io.StringIO()
        with mock.patch.object(factory, "spawn", side_effect=OSError("no fork")), redirect_stdout(out):
            factory.review_tick(SimpleNamespace(root=self.root))
        self.assertIn("review: OSError: no fork", out.getvalue())

    def test_the_watch_runs_it_beside_the_ops_tick(self):
        order = []
        with mock.patch.object(factory, "Repo"), mock.patch.object(factory, "ops_tick", lambda r: order.append("ops")), \
                mock.patch.object(factory, "review_tick", lambda r: order.append("review")), \
                mock.patch.object(factory, "repo_lock", lambda r, n: contextlib.nullcontext(False)), redirect_stdout(io.StringIO()):
            factory.cmd_watch(SimpleNamespace())
        self.assertEqual(order, ["ops", "review"])


class Session(Store):
    """`review_run` against a stubbed `claude` and `gh`: the session writes `files` under its folder."""

    def setUp(self):
        super().setUp()
        self.calls, self.envs = [], []
        self.files = {"report.md": "# report\n"}
        self.claude = {"subtype": "success", "is_error": False, "session_id": "sid-r", "total_cost_usd": 0.42}
        self.claude_rc = 0
        self.gh = {}
        self.identity = {"bot_login": "bot", "owner": "me"}
        self.tokens = []
        self.issues = [{"number": 3, "title": "t", "body": "", "state": "OPEN", "labels": [{"name": "kaizen"}], "comments": []}]

    def fake(self, argv, cwd=None, timeout=None, input=None, env=None, group=False):
        self.calls.append(list(argv))
        self.envs.append(env)
        if argv[:3] == ["claude", "plugin", "list"]:
            return 0, "[]", ""
        if argv[0] == "claude":
            folder = Path(re.search(r"Your folder: (\S+)\. Previous", argv[-1])[1])
            for name, text in self.files.items():
                (folder / name).parent.mkdir(parents=True, exist_ok=True)
                (folder / name).write_text(text)
            self.timeout = timeout
            return (self.claude_rc, json.dumps(self.claude) if self.claude is not None else "", "") if self.claude_rc != 124 \
                else (124, "", "timed out")
        key = tuple(argv[1:3]) if argv[1] != "issue" or argv[2] != "comment" else ("issue", "comment")
        answer = self.gh.get(key)
        if answer is None:
            answer = {("issue", "list"): (0, json.dumps(self.issues), ""), ("label", "list"): (0, "[]", ""),
                      ("label", "create"): (0, "", ""), ("issue", "create"): (0, ISSUE_URL + "\n", ""),
                      ("issue", "comment"): (0, "https://x/y#c\n", "")}[key]
        return answer[0], answer[1], answer[2]

    def run_review(self, target=""):
        if target:
            self.settings_file(f'[kaizen]\nissues_repo = "{target}"\n')
        settings = factory.load_settings()
        self.token = mock.Mock(side_effect=lambda identity, name: self.tokens.append(name) or "TOK")
        with mock.patch.object(factory, "run", self.fake), mock.patch.object(factory, "load_identity", lambda: self.identity), \
                mock.patch.object(factory, "app_token", self.token), redirect_stdout(io.StringIO()) as out:
            code = factory.review_run(SimpleNamespace(root=self.root), settings)
        [started, record] = self.reviews()
        self.assertEqual((started["outcome"], started["ts"], started["folder"]), ("started", record["ts"], record["folder"]))
        return code, record, out.getvalue()

    def gh_calls(self):
        return [c for c in self.calls if c[0] == "gh"]


class RunTests(Session):
    def test_a_review_that_ran_is_recorded_with_its_cost(self):
        code, r, out = self.run_review()
        self.assertEqual(code, 0)
        self.assertEqual((r["outcome"], r["cost_usd"], r["session"], r["posted"], r["held"]), ("ran", 0.42, "sid-r", [], []))
        self.assertRegex(r["folder"], r"/deliverables/kaizen-review-\d{8}T\d{6}Z$")
        self.assertIsInstance(r["seconds"], float)
        self.assertRegex(r["since"], r"^\d{4}-")
        self.assertNotIn("reason", r)
        folder = Path(r["folder"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["evidence", "proposals", "report.md", "scripts"])
        self.assertIn("0.42 USD", out)
        self.assertEqual(self.timeout, factory.REVIEW_TIMEOUT)

    def test_no_cost_is_null_not_zero(self):
        del self.claude["total_cost_usd"]
        _, r, out = self.run_review()
        self.assertIsNone(r["cost_usd"])
        self.assertIn("unpriced", out)

    def test_a_timeout_fails_with_the_reason_and_posts_nothing(self):
        self.claude_rc = 124
        self.files["proposals/p.md"] = proposal_file()
        code, r, _ = self.run_review(TARGET)
        self.assertEqual((code, r["outcome"], r["reason"], r["posted"], r["held"], r["cost_usd"]), (1, "failed", "reached 20 minutes", [], [], None))
        self.assertEqual([c[1:3] for c in self.gh_calls()], [["issue", "list"]])

    def test_the_budget_fails_with_the_reason(self):
        self.claude_rc, self.claude = 1, {"subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 3.01}
        self.files["proposals/p.md"] = proposal_file()
        _, r, _ = self.run_review(TARGET)
        self.assertEqual((r["outcome"], r["reason"], r["cost_usd"], r["posted"]), ("failed", "reached 3 USD", 3.01, []))
        self.assertEqual([c[1:3] for c in self.gh_calls()], [["issue", "list"]])

    def test_no_report_fails(self):
        del self.files["report.md"]
        _, r, _ = self.run_review()
        self.assertEqual((r["outcome"], r["reason"]), ("failed", "the session wrote no report.md"))

    def test_garbage_output_fails(self):
        self.claude, self.claude_rc = None, 1
        self.assertEqual(self.run_review()[1]["reason"], "the session ended with an error")

    def test_a_failed_run_counts_for_due(self):
        self.put("tasks", 0.01, slug="a")
        del self.files["report.md"]
        self.run_review()
        self.assertFalse(factory.review_decide(factory.load_settings())[0])

    def test_the_session_is_launched_fresh_and_fenced(self):
        self.run_review()
        [argv] = [c for c in self.calls if c[:2] == ["claude", "-p"]]
        folder = re.search(r"Your folder: (\S+)\. Previous", argv[-1])[1]
        self.assertEqual(argv[argv.index("--model") + 1], "fable")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3.00")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], factory.REVIEW_ROLE.read_text())
        self.assertIn("--tools=Read,Grep,Glob,Bash,Write", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")],
                         ["Read", "Grep", "Glob", "Bash", f"Edit(/{folder}/**)"])
        self.assertEqual(argv[argv.index("--disallowedTools") + 1:argv.index("--disallowedTools") + 6],
                         ["Bash(gh *)", "Bash(git push *)", "Bash(git commit *)", "Bash(curl *)", "Bash(factory *)"])
        for flag in ("--plugin-dir", "--agents", "--resume", "--continue"):
            self.assertNotIn(flag, argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("input/issues.json: absent", argv[-1])
        self.assertEqual(self.gh_calls(), [])

    def test_no_mcp_whatever_lean_start_says(self):
        for lean in ("true", "false"):
            with self.subTest(lean_start=lean):
                self.settings_file(f"[sessions]\nlean_start = {lean}\n")
                with mock.patch.object(factory, "session_flags", side_effect=lambda lean: ["--strict-mcp-config"] if lean else []):
                    argv = factory.review_argv(factory.load_settings(), self.tmp, "p")
                self.assertEqual(argv.count("--strict-mcp-config"), 1)

    def test_the_session_cannot_reach_github(self):
        self.run_review()
        [env] = [e for c, e in zip(self.calls, self.envs) if c[:2] == ["claude", "-p"]]
        self.assertEqual(env["GH_TOKEN"], "unavailable")
        self.assertEqual({k: v for k, v in env.items() if k in factory.lockdown_exports()}, factory.lockdown_exports())

    def test_a_started_record_alone_is_not_due_for_a_period(self):
        self.put("tasks", 0.1, slug="a")
        self.put("reviews", 0.5, outcome="started", folder="/f")
        self.assertFalse(factory.review_decide(factory.load_settings())[0])
        self.assertEqual([r["outcome"] for r in self.reviews()], ["started"])

    def test_a_started_record_is_not_the_last_run(self):
        self.put("reviews", 5, outcome="ran")
        self.put("reviews", 3, outcome="started", folder="/f")
        self.put("tasks", 4, slug="a")
        self.assertEqual(factory.review_since(self.reviews()), self.reviews()[0]["ts"])
        self.assertTrue(factory.review_decide(factory.load_settings())[0])

    def test_a_folder_that_cannot_be_made_fails_with_the_reason(self):
        self.store.mkdir(parents=True, exist_ok=True)
        (self.store / "deliverables").write_text("a file where the folder should go")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(factory.review_run(SimpleNamespace(root=self.root), factory.load_settings()), 1)
        self.assertEqual([(r["outcome"], r.get("reason", "")[:30]) for r in self.reviews()],
                         [("started", ""), ("failed", "the review failed: NotADirecto")])

    def test_the_prompt_names_the_previous_report(self):
        _, first, _ = self.run_review()
        self.calls.clear()
        with mock.patch.object(factory, "run", self.fake), mock.patch.object(factory, "now", lambda: ago(-1)), redirect_stdout(io.StringIO()):
            factory.review_run(SimpleNamespace(root=self.root), factory.load_settings())
        self.assertIn(f"Previous report: {first['folder']}/report.md", self.calls[-1][-1])
        self.assertIn(f"Since: {first['ts']}", self.calls[-1][-1])

    def test_an_exception_still_leaves_a_record(self):
        with mock.patch.object(factory, "run", side_effect=RuntimeError("boom")), redirect_stdout(io.StringIO()):
            self.assertEqual(factory.review_run(SimpleNamespace(root=self.root), factory.load_settings()), 1)
        [_, r] = self.reviews()
        self.assertEqual((r["outcome"], r["reason"]), ("failed", "the review failed: RuntimeError: boom"))

    def test_cmd_review_by_hand_prints_the_decision(self):
        self.put("tasks", 1, slug="a")
        out = io.StringIO()
        with mock.patch.object(factory, "run", self.fake), mock.patch.object(factory, "Repo", lambda: SimpleNamespace(root=self.root)), \
                redirect_stdout(out):
            self.assertEqual(factory.main(["review"]), 0)
            self.assertEqual(factory.main(["review"]), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("review: due: there was activity since "))
        self.assertTrue(lines[1].startswith("review: ran; 0.42 USD"))
        self.assertTrue(lines[2].startswith("review: not due: the last review (ran) was at "))

    def test_review_run_flag_runs_without_deciding(self):
        out = io.StringIO()
        with mock.patch.object(factory, "run", self.fake), mock.patch.object(factory, "Repo", lambda: SimpleNamespace(root=self.root)), \
                redirect_stdout(out):
            self.assertEqual(factory.main(["review", "--run"]), 0)
        self.assertEqual([r["outcome"] for r in self.reviews()], ["started", "ran"])

    def test_a_second_review_cannot_start_while_one_runs(self):
        with factory.file_lock(self.store / "review.lock"), mock.patch.object(factory, "Repo", lambda: SimpleNamespace(root=self.root)), \
                self.assertRaises(factory.Fail):
            factory.cmd_review(SimpleNamespace(run=True))


class ShapeTests(Store):
    ISSUES = [{"number": 3}]

    def parse(self, text):
        return factory.review_parse(text, self.ISSUES)

    def test_valid_files_pass(self):
        kind, fields, sections = self.parse(proposal_file())
        self.assertEqual((kind, fields["title"], fields["rung"], fields["role"], list(sections)),
                         ("proposal", "Reviewer gets the fact list", "information", "reviewer", list(BODY)))
        self.assertEqual(self.parse(proposal_file("evidence", issue=3))[2], {"Evidence": "Evidence text"})
        self.assertEqual(self.parse(proposal_file("check", issue=3, cites="[]"))[2], {"Number": "Number text"})

    def test_each_invalid_file_is_held_with_its_reason(self):
        cases = {
            "no front matter": ("## Problem\nx\n", "no front matter"),
            "kind": (proposal_file().replace("kind: proposal", "kind: idea"), "unknown kind 'idea'"),
            "rung": (proposal_file(rung="vibes"), "unknown rung 'vibes'"),
            "no title": (proposal_file(title=""), "the title is missing"),
            "long title": (proposal_file(title="x" * 81), "over 80 characters"),
            "no role": (proposal_file(role=""), "no role"),
            "cites": (proposal_file().replace("cites: [3fa9c1d2e4b6, 77aa01b2c3d4]", "cites: 3fa9"), "cites is not a list"),
            "no cites": (proposal_file().replace("cites: [3fa9c1d2e4b6, 77aa01b2c3d4]\n", ""), "cites is not a list"),
            "missing section": (proposal_file(sections=["Problem", "Proposal", "Evidence", "Number"]), "the section What the role lacked is missing"),
            "empty section": (proposal_file().replace("Number text", ""), "the section Number is missing or empty"),
            "evidence without issue": (proposal_file("evidence"), "issue None is not in input/issues.json"),
            "unknown issue": (proposal_file("check", issue=9), "issue '9' is not in input/issues.json"),
            "check without number": (proposal_file("check", issue=3, sections=["Evidence"]), "the section Number is missing"),
        }
        for name, (text, reason) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, re.escape(reason)):
                self.parse(text)

    def test_a_title_with_a_hash_keeps_it(self):
        self.assertEqual(self.parse(proposal_file(title="Fix #12 now"))[1]["title"], "Fix #12 now")


class ScanTests(Store):
    def setUp(self):
        super().setUp()
        self.put("tasks", 1, repo=self.root)
        self.put("tasks", 1, repo="/elsewhere/secretproject")
        self.record = put_access("/elsewhere/secretproject", tools=["mcp__prodmetrics__query"])
        (self.store / "hosts.json").write_text(json.dumps({"edgebox": {"target": "deploy@10.0.0.9", "repos": []}}))
        remotes = {self.root: ("me", "widgets"), "/elsewhere/secretproject": ("me", "gizmos")}
        patcher = mock.patch.object(factory, "remote_repo", lambda cwd=None: remotes.get(cwd))
        patcher.start()
        self.addCleanup(patcher.stop)

    def held(self, text):
        patterns = factory.review_patterns(TARGET, factory.load_settings())
        return next((k for k, p in patterns if p.search(text)), None)

    def test_each_kind_of_private_detail_is_found_by_its_kind(self):
        for text, kind in ((f"see {self.tmp}/notes", "a path under a home directory"),
                           ("in /Users/alice/code/x", "a path under a home directory"),
                           ("under /home/bob/src", "a path under a home directory"),
                           ("in ~/code/x", "a path under a home directory"),
                           ("the Gizmos repo", "the name of another repository"),
                           ("me/gizmos broke", "the name of another repository"),
                           ("the prodmetrics server", "a host or MCP server name"),
                           ("host EDGEBOX is slow", "a host or MCP server name"),
                           ("at 10.20.30.40 it", "an IPv4 address")):
            with self.subTest(text):
                self.assertEqual(self.held(text), kind)

    def test_a_server_granted_in_another_repository_is_held(self):
        put_access("/elsewhere/other", tools=["mcp__granted__query"])
        self.assertEqual(self.held("the granted server"), "a host or MCP server name")

    def test_an_unreadable_record_fails_the_scan(self):
        self.record.write_text("{")
        with self.assertRaisesRegex(factory.Fail, "cannot be read as a production-access record"):
            self.held("text")

    def test_a_repository_seen_only_in_the_firings_or_ops_log_is_found(self):
        for name in ("firings", "ops"):
            with self.subTest(name=name):
                self.put(name, 1, repo=f"/elsewhere/only-{name}")
                remotes = {self.root: ("me", "widgets"), f"/elsewhere/only-{name}": ("me", f"only{name}")}
                with mock.patch.object(factory, "remote_repo", lambda cwd=None: remotes.get(cwd)):
                    self.assertEqual(self.held(f"the only{name} repo"), "the name of another repository")

    def test_the_target_and_ordinary_words_pass(self):
        for text in ("the widgets repo, me/widgets", "version 1.2.3 of a gizmos-free thing", "in the repo directory",
                     "a path like src/factory/bin"):
            with self.subTest(text):
                self.assertIsNone(self.held(text))

    def test_a_held_file_says_the_kind_and_never_the_match(self):
        folder = self.tmp / "f"
        (folder / "proposals").mkdir(parents=True)
        for i, secret in enumerate(("/Users/alice/code/x", "gizmos", "prodmetrics", "10.20.30.40")):
            (folder / "proposals" / f"{i}.md").write_text(proposal_file().replace("Problem text", f"it broke at {secret}"))
        spy = mock.Mock(return_value=(0, "", ""))
        with mock.patch.object(factory, "review_gh", spy):
            posted, held = factory.review_post(TARGET, factory.load_settings(), folder, [], "S")
        self.assertEqual((posted, len(held)), ([], 4))
        self.assertEqual([h["why"] for h in held][:1], ["it holds a path under a home directory"])
        for h, secret in zip(held, ("alice", "gizmos", "prodmetrics", "10.20")):
            self.assertNotIn(secret, h["why"])
        spy.assert_not_called()


class PostTests(Session):
    def test_with_the_setting_empty_no_gh_call_is_made(self):
        self.files["proposals/p.md"] = proposal_file()
        _, r, _ = self.run_review()
        self.assertEqual((self.gh_calls(), r["posted"], r["held"]), ([], [], []))
        self.assertFalse((Path(r["folder"]) / "input").exists())
        self.assertTrue((Path(r["folder"]) / "proposals" / "p.md").exists())

    def test_a_proposal_an_evidence_and_a_check_are_posted_as_the_app_for_the_target(self):
        self.files.update({"proposals/1.md": proposal_file(), "proposals/2.md": proposal_file("evidence", issue=3),
                           "proposals/3.md": proposal_file("check", issue=3)})
        code, r, _ = self.run_review(TARGET)
        self.assertEqual(code, 0)
        self.assertEqual(r["posted"], [{"file": "1.md", "issue": 41, "kind": "proposal"}, {"file": "2.md", "issue": 3, "kind": "evidence"},
                                       {"file": "3.md", "issue": 3, "kind": "check"}])
        calls = self.gh_calls()
        self.assertEqual([c[1:3] for c in calls], [["issue", "list"], ["label", "list"], ["label", "create"], ["label", "create"],
                                                  ["issue", "create"], ["issue", "comment"], ["issue", "comment"]])
        self.assertEqual([c[c.index("create") + 1] for c in calls if c[1:3] == ["label", "create"]], ["kaizen", "kaizen:accepted"])
        self.assertEqual(calls[0][calls[0].index("--label") + 1], "kaizen")
        self.assertEqual(calls[0][calls[0].index("--limit") + 1], "200")
        create = calls[4]
        self.assertEqual(create[create.index("--title") + 1], "Reviewer gets the fact list")
        self.assertEqual(create[create.index("--label") + 1], "kaizen")
        body = create[create.index("--body") + 1]
        self.assertIn("## Problem\nProblem text\n\n## What the role lacked\n", body)
        self.assertRegex(body, r"\n\nRole: reviewer · Rung: information · Cites: 3fa9c1d2e4b6, 77aa01b2c3d4 · Review: \d{8}T\d{6}Z$")
        self.assertEqual(calls[5][2:4], ["comment", "3"])
        self.assertTrue(calls[5][calls[5].index("--body") + 1].startswith("## Evidence\nEvidence text"))
        self.assertTrue(calls[6][calls[6].index("--body") + 1].startswith("## Number\nNumber text"))
        for c, env in zip(self.calls, self.envs):
            if c[0] == "gh":
                self.assertEqual((env["GH_TOKEN"], c[c.index("--repo") + 1]), ("TOK", TARGET))
        self.assertEqual(set(self.tokens), {"widgets"})
        self.assertEqual(json.loads((Path(r["folder"]) / "input" / "issues.json").read_text()), self.issues)
        self.assertIn("input/issues.json: present", [c for c in self.calls if c[:2] == ["claude", "-p"]][0][-1])
        self.assertIn("Script record: posted 3, held 0", (Path(r["folder"]) / "report.md").read_text())

    def test_a_refusal_holds_that_file_and_the_rest_still_post(self):
        self.gh[("issue", "create")] = (1, "", REFUSED + "\nHTTP 403")
        self.files.update({"proposals/1.md": proposal_file(), "proposals/2.md": proposal_file("evidence", issue=3)})
        _, r, _ = self.run_review(TARGET)
        self.assertEqual(r["held"], [{"file": "1.md", "why": REFUSED}])
        self.assertEqual(r["posted"], [{"file": "2.md", "issue": 3, "kind": "evidence"}])
        self.assertIn(f"held 1.md: {REFUSED}", (Path(r["folder"]) / "report.md").read_text())

    def test_a_refused_label_holds_the_proposals_only(self):
        self.gh[("label", "create")] = (1, "", REFUSED)
        self.files.update({"proposals/1.md": proposal_file(), "proposals/2.md": proposal_file("evidence", issue=3)})
        _, r, _ = self.run_review(TARGET)
        self.assertEqual(r["held"], [{"file": "1.md", "why": f"labels: {REFUSED}"}])
        self.assertEqual([p["kind"] for p in r["posted"]], ["evidence"])

    def test_without_an_identity_everything_is_held_and_gh_is_not_run(self):
        self.identity = None
        self.files["proposals/1.md"] = proposal_file()
        _, r, _ = self.run_review(TARGET)
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual((r["outcome"], r["held"]), ("ran", [{"file": "1.md", "why": "no App identity for me"}]))
        self.assertFalse((Path(r["folder"]) / "input" / "issues.json").exists())

    def test_an_identity_of_another_owner_is_no_identity(self):
        self.identity = {"bot_login": "bot", "owner": "someone"}
        self.files["proposals/1.md"] = proposal_file()
        _, r, _ = self.run_review(TARGET)
        self.assertEqual((self.gh_calls(), r["held"][0]["why"]), ([], "no App identity for me"))

    def test_an_unreadable_issue_list_holds_everything(self):
        self.gh[("issue", "list")] = (0, "not json", "")
        self.files["proposals/1.md"] = proposal_file()
        _, r, _ = self.run_review(TARGET)
        self.assertEqual(r["held"], [{"file": "1.md", "why": "the issue list is not JSON"}])

    def test_a_token_failure_holds_with_the_error(self):
        self.files["proposals/1.md"] = proposal_file()
        self.settings_file(f'[kaizen]\nissues_repo = "{TARGET}"\n')
        with mock.patch.object(factory, "run", self.fake), mock.patch.object(factory, "load_identity", lambda: self.identity), \
                mock.patch.object(factory, "app_token", side_effect=factory.Fail("no token", 1)), redirect_stdout(io.StringIO()):
            factory.review_run(SimpleNamespace(root=self.root), factory.load_settings())
        self.assertEqual(self.reviews()[-1]["held"], [{"file": "1.md", "why": "no token"}])
        self.assertEqual(self.gh_calls(), [])

    def test_a_proposal_that_cites_nothing_says_none(self):
        self.files["proposals/1.md"] = proposal_file(cites="[]")
        self.run_review(TARGET)
        create = next(c for c in self.gh_calls() if c[1:3] == ["issue", "create"])
        self.assertRegex(create[create.index("--body") + 1], r" · Cites: none · Review: \d{8}T\d{6}Z$")

    def test_a_file_of_a_missing_issue_is_held_and_labels_already_there_are_kept(self):
        self.gh[("label", "list")] = (0, json.dumps([{"name": "kaizen"}, {"name": "kaizen:accepted"}]), "")
        self.files.update({"proposals/1.md": proposal_file("check", issue=99), "proposals/2.md": proposal_file()})
        _, r, _ = self.run_review(TARGET)
        self.assertEqual(r["held"], [{"file": "1.md", "why": "issue '99' is not in input/issues.json"}])
        self.assertEqual([c[1:3] for c in self.gh_calls() if c[1] == "label"], [["label", "list"]])


class RunEnvTests(unittest.TestCase):
    def test_a_token_the_caller_brings_is_not_replaced_by_the_working_directory_s(self):
        seen = []
        with mock.patch.object(factory, "acting_identity", lambda cwd=None: ({}, "other")), \
                mock.patch.object(factory, "app_token", lambda *a: "THEIRS"), \
                mock.patch.object(factory.subprocess, "Popen", stub_popen(lambda argv, **kw: seen.append(kw["env"]) or CompletedProcess(argv, 0, "", ""))):
            factory.run(["gh", "x"], env={"GH_TOKEN": "MINE"})
            factory.run(["gh", "x"])
        self.assertEqual([e["GH_TOKEN"] for e in seen], ["MINE", "THEIRS"])


class MirrorTests(Store):
    def setUp(self):
        super().setUp()
        for i, fid in enumerate(("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd")):
            self.put("feedback", 10 - i, id=fid, text=f"observation {i}", role="lead", slug="s", model="fable")

    def status(self):
        return {e["id"][0]: e["status"] for e in factory.read_feedback()}

    def issue(self, number, state, accepted, cites):
        return {"number": number, "state": state, "body": f"## Problem\nx\n\nRole: lead · Cites: {cites} · Review: S",
                "labels": [{"name": "kaizen"}, *([{"name": "kaizen:accepted"}] if accepted else [])]}

    def test_accepted_done_and_rejected_issues_set_the_cited_entries_once(self):
        issues = [self.issue(1, "OPEN", True, "aaaaaaaaaaaa"), self.issue(2, "CLOSED", True, "bbbbbbbbbbbb, aaaa00000000"),
                  self.issue(3, "CLOSED", False, "cccccccccccc"), self.issue(4, "OPEN", False, "dddddddddddd")]
        factory.review_mirror(issues)
        self.assertEqual(self.status(), {"a": "accepted", "b": "done", "c": "rejected", "d": "open"})
        written = len(list(factory.read_log("feedback")))
        factory.review_mirror(issues)
        self.assertEqual(len(list(factory.read_log("feedback"))), written)
        self.assertEqual(self.status(), {"a": "accepted", "b": "done", "c": "rejected", "d": "open"})

    def test_mirror_lines_say_who_wrote_them_and_are_read_as_before(self):
        factory.review_mirror([self.issue(1, "OPEN", True, "aaaaaaaaaaaa")])
        [line] = [r for r in factory.read_log("feedback") if "status" in r]
        self.assertEqual((line["status"], line["by"]), ("accepted", "kaizen-review"))
        folded = {e["id"][0]: e["status"] for e in factory.fold_feedback(factory.read_log("feedback"))}
        self.assertEqual(folded["a"], "accepted")
        out = io.StringIO()
        with redirect_stdout(out):
            factory.main(["feedback", "--status", "accepted"])
        self.assertRegex(out.getvalue(), r"^aaaaaaaaaaaa  accepted  ")

    def test_mirror_lines_are_not_activity(self):
        self.put("reviews", 3, outcome="ran")
        factory.review_mirror([self.issue(1, "OPEN", True, "aaaaaaaaaaaa")])
        self.assertFalse(factory.review_decide(factory.load_settings())[0])
        self.assertEqual(self.reviews()[-1]["outcome"], "skipped")

    def test_a_status_set_by_hand_is_activity(self):
        self.put("reviews", 3, outcome="ran")
        self.put("feedback", 0.01, id="aaaaaaaaaaaa", status="done")
        self.assertTrue(factory.review_decide(factory.load_settings())[0])

    def test_the_run_mirrors_after_it_fetches(self):
        gh = mock.Mock(return_value=(0, json.dumps([self.issue(1, "CLOSED", False, "aaaaaaaaaaaa")]), ""))
        self.settings_file(f'[kaizen]\nissues_repo = "{TARGET}"\n')
        claude = (0, json.dumps({"session_id": "s"}), "")
        run = lambda argv, **kw: (0, "[]", "") if argv[:3] == ["claude", "plugin", "list"] else claude if argv[0] == "claude" else gh(argv)
        with mock.patch.object(factory, "run", run), mock.patch.object(factory, "load_identity", lambda: {"bot_login": "b", "owner": "me"}), \
                mock.patch.object(factory, "app_token", lambda *a: "T"), redirect_stdout(io.StringIO()):
            factory.review_run(SimpleNamespace(root=self.root), factory.load_settings())
        self.assertEqual(self.status()["a"], "rejected")


class PlumbingTests(Store):
    def test_reviews_is_a_log_the_factory_owns_and_never_prunes(self):
        self.assertRegex("reviews-2026-11.jsonl", factory.HOME_FILE_RE)
        for name in ("review.log", "review.lock"):
            self.assertRegex(name, factory.STORE_WRITTEN_RE)
        self.store.mkdir(parents=True)
        old = self.store / "reviews-2020-01.jsonl"
        old.write_text('{"ts": "2020-01-05T00:00:00Z"}\n')
        factory.prune("reviews")
        self.assertTrue(old.exists())
        old.rename(self.store / "ops-2020-01.jsonl")
        factory.prune("ops")
        self.assertFalse((self.store / "ops-2020-01.jsonl").exists())

    def test_factory_log_prints_the_reviews(self):
        self.put("reviews", 1, outcome="ran")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(factory.main(["log", "reviews"]), 0)
        self.assertEqual(json.loads(out.getvalue())["outcome"], "ran")

    def test_the_settings(self):
        s = factory.load_settings()
        self.assertEqual((s["review_hours"], s["issues_repo"], s["models"]["kaizen_review"]), (1, "", "fable"))
        self.settings_file('[kaizen]\nreview_hours = 0\nissues_repo = "me/widgets"\n[models]\nkaizen_review = "opus"\n')
        s = factory.load_settings()
        self.assertEqual((s["review_hours"], s["issues_repo"], s["models"]["kaizen_review"]), (0, "me/widgets", "opus"))
        for bad in ('review_hours = -1', 'review_hours = "x"', 'issues_repo = "widgets"', 'issues_repo = "a/b/c"'):
            self.settings_file(f"[kaizen]\n{bad}\n")
            with self.subTest(bad), self.assertRaises(factory.Fail):
                factory.load_settings()

    def test_the_charter_is_the_session_prompt(self):
        text = factory.REVIEW_ROLE.read_text()
        self.assertTrue(text.startswith("# You run the kaizen review\n"))
        for needle in ("## The questions, in order", "## What is published", "    kind: proposal"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
