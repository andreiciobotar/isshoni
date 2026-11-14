import io
import json
import re
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from test_factory import ROOT, factory
from test_factory import DISPATCHER
from test_health import NOW, SchedHarness, iso, lead, register
from test_merge import SESSION, SHA, World, pr

LIST = ("gh", "pr", "list", "--state", "open")
GAVE = "#1 merged https://x/pull/1\n#2 stopped ci: check test failed https://x/run/2"


def reviewed(n, at=NOW - 60, state="APPROVED", **kw):
    return {**pr(n, **kw), "latestReviews": [{"state": state, "submittedAt": iso(at)}], "comments": []}


def said(n, at=NOW - 60, body=" LGTM\n", association="OWNER", **kw):
    return {**pr(n, **kw), "latestReviews": [],
            "comments": [{"body": body, "createdAt": iso(at), "authorAssociation": association}]}


def plain(n, **kw):
    return {**pr(n, **kw), "latestReviews": [], "comments": []}


def reply(text, rc=0):
    return rc, json.dumps({"result": text, "num_turns": 7, "total_cost_usd": 0.42, "duration_ms": 91000}), ""


def left_open(prs, answer):
    """The pull requests still open after a run whose reply is `answer`: those it does not say it merged."""
    text = json.loads(answer[1]).get("result", "") if answer and answer[0] == 0 and answer[1].startswith("{") else ""
    return [p for p in prs if f"#{p['number']} merged" not in str(text)]


def lander_state():
    return factory.read_lander(SimpleNamespace(root=ROOT))


