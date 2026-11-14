import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import ROOT, WT, FakeRun, factory, invoke, repo_answers, task_entry
from test_health import DISPATCHER, NOW, Harness, check, iso, pic, row

SHA = "sha-1"
GREEN = [{"name": "test", "bucket": "pass"}]
SESSION = "dispatcher-repo"
COMMENTS = "repos/{owner}/{repo}/issues/%d/comments"
ACTIVITY = "repos/{owner}/{repo}/activity"
REVIEWS = "repos/{owner}/{repo}/pulls/%d/reviews"
AUTHOR = {"login": "andrei", "is_bot": False}


def pr(n, base="main", draft=False, fork=False, sha=SHA, author=AUTHOR):
    return {"number": n, "url": f"https://x/pull/{n}", "isDraft": draft, "headRefName": f"worktree-t{n}",
            "headRefOid": sha, "baseRefName": base, "isCrossRepository": fork, "author": author}


def raw(x):
    return x if isinstance(x, tuple) else (0, json.dumps(x), "")


def per(value, n):
    return value[n] if isinstance(value, dict) else value


def comment(body="lgtm", at=NOW - 60, login="andrei", n=1, type="User"):
    return {"body": body, "created_at": iso(at), "html_url": f"https://x/pull/{n}#c{at}",
            "user": {"login": login, "type": type}}


def review(state="APPROVED", at=NOW - 60, login="dev", sha=SHA, n=1, type="User"):
    return {"state": state, "submitted_at": iso(at), "commit_id": sha, "html_url": f"https://x/pull/{n}#r{at}",
            "user": {"login": login, "type": type}}


class World(Harness):
    """Open pull requests, one task per head branch, in a faked `gh`."""

    CRITERIA = ["lgtm-blocked", "base-merged"]

    def world(self, prs, *, checks=GREEN, mergeable="MERGEABLE", state="handed-over", tasks=None, local=SHA,
              extra=None):
        slugs = [f"t{p['number']}" for p in prs if not p["isCrossRepository"]] if tasks is None else tasks
        states = {s: (state[s] if isinstance(state, dict) else state) for s in slugs}
        self.seed("tasks", [{"repo": ROOT, "slug": s, "state": st, "ts": iso(NOW - 3600)} for s, st in states.items()])
        answers = {
            ("git", "status"): (0, "", ""), ("git", "rev-list"): (0, "0\t1\n", ""), ("git", "fetch"): (0, "", ""),
            ("git", "merge-base", "--is-ancestor"): (0, "", ""),
            ("git", "log"): (0, f"{NOW} {local}\n", ""),
            ("gh", "pr", "list", "--state", "open"): (0, json.dumps(prs), ""),
            ("gh", "pr", "edit"): (0, "", ""), ("gh", "pr", "ready"): (0, "", ""), ("gh", "pr", "merge"): (0, "", ""),
        }
        for s in slugs:
            n = int(s[1:])
            base = next(p["baseRefName"] for p in prs if p["number"] == n)
            answers[("gh", "pr", "list", "--head", f"worktree-{s}")] = (0, json.dumps([
                {"state": "OPEN", "url": f"https://x/pull/{n}", "headRefOid": SHA, "baseRefName": base}]), "")
        for p in prs:
            n = str(p["number"])
            answers[("gh", "pr", "checks", n)] = raw(per(checks, p["number"]))
            m = per(mergeable, p["number"])
            answers[("gh", "pr", "view", n)] = raw({"mergeable": m}) if isinstance(m, str) else (
                [raw({"mergeable": x}) for x in m] if isinstance(m, list) else m)
        answers.update(extra or {})
        finished = [{"id": f"id-{s}", "sessionId": f"sid-{s}", "cwd": f"{WT}/{s}", "kind": "background",
                     "status": "idle", "state": "done"} for s in slugs]
        return answers, [DISPATCHER | {"name": SESSION}, *finished], slugs

    def merge(self, *argv, prs, **kw):
        answers, sessions, slugs = self.world(prs, **{k: kw.pop(k) for k in list(kw) if k in (
            "checks", "mergeable", "state", "tasks", "local", "extra")})
        return self.cli("merge", *argv, answers=answers, sessions=sessions, entries=slugs)

    def gh_mutations(self):
        return [c[1:] for c in self.fake.calls if c[:3] in (["gh", "pr", "edit"], ["gh", "pr", "ready"],
                                                              ["gh", "pr", "merge"])]

    def pr_calls(self):
        return [c[1:] for c in self.fake.calls if c[:2] == ["gh", "pr"] and c[2] in
                ("checks", "view", "edit", "ready", "merge")]


