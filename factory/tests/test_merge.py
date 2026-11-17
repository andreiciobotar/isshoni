import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import ROOT, FakeTime, WT, FakeRun, factory, invoke, repo_answers, task_entry
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


RECORDED = {("git", "config", "--get-regexp"): (0, "branch.worktree-t1.factorypr 1\n", "")}  # task t1 recorded #1


def namesake(**kw):
    """Another pull request (#2), by the same author, on the head branch of task t1, whose recorded pull request is #1."""
    return dict(pr(2, **kw), headRefName="worktree-t1")


def stacked_base(n=1):
    """What GitHub says about the base pull request #n: open when the tasks are read, merged after that."""
    def state(word):
        return (0, json.dumps([{"state": word, "url": f"https://x/pull/{n}", "headRefOid": SHA, "baseRefName": "main"}]), "")
    return {("gh", "pr", "list", "--head", f"worktree-t{n}"): [state("OPEN"), state("MERGED")]}


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


    def world(self, prs, *, checks=GREEN, mergeable="MERGEABLE", state="handed-over", tasks=None, local=SHA,
              extra=None):
        slugs = [f"t{p['number']}" for p in prs if not p["isCrossRepository"]] if tasks is None else tasks
        states = {s: (state[s] if isinstance(state, dict) else state) for s in slugs}
        self.seed("tasks", [{"repo": ROOT, "slug": s, "state": st, "ts": iso(NOW - 3600)} for s, st in states.items()])
        answers = {
            ("git", "status"): (0, "", ""), ("git", "rev-list"): (0, "0\t1\n", ""), ("git", "fetch"): (0, "", ""),
            ("git", "merge-base", "--is-ancestor"): lambda: (1, "", "") if self.tip_moved_in_last_fetch() else (0, "", ""),
            ("git", "log"): (0, f"{NOW} {local}\n", ""),
            ("gh", "pr", "list", "--state", "open"): (0, json.dumps(prs), ""),
            ("gh", "pr", "edit"): (0, "", ""), ("gh", "pr", "ready"): (0, "", ""), ("gh", "pr", "merge"): (0, "", ""),
            ("gh", "api", "-H", factory.STACKS_VERSION): (0, "[]", ""),
        }
        for s in slugs:
            n = int(s[1:])
            mine = next(p for p in prs if p["number"] == n)
            answers[("gh", "pr", "list", "--head", f"worktree-{s}")] = (0, json.dumps([
                {"state": "OPEN", "url": f"https://x/pull/{n}", "headRefOid": SHA, "baseRefName": mine["baseRefName"],
                 "isDraft": mine["isDraft"]}]), "")
        for p in prs:
            n = str(p["number"])
            answers[("gh", "pr", "checks", n)] = raw(per(checks, p["number"]))
            m = per(mergeable, p["number"])
            answers[("gh", "pr", "view", n)] = raw({"mergeable": m}) if isinstance(m, str) else (
                [raw({"mergeable": x}) for x in m] if isinstance(m, list) else m)
        answers.update(extra or {})
        finished = [{"id": f"id-{s}", "sessionId": f"sid-{s}", "cwd": f"{WT}/{s}", "kind": "background",
                     "pid": 7, "status": "idle", "state": "done"} for s in slugs]
        return answers, [DISPATCHER | {"name": SESSION}, *finished], slugs

    def tip_moved_in_last_fetch(self):
        """Whether the default branch's tip, as the last `git fetch` left it, holds a merge of this command: a fetch
        made before a merge still shows the old tip, which every head then contains."""
        merges, seen = 0, 0
        for call in self.fake.calls:
            if call[:3] == ["gh", "pr", "merge"]:
                merges += 1
            elif call[:2] == ["git", "fetch"]:
                seen = merges
        return seen > 0

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

    def test_a_stack_named_in_reverse_merges_the_bottom_and_stops_the_next_behind(self):
        code, out, _ = self.merge("2", "1", prs=[pr(1), pr(2, base="worktree-t1")], extra=stacked_base())
        self.assertEqual(code, 1)
        self.assertEqual(self.pr_calls(), [
            ["pr", "checks", "1", "--json", "name,bucket"], ["pr", "view", "1", "--json", "mergeable"],
            ["pr", "merge", "1", "--merge", "--match-head-commit", SHA]])
        self.assertLess(out.index("#1 merged"), out.index(self.behind(2)))

    def test_a_base_merged_earlier_is_retargeted_in_one_pass_and_merged_in_the_next(self):
        merged = (0, json.dumps([{"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), "")
        code, out, _ = self.merge("2", prs=[pr(2, base="worktree-t1")],
                                  extra={("gh", "pr", "list", "--head", "worktree-t1"): merged})
        self.assertEqual(code, 1)
        self.assertIn("#2 stopped: its base was changed to main in this pass: it merges on the next one\n", out)
        self.assertEqual([c[:2] for c in self.pr_calls()], [["pr", "checks"], ["pr", "edit"]])
        self.setUp()
        code, out, _ = self.merge("2", prs=[pr(2)])
        self.assertEqual(code, 0)
        self.assertEqual([c[:2] for c in self.pr_calls()], [["pr", "checks"], ["pr", "view"], ["pr", "merge"]])
        self.assertIn("#2 merged", out)

    def test_a_dry_run_on_a_merged_base_says_so_and_changes_nothing(self):
        merged = (0, json.dumps([{"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), "")
        code, out, _ = self.merge("2", "--dry-run", prs=[pr(2, base="worktree-t1")],
                                  extra={("gh", "pr", "list", "--head", "worktree-t1"): merged})
        self.assertEqual(code, 0)
        self.assertEqual(out, "#2 would merge (its base is changed to main first; it merges on the pass after that)\n")
        self.assertEqual(self.gh_mutations(), [])

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
            "local head differs": (dict(prs=[pr(1)], local="other"), "worktree head is not the pull request's head"),
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
            with self.subTest(label), mock.patch.object(factory, "time", FakeTime(sleep=mock.Mock())):
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
        sleep = mock.Mock()
        with mock.patch.object(factory, "time", FakeTime(sleep=sleep)):
            code, out, _ = self.merge("1", prs=[pr(1)], mergeable="UNKNOWN")
        self.assertEqual(code, 1)
        self.assertEqual(sum(c[:3] == ["gh", "pr", "view"] for c in self.fake.calls), 5)
        self.assertEqual(sleep.call_args_list, [mock.call(3)] * 4)
        self.assertIn("#1 stopped: GitHub has not computed", out)

    def test_unknown_then_mergeable_merges(self):
        sleep = mock.Mock()
        with mock.patch.object(factory, "time", FakeTime(sleep=sleep)):
            code, out, _ = self.merge("1", prs=[pr(1)], mergeable=["UNKNOWN", "MERGEABLE"])
        self.assertEqual(code, 0)
        self.assertEqual(sleep.call_count, 1)
        self.assertIn("#1 merged", out)

    def behind(self, n):
        return f"#{n} stopped: its base has moved since its head: bring it up to date (the lander does when its turn comes) so its checks run on the combination"

    def test_a_stop_ends_the_run(self):
        code, out, _ = self.merge("1", "2", "3", prs=[pr(1), pr(2), pr(3)])
        self.assertEqual(code, 1)
        self.assertIn("#1 merged", out)
        self.assertIn(self.behind(2) + "\n#3 not attempted\n", out)
        self.assertEqual([c[1] for c in self.gh_mutations()], ["merge"])
        self.assertEqual(self.gh_mutations()[0][2], "1")
        self.assertIn("kept t1:", out)

    def test_siblings_on_one_base_merge_the_first_and_stop_behind(self):
        code, out, _ = self.merge("1", "2", "3", prs=[pr(1), pr(2, base="worktree-t1"), pr(3, base="worktree-t1")],
                                  extra=stacked_base())
        self.assertEqual(code, 1)
        self.assertIn("#1 merged", out)
        self.assertIn(self.behind(2) + "\n#3 not attempted\n", out)

    def test_a_base_merged_in_an_earlier_command_and_main_moved_since_is_behind(self):
        merged = (0, json.dumps([{"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), "")
        code, out, _ = self.merge("5", prs=[pr(5, base="worktree-t9")],
                                  extra={("gh", "pr", "list", "--head", "worktree-t9"): merged,
                                         ("git", "merge-base", "--is-ancestor"): (1, "", "")})
        self.assertEqual(code, 1)
        self.assertIn(self.behind(5), out)
        self.assertEqual(self.gh_mutations(), [])

    def test_every_pull_request_gets_a_fresh_fetch(self):
        self.merge("1", "2", prs=[pr(1), pr(2)])
        self.assertEqual(sum(c[:2] == ["git", "fetch"] for c in self.fake.calls), 2)

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

    def test_two_pull_requests_on_the_default_branch_merge_one_and_stop_the_other(self):
        code, out, _ = self.merge("1", "2", prs=[pr(1), pr(2)])
        self.assertEqual(code, 1)
        self.assertIn("#1 merged", out)
        self.assertIn(self.behind(2), out)
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
    def test_dry_run_changes_nothing_and_every_pull_request_after_the_first_would_be_behind(self):
        prs = [pr(1, draft=True), pr(2, base="worktree-t1"), pr(3), pr(4)]
        code, out, _ = self.merge("--dry-run", "--all", prs=prs)
        self.assertEqual(code, 1)
        why = "its base has moved since its head: bring it up to date (the lander does when its turn comes) so its checks run on the combination"
        self.assertEqual(out.splitlines(), [
            "#1 would merge (draft: would be marked ready)",
            "#2 would stop: " + why.format(2), "#3 would stop: " + why.format(3), "#4 would stop: " + why.format(4)])
        self.assertEqual(self.gh_mutations(), [])
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


COMMITS = "repos/{owner}/{repo}/pulls/%d/commits"
COMMIT = "repos/{owner}/{repo}/commits/"


def patch(i, tag):
    """The patch of change `i`: its hunk header (numbers and heading) depends on the base, here on `tag`."""
    return f"@@ -{i + len(tag)},2 +{i + len(tag)},3 @@ def {tag}():\n context {i}\n+added {i}\n"


def file(text, name="a.py"):
    return {"filename": name, "status": "modified", "previous_filename": None, "patch": text}


def commit_of(sha, parents, *files):
    return {"sha": sha, "parents": [{"sha": x} for x in parents], "files": list(files)}


def stack(tag, n, base, patches=None):
    """(shas, records) of `n` commits on `base`, oldest first: commit i changes the same lines whatever the tag."""
    shas = [f"{tag}{i}000000" for i in range(1, n + 1)]
    records, parent = {}, base
    for i, sha in enumerate(shas, 1):
        records[sha] = commit_of(sha, [parent], file((patches or {}).get(i, patch(i, tag))))
        parent = sha
    return shas, records


def served(listed, records, n=1):
    """The `gh` answers for the commits of pull request `n` (those listed, oldest first) and for each commit in `records`."""
    answers = {("gh", "api", "--paginate", "--slurp", COMMITS % n): raw([[{"sha": s} for s in listed]])}
    answers.update({("gh", "api", COMMIT + sha): raw(rec) for sha, rec in records.items()})
    return answers


def commits_gh(listed, records, asked=None, raises=None):
    """A `p["gh"]` that serves what `served` does; GitHub's error body for a commit it does not know."""
    def gh(kind, *args):
        if asked is not None:
            asked.append(args)
        if raises:
            raise raises
        if "--slurp" in args:
            return [[{"sha": s} for s in listed]]
        if "-X" in args:
            return []
        sha = args[1].removeprefix(COMMIT)
        return records.get(sha) or {"message": f"No commit found for SHA: {sha}", "status": "422"}
    return gh


class SameDiffTests(unittest.TestCase):
    """`same_diff` and the lgtm rescue of `stale_approval`, against made-up commits."""

    def setUp(self):
        self.on_main, self.fetch_rc = {"base-o"}, 0
        fetch = mock.patch.object(factory, "act_git", lambda *a: (self.fetch_rc, "", ""))
        fetch.start()
        self.addCleanup(fetch.stop)

    def repo(self):
        """A repository whose default branch holds the shas in `on_main` (the base of the approved commits, by default)."""
        def git(*a):
            return (0 if a[:2] == ("merge-base", "--is-ancestor") and a[2] in self.on_main else 1), "", ""
        return SimpleNamespace(root="/r", remote="origin", default="main", git=git)

    def same(self, listed, records, commit_id, head=None, **kw):
        self.asked = []
        p = {"gh": commits_gh(listed, records, self.asked, **kw), "repo": self.repo()}
        return factory.same_diff(p, pr(1, sha=head or (listed[-1] if listed else "none")), commit_id)

    def one_each(self, **patches):
        old, old_records = stack("o", 1, "base-o", patches.get("old"))
        new, new_records = stack("n", 1, "base-n", patches.get("new"))
        return new, {**old_records, **new_records}, old[0]

    def test_one_commit_on_a_new_base_is_the_same_change_in_three_calls(self):
        # the #157 shape: the approved commit's parent is on the default branch (#158's was not, see below)
        listed, records, old = self.one_each()
        self.assertEqual(self.same(listed, records, old), (True, None))
        self.assertEqual(len(self.asked), 3)

    def test_two_commits_are_compared_one_by_one(self):
        old, old_records = stack("o", 2, "base-o")
        new, new_records = stack("n", 2, "base-n")
        self.assertEqual(self.same(new, {**old_records, **new_records}, old[-1]), (True, None))
        self.assertEqual(len(self.asked), 5)

    def test_a_third_commit_on_top_is_not_the_same(self):
        old, old_records = stack("o", 2, "base-o")
        new, new_records = stack("n", 3, "base-n")
        same, why = self.same(new, {**old_records, **new_records}, old[-1])
        self.assertFalse(same)
        self.assertEqual(why, f"commit 1 of 3 is not the same change ({old[-1][:7]} then, {new[-1][:7]} now)")

    def test_a_changed_patch_is_not_the_same_and_names_the_commit(self):
        old, old_records = stack("o", 2, "base-o")
        new, new_records = stack("n", 2, "base-n", {2: "@@ -1 +1 @@\n+other\n"})
        same, why = self.same(new, {**old_records, **new_records}, old[-1])
        self.assertEqual((same, why), (False, f"commit 1 of 2 is not the same change ({old[-1][:7]} then, {new[-1][:7]} now)"))
        new, new_records = stack("n", 2, "base-n", {1: "@@ -1 +1 @@\n+other\n"})
        same, why = self.same(new, {**old_records, **new_records}, old[-1])
        self.assertEqual((same, why), (False, f"commit 2 of 2 is not the same change ({old[0][:7]} then, {new[0][:7]} now)"))

    def test_a_reordered_pair_is_not_the_same(self):
        old, old_records = stack("o", 2, "base-o")
        new, new_records = stack("n", 2, "base-n", {1: patch(2, "n"), 2: patch(1, "n")})
        self.assertFalse(self.same(new, {**old_records, **new_records}, old[-1])[0])

    def test_a_merge_commit_among_the_walked_ones_is_not_the_same(self):
        listed, records, old = self.one_each()
        records[listed[0]] = commit_of(listed[0], ["base-n", "other"], file(patch(1, "n")))
        self.assertEqual(self.same(listed, records, old), (False, f"commit {listed[0][:7]} has no single parent"))

    def test_hunk_headers_are_not_part_of_the_change(self):
        a, b = "@@ -10,2 +10,3 @@ def a():\n ctx\n+new\n", "@@ -40,2 +41,3 @@ def b():\n ctx\n+new\n"
        listed, records, old = self.one_each(old={1: a}, new={1: b})
        self.assertEqual(self.same(listed, records, old), (True, None))

    def test_a_commits_call_that_cannot_answer_is_not_the_same(self):
        listed, records, old = self.one_each()
        for label, kw in (("no json", {"raises": factory.NoJson("gh api failed")}),
                          ("not evaluated", {"raises": factory.NotEvaluated("shape")})):
            with self.subTest(label):
                self.assertEqual(self.same(listed, records, old, **kw), (False, "GitHub could not list its commits"))
        p = {"gh": lambda kind, *args: {"message": "Not Found", "status": "404"}}
        self.assertEqual(factory.same_diff(p, pr(1), old), (False, "GitHub could not list its commits"))

    def test_what_is_unreadable_or_not_shown_in_full_is_not_the_same(self):
        listed, records, old = self.one_each()
        head = listed[0]
        cases = {
            "a file without a patch": ({**records, head: commit_of(head, ["b"], {"filename": "a.png", "status": "added"})}, old),
            "a commit id of None": (records, None),
            "a commit with no parent": ({**records, head: commit_of(head, [], file(patch(1, "n")))}, old),
            "an answer for another sha": ({**records, old: dict(records[old], sha="else")}, old),
            "a commit GitHub does not know": ({k: v for k, v in records.items() if k != old}, old),
        }
        for label, (recs, commit_id) in cases.items():
            with self.subTest(label):
                self.assertFalse(self.same(listed, recs, commit_id)[0])

    def test_a_head_that_moved_while_it_was_read_is_named(self):
        listed, records, old = self.one_each()
        self.assertEqual(self.same(listed, records, old, head="moved00"), (False, "its head moved while it was read"))

    def test_299_files_are_compared_and_300_are_not(self):
        for count, same in ((299, True), (300, False)):
            with self.subTest(count):
                files = [file(patch(i, "n"), f"f{i}") for i in range(count)]
                listed, records, old = self.one_each()
                records[listed[0]] = commit_of(listed[0], ["base-n"], *files)
                records[old] = commit_of(old, ["base-o"], *files)
                self.assertEqual(self.same(listed, records, old)[0], same)

    def test_a_dropped_oldest_commit_is_not_the_same(self):
        old, old_records = stack("o", 2, "base-o")
        new, new_records = stack("n", 1, "base-n", {1: patch(2, "n")})
        self.on_main = {"base-n"}
        self.assertEqual(self.same(new, {**old_records, **new_records}, old[-1]), (False,
            f"the commits approved at {old[-1][:7]} sat on {old[0][:7]}, which is not on main: a commit may have been dropped"))

    def test_the_158_shape_where_the_approved_base_is_on_no_branch_is_not_the_same(self):
        listed, records, old = self.one_each()
        self.on_main = set()
        self.assertEqual(self.same(listed, records, old), (False,
            f"the commits approved at {old[:7]} sat on base-o, which is not on main: a commit may have been dropped"))

    def test_a_failed_fetch_is_not_the_same(self):
        listed, records, old = self.one_each()
        self.fetch_rc = 1
        self.assertFalse(self.same(listed, records, old)[0])

    def test_no_commits_and_too_many_are_not_compared(self):
        listed, records, old = self.one_each()
        self.assertFalse(self.same([], records, old)[0])
        for n, same in ((20, True), (21, False)):
            with self.subTest(n):
                old, old_records = stack("o", n, "base-o")
                new, new_records = stack("n", n, "base-n")
                self.assertEqual(self.same(new, {**old_records, **new_records}, old[-1])[0], same)
                if not same:
                    self.assertEqual(len(self.asked), 1)

    def lgtm(self, approved, activity, listed, records, head):
        p = {"gh": lambda kind, *args: activity if "-X" in args else commits_gh(listed, records)(kind, *args),
             "repo": self.repo()}
        return factory.stale_approval(p, pr(1, sha=head), comment(at=approved), [head])

    def pushes(self, listed, records, old):
        """The observed log: pushed to `old` (the lgtm sees it), then to another head, then to the head."""
        mid, mid_records = stack("m", 1, "base-m")
        return [{"after": listed[0], "timestamp": iso(NOW - 600)}, {"after": mid[0], "timestamp": iso(NOW - 900)},
                {"after": old, "timestamp": iso(NOW - 3600)}], {**records, **mid_records}

    def test_an_lgtm_given_before_two_rebasing_pushes_still_holds(self):
        listed, records, old = self.one_each()
        activity, records = self.pushes(listed, records, old)
        self.assertIsNone(self.lgtm(NOW - 1800, activity, listed, records, listed[0]))

    def test_an_lgtm_given_before_a_push_that_changed_a_patch_is_stale_and_says_what_was_found(self):
        listed, records, old = self.one_each(new={1: "@@ -1 +1 @@\n+other\n"})
        activity, records = self.pushes(listed, records, old)
        kind, why = self.lgtm(NOW - 1800, activity, listed, records, listed[0])
        self.assertEqual(kind, "stale")
        self.assertEqual(why, f"{factory.STALE_LGTM} (the lgtm was given on {old[:7]}, the head is now {listed[0][:7]}: "
                              f"commit 1 of 1 is not the same change ({old[:7]} then, {listed[0][:7]} now))")

    def test_an_lgtm_with_no_earlier_push_is_stale_and_a_same_second_push_is_not_earlier(self):
        listed, records, old = self.one_each()
        for approved in (NOW - 7200, NOW - 3600):
            with self.subTest(approved):
                activity = [{"after": listed[0], "timestamp": iso(NOW - 3600)}]
                kind, why = self.lgtm(approved, activity, listed, records, listed[0])
                self.assertEqual(kind, "stale")
                self.assertIn(f"(GitHub's log has no push older than the lgtm; the head is now {listed[0][:7]})", why)

    def test_a_push_with_an_unreadable_time_before_the_one_the_lgtm_saw_is_stale(self):
        listed, records, old = self.one_each()
        x, x_records = stack("x", 1, "base-x", {1: "@@ -1 +1 @@\n+other\n"})
        activity = [{"after": listed[0], "timestamp": iso(NOW - 600)}, {"after": x[0], "timestamp": "garbled"},
                    {"after": old, "timestamp": iso(NOW - 3600)}]
        kind, why = self.lgtm(NOW - 1800, activity, listed, {**records, **x_records}, listed[0])
        self.assertEqual(kind, "stale")
        self.assertIn("(a push in GitHub's log has no readable time;", why)


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

    def refused(self, review_decision, state="BLOCKED", **kw):
        view = {k: v for k, v in {"reviewDecision": review_decision, "mergeStateStatus": state}.items() if v is not None}
        return self.approved([pr(1)], {1: [comment()]}, extra={
            ("gh", "pr", "merge"): (1, "", "base branch policy prohibits the merge"),
            ("gh", "pr", "view", "1", "--json", "reviewDecision,mergeStateStatus"): raw(view), **kw.pop("extra", {})}, **kw)

    def test_a_merge_github_refuses_for_a_review_says_so_before_the_raw_text(self):
        raw_text = "gh pr merge 1 --merge --match-head-commit sha-1: base branch policy prohibits the merge"
        for decision, what in (("REVIEW_REQUIRED", "a review is still required"),
                               ("CHANGES_REQUESTED", "a reviewer requested changes")):
            with self.subTest(decision):
                self.setUp()
                code, out, _ = self.refused(decision)
                self.assertEqual(code, 1)
                self.assertIn(f"#1 stopped: {factory.REFUSED}{what} (reviewDecision {decision}, mergeStateStatus BLOCKED); {raw_text}\n", out)

    def test_the_merge_state_is_left_out_when_empty_or_absent(self):
        for label, state in (("empty", ""), ("absent", None)):
            with self.subTest(label):
                self.setUp()
                _, out, _ = self.refused("REVIEW_REQUIRED", state)
                self.assertIn(f"{factory.REFUSED}a review is still required (reviewDecision REVIEW_REQUIRED); gh pr merge 1", out)

    def test_a_refusal_no_record_explains_is_kind_refused_and_says_what_was_read(self):
        view = ("gh", "pr", "view", "1", "--json", "reviewDecision,mergeStateStatus")
        for label, decision, seen, extra in (
                ("empty", "", "reviewDecision none, mergeStateStatus BLOCKED, checks passed", {}),
                ("absent", None, "reviewDecision none, mergeStateStatus BLOCKED, checks passed", {}),
                ("approved", "APPROVED", "reviewDecision APPROVED, mergeStateStatus BLOCKED, checks passed", {}),
                ("view unreadable", "", "reviewDecision unreadable, mergeStateStatus unreadable, checks passed", {view: (1, "", "boom")}),
                ("checks unreadable", "APPROVED", "reviewDecision APPROVED, mergeStateStatus BLOCKED, checks unreadable",
                 {("gh", "pr", "checks", "1"): [raw(GREEN), (1, "", "boom")]})):
            with self.subTest(label):
                self.setUp()
                code, out, _ = self.refused(decision, extra=extra)
                self.assertEqual(code, 1)
                self.assertIn(f"#1 stopped: GitHub refused the merge and nothing it shows explains why ({seen}); "
                              "gh pr merge 1 --merge --match-head-commit sha-1: base branch policy prohibits the merge\n", out)
                self.assertNotIn(factory.REFUSED, out)

    def test_a_refusal_while_a_check_runs_again_is_kind_pending_with_its_start(self):
        again = [{"name": "test", "bucket": "pass"}, {"name": "lint", "bucket": "pending", "startedAt": "2026-01-02T03:04:05Z"}]
        code, out, _ = self.approved([pr(1)], {1: [comment(n=1)]}, extra={
            ("gh", "pr", "merge"): (1, "", "base branch policy prohibits the merge"),
            ("gh", "pr", "checks", "1"): [raw(GREEN), raw(again)],
            ("gh", "pr", "view", "1", "--json", "reviewDecision,mergeStateStatus"): raw(
                {"reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN"})})
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: GitHub refused the merge while a check ran again on this head: "
                      "lint (started 2026-01-02T03:04:05Z); gh pr merge 1 --merge", out)

    def test_a_human_authors_own_lgtm_is_refused_for_a_review_and_nothing_is_read_before_a_merge_that_succeeds(self):
        code, out, _ = self.refused("REVIEW_REQUIRED")
        self.assertEqual(code, 1)
        self.assertIn(f"{factory.REFUSED}a review is still required (reviewDecision REVIEW_REQUIRED, mergeStateStatus BLOCKED); gh pr merge 1 --merge --match-head-commit", out)
        self.setUp()
        code, out, _ = self.approved([pr(1)], {1: [comment()]})
        self.assertEqual((code, self.merges()), (0, ["1"]))
        self.assertIn("#1 merged", out)
        self.assertFalse(any("reviewDecision" in part for c in self.pr_calls() for part in c))

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
        (self.store / "settings.toml").write_text('[merging]\nlgtm_from_author = false\n')
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

    def test_a_stack_merges_the_bottom_and_stops_the_next_behind(self):
        code, out, _ = self.approved([pr(2, base="worktree-t1"), pr(1)], {1: [comment()], 2: [comment(n=2)]},
                                     extra=stacked_base())
        self.assertEqual(code, 1)
        self.assertEqual(self.merges(), ["1"])
        self.assertIn("#2 stopped: its base has moved", out)

    def queued(self):
        """The `queue` records, without their `ts`."""
        return [{k: v for k, v in r.items() if k != "ts"} for r in self.records("queue")]

    def merged_record(self, approved_at):
        return {"repo": ROOT, "slug": "t1", "pr": 1, "head": SHA, "step": "merged", "approved_at": approved_at}

    def test_a_merge_records_the_time_of_the_approving_review(self):
        self.approved([pr(1)], reviews={1: [review(at=NOW - 60)]})
        self.assertEqual(self.queued(), [self.merged_record(iso(NOW - 60))])

    def test_a_merge_records_the_time_of_the_lgtm_comment(self):
        self.approved([pr(1)], {1: [comment(at=NOW - 90)]})
        self.assertEqual(self.queued(), [self.merged_record(iso(NOW - 90))])

    def test_a_merge_with_no_approval_required_records_a_null_time(self):
        self.merge("1", prs=[pr(1)])
        self.assertEqual(self.queued(), [self.merged_record(None)])

    def test_a_dry_run_and_a_refused_merge_record_nothing(self):
        self.merge("1", "--dry-run", prs=[pr(1)])
        self.approved([pr(1)])
        self.approved([pr(1)], reviews={1: [review()]}, extra={("gh", "pr", "merge"): (1, "", "head moved")})
        self.assertEqual(self.queued(), [])

    def test_all_skips_what_is_not_approved_and_merges_the_rest(self):
        code, out, _ = self.approved([pr(1), pr(2), pr(3)], {1: [comment()], 3: [comment(n=3)]}, "--all")
        self.assertEqual(code, 1)
        self.assertIn("#2 skipped: not approved on GitHub", out)
        self.assertIn("#3 stopped: its base has moved since its head: bring it up to date (the lander does when its turn comes) so its checks run on the combination", out)
        self.assertEqual(self.merges(), ["1"])

    def test_an_approved_draft_that_is_handed_over_up_to_date_and_green_is_marked_ready_and_merged(self):
        code, out, _ = self.approved([pr(1, draft=True)], {1: [comment()]})
        self.assertEqual(code, 0)
        self.assertIn("#1 merged", out)
        self.assertEqual([c[:2] for c in self.gh_mutations()], [["pr", "ready"], ["pr", "merge"]])

    def test_a_pull_request_that_only_shares_the_branch_of_the_recorded_one_is_refused_and_the_recorded_one_merges(self):
        both = [pr(1), namesake(draft=True)]
        code, out, _ = self.approved(both, {2: [comment(n=2)]}, "2", extra=RECORDED, tasks=["t1"])
        self.assertEqual(code, 1)
        self.assertIn("#2 stopped: task t1 recorded #1 as its pull request, not this one (opened by andrei)\n", out)
        self.assertEqual(self.gh_mutations(), [])
        self.setUp()
        code, out, _ = self.approved(both, {1: [comment()]}, "1", extra=RECORDED, tasks=["t1"])
        self.assertEqual((code, self.merges()), (0, ["1"]))

    def test_the_recorded_pull_request_still_merges_after_a_hand_push_and_a_fresh_approval(self):
        new = "sha-2"
        code, _, _ = self.approved([pr(1, sha=new), namesake()], {1: [comment()]}, "1", extra=RECORDED, tasks=["t1"],
                                   local=new, activity={1: [{"after": new, "timestamp": iso(NOW - 3600)}]})
        self.assertEqual((code, self.merges()), (0, ["1"]))
        self.assertEqual(self.gh_mutations()[-1], ["pr", "merge", "1", "--merge", "--match-head-commit", new])

    def test_an_approved_draft_whose_task_is_working_is_refused_for_the_handover(self):
        code, out, _ = self.approved([pr(1, draft=True)], {1: [comment()]}, state="working")
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: task t1 is in phase motion (declared working)", out)
        self.assertEqual(self.gh_mutations(), [])

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
        why = ("#1 stopped: its base has moved since its head: bring it up to date (the lander does when its turn comes) so its checks "
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

    def test_every_pull_request_gets_its_own_fetch(self):
        self.approved([pr(1), pr(2)], {1: [comment()], 2: [comment(n=2)]})
        self.assertEqual(sum(c[:2] == ["git", "fetch"] for c in self.fake.calls), 2)

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

    def rebased(self, n_old=1, n_new=1, **patches):
        """(head, old, extra): a pull request of `n_new` commits whose review sits on the head of `n_old` others."""
        old, old_records = stack("o", n_old, "base-o")
        new, new_records = stack("n", n_new, "base-n", patches.get("new"))
        return new[-1], old[-1], served(new, {**old_records, **new_records})

    def test_a_review_on_a_head_github_rebased_without_changing_its_diff_merges(self):
        head, old, extra = self.rebased()
        code, out, _ = self.approved([pr(1, sha=head)], reviews={1: [review(sha=old)]}, extra=extra, local=head)
        self.assertEqual((code, self.merges()), (0, ["1"]))
        self.assertEqual(self.queued(), [dict(self.merged_record(iso(NOW - 60)), head=head)])

    def test_a_review_on_a_head_with_another_commit_on_top_is_stale_and_the_line_says_what_was_found(self):
        head, old, extra = self.rebased(n_old=1, n_new=2)
        code, out, _ = self.approved([pr(1, sha=head)], reviews={1: [review(sha=old)]}, extra=extra, local=head)
        self.assertEqual((code, self.merges()), (1, []))
        self.assertIn(f"#1 stopped: {factory.STALE_REVIEW} (approved at {old[:7]}, the head is now {head[:7]}: "
                      f"commit 1 of 2 is not the same change ({old[:7]} then, {head[:7]} now))\n", out)

    def test_an_lgtm_given_before_two_rebasing_pushes_merges(self):
        head, old, extra = self.rebased()
        mid, mid_records = stack("m", 1, "base-m")
        activity = [{"after": head, "timestamp": iso(NOW - 600)}, {"after": mid[0], "timestamp": iso(NOW - 900)},
                    {"after": old, "timestamp": iso(NOW - 3600)}]
        code, out, _ = self.approved([pr(1, sha=head)], {1: [comment(at=NOW - 1800)]}, activity={1: activity},
                                     extra={**extra, **served([head], mid_records)}, local=head)
        self.assertEqual((code, self.merges()), (0, ["1"]))

    def test_a_review_on_the_head_and_an_lgtm_newer_than_the_push_read_no_commit(self):
        self.approved([pr(1)], reviews={1: [review()]})
        self.setUp()
        self.approved([pr(1)], {1: [comment()]})
        self.assertFalse(any("commits" in part for c in self.fake.calls for part in c))

    def test_a_valid_lgtm_is_checked_before_a_stale_review_reads_any_commit(self):
        head, old, extra = self.rebased()
        code, out, _ = self.approved([pr(1, sha=head)], {1: [comment()]}, reviews={1: [review(sha=old)]}, extra=extra,
                                     activity={1: [{"after": head, "timestamp": iso(NOW - 3600)}]}, local=head)
        self.assertEqual((code, self.merges()), (0, ["1"]))
        self.assertFalse(any("commits" in part for c in self.fake.calls for part in c))

    def test_plain_merge_reads_no_approval(self):
        self.merge("1", prs=[pr(1)])
        self.assertFalse(any(c[:2] == ["gh", "api"] and factory.STACKS_VERSION not in c for c in self.fake.calls))
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


PENDING = [{"name": "test", "bucket": "pending"}]
FAILED = [{"name": "test", "bucket": "fail"}]


class SyncDraftTests(World):
    def tick(self, prs, orphaned=False, at=None, **kw):
        answers, sessions, slugs = self.world(prs, **kw)
        if orphaned:
            sessions = [s for s in sessions if s.get("name") != SESSION]
        return self.cli("watch", answers=answers, sessions=sessions, entries=slugs, at=at)

    def flips(self):
        return [c[1:] for c in self.fake.calls if c[:3] == ["gh", "pr", "ready"]]

    def test_each_transition_and_each_state_that_is_already_right(self):
        cases = {
            "handed over, green, draft": (dict(prs=[pr(1, draft=True)]), [["pr", "ready", "1"]], "#1 ready for review\n"),
            "handed over, pending, draft": (dict(prs=[pr(1, draft=True)], checks=PENDING), [], ""),
            "handed over, failed, ready": (dict(prs=[pr(1)], checks=FAILED), [["pr", "ready", "1", "--undo"]],
                                           "#1 back to draft: failed or cancelled checks: test\n"),
            "handed over, green, ready": (dict(prs=[pr(1)]), [], ""),
            "working, ready": (dict(prs=[pr(1)], state="working"), [["pr", "ready", "1", "--undo"]],
                               "#1 back to draft: task t1 is in phase motion (declared working)\n"),
            "working, draft": (dict(prs=[pr(1, draft=True)], state="working"), [], ""),
            "blocked, ready": (dict(prs=[pr(1)], state="blocked"), [["pr", "ready", "1", "--undo"]],
                               "#1 back to draft: task t1 is in phase blocked (declared blocked)\n"),
            "head moved, ready": (dict(prs=[pr(1)], local="other"), [["pr", "ready", "1", "--undo"]],
                                  "#1 back to draft: task t1's worktree head is not the pull request's head\n"),
            "no task worktree": (dict(prs=[pr(1)], tasks=[], checks=FAILED), [], ""),
            "a fork": (dict(prs=[pr(1, fork=True)], tasks=["t1"], checks=FAILED), [], ""),
            "settled": (dict(prs=[pr(1)], state="acknowledged", checks=FAILED), [], ""),
        }
        for label, (kw, flips, line) in cases.items():
            with self.subTest(label):
                self.setUp()
                code, out, _ = self.tick(**kw)
                self.assertEqual(code, 0)
                self.assertEqual(self.flips(), flips)
                self.assertEqual("".join(l + "\n" for l in out.splitlines() if l.startswith("#1 ")), line)

    def test_a_held_pull_request_with_red_checks_stays_ready(self):
        hold = {"stopped": {"1": {"head": SHA, "kind": "not-ours", "text": "t", "ts": iso(NOW)}}}
        for label, (lander, flips) in {"held": (hold, []), "released": ({**hold, "unblocked": {"1": {}}}, [["pr", "ready", "1", "--undo"]]),
                                     "held at another head": ({"stopped": {"1": {**hold["stopped"]["1"], "head": "old"}}},
                                                              [["pr", "ready", "1", "--undo"]]),
                                     "not held": ({}, [["pr", "ready", "1", "--undo"]])}.items():
            with self.subTest(label):
                self.setUp()
                factory.write_lander(SimpleNamespace(root=ROOT), lander)
                code, out, _ = self.tick([pr(1)], checks=FAILED)
                self.assertEqual(code, 0)
                self.assertEqual(self.flips(), flips)
        for label, (draft, flips) in {"working, ready, held": (False, [["pr", "ready", "1", "--undo"]]),
                                      "working, draft, held": (True, [])}.items():
            with self.subTest(label):
                self.setUp()
                factory.write_lander(SimpleNamespace(root=ROOT), hold)
                code, out, _ = self.tick([pr(1, draft=draft)], checks=FAILED, state="working")
                self.assertEqual(code, 0)
                self.assertEqual(self.flips(), flips)

    def test_a_red_pull_request_the_sync_made_a_draft_is_ready_again_once_held(self):
        hold = {"stopped": {"1": {"head": SHA, "kind": "not-ours", "text": "t", "ts": iso(NOW)}}}
        self.tick([pr(1)], checks=FAILED)  # the tick's sync runs before the pass that holds it
        self.assertEqual(self.flips(), [["pr", "ready", "1", "--undo"]])
        self.setUp()
        factory.write_lander(SimpleNamespace(root=ROOT), hold)
        code, out, _ = self.tick([pr(1, draft=True)], checks=FAILED)
        self.assertEqual(code, 0)
        self.assertEqual(self.flips(), [["pr", "ready", "1"]])
        self.assertIn("#1 ready for review\n", out)

    def test_status_agrees_with_what_the_sync_leaves_on_github(self):
        cases = {
            "working": (dict(state="working", prs=[pr(1)]), True, "motion", None),
            "blocked": (dict(state="blocked", prs=[pr(1)]), True, "blocked", None),
            "handed over, draft": (dict(prs=[pr(1, draft=True)], checks=PENDING), True, "review", "waiting on checks: "),
            "handed over, ready": (dict(prs=[pr(1, draft=True)]), False, "review", "waiting on the human: "),
        }
        for label, (kw, draft, phase, line) in cases.items():
            with self.subTest(label):
                self.setUp()
                self.tick(**kw)
                flips = self.flips()
                left = ("--undo" in flips[-1]) if flips else kw["prs"][0]["isDraft"]
                self.assertEqual(left, draft)
                answers, sessions, slugs = self.world([{**kw["prs"][0], "isDraft": left}], state=kw.get("state", "handed-over"))
                _, out, _ = self.cli("status", "--json", answers=answers, sessions=sessions, entries=slugs)
                (row,) = json.loads(out)["rows"]
                self.assertEqual((row["phase"], row["pr_draft"]), (phase, draft))
                _, out, _ = self.cli("status", answers=answers, sessions=sessions, entries=slugs)
                self.assertEqual([l for l in out.splitlines() if l.startswith("waiting on")],
                                 [] if line is None else [f"{line}t1 https://x/pull/1"])

    def test_status_says_waiting_on_github_for_a_head_whose_merge_it_refused(self):
        said = factory.REFUSED + "a review is still required (reviewDecision REVIEW_REQUIRED, mergeStateStatus BLOCKED); gh pr merge 1 ..."
        cases = {"this head": ({"head": SHA, "kind": "merge", "text": said},
                               "waiting on GitHub: t1 https://x/pull/1: a review is still required "
                               "(reviewDecision REVIEW_REQUIRED, mergeStateStatus BLOCKED)"),
                 "another head": ({"head": "old", "kind": "merge", "text": said}, "waiting on the human: t1 https://x/pull/1"),
                 "another kind": ({"head": SHA, "kind": "ci", "text": said}, "waiting on the human: t1 https://x/pull/1"),
                 "raw text": ({"head": SHA, "kind": "merge", "text": "gh pr merge 1: head moved"},
                              "waiting on the human: t1 https://x/pull/1")}
        for label, (record, line) in cases.items():
            with self.subTest(label):
                self.setUp()
                factory.repo_state(ROOT, "lander", "json").write_text(json.dumps({"stopped": {"1": record}}))
                answers, sessions, slugs = self.world([pr(1)])
                _, out, _ = self.cli("status", answers=answers, sessions=sessions, entries=slugs)
                self.assertEqual([l for l in out.splitlines() if l.startswith("waiting on")], [line])

    def test_the_draft_flag_of_a_pull_request_that_is_not_the_recorded_one_is_left_alone(self):
        for label, kw in {"a draft that would be marked ready": dict(prs=[pr(1), namesake(draft=True)]),
                          "a ready one that would go back to draft": dict(prs=[pr(1, draft=True), namesake()], state="working")}.items():
            with self.subTest(label):
                self.setUp()
                self.tick(tasks=["t1"], extra=RECORDED, **kw)
                self.assertEqual(self.flips(), [])

    def test_each_actor_leaves_the_pull_request_of_another_machines_task_alone(self):
        """Head `worktree-other` has no worktree here, though the store holds a handed-over record for `other`."""
        theirs = lambda **kw: dict(pr(1, **kw), headRefName="worktree-other")
        record = [{"repo": ROOT, "slug": "other", "state": "handed-over", "ts": iso(NOW - 3600)}]
        with self.subTest("sync_drafts"):
            self.seed("tasks", record)
            self.tick([theirs(draft=True)], tasks=[])
            self.assertEqual(self.flips(), [])
            self.setUp()
            self.seed("tasks", record)
            self.tick([theirs()], tasks=[], checks=FAILED)
            self.assertEqual(self.flips(), [])
        with self.subTest("the lander loop"):
            self.setUp()
            self.seed("tasks", record)
            approved = {"latestReviews": [{"state": "APPROVED", "submittedAt": iso(NOW - 60)}], "comments": []}
            self.tick([{**theirs(), **approved}, pr(2)], tasks=["t2"])  # t2 is handed over here, so the tick looks
            self.assertEqual([c for c in self.claude_runs() if "--agent" in c], [])
            self.assertEqual(self.gh_mutations(), [])
        with self.subTest("merge --all"):
            self.setUp()
            self.seed("tasks", record)
            code, out, _ = self.merge("--all", prs=[theirs()], tasks=[])
            self.assertEqual(self.gh_mutations(), [])
            self.assertIn("#1 skipped: no task worktree has its head branch", out)

    def test_unlistable_sessions_change_nothing(self):
        code, out, _ = self.tick([pr(1)], extra={("claude", "agents"): (1, "", "no claude")})
        self.assertEqual(code, 0)
        self.assertIn("drafts not synced: sessions cannot be listed", out)
        self.assertEqual(self.flips(), [])

    def test_unreadable_checks_leave_that_pull_request_and_the_others_are_handled(self):
        code, out, _ = self.tick([pr(1), pr(2, draft=True)], checks={1: (1, "", "boom"), 2: GREEN})
        self.assertEqual(code, 0)
        self.assertIn("#1 draft flag not changed: gh pr checks failed: boom\n", out)
        self.assertIn("#2 ready for review\n", out)
        self.assertEqual(self.flips(), [["pr", "ready", "2"]])

    def test_a_task_whose_head_cannot_be_read_leaves_its_pull_request(self):
        code, out, _ = self.tick([pr(1)], extra={("git", "log"): (1, "", "bad")})
        self.assertEqual(code, 0)
        self.assertIn("#1 draft flag not changed: its task's head cannot be read\n", out)
        self.assertEqual(self.flips(), [])

    def test_an_unreadable_list_changes_nothing(self):
        code, out, _ = self.tick([pr(1, draft=True)], extra={("gh", "pr", "list", "--state", "open"): (1, "", "down")})
        self.assertIn("comments: gh pr list failed: down\n", out)
        self.assertEqual(self.flips(), [])

    def test_a_refusal_is_reported_and_does_not_stop_the_tick(self):
        code, out, _ = self.tick([pr(1, draft=True), pr(2, draft=True)],
                                 extra={("gh", "pr", "ready", "1"): (1, "", "refused")})
        self.assertEqual(code, 0)
        self.assertIn("#1 draft flag not changed: gh pr ready 1: refused\n", out)
        self.assertIn("#2 ready for review\n", out)

    def test_a_green_approved_draft_turns_ready_and_is_queued_as_a_draft_too(self):
        approved = {"latestReviews": [{"state": "APPROVED", "submittedAt": iso(NOW - 60)}], "comments": []}
        reviews = lambda: [c for c in self.fake.calls if REVIEWS % 1 in c]
        code, out, _ = self.tick([{**pr(1, draft=True), **approved}])
        self.assertIn("#1 ready for review\n", out)
        self.assertTrue(reviews())  # the draft flag is not a gate: a draft is queued too
        code, out, _ = self.tick([{**pr(1), **approved}], at=NOW + 120)
        self.assertTrue(reviews())
        self.assertEqual(self.claude_runs(), [])

    def test_an_orphaned_tick_still_syncs_the_drafts(self):
        code, out, _ = self.tick([pr(1, draft=True)], orphaned=True)
        self.assertEqual(self.flips(), [["pr", "ready", "1"]])
        self.assertIn("#1 ready for review\n", out)

    def test_the_cheap_tick_asks_github_once_for_the_list_and_once_per_checked_pull_request(self):
        self.tick([pr(1), pr(2, draft=True)], tasks=["t1", "t2"])
        lists = [c for c in self.fake.calls if c[:5] == ["gh", "pr", "list", "--state", "open"] and factory.NOTICE_FIELDS in c]
        checks = [c for c in self.fake.calls if c[:3] == ["gh", "pr", "checks"] and not c[3].startswith("http")]  # not the watch's `ci-red-handover` reads
        self.assertEqual((len(lists), len(checks)), (2, 2))  # the cheap tick's list, and the loop's

    def test_a_draft_whose_lead_went_back_to_work_is_not_merged_even_when_approved(self):
        code, out, _ = self.merge("1", prs=[pr(1, draft=True)], state="working")
        self.assertEqual(code, 1)
        self.assertIn("#1 stopped: task t1 is in phase motion (declared working)", out)
        self.assertEqual(self.gh_mutations(), [])


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
            reason = factory.keep_rule(None, {"path": f"{WT}/x"}, [])[0]
        self.assertEqual(reason, "this factory runs from it; leads started by it load their plugin from there")

    def test_another_worktree_is_judged_as_before(self):
        with mock.patch.object(factory, "PLUGIN_ROOT", Path(f"{WT}/xy/factory")):
            reason = factory.keep_rule(None, {"path": f"{WT}/x", "branch": None}, [])[0]
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