class StarterTests(World):
    def lander(self, *argv, prs, answer=None, sessions=None, **kw):
        answers, listed, slugs = self.world(prs, **kw)
        if answer is not None:
            answers[("claude", "-p")] = answer
        answers[LIST] = [(0, json.dumps(prs), ""), (0, json.dumps(left_open(prs, answer)), "")]
        return self.cli("lander", *argv, answers=answers, sessions=listed if sessions is None else sessions,
                        entries=slugs)

    def started(self):
        [argv] = self.claude_runs()
        at = self.fake.calls.index(argv)
        return argv, self.fake.timeouts[at], self.fake.envs[at]

    def test_the_word_of_the_human_starts_one_lander_with_exactly_those_merge_commands(self):
        code, out, _ = self.lander("12", "13", prs=[plain(12), plain(13), plain(14)],
                                   answer=reply("## Result\n#12 merged u12\n#13 merged u13"))
        self.assertEqual(code, 0)
        argv, timeout, env = self.started()
        tools = argv[argv.index("--allowedTools") + 1:argv.index("--output-format")]
        self.assertEqual(argv[:10], ["claude", "-p", "--agent", "factory:lander", "--plugin-dir", str(factory.PLUGIN_ROOT),
                                     "--model", "sonnet", "--permission-mode", "dontAsk"])
        self.assertEqual(tools, [*factory.MERGE_TOOLS, "Bash(factory merge 12)", "Bash(factory merge 13)"])
        self.assertEqual(argv[-3:-1], ["--output-format", "json"])
        self.assertEqual(timeout, 3600)
        self.assertEqual((env["BASH_DEFAULT_TIMEOUT_MS"], env["BASH_MAX_TIMEOUT_MS"]), ("2400000", "2400000"))
        brief = argv[-1]
        self.assertTrue(brief.startswith("Merge run.\nRepository: /repo\n"))
        self.assertIn("- #12: head worktree-t12, base main, "
                      "task t12 (its lead is the session t12); on the human's word: merge with `factory merge 12`", brief)
        self.assertNotIn("#14", brief)
        self.assertTrue(brief.endswith("The dispatcher started this run: do not message it."))

    def test_without_a_live_dispatcher_the_brief_never_says_it_started_the_run(self):
        self.lander("1", prs=[plain(1)], answer=reply("#1 merged u"), sessions=[])
        self.assertTrue(self.claude_runs()[0][-1].endswith("No dispatcher session is alive."))

    def test_the_run_has_this_factory_first_on_its_path(self):
        self.lander("1", prs=[plain(1)], answer=reply("#1 merged u"))
        at = self.fake.calls.index(self.claude_runs()[0])
        path = self.fake.envs[at]["PATH"].split(":")
        self.assertEqual(path[0], str(factory.FACTORY.parent))
        self.assertIn("/usr/bin", path)

    def test_a_merged_line_for_a_pull_request_that_is_still_open_is_a_stop(self):
        answers, listed, slugs = self.world([plain(1)])
        answers[LIST] = (0, json.dumps([plain(1)]), "")  # still open after the run
        answers[("claude", "-p")] = reply("#1 merged https://x/pull/1")
        code, out, _ = self.cli("lander", "1", answers=answers, sessions=listed, entries=slugs)
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped other: the lander said merged but it is still open\n", out)

    def test_numbers_that_are_null_in_the_reply_do_not_raise(self):
        null = (0, json.dumps({"result": "#1 merged u", "num_turns": None, "total_cost_usd": None,
                               "duration_ms": None}), "")
        code, out, _ = self.lander("1", prs=[plain(1)], answer=null)
        self.assertEqual(code, 0)
        self.assertIn("lander: ? turns, $?, ? s\n", out)

    def test_the_brief_says_who_started_the_run(self):
        self.lander(prs=[reviewed(1)], answer=reply("#1 merged u"))
        self.assertTrue(self.claude_runs()[0][-1].endswith(f"The dispatcher's session name is {SESSION}."))
        self.assertNotIn("moved", self.claude_runs()[0][-1])

    def test_the_brief_names_the_registered_dispatcher_by_its_current_name(self):
        register("sid-d")
        self.lander(prs=[reviewed(1)], answer=reply("#1 merged u"), sessions=[
            {"sessionId": "sid-d", "name": "renamed", "cwd": ROOT, "kind": "interactive", "pid": 7}])
        self.assertTrue(self.claude_runs()[0][-1].endswith("The dispatcher's session name is renamed."))

    def test_the_merge_tools_cannot_merge_approve_comment_push_or_edit(self):
        tools = " ".join(factory.MERGE_TOOLS)
        for never in ("Edit", "Write", "Bash(git", "pr merge", "pr review", "pr comment", "pr edit", "pr ready", "push"):
            self.assertNotIn(never, tools)
        self.assertFalse(any(re.fullmatch(r"Bash\(factory merge \d+\)", t) for t in factory.MERGE_TOOLS))
        self.assertIn("Bash(factory rerun *)", factory.MERGE_TOOLS)
        self.assertNotIn("gh run rerun", tools)

    def test_without_numbers_it_takes_what_is_approved_and_the_word_permission_is_absent(self):
        code, out, _ = self.lander(prs=[reviewed(1), said(2), plain(3), reviewed(4, state="CHANGES_REQUESTED"),
                                        said(5, body="lgtm, thanks")],
                                   answer=reply("## Result\n#1 merged u1\n#2 merged u2"))
        self.assertEqual(code, 0)
        argv, _, _ = self.started()
        self.assertEqual([t for t in argv if t.startswith("Bash(factory merge")], ["Bash(factory merge --approved *)"])
        brief = argv[-1]
        self.assertIn("approved on GitHub: merge with `factory merge --approved 1`", brief)
        self.assertIn("- #2:", brief)
        self.assertNotIn("- #3:", brief)
        self.assertNotIn("- #4:", brief)
        self.assertNotIn("- #5:", brief)
        self.assertNotIn("on the human's word", brief)

    def test_the_snapshot_is_one_call_with_a_time_budget(self):
        self.lander(prs=[reviewed(1)], answer=reply("#1 merged u"))
        at = [i for i, c in enumerate(self.fake.calls) if c[:5] == list(LIST)][0]
        self.assertEqual(self.fake.calls[at][5:], ["--limit", "100", "--json", factory.NOTICE_FIELDS])
        self.assertEqual(self.fake.timeouts[at], 20)

    def test_nothing_approved_starts_nothing(self):
        code, out, _ = self.lander(prs=[plain(1)])
        self.assertEqual((code, out), (0, "nothing approved\n"))
        self.assertEqual(self.claude_runs(), [])

    def test_a_number_that_is_not_open_is_refused(self):
        code, _, err = self.lander("9", prs=[plain(1)])
        self.assertEqual(code, 2)
        self.assertIn("#9 is not an open pull request", err)
        self.assertEqual(self.claude_runs(), [])

    def test_a_snapshot_that_fails_is_refused(self):
        answers, listed, slugs = self.world([plain(1)])
        answers[LIST] = (1, "", "boom")
        code, _, err = self.cli("lander", "1", answers=answers, sessions=listed, entries=slugs)
        self.assertEqual(code, 1)
        self.assertIn("gh pr list failed: boom", err)

    def test_a_run_that_is_going_blocks_a_second(self):
        with factory.repo_lock(SimpleNamespace(root=ROOT), "lander") as free:
            self.assertTrue(free)
            code, out, _ = self.lander("1", prs=[plain(1)])
        self.assertEqual((code, out), (1, "a lander run is already going; run it again when it ends\n"))
        self.assertEqual(self.claude_runs(), [])
        code, _, _ = self.lander("1", prs=[plain(1)], answer=reply("#1 merged u"))
        self.assertEqual(code, 0)

    def test_the_result_lines_are_printed_recorded_and_summed_up(self):
        text = "## Result\n" + GAVE + "\n- `#3 stopped nonsense: odd`\n\n## Factory feedback\nnone"
        code, out, _ = self.lander("1", "2", "3", prs=[plain(1), plain(2), plain(3)], answer=reply(text))
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            "#1 merged https://x/pull/1", "#2 stopped ci: check test failed https://x/run/2",
            "#3 stopped other: odd", "lander: 7 turns, $0.42, 91 s"])
        stopped = lander_state()["stopped"]
        self.assertEqual(sorted(stopped), ["2", "3"])
        self.assertEqual({k: stopped["2"][k] for k in ("head", "kind", "text")},
                         {"head": SHA, "kind": "ci", "text": "check test failed https://x/run/2"})
        self.assertEqual(stopped["3"]["kind"], "other")

    def test_a_pull_request_without_a_line_is_a_stop_other(self):
        code, out, _ = self.lander("1", "2", prs=[plain(1), plain(2)], answer=reply("## Result\n#1 merged u"))
        self.assertEqual(code, 1)
        self.assertIn("#2 stopped other: the lander gave no line for it\n", out)

    def test_a_failed_run_stops_every_pull_request_with_the_reason(self):
        cases = {"timed out": ((124, "", "timed out"), "timed out"), "exited": ((3, "", "boom"), "exited 3"),
                 "unreadable": ((0, "not json", ""), "its reply could not be read"),
                 "a lander that said nothing but rc 0": ((0, json.dumps({"result": 5}), ""), "its reply could not be read")}
        for label, (answer, why) in cases.items():
            with self.subTest(label):
                self.setUp()
                code, out, _ = self.lander("1", "2", prs=[plain(1), plain(2)], answer=answer)
                self.assertEqual(code, 1)
                for n in (1, 2):
                    self.assertIn(f"#{n} stopped other: the lander gave no line for it ({why})\n", out)
                self.assertNotIn("lander:", out)

    def test_a_run_that_failed_ignores_what_a_reply_may_hold(self):
        _, out, _ = self.lander("1", prs=[plain(1)], answer=(1, json.dumps({"result": "#1 merged u"}), ""))
        self.assertNotIn("merged", out)

    def test_a_stop_is_told_to_the_human_when_no_dispatcher_is_alive(self):
        answers = {("osascript",): (0, "", "")}
        world, listed, slugs = self.world([reviewed(1)])
        world.update(answers)
        world[("claude", "-p")] = reply("#1 stopped ci: red")
        world[LIST] = (0, json.dumps([reviewed(1)]), "")
        code, out, _ = self.cli("lander", answers=world, sessions=listed[1:], entries=slugs)
        self.assertEqual(code, 1)
        [notice] = [c for c in self.fake.calls if c[0] == "osascript"]
        self.assertIn("#1 stopped ci: red", notice[2])
        self.assertIn("No dispatcher session is alive.", self.claude_runs()[0][-1])
        [fired] = [r for r in self.records() if "criterion" in r]
        self.assertEqual((fired["criterion"], fired["subject"], fired["escalate"]), ("lgtm-blocked", "#1/ci", "always"))
        self.assertEqual([r["decision"] for r in self.records() if "decision" in r], ["escalated"])

    def test_with_a_dispatcher_the_stop_is_recorded_as_already_escalated(self):
        self.lander("1", prs=[plain(1)], answer=reply("#1 stopped ci: red"))
        self.assertFalse(self.fake.ran("osascript"))
        [fired] = [r for r in self.records() if "criterion" in r]
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertEqual((fired["subject"], fired["slug"]), ("#1/ci", "t1"))
        self.assertEqual((decision["id"], decision["decision"], decision["reason"]),
                         (fired["id"], "escalated", "reported by the lander run"))

    def test_a_behind_stop_is_recorded_as_that_kind_and_fires_lgtm_blocked(self):
        code, out, _ = self.lander("1", prs=[plain(1)], answer=reply("#1 stopped behind: its base has moved"))
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped behind: its base has moved\n", out)
        self.assertEqual(lander_state()["stopped"]["1"]["kind"], "behind")
        self.assertEqual([r["subject"] for r in self.records() if r.get("criterion") == "lgtm-blocked"], ["#1/behind"])

    def test_a_draft_is_given_on_the_humans_word_but_never_taken_on_an_approval(self):
        draft = {**reviewed(1), "isDraft": True}
        code, out, _ = self.lander(prs=[draft])
        self.assertEqual((code, out), (0, "nothing approved\n"))
        self.lander("1", prs=[draft], answer=reply("#1 merged u"))
        self.assertIn("on the human's word", self.claude_runs()[0][-1])

    def test_a_run_that_died_reports_what_is_no_longer_open_as_merged(self):
        both = [plain(1), plain(2)]
        answers, listed, slugs = self.world(both)
        answers[LIST] = [(0, json.dumps(both), ""), (0, json.dumps([plain(2)]), "")]
        answers[("claude", "-p")] = (124, "", "timed out")
        code, out, _ = self.cli("lander", "1", "2", answers=answers, sessions=listed, entries=slugs)
        self.assertEqual(code, 1)
        self.assertIn("#1 merged https://x/pull/1\n", out)
        self.assertIn("#2 stopped other: the lander gave no line for it (timed out)\n", out)
        self.assertEqual(sorted(lander_state()["stopped"]), ["2"])

    def test_lander_json_keeps_what_was_noticed_and_drops_what_closed(self):
        repo = SimpleNamespace(root=ROOT)
        state = factory.read_lander(repo)
        state["stopped"] = {"1": {"head": "old", "kind": "ci", "text": "t", "ts": "x"},
                            "9": {"head": "old", "kind": "ci", "text": "t", "ts": "x"}}
        state["noticed"] = {"9": "old:x"}
        factory.write_lander(repo, state)
        self.lander("1", prs=[reviewed(1), plain(2)], answer=reply("#1 stopped ci: red"))
        state = lander_state()
        self.assertEqual(sorted(state["stopped"]), ["1"])
        self.assertEqual(state["stopped"]["1"]["head"], SHA)
        self.assertEqual(state["noticed"], {"1": f"{SHA}:{iso(NOW - 60)}:free"})

    def test_the_file_is_written_whole_and_leaves_no_temporary(self):
        self.lander("1", prs=[plain(1)], answer=reply("#1 merged u"))
        path = factory.repo_state(ROOT, "lander", "json")
        self.assertTrue(path.exists())
        self.assertEqual(sorted(p.name for p in self.store.glob("lander-*")),
                         sorted([path.name, path.with_suffix(".lock").name]))