class MergeCommandTests(World):
    def test_one_pull_request_merges_and_cleans(self):
        code, out, _ = self.merge("1", prs=[pr(1)])
        self.assertEqual(code, 0)
        self.assertEqual(self.pr_calls(), [
            ["pr", "checks", "1", "--json", "name,bucket"], ["pr", "view", "1", "--json", "mergeable"],
            ["pr", "merge", "1", "--merge", "--match-head-commit", SHA]])
        self.assertIn("#1 merged https://x/pull/1\n", out)
        self.assertIn("kept t1:", out)
        self.assertEqual(self.claude_runs(), [])

    def test_a_draft_is_marked_ready_first(self):
        code, _, _ = self.merge("1", prs=[pr(1, draft=True)])
        self.assertEqual(code, 0)
        self.assertEqual([c[:2] for c in self.pr_calls()][-2:], [["pr", "ready"], ["pr", "merge"]])

    def test_a_stack_named_in_reverse_merges_bottom_up_and_retargets(self):
        code, out, _ = self.merge("2", "1", prs=[pr(1), pr(2, base="worktree-t1")])
        self.assertEqual(code, 0)
        self.assertEqual(self.pr_calls(), [
            ["pr", "checks", "1", "--json", "name,bucket"], ["pr", "view", "1", "--json", "mergeable"],
            ["pr", "merge", "1", "--merge", "--match-head-commit", SHA],
            ["pr", "checks", "2", "--json", "name,bucket"], ["pr", "edit", "2", "--base", "main"],
            ["pr", "view", "2", "--json", "mergeable"], ["pr", "merge", "2", "--merge", "--match-head-commit", SHA]])
        self.assertLess(out.index("#1 merged"), out.index("#2 merged"))

    def test_a_base_merged_earlier_is_retargeted(self):
        merged = (0, json.dumps([{"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), "")
        code, _, _ = self.merge("2", prs=[pr(2, base="worktree-t1")],
                                extra={("gh", "pr", "list", "--head", "worktree-t1"): merged})
        self.assertEqual(code, 0)
        self.assertEqual([c[:2] for c in self.pr_calls()], [["pr", "checks"], ["pr", "edit"], ["pr", "view"],
                                                            ["pr", "merge"]])

    def test_a_base_that_is_open_and_not_selected_is_a_stop(self):
        code, out, _ = self.merge("2", prs=[pr(1), pr(2, base="worktree-t1")])
        self.assertEqual(code, 1)
        self.assertIn("#2 stopped: its base worktree-t1 is not main", out)
        self.assertEqual(self.gh_mutations(), [])

    def test_a_base_merged_into_another_branch_is_a_stop(self):
        other = (0, json.dumps([{"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "dev"}]), "")
        code, out, _ = self.merge("2", prs=[pr(2, base="worktree-t1")],
                                  extra={("gh", "pr", "list", "--head", "worktree-t1"): other})
        self.assertEqual(code, 1)
        self.assertIn("#2 stopped: its base", out)

    def test_each_blocker(self):
        checks = lambda *buckets: [{"name": f"c{i}", "bucket": b} for i, b in enumerate(buckets)]
        cases = {
            "fork": (dict(prs=[pr(1, fork=True)]), "another repository"),
            "no task row": (dict(prs=[pr(1)], tasks=[]), "no task worktree has its head branch"),
            "motion": (dict(prs=[pr(1)], state="working"), "phase motion (declared working)"),
            "blocked": (dict(prs=[pr(1)], state="blocked"), "phase blocked (declared blocked)"),
            "local head differs": (dict(prs=[pr(1)], local="other"), "local commits that are not on the pull request"),
            "no checks": (dict(prs=[pr(1)], checks=[]), "no check passed"),
            "gh checks printed nothing": (dict(prs=[pr(1)], checks=(1, "", "no checks reported")),
                                          "no checks reported"),
            "failed": (dict(prs=[pr(1)], checks=checks("pass", "fail")), "failed or cancelled checks: c1"),
            "cancelled": (dict(prs=[pr(1)], checks=checks("pass", "cancel")), "failed or cancelled checks: c1"),
            "only skipped": (dict(prs=[pr(1)], checks=checks("skipping", "skipping")), "no check passed"),
            "pending": (dict(prs=[pr(1)], checks=checks("pass", "pending")), "checks are still running"),
            "pending, none passed": (dict(prs=[pr(1)], checks=checks("pending")), "checks are still running"),
            "pending and failed": (dict(prs=[pr(1)], checks=checks("fail", "pending")), "failed or cancelled"),
            "base unreadable": (dict(prs=[pr(2, base="worktree-t1")],
                                     extra={("gh", "pr", "list", "--head", "worktree-t1"): (1, "", "boom")}),
                                "cannot read the pull request of its base worktree-t1"),
            "conflicting": (dict(prs=[pr(1)], mergeable="CONFLICTING"), "conflicts with its base"),
            "never computed": (dict(prs=[pr(1)], mergeable="UNKNOWN"), "not computed"),
            "view fails": (dict(prs=[pr(1)], mergeable=(1, "", "boom")), "boom"),
            "retarget fails": (dict(prs=[pr(2, base="worktree-t1")],
                                    extra={("gh", "pr", "list", "--head", "worktree-t1"): (0, json.dumps([
                                        {"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), ""),
                                           ("gh", "pr", "edit"): (1, "", "no edit")}), "gh pr edit 2 --base main: no edit"),
            "ready fails": (dict(prs=[pr(1, draft=True)], extra={("gh", "pr", "ready"): (1, "", "no ready")}),
                            "gh pr ready 1: no ready"),
            "merge fails": (dict(prs=[pr(1)], extra={("gh", "pr", "merge"): (1, "", "head moved")}),
                            f"gh pr merge 1 --merge --match-head-commit {SHA}: head moved"),
        }
        for label, (kw, why) in cases.items():
            with self.subTest(label), mock.patch.object(factory.time, "sleep"):
                self.setUp()
                number = kw["prs"][0]["number"]
                code, out, _ = self.merge(str(number), **kw)
                self.assertEqual(code, 1)
                self.assertIn(f"#{number} stopped: ", out)
                self.assertIn(why, out)
                self.assertNotIn("merged https", out)
                self.assertNotIn("kept", out)
                if label not in ("retarget fails", "ready fails", "merge fails"):
                    self.assertEqual(self.gh_mutations(), [])

    def test_unknown_five_times_waits_between_asks(self):
        with mock.patch.object(factory.time, "sleep") as sleep:
            code, out, _ = self.merge("1", prs=[pr(1)], mergeable="UNKNOWN")
        self.assertEqual(code, 1)
        self.assertEqual(sum(c[:3] == ["gh", "pr", "view"] for c in self.fake.calls), 5)
        self.assertEqual(sleep.call_args_list, [mock.call(3)] * 4)
        self.assertIn("#1 stopped: GitHub has not computed", out)

    def test_unknown_then_mergeable_merges(self):
        with mock.patch.object(factory.time, "sleep") as sleep:
            code, out, _ = self.merge("1", prs=[pr(1)], mergeable=["UNKNOWN", "MERGEABLE"])
        self.assertEqual(code, 0)
        self.assertEqual(sleep.call_count, 1)
        self.assertIn("#1 merged", out)

    def test_a_stop_ends_the_run(self):
        prs = [pr(1), pr(2, base="worktree-t1"), pr(3, base="worktree-t2")]
        code, out, _ = self.merge("1", "2", "3", prs=prs, checks={1: GREEN, 2: [{"name": "t", "bucket": "fail"}], 3: GREEN})
        self.assertEqual(code, 1)
        self.assertIn("#1 merged", out)
        self.assertIn("#2 stopped: failed or cancelled checks: t\n#3 not attempted\n", out)
        self.assertEqual([c[1] for c in self.gh_mutations()], ["merge"])
        self.assertEqual(self.gh_mutations()[0][2], "1")
        self.assertIn("kept t1:", out)

    def test_nothing_merged_runs_no_clean(self):
        code, out, _ = self.merge("1", "2", prs=[pr(1), pr(2)], checks=[])
        self.assertEqual(code, 1)
        self.assertIn("#2 not attempted", out)
        self.assertNotIn("kept", out)

    def test_finished_probes_in_the_worktree_do_not_skip_a_task_for_its_phase(self):
        answers, sessions, slugs = self.world([pr(1)])
        probes = [{"id": f"p{i}", "name": f"probe{i}-t1", "kind": "background", "state": state, "cwd": f"{WT}/t1"}
                  for i, state in enumerate(("done", "stopped"))]
        code, out, _ = self.cli("merge", "1", answers=answers, sessions=[*sessions, *probes], entries=slugs)
        self.assertEqual(code, 0)
        self.assertNotIn("skipped", out)
        self.assertIn("#1 merged", out)

    def test_all_skips_tasks_not_handed_over(self):
        code, out, _ = self.merge("--all", prs=[pr(1), pr(2), pr(3, base="worktree-t2")],
                                  state={"t1": "handed-over", "t2": "working", "t3": "handed-over"},
                                  tasks=["t1", "t2", "t3"])
        self.assertEqual(code, 0)
        self.assertIn("#2 skipped: task t2 is in phase motion (declared working)\n", out)
        self.assertIn("#3 skipped: its base worktree-t2 was skipped\n", out)
        self.assertEqual([c[1] for c in self.gh_mutations()], ["merge"])
        self.assertNotIn("stopped", out)

    def test_one_command_merges_one_pull_request_per_base_and_what_is_stacked_on_it(self):
        code, out, _ = self.merge("1", "2", prs=[pr(1), pr(2)])
        self.assertEqual(code, 1)
        self.assertIn("#1 merged", out)
        self.assertIn("#2 stopped: " + ("its base has moved since its head: bring it up to date (factory land t2) so its checks run on the combination"), out)
        self.assertEqual([c[2] for c in self.gh_mutations() if c[1] == "merge"], ["1"])
        self.assertFalse(any(c[:4] == ["gh", "pr", "checks", "2"] for c in self.fake.calls))

    def test_a_named_number_that_is_not_open_exits_2(self):
        code, _, err = self.merge("9", prs=[pr(1)])
        self.assertEqual(code, 2)
        self.assertIn("#9 is not an open pull request", err)
        self.assertEqual(self.gh_mutations(), [])

    def test_usage(self):
        for argv in ((), ("1", "--all")):
            with self.subTest(argv):
                self.assertEqual(self.merge(*argv, prs=[pr(1)])[0], 2)
                self.assertEqual(self.gh_mutations(), [])

    def test_sessions_unlistable_exits_1_with_nothing_done(self):
        code, _, err = self.merge("1", prs=[pr(1)], extra={("claude", "agents"): (1, "", "boom")})
        self.assertEqual(code, 1)
        self.assertIn("nothing done", err)
        self.assertEqual(self.pr_calls(), [])

    def test_gh_pr_list_failing_exits_1_with_nothing_done(self):
        code, _, err = self.merge("1", prs=[pr(1)], extra={("gh", "pr", "list", "--state", "open"): (1, "", "boom")})
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.assertEqual(self.pr_calls(), [])


class DryRunTests(World):
    def test_dry_run_changes_nothing_and_continues_past_a_stop(self):
        prs = [pr(1, draft=True), pr(2, base="worktree-t1"), pr(3), pr(4)]
        code, out, _ = self.merge("--dry-run", "--all", prs=prs,
                                  checks={1: GREEN, 2: GREEN, 3: [{"name": "t", "bucket": "fail"}], 4: GREEN})
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            "#1 would merge (draft: would be marked ready)",
            "#2 would merge (retarget onto main first; mergeability is checked after that)",
            "#3 would stop: its base has moved since its head: bring it up to date (factory land t3) so its checks run on the combination",
            "#4 would stop: its base has moved since its head: bring it up to date (factory land t4) so its checks run on the combination"])
        self.assertEqual(self.gh_mutations(), [])
        self.assertFalse(any(c[:3] == ["gh", "pr", "view"] and c[3] == "2" for c in self.fake.calls))
        self.assertFalse(self.fake.ran("claude", "rm"))

    def test_dry_run_exits_0_when_all_would_merge(self):
        code, out, _ = self.merge("1", "--dry-run", prs=[pr(1)])
        self.assertEqual((code, out), (0, "#1 would merge\n"))


def approval_gh(pages, permissions, asked=None):
    def gh(kind, *args):
        if asked is not None:
            asked.append(args)
        if "--slurp" in args:
            return pages
        return {"permission": permissions[args[1].split("/")[4]]}
    return gh


class ApprovalTests(unittest.TestCase):
    def approval(self, *comments, permissions=None, asked=None):
        p = {"gh": approval_gh([list(comments)], permissions or {"andrei": "admin"}, asked), "lgtm_from_author": True}
        return factory.newest_approval(p, pr(1), {})

    def test_body_variants(self):
        cases = {"lgtm": True, " LGTM\n": True, "Lgtm": True, "lgtm!": False, "lgtm, thanks": False,
                 "not lgtm": False, "": False}
        for body, approved in cases.items():
            with self.subTest(body=body):
                self.assertEqual(bool(self.approval(comment(body))), approved)

    def test_a_commenter_without_write_access_is_ignored(self):
        self.assertIsNone(self.approval(comment(login="octocat"), permissions={"octocat": "read"}))
        self.assertIsNone(self.approval(comment(login="octocat"), permissions={"octocat": "none"}))
        self.assertTrue(self.approval(comment(login="dev"), permissions={"dev": "write"}))

    def test_the_newest_approval_wins_over_pages_and_order(self):
        p = {"gh": approval_gh([[comment(at=NOW - 10)], [comment(at=NOW - 500), comment("hello", at=NOW)]],
                               {"andrei": "admin"}), "lgtm_from_author": True}
        self.assertEqual(factory.newest_approval(p, pr(1), {})["created_at"], iso(NOW - 10))

    def test_permission_is_asked_once_per_login(self):
        asked = []
        self.approval(comment(at=NOW - 3), comment(at=NOW - 2), comment(at=NOW - 1), asked=asked)
        self.assertEqual(sum("collaborators" in a[1] for a in asked), 1)
        self.assertIn(("api", "repos/{owner}/{repo}/collaborators/andrei/permission"), asked)

    def test_permission_is_not_asked_for_other_comments(self):
        asked = []
        self.approval(comment("looks good"), asked=asked)
        self.assertEqual(sum("collaborators" in a[1] for a in asked), 0)

    def test_an_unexpected_shape_is_not_evaluated(self):
        p = {"gh": lambda kind, *args: [{"body": "lgtm"}]}
        with self.assertRaises(factory.NotEvaluated):
            factory.newest_approval(p, pr(1), {})

    def stale(self, approved, activity):
        p = {"gh": lambda kind, *args: activity}
        return factory.stale_approval(p, pr(1), comment(at=approved), [SHA])

    def test_the_approval_against_the_activity_timestamp(self):
        change = {"after": SHA, "timestamp": iso(NOW - 100)}
        self.assertIsNone(self.stale(NOW - 99, [change]))
        self.assertEqual(self.stale(NOW - 100, [change])[0], "stale")
        self.assertEqual(self.stale(NOW - 101, [change])[0], "stale")

    def test_the_activity_must_name_the_head(self):
        self.assertEqual(self.stale(NOW, [])[0], "head-time")
        self.assertEqual(self.stale(NOW, [{"after": "other", "timestamp": iso(NOW - 100)}])[0], "head-time")
        self.assertEqual(self.stale(NOW, [{"after": SHA}])[0], "head-time")


class ApprovedTests(World):
    """`factory merge --approved`: GitHub's own records must hold a valid approval, read at that moment."""

    def approved(self, prs, comments=None, *argv, activity=None, reviews=None, **kw):
        answers, sessions, slugs = self.world(prs, **kw)
        for p in prs:
            n = p["number"]
            answers[("gh", "api", "--paginate", "--slurp", REVIEWS % n)] = raw([(reviews or {}).get(n, [])])
            answers[("gh", "api", "--paginate", "--slurp", COMMENTS % n)] = raw([(comments or {}).get(n, [])])
            answers[("gh", "api", "-X", "GET", ACTIVITY, "-f", f"ref=refs/heads/worktree-t{n}")] = raw(
                (activity or {}).get(n, [{"after": SHA, "timestamp": iso(NOW - 3600)}]))
        for login, permission in {"andrei": "admin", "dev": "write", "reader": "read"}.items():
            answers[("gh", "api", f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission")] = raw(
                {"permission": permission})
        answers.update(kw.get("extra") or {})
        return self.cli("merge", "--approved", *(argv or [str(p["number"]) for p in prs]), answers=answers,
                        sessions=sessions, entries=slugs)

    def merges(self):
        return [c[2] for c in self.gh_mutations() if c[1] == "merge"]

    def test_a_review_on_the_head_merges(self):
        code, out, _ = self.approved([pr(1)], reviews={1: [review()]})
        self.assertEqual(code, 0)
        self.assertIn("#1 merged https://x/pull/1\n", out)
        self.assertEqual(self.merges(), ["1"])
        self.assertFalse(any(COMMENTS % 1 in c or ACTIVITY in c for c in self.fake.calls))
        self.assertEqual(self.claude_runs(), [])

    def test_an_lgtm_newer_than_the_push_merges(self):
        code, out, _ = self.approved([pr(1)], {1: [comment()]})
        self.assertEqual((code, self.merges()), (0, ["1"]))
        self.assertIn("#1 merged", out)

    def test_nothing_that_approves_is_a_stop_with_nothing_changed(self):
        cases = {"no review, no lgtm": ({}, {}),
                 "an lgtm that is not exactly lgtm": ({1: [comment("lgtm, thanks")]}, {}),
                 "a review that only comments": ({}, {1: [review("COMMENTED")]}),
                 "the author's own review": ({}, {1: [review(login="andrei")]}),
                 "a login without write access": ({}, {1: [review(login="reader")]}),
                 "a bot": ({}, {1: [review(login="factory[bot]", type="Bot")]}),
                 "an lgtm from a login without write access": ({1: [comment(login="reader")]}, {})}
        for label, (comments, reviews) in cases.items():
            with self.subTest(label):
                self.setUp()
                code, out, _ = self.approved([pr(1)], comments, reviews=reviews)
                self.assertEqual(code, 1)
                self.assertIn("#1 stopped: not approved on GitHub", out)
                self.assertEqual(self.gh_mutations(), [])
                self.assertFalse(any("factory[bot]" in part for c in self.fake.calls for part in c))

    def test_a_review_on_an_older_commit_is_stale(self):
        code, out, _ = self.approved([pr(1)], reviews={1: [review(sha="old")]})
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: its head changed after the approval; a fresh approval is needed", out)
        self.assertEqual(self.gh_mutations(), [])

    def test_an_lgtm_older_than_the_push_is_stale(self):
        code, out, _ = self.approved([pr(1)], {1: [comment(at=NOW - 7200)]})
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: its head changed after the lgtm; a fresh lgtm is needed", out)
        self.assertEqual(self.gh_mutations(), [])

    def test_an_lgtm_rescues_a_review_on_an_older_commit_and_a_review_on_the_head_needs_none(self):
        code, out, _ = self.approved([pr(1)], {1: [comment()]}, reviews={1: [review(sha="old")]})
        self.assertEqual((code, self.merges()), (0, ["1"]))
        self.setUp()
        self.approved([pr(1)], {1: [comment()]}, reviews={1: [review()]})
        self.assertFalse(any(ACTIVITY in c for c in self.fake.calls))

    def test_a_later_verdict_withdraws_an_approval_and_a_comment_does_not(self):
        cases = {"changes requested": ([review(at=NOW - 120), review("CHANGES_REQUESTED")], False),
                 "dismissed": ([review(at=NOW - 120), review("DISMISSED")], False),
                 "listed out of order": ([review("CHANGES_REQUESTED"), review(at=NOW - 120)], False),
                 "commented": ([review(at=NOW - 120), review("COMMENTED")], True)}
        for label, (reviews, merged) in cases.items():
            with self.subTest(label):
                self.setUp()
                self.approved([pr(1)], reviews={1: reviews})
                self.assertEqual(self.merges(), ["1"] if merged else [])

    def test_lgtm_from_author_false_leaves_the_author_out_only(self):
        (self.store / "settings.json").write_text('{"lgtm_from_author": false}')
        self.approved([pr(1)], {1: [comment()]})
        self.assertEqual(self.gh_mutations(), [])
        self.approved([pr(1)], {1: [comment(login="dev")]})
        self.assertEqual(self.merges(), ["1"])

    def test_a_fork_is_a_stop_before_any_approval_is_read(self):
        code, out, _ = self.approved([pr(1, fork=True)], {1: [comment()]})
        self.assertIn("#1 stopped: its head is in another repository", out)
        self.assertFalse(any(ACTIVITY in c for c in self.fake.calls))

    def test_the_permission_of_a_login_is_asked_once_across_pull_requests(self):
        self.approved([pr(1), pr(2)], {1: [comment()], 2: [comment(n=2)]})
        self.assertEqual(sum(c[:3] == ["gh", "api", "repos/{owner}/{repo}/collaborators/andrei/permission"]
                             for c in self.fake.calls), 1)
        self.assertEqual(self.merges(), ["1"])

    def test_every_other_check_still_applies(self):
        code, out, _ = self.approved([pr(1)], {1: [comment()]}, checks=[{"name": "t", "bucket": "fail"}])
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: failed or cancelled checks: t", out)
        self.assertEqual(self.gh_mutations(), [])

    def test_a_failing_reviews_call_is_a_stop_that_changes_nothing(self):
        code, out, _ = self.approved([pr(1)], {1: [comment()]},
                                     extra={("gh", "api", "--paginate", "--slurp", REVIEWS % 1): (1, "", "boom")})
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: gh api --paginate failed: boom", out)
        self.assertEqual(self.gh_mutations(), [])

    def test_a_stack_merges_bottom_up(self):
        self.approved([pr(2, base="worktree-t1"), pr(1)], {1: [comment()], 2: [comment(n=2)]})
        self.assertEqual([c[:2] for c in self.pr_calls() if c[1] not in ("view", "checks")],
                         [["pr", "merge"], ["pr", "edit"], ["pr", "merge"]])

    def test_all_skips_what_is_not_approved_and_merges_the_rest(self):
        code, out, _ = self.approved([pr(1), pr(2), pr(3)], {1: [comment()], 3: [comment(n=3)]}, "--all")
        self.assertEqual(code, 1)
        self.assertIn("#2 skipped: not approved on GitHub", out)
        self.assertIn("#3 stopped: its base has moved since its head: bring it up to date (factory land t3) so its checks run on the combination", out)
        self.assertEqual(self.merges(), ["1"])

    def test_a_draft_is_not_merged_on_an_approval(self):
        code, out, _ = self.approved([pr(1, draft=True)], {1: [comment()]}, reviews={1: [review()]})
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: not approved on GitHub: it is a draft, and only a pull request that is ready "
                      "for review is merged on an approval", out)
        self.assertEqual(self.gh_mutations(), [])
        code, out, _ = self.approved([pr(1, draft=True), pr(2)], {}, "--all", reviews={1: [review()], 2: [review(n=2)]})
        self.assertEqual(code, 0)
        self.assertIn("#1 skipped: not approved on GitHub: it is a draft", out)
        self.assertEqual(self.merges(), ["2"])

    def test_all_skips_a_stale_approval_and_goes_on(self):
        code, out, _ = self.approved([pr(1), pr(2)], {1: [comment(at=NOW - 7200)], 2: [comment(n=2)]}, "--all")
        self.assertEqual(code, 0)
        self.assertIn("#1 skipped: its head changed after the lgtm", out)
        self.assertEqual(self.merges(), ["2"])

    def test_a_named_number_that_is_not_approved_stops_the_run(self):
        code, out, _ = self.approved([pr(1), pr(2)], {2: [comment(n=2)]}, "1", "2")
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: not approved on GitHub", out)
        self.assertIn("#2 not attempted", out)

    def test_a_head_behind_its_base_is_refused_in_both_modes(self):
        behind = {("git", "merge-base", "--is-ancestor"): (1, "", "")}
        why = ("#1 stopped: its base has moved since its head: bring it up to date (factory land t1) so its checks "
               "run on the combination")
        code, out, _ = self.approved([pr(1)], {1: [comment()]}, extra=behind)
        self.assertEqual(code, 1)
        self.assertIn(why, out)
        self.assertEqual(self.gh_mutations(), [])
        code, out, _ = self.merge("1", prs=[pr(1)], extra=behind)
        self.assertEqual(code, 1)
        self.assertIn(why, out)
        self.assertEqual(self.gh_mutations(), [])
        self.assertEqual(self.pr_calls(), [])

    def test_behind_is_a_stop_not_a_skip_with_all_and_the_rest_is_not_attempted(self):
        code, out, _ = self.approved([pr(1), pr(2)], {1: [comment()], 2: [comment(n=2)]}, "--all",
                                     extra={("git", "merge-base", "--is-ancestor"): (1, "", "")})
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: its base has moved", out)
        self.assertIn("#2 not attempted", out)

    def test_the_default_branch_is_the_one_compared_when_the_base_has_merged(self):
        merged = (0, json.dumps([{"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), "")
        self.merge("2", prs=[pr(2, base="worktree-t1")], extra={("gh", "pr", "list", "--head", "worktree-t1"): merged})
        self.assertIn(["git", "merge-base", "--is-ancestor", "origin/main", SHA], self.fake.calls)

    def test_git_that_cannot_answer_is_pending(self):
        for label, extra in {"fetch": {("git", "fetch"): (1, "", "no network")},
                             "merge-base": {("git", "merge-base", "--is-ancestor"): (128, "", "bad object")}}.items():
            with self.subTest(label):
                self.setUp()
                code, out, _ = self.merge("1", prs=[pr(1)], extra=extra)
                self.assertEqual(code, 1)
                self.assertIn("#1 stopped: ", out)
                self.assertEqual(self.gh_mutations(), [])

    def test_one_fetch_serves_the_whole_command(self):
        self.approved([pr(1), pr(2)], {1: [comment()], 2: [comment(n=2)]})
        self.assertEqual(sum(c[:2] == ["git", "fetch"] for c in self.fake.calls), 1)

    def test_all_skips_a_pull_request_stacked_on_a_skipped_one(self):
        code, out, _ = self.approved([pr(1), pr(2, base="worktree-t1"), pr(3)], {3: [comment(n=3)]}, "--all",
                                     reviews={2: [review(n=2)]})
        self.assertEqual(code, 0)
        self.assertIn("#1 skipped: not approved on GitHub", out)
        self.assertIn("#2 skipped: its base worktree-t1 was skipped", out)
        self.assertEqual(self.merges(), ["3"])

    def test_a_dry_run_reads_the_approval_too(self):
        code, out, _ = self.approved([pr(1)], {}, "--dry-run", "1")
        self.assertEqual(code, 1)
        self.assertIn("#1 would stop: not approved on GitHub", out)

    def test_the_heads_of_a_clean_merge_of_the_base_carry_the_approval(self):
        heads = mock.patch.object(factory, "approved_heads", lambda p, pr: [SHA, "before"])
        with heads:
            code, out, _ = self.approved([pr(1)], reviews={1: [review(sha="before")]})
        self.assertEqual((code, self.merges()), (0, ["1"]))
        self.setUp()
        with heads:
            self.approved([pr(1)], {1: [comment(at=NOW - 1800)]}, reviews={1: [review(sha="other")]},
                          activity={1: [{"after": SHA, "timestamp": iso(NOW - 60)},
                                        {"after": "before", "timestamp": iso(NOW - 3600)},
                                        {"after": "other", "timestamp": iso(NOW - 7200)}]})
        self.assertEqual(self.merges(), ["1"])

    def test_plain_merge_reads_no_approval(self):
        self.merge("1", prs=[pr(1)])
        self.assertFalse(any(c[:2] == ["gh", "api"] for c in self.fake.calls))
        self.assertEqual(self.merges(), ["1"])


class StaleTests(unittest.TestCase):
    def stale(self, approved, activity, heads=(SHA,)):
        p = {"gh": lambda kind, *args: activity}
        return factory.stale_approval(p, pr(1), comment(at=approved), list(heads))

    def test_the_lgtm_is_newer_than_the_oldest_of_the_pushes_that_kept_an_approved_head(self):
        run = [{"after": SHA, "timestamp": iso(NOW - 60)}, {"after": "before", "timestamp": iso(NOW - 3600)},
               {"after": "elsewhere", "timestamp": iso(NOW - 7200)}]
        self.assertIsNone(self.stale(NOW - 1800, run, (SHA, "before")))
        self.assertEqual(self.stale(NOW - 3600, run, (SHA, "before"))[0], "stale")
        self.assertEqual(self.stale(NOW - 1800, run)[0], "stale")
        self.assertIsNone(self.stale(NOW - 7000, run, (SHA, "before", "elsewhere")))

    def test_a_push_in_between_ends_the_run(self):
        run = [{"after": SHA, "timestamp": iso(NOW - 60)}, {"after": "other", "timestamp": iso(NOW - 600)},
               {"after": "before", "timestamp": iso(NOW - 3600)}]
        self.assertEqual(self.stale(NOW - 300, run, (SHA, "before"))[0], "stale")
        self.assertIsNone(self.stale(NOW - 30, run, (SHA, "before")))


class HeadsTests(unittest.TestCase):
    """`approved_heads` against real git: a bare remote, a clone, a branch, a base that moved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.remote, self.work = root / "remote.git", root / "work"
        patch = mock.patch.object(factory, "identity_exports", lambda: ({}, None))
        patch.start()
        self.addCleanup(patch.stop)
        self.git("init", "--bare", "-b", "main", str(self.remote), cwd=root)
        self.git("clone", str(self.remote), str(self.work), cwd=root)
        for key, value in (("user.name", "t"), ("user.email", "t@x"), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        self.commit("a.txt", "a\n")
        self.git("push", "origin", "HEAD:main")
        self.git("checkout", "-b", "feature")
        self.commit("f.txt", "f\n")
        self.git("push", "origin", "feature")
        self.git("checkout", "main")
        self.commit("b.txt", "b\n")
        self.git("push", "origin", "main")
        self.git("checkout", "feature")
        self.repo = SimpleNamespace(root=str(self.work), remote="origin", default="main",
                                    git=lambda *a, path=None: factory.run(["git", *a], cwd=str(self.work)))

    def git(self, *args, cwd=None):
        done = subprocess.run(["git", *args], cwd=cwd or self.work, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def commit(self, name, text):
        (self.work / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-m", name)

    def head(self):
        return self.git("rev-parse", "HEAD")

    def heads(self, base="main"):
        return factory.approved_heads({"repo": self.repo}, {"headRefOid": self.head(), "baseRefName": base})

    def test_a_head_that_is_no_merge_is_the_only_head(self):
        self.assertEqual(self.heads(), [self.head()])

    def test_a_clean_merge_of_the_base_steps_back_to_the_first_parent(self):
        before = self.head()
        self.git("merge", "--no-edit", "origin/main")
        self.assertEqual(self.heads(), [self.head(), before])

    def test_two_merges_of_the_base_in_a_row(self):
        first = self.head()
        self.git("merge", "--no-edit", "origin/main")
        second = self.head()
        self.git("checkout", "main")
        self.commit("c.txt", "c\n")
        self.git("push", "origin", "main")
        self.git("checkout", "feature")
        self.git("merge", "--no-edit", "main")
        self.assertEqual(self.heads(), [self.head(), second, first])

    def test_a_commit_on_top_of_the_merge_makes_the_approval_stale(self):
        self.git("merge", "--no-edit", "origin/main")
        self.commit("g.txt", "g\n")
        self.assertEqual(self.heads(), [self.head()])

    def test_a_merge_commit_whose_tree_differs_from_gits_own_is_not_stepped_back_through(self):
        self.git("merge", "--no-edit", "--no-commit", "origin/main")
        (self.work / "f.txt").write_text("changed in the merge\n")
        self.git("add", "f.txt")
        self.git("commit", "-m", "merge with a change")
        self.assertEqual(self.heads(), [self.head()])

    def test_a_second_parent_that_is_not_on_the_default_branch_is_not_stepped_back_through(self):
        self.git("checkout", "-b", "other")
        self.commit("o.txt", "o\n")
        self.git("checkout", "feature")
        self.commit("g.txt", "g\n")
        self.git("merge", "--no-ff", "--no-edit", "other")
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        self.assertEqual(self.heads(), [self.head()])

    def test_a_resolved_conflict_is_not_stepped_back_through(self):
        self.git("checkout", "main")
        self.commit("f.txt", "main's f\n")
        self.git("push", "origin", "main")
        self.git("checkout", "feature")
        done = subprocess.run(["git", "merge", "main"], cwd=self.work, capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        (self.work / "f.txt").write_text("resolved\n")
        self.git("add", "f.txt")
        self.git("commit", "-m", "resolve")
        self.assertEqual(self.heads(), [self.head()])

    def test_the_branch_of_the_base_pull_request_does_not_count_as_the_base(self):
        self.git("checkout", "-b", "stack")
        self.commit("s.txt", "s\n")
        self.git("push", "origin", "stack")
        self.git("checkout", "feature")
        self.commit("f2.txt", "f2\n")
        self.git("push", "origin", "feature")
        self.git("checkout", "stack")
        self.git("merge", "--no-ff", "--no-edit", "feature")
        self.assertEqual(self.heads(base="feature"), [self.head()])

    def test_a_failing_fetch_leaves_just_the_head(self):
        self.git("merge", "--no-edit", "origin/main")
        self.assertEqual(len(self.heads()), 2)
        with mock.patch.object(factory, "act_git", lambda *a: (1, "", "no network")):
            self.assertEqual(self.heads(), [self.head()])


class LanderBlockedTests(World):
    """`lgtm-blocked` fires from the stops lander.json records, for pull requests still open at the recorded head."""

    def stop(self, n=1, head=SHA, kind="ci", text="check test failed https://x/run/1"):
        repo = SimpleNamespace(root=ROOT)
        state = factory.read_lander(repo)
        state["stopped"][str(n)] = {"head": head, "kind": kind, "text": text, "ts": iso(NOW - 60)}
        factory.write_lander(repo, state)

    def health(self, prs, **kw):
        answers, sessions, slugs = self.world(prs, **kw)
        return self.cli("health", answers=answers, sessions=sessions, entries=slugs)[1]

    def test_it_fires_under_the_kind_the_lander_gave(self):
        self.stop()
        out = self.health([pr(1)])
        self.assertIn("lgtm-blocked  always  #1/ci  check test failed https://x/run/1 https://x/pull/1", out)

    def test_it_does_not_fire_once_the_head_moved_or_the_pull_request_is_gone(self):
        self.stop(head="old")
        self.assertEqual(self.health([pr(1)]), "nothing fired\n")
        self.stop(n=2)
        self.assertEqual(self.health([pr(1)]), "nothing fired\n")

    def test_nothing_stopped_asks_github_nothing(self):
        self.health([pr(1)])
        self.assertFalse(self.fake.ran("gh", "pr", "list", "--state", "open"))

    def test_a_failing_pull_request_list_is_not_evaluated(self):
        self.stop()
        out = self.health([pr(1)], extra={("gh", "pr", "list", "--state", "open"): (1, "", "boom")})
        self.assertIn("not evaluated: lgtm-blocked: gh pr list failed: boom", out)


class PullMainTests(World):
    def merged(self, branch, pull, **kw):
        extra = {("git", "rev-parse", "--abbrev-ref"): (0, f"{branch}\n", ""), ("git", "pull"): pull}
        return self.merge("1", prs=[pr(1)], extra=extra, **kw)

    def test_the_main_checkout_on_the_default_branch_is_fast_forwarded_after_a_merge(self):
        _, out, _ = self.merged("main", (0, "", ""))
        self.assertIn(["git", "pull", "--ff-only", "origin", "main"], self.fake.calls)
        self.assertNotIn("not updated", out)

    def test_a_failure_prints_one_line_and_does_not_fail_the_merge(self):
        code, out, _ = self.merged("main", (1, "", "not possible to fast-forward"))
        self.assertEqual(code, 0)
        self.assertIn("main checkout not updated: not possible to fast-forward\n", out)

    def test_another_branch_or_a_stop_leaves_the_checkout_alone(self):
        self.merged("other", (0, "", ""))
        self.assertFalse(self.fake.ran("git", "pull"))
        self.setUp()
        self.merged("main", (0, "", ""), checks=[])
        self.assertFalse(self.fake.ran("git", "pull"))


class BaseMergedTests(unittest.TestCase):
    REPO = SimpleNamespace(root=ROOT, default="main", remote="origin",
                           git=lambda *a, path=None: factory.run(["git", *a], cwd=path))
    MERGED = {("gh", "pr", "list", "--head", "worktree-base"): (0, json.dumps([
        {"state": "MERGED", "url": "https://x/pull/5", "headRefOid": "z", "baseRefName": "main"}]), "")}

    def found(self, phase="motion", base="worktree-base", answers=None, oid="abc", ancestor=1, **kw):
        merge_commit = {"oid": oid} if oid else None
        p = pic(repo=self.REPO, gh=lambda kind, *a: {"mergeCommit": merge_commit},
                rows=[row("x", phase=phase, stacked_on=base, **kw)])
        self.fake = FakeRun({**(answers or self.MERGED), ("git", "merge-base"): (ancestor, "", "")})
        with mock.patch.object(factory, "run", self.fake):
            return check("base-merged", p)

    def test_fires_for_motion_and_blocked(self):
        for phase in ("motion", "blocked"):
            with self.subTest(phase):
                self.assertEqual(self.found(phase), [(
                    "x", "x", "its base worktree-base merged (https://x/pull/5): tell its lead to merge "
                    "origin/main and to open or retarget its pull request against main")])
        self.assertIn(["git", "merge-base", "--is-ancestor", "abc", "HEAD"], self.fake.calls)

    def test_fires_without_a_merge_commit_or_when_it_is_not_an_ancestor(self):
        self.assertEqual(len(self.found(oid=None)), 1)
        self.assertEqual(len(self.found(ancestor=128)), 1)

    def test_does_not_fire_once_the_worktree_has_the_merge_commit(self):
        self.assertEqual(self.found(ancestor=0), [])

    def test_does_not_fire_otherwise(self):
        open_base = {("gh", "pr", "list", "--head", "worktree-base"): (0, json.dumps([
            {"state": "OPEN", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), "")}
        elsewhere = {("gh", "pr", "list", "--head", "worktree-base"): (0, json.dumps([
            {"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "dev"}]), "")}
        self.assertEqual(self.found("review"), [])
        self.assertEqual(self.found("settled"), [])
        self.assertEqual(self.found(answers=open_base), [])
        self.assertEqual(self.found(answers=elsewhere), [])
        self.assertEqual(self.found(base=None), [])
        self.assertEqual(self.found(pr_state="merged", pr_base="main"), [])
        self.assertEqual(self.found(pr_state="open", pr_base="main"), [])
        self.assertEqual(len(self.found(pr_state="open", pr_base="worktree-base")), 1)

    def test_an_unknown_base_state_is_not_evaluated(self):
        with self.assertRaises(factory.NotEvaluated):
            self.found(answers={("gh", "pr", "list"): (1, "", "boom")})


class SharedCodeTests(unittest.TestCase):
    def test_the_plugins_own_worktree_is_kept(self):
        with mock.patch.object(factory, "PLUGIN_ROOT", Path(f"{WT}/x/factory")):
            reason = factory.keep_reason(None, {"path": f"{WT}/x"}, [])
        self.assertEqual(reason, "this factory runs from it; leads started by it load their plugin from there")

    def test_another_worktree_is_judged_as_before(self):
        with mock.patch.object(factory, "PLUGIN_ROOT", Path(f"{WT}/xy/factory")):
            reason = factory.keep_reason(None, {"path": f"{WT}/x", "branch": None}, [])
        self.assertEqual(reason, "detached HEAD, no branch to judge")

    def test_collect_sets_stacked_on(self):
        for recorded, want in (("worktree-y\n", "worktree-y"), ("", None)):
            with self.subTest(recorded):
                answers = repo_answers([task_entry("x")], [], {
                    ("git", "status"): (0, "", ""), ("git", "rev-list"): (0, "0\t1\n", ""),
                    ("git", "log"): (0, f"{NOW} h\n", ""), ("gh",): (0, "[]", ""),
                    ("git", "config"): (0, recorded, "")})
                code, out, _, _ = invoke(["status", "--json"], answers)
                self.assertEqual(json.loads(out)["rows"][0]["stacked_on"], want)


if __name__ == "__main__":
    unittest.main()