class TickTests(SchedHarness, World):
    CRITERIA = ["lgtm-blocked"]

    def tick(self, prs, *, answer=None, at=None, sessions=None, snapshot=None, **kw):
        answers, listed, slugs = self.world(prs, **kw)
        answers[LIST] = [snapshot or (0, json.dumps(prs), ""), (0, json.dumps(left_open(prs, answer)), "")]
        if answer is not None:
            answers[("claude", "-p")] = answer
        return self.cli("watch", answers=answers, sessions=listed if sessions is None else sessions,
                        entries=slugs, at=at)

    def landers(self):
        return [c for c in self.claude_runs() if "--agent" in c]

    def noticed(self):
        return [c for c in self.fake.calls if c[:5] == list(LIST) and factory.NOTICE_FIELDS in c]

    def test_a_working_or_blocked_task_costs_one_list_call_and_starts_no_lander(self):
        for state in ("working", "blocked"):
            with self.subTest(state):
                self.setUp()
                code, out, _ = self.tick([reviewed(1)], state=state)
                self.assertEqual(code, 0)
                self.assertEqual(len(self.noticed()), 1)  # the one list the comment forwarder reads
                self.assertEqual(self.landers(), [])
                self.assertFalse(any(c[:3] == ["gh", "pr", "checks"] for c in self.fake.calls))
        self.setUp()
        self.cli("watch", entries=[])
        self.assertFalse(any(c[0] == "gh" for c in self.fake.calls))

    def test_a_task_whose_worktree_is_gone_is_not_handed_over(self):
        self.seed("tasks", [{"repo": ROOT, "slug": "gone", "state": "handed-over", "ts": iso(NOW - 3600)}])
        self.cli("watch", entries=[], answers={LIST: (0, json.dumps([reviewed(1)]), "")})
        self.assertFalse(any(c[0] == "gh" for c in self.fake.calls))

    def test_an_approving_review_or_an_lgtm_starts_one_lander_for_all_of_them(self):
        code, out, _ = self.tick([reviewed(1), said(2), plain(3), reviewed(4, state="COMMENTED")],
                                 answer=reply("## Result\n#1 merged u1\n#2 merged u2"))
        self.assertEqual(code, 0)
        [argv] = self.landers()
        self.assertIn("- #1:", argv[-1])
        self.assertIn("- #2:", argv[-1])
        self.assertNotIn("- #3:", argv[-1])
        self.assertNotIn("- #4:", argv[-1])
        self.assertEqual([t for t in argv if t.startswith("Bash(factory merge")], ["Bash(factory merge --approved *)"])
        self.assertIn("#1 merged u1\n#2 merged u2\n", out)
        self.assertEqual(len(self.noticed()), 2)  # before the run, and after it

    def test_what_was_noticed_does_not_start_a_second_run(self):
        self.tick([reviewed(1)], answer=reply("#1 stopped ci: red"))
        self.assertEqual(len(self.landers()), 1)
        self.tick([reviewed(1)], at=NOW + 120)
        self.assertEqual(self.landers(), [])

    def busy_lead(self):
        return [DISPATCHER | {"name": SESSION}, lead("t1", status="busy")]

    def test_a_pull_request_noticed_while_its_lead_was_busy_is_run_once_more_when_it_is_free(self):
        self.tick([reviewed(1)], answer=reply("#1 stopped handover: the lead session is busy"), sessions=self.busy_lead())
        self.assertEqual(lander_state()["noticed"], {"1": f"{SHA}:{iso(NOW - 60)}:busy"})
        self.assertEqual(len(self.landers()), 1)
        self.tick([reviewed(1)], at=NOW + 120, sessions=self.busy_lead())
        self.assertEqual(self.landers(), [])  # still busy
        self.tick([reviewed(1)], at=NOW + 240, answer=reply("#1 stopped ci: red"))  # free now (the world's finished lead)
        self.assertEqual(len(self.landers()), 1)
        self.assertEqual(lander_state()["noticed"], {"1": f"{SHA}:{iso(NOW - 60)}:free"})
        self.tick([reviewed(1)], at=NOW + 360)
        self.assertEqual(self.landers(), [])

    def test_a_failed_session_listing_keeps_the_key_busy_and_starts_no_run(self):
        failing = {("claude", "agents"): (1, "", "boom")}
        self.tick([reviewed(1)], answer=reply("#1 stopped handover: busy"), sessions=self.busy_lead())
        self.tick([reviewed(1)], at=NOW + 120, extra=failing)
        self.assertEqual(self.landers(), [])
        self.assertEqual(lander_state()["noticed"], {"1": f"{SHA}:{iso(NOW - 60)}:busy"})
        self.tick([reviewed(1)], at=NOW + 240, answer=reply("#1 stopped ci: red"))  # free now
        self.assertEqual(len(self.landers()), 1)

    def test_a_lead_that_frees_during_the_run_keeps_its_retry(self):
        busy, free = (0, json.dumps(self.busy_lead()), ""), (0, json.dumps(self.busy_lead()[:1]), "")
        # listings: the draft sync's, the run's before and after it
        self.tick([reviewed(1)], answer=reply("#1 stopped handover: busy"), extra={("claude", "agents"): [busy, busy, free]})
        self.assertEqual(lander_state()["noticed"], {"1": f"{SHA}:{iso(NOW - 60)}:busy"})

    def test_a_retry_that_ends_with_the_lead_busy_writes_free_and_starts_no_run_after(self):
        self.tick([reviewed(1)], answer=reply("#1 stopped handover: busy"), sessions=self.busy_lead())
        self.assertEqual(lander_state()["noticed"], {"1": f"{SHA}:{iso(NOW - 60)}:busy"})
        # the retry: the lead is free before it, and the stop it sends wakes the lead
        busy, free = (0, json.dumps(self.busy_lead()), ""), (0, json.dumps(self.busy_lead()[:1]), "")
        self.tick([reviewed(1)], at=NOW + 120, answer=reply("#1 stopped ci: red"),
                  extra={("claude", "agents"): [free, free, free, busy]})
        self.assertEqual(len(self.landers()), 1)
        self.assertEqual(lander_state()["noticed"], {"1": f"{SHA}:{iso(NOW - 60)}:free"})
        self.tick([reviewed(1)], at=NOW + 240)
        self.assertEqual(self.landers(), [])

    def test_a_run_by_hand_while_the_lead_is_busy_does_not_use_up_the_retry(self):
        key = {"1": f"{SHA}:{iso(NOW - 60)}:busy"}
        self.tick([reviewed(1)], answer=reply("#1 stopped handover: busy"), sessions=self.busy_lead())
        self.assertEqual(lander_state()["noticed"], key)
        answers, _, slugs = self.world([reviewed(1)])
        answers[LIST] = (0, json.dumps([reviewed(1)]), "")
        answers[("claude", "-p")] = reply("#1 stopped handover: busy")
        self.cli("lander", "1", answers=answers, sessions=self.busy_lead(), entries=slugs, at=NOW + 60)
        self.assertEqual(len(self.landers()), 1)
        self.assertEqual(lander_state()["noticed"], key)
        self.tick([reviewed(1)], at=NOW + 120, answer=reply("#1 stopped ci: red"))  # the lead is free now
        self.assertEqual(len(self.landers()), 1)
        self.tick([reviewed(1)], at=NOW + 240)
        self.assertEqual(self.landers(), [])

    def test_a_stop_that_wakes_the_lead_gives_two_runs_in_four_ticks_not_four(self):
        busy, free = (0, json.dumps(self.busy_lead()), ""), (0, json.dumps(self.busy_lead()[:1]), "")
        runs = 0
        for later in (0, 120, 240, 360):  # free before the run, busy after it; the retry's notice lists once more
            listings = [free, free, busy] if later == 0 else [free, free, free, busy]
            self.tick([reviewed(1)], at=NOW + later, answer=reply("#1 stopped ci: red"),
                      extra={("claude", "agents"): listings})
            runs += len(self.landers())
        self.assertEqual(runs, 2)

    def test_a_free_pull_request_with_an_unchanged_key_starts_no_run(self):
        self.tick([reviewed(1)], answer=reply("#1 stopped ci: red"))
        self.assertEqual(lander_state()["noticed"], {"1": f"{SHA}:{iso(NOW - 60)}:free"})
        self.tick([reviewed(1)], at=NOW + 120)
        self.tick([reviewed(1)], at=NOW + 240)
        self.assertEqual(self.landers(), [])

    def test_a_stored_two_part_key_starts_no_run_whatever_the_lead_does(self):
        repo = SimpleNamespace(root=ROOT)
        state = factory.read_lander(repo)
        state["noticed"] = {"1": f"{SHA}:{iso(NOW - 60)}"}
        factory.write_lander(repo, state)
        self.tick([reviewed(1)], sessions=self.busy_lead())
        self.tick([reviewed(1)], at=NOW + 120)
        self.assertEqual(self.landers(), [])

    def test_a_changed_head_or_a_newer_approval_does_start_one(self):
        self.tick([reviewed(1)], answer=reply("#1 stopped ci: red"))
        self.tick([reviewed(1, sha="moved")], at=NOW + 120, answer=reply("#1 stopped ci: red"))
        self.assertEqual(len(self.landers()), 1)
        self.tick([reviewed(1, sha="moved", at=NOW + 60)], at=NOW + 240, answer=reply("#1 stopped ci: red"))
        self.assertEqual(len(self.landers()), 1)

    def test_a_head_the_lander_moved_does_not_start_a_second_run(self):
        before, after = [reviewed(1)], [reviewed(1, sha="pushed-by-the-lander")]
        answers, listed, slugs = self.world(before)
        answers[LIST] = [(0, json.dumps(before), ""), (0, json.dumps(after), "")]
        answers[("claude", "-p")] = reply("#1 stopped ci: red")
        self.cli("watch", answers=answers, sessions=listed, entries=slugs)
        self.assertEqual(lander_state()["noticed"], {"1": f"pushed-by-the-lander:{iso(NOW - 60)}:free"})
        self.assertEqual(lander_state()["stopped"]["1"]["head"], "pushed-by-the-lander")
        self.tick(after, at=NOW + 120)
        self.assertEqual(self.landers(), [])

    def test_an_approval_that_arrives_during_a_run_starts_the_next_one(self):
        before = [reviewed(1)]
        after = [reviewed(1, sha="pushed-by-the-lander"), reviewed(2)]  # #2 was approved while the run went on
        answers, listed, slugs = self.world([reviewed(1), reviewed(2)])
        answers[LIST] = [(0, json.dumps(before), ""), (0, json.dumps(after), "")]
        answers[("claude", "-p")] = reply("#1 stopped ci: red")
        self.cli("watch", answers=answers, sessions=listed, entries=slugs)
        self.assertEqual(lander_state()["noticed"], {"1": f"pushed-by-the-lander:{iso(NOW - 60)}:free"})
        self.tick(after, at=NOW + 120, answer=reply("#2 stopped ci: red"))
        [argv] = self.landers()
        self.assertIn("- #2:", argv[-1])
        self.assertNotIn("- #1:", argv[-1])

    def test_a_newer_approval_on_a_given_pull_request_during_the_run_starts_the_next_one(self):
        answers, listed, slugs = self.world([reviewed(1)])
        newer = [reviewed(1, at=NOW + 30)]
        answers[LIST] = [(0, json.dumps([reviewed(1)]), ""), (0, json.dumps(newer), "")]
        answers[("claude", "-p")] = reply("#1 stopped ci: red")
        self.cli("watch", answers=answers, sessions=listed, entries=slugs)
        self.tick(newer, at=NOW + 120, answer=reply("#1 stopped ci: red"))
        self.assertEqual(len(self.landers()), 1)

    def test_a_pull_request_not_in_the_run_keeps_its_noticed_key(self):
        repo = SimpleNamespace(root=ROOT)
        state = factory.read_lander(repo)
        state["noticed"] = {"2": f"{SHA}:{iso(NOW - 500)}"}
        factory.write_lander(repo, state)
        self.tick([reviewed(1), reviewed(2, at=NOW - 500)], answer=reply("#1 stopped ci: red"))
        noticed = lander_state()["noticed"]
        self.assertEqual(noticed["2"], f"{SHA}:{iso(NOW - 500)}")
        self.assertEqual(sorted(noticed), ["1", "2"])
        [argv] = self.landers()
        self.assertNotIn("- #2:", argv[-1])

    def test_a_pull_request_not_in_the_run_that_closed_loses_its_noticed_key(self):
        repo = SimpleNamespace(root=ROOT)
        state = factory.read_lander(repo)
        state["noticed"] = {"9": f"{SHA}:x"}
        factory.write_lander(repo, state)
        self.tick([reviewed(1)], answer=reply("#1 stopped ci: red"))
        self.assertEqual(sorted(lander_state()["noticed"]), ["1"])

    def test_a_pull_request_without_a_handed_over_task_or_with_a_stranger_lgtm_is_not_noticed(self):
        self.tick([reviewed(1), reviewed(2), said(3, association="NONE")], tasks=["t1", "t3"],
                  answer=reply("#1 merged u"))
        [argv] = self.landers()
        self.assertIn("- #1:", argv[-1])
        self.assertNotIn("- #2:", argv[-1])
        self.assertNotIn("- #3:", argv[-1])

    def test_a_held_lock_skips_the_tick_silently_and_records_nothing(self):
        with factory.repo_lock(SimpleNamespace(root=ROOT), "lander"):
            code, out, _ = self.tick([reviewed(1)], answer=reply("#1 merged u"))
        self.assertEqual(code, 0)
        self.assertEqual(self.landers(), [])
        self.assertNotIn("lander", out)
        self.assertEqual(lander_state()["noticed"], {})
        self.tick([reviewed(1)], at=NOW + 120, answer=reply("#1 merged u"))
        self.assertEqual(len(self.landers()), 1)

    def test_a_snapshot_that_fails_prints_one_line_and_the_tick_goes_on(self):
        code, out, _ = self.tick([reviewed(1)], snapshot=(1, "", "boom"), answer=reply("#1 merged u"))
        self.assertEqual(code, 0)
        self.assertIn("notice: gh pr list failed: boom\n", out)
        self.assertEqual(self.landers(), [])
        self.assertIn("nothing new", out)

    def test_it_notices_without_a_dispatcher_and_the_run_tells_the_human(self):
        answers, listed, slugs = self.world([reviewed(1)])
        answers.update({LIST: (0, json.dumps([reviewed(1)]), ""), ("claude", "-p"): reply("#1 stopped ci: red"),
                        ("osascript",): (0, "", "")})
        code, out, _ = self.cli("watch", answers=answers, sessions=listed[1:], entries=slugs)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.landers()), 1)
        self.assertTrue(self.fake.ran("osascript"))
        self.assertNotIn("removing the schedule", out)

    def test_health_runs_at_most_every_thirty_minutes_less_slack(self):
        self.cli("watch", entries=[])
        self.assertTrue(self.fake.ran("claude", "agents"))
        for later, ran in ((120, False), (1739, False), (1740, True)):
            with self.subTest(later):
                self.cli("watch", entries=[], at=NOW + later)
                self.assertEqual(self.fake.ran("claude", "agents"), ran)

    def test_a_tick_with_a_handed_over_task_lists_sessions_once_for_the_drafts_and_starts_nothing_unapproved(self):
        self.tick([plain(1)])  # the first tick also runs health
        self.tick([plain(1)], at=NOW + 120)
        self.assertEqual(sum(c[:2] == ["claude", "agents"] for c in self.fake.calls), 1)
        self.assertEqual(self.landers(), [])

    def test_a_stop_of_the_lander_fires_lgtm_blocked_in_the_health_run(self):
        self.tick([reviewed(1)], answer=reply("#1 stopped ci: red https://x/run/1"))
        self.tick([reviewed(1)], at=NOW + 1800)
        self.assertEqual([r for r in self.records() if "decision" in r][0]["decision"], "escalated")
        self.assertEqual(len([r for r in self.records() if r.get("criterion") == "lgtm-blocked"]), 1)
        self.assertEqual(self.claude_runs(), [])  # the lander told the dispatcher: no second send

    def test_a_draft_is_not_noticed(self):
        self.tick([{**reviewed(1), "isDraft": True}], answer=reply("#1 merged u"))
        self.assertEqual(self.landers(), [])


class EnvTests(unittest.TestCase):
    def printed(self, tty):
        class Out(io.StringIO):
            def isatty(self):
                return tty
        out = Out()
        exports = {"GH_TOKEN": "tok en", "GIT_TERMINAL_PROMPT": "0"}
        with mock.patch.object(factory, "identity_exports", lambda: (exports, None)), redirect_stdout(out):
            self.assertEqual(factory.cmd_env(None), 0)
        return out.getvalue().splitlines()

    def test_the_token_is_masked_on_a_terminal(self):
        self.assertEqual(self.printed(True), ["export GH_TOKEN=***", "export GIT_TERMINAL_PROMPT=0"])

    def test_the_token_is_printed_when_stdout_is_not_a_terminal(self):
        self.assertEqual(self.printed(False), ["export GH_TOKEN='tok en'", "export GIT_TERMINAL_PROMPT=0"])


if __name__ == "__main__":
    unittest.main()
