import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from test_factory import ROOT, FakeRun, factory
from test_merge import World, pr

V, STACKS = factory.STACKS_VERSION, factory.STACKS
REPO = SimpleNamespace(root=ROOT, default="main")
LIST = ("gh", "api", "-H", V, f"{STACKS}?per_page=100")
UNSTACK = ("gh", "api", "-H", V, "-i", "-X", "POST", f"{STACKS}/7/unstack")
NO_CONTENT = (0, "HTTP/2.0 204 No Content\n\n", "")
MERGED = (0, json.dumps([{"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), "")


def node(n, base, head, fork=False):
    return {"number": n, "baseRefName": base, "headRefName": head, "isCrossRepository": fork}


def stack(number, *members):
    return {"number": number, "open": True, "pull_requests": [{"number": n} for n in members]}


def posts(fake):
    return [c for c in fake.calls if "POST" in c]


class EnsureTests(unittest.TestCase):
    def ensure(self, snap, stacks=(), answers=None):
        fake = FakeRun({LIST: (0, json.dumps(list(stacks)), ""), ("gh", "api", "-H", V): (0, "{}", ""), **(answers or {})})
        out = io.StringIO()
        with mock.patch.object(factory, "run", fake), redirect_stdout(out):
            factory.ensure_stacks(REPO, snap)
        return fake, out.getvalue()

    def test_a_chain_without_a_stack_is_created_bottom_to_top(self):
        fake, _ = self.ensure([node(1, "main", "a"), node(2, "a", "b")])
        self.assertEqual(posts(fake), [["gh", "api", "-H", V, STACKS, "-X", "POST", "-F", "pull_requests[]=1",
                                        "-F", "pull_requests[]=2"]])

    def test_a_chain_that_is_one_stack_gets_no_call(self):
        fake, _ = self.ensure([node(1, "main", "a"), node(2, "a", "b")], [stack(5, 1, 2)])
        self.assertEqual(posts(fake), [])

    def test_a_new_top_is_added_to_the_stack_below_it(self):
        fake, _ = self.ensure([node(1, "main", "a"), node(2, "a", "b"), node(3, "b", "c")], [stack(5, 1, 2)])
        self.assertEqual(posts(fake), [["gh", "api", "-H", V, f"{STACKS}/5/add", "-X", "POST", "-F", "pull_requests[]=3"]])

    def test_siblings_end_the_chain(self):
        fake, _ = self.ensure([node(1, "main", "a"), node(2, "a", "b"), node(3, "a", "c")])
        self.assertEqual(fake.calls, [])

    def test_only_a_chain_from_the_default_branch_and_no_fork_joins(self):
        fake, _ = self.ensure([node(1, "dev", "a"), node(2, "a", "b"), node(3, "main", "c"), node(4, "c", "d", fork=True),
                               node(5, "main", "e", fork=True), node(6, "e", "f")])
        self.assertEqual(fake.calls, [])

    def test_a_stack_that_is_not_exactly_the_chains_lower_members_gets_no_call(self):
        fake, _ = self.ensure([node(1, "main", "a"), node(2, "a", "b"), node(3, "b", "c")], [stack(5, 1, 9)])
        self.assertEqual(posts(fake), [])

    def test_a_member_above_the_bottom_already_in_a_stack_gets_no_call(self):
        fake, _ = self.ensure([node(1, "main", "a"), node(2, "a", "b")], [stack(9, 2)])
        self.assertEqual(posts(fake), [])

    def test_a_closed_stack_is_not_counted(self):
        closed = {**stack(5, 1, 2), "open": False}
        fake, _ = self.ensure([node(1, "main", "a"), node(2, "a", "b")], [closed])
        self.assertEqual(len(posts(fake)), 1)

    def test_a_failing_stacks_call_prints_one_line(self):
        fake, out = self.ensure([node(1, "main", "a"), node(2, "a", "b")], answers={("gh", "api", "-H", V, STACKS): (1, "", "boom")})
        self.assertEqual(out.count("lander: stacks:"), 1)
        self.assertIn("boom", out)
        fake, out = self.ensure([node(1, "main", "a"), node(2, "a", "b")], answers={LIST: (1, "", "down")})
        self.assertEqual((out.count("lander: stacks:"), posts(fake)), (1, []))

    def test_the_pass_goes_on_after_a_failing_stacks_call(self):
        snap = [node(1, "main", "a"), node(2, "a", "b")]
        lander_pass = mock.Mock(return_value=(False, {}, []))
        fake = FakeRun({("gh", "api"): (1, "", "boom")})
        out = io.StringIO()
        with mock.patch.object(factory, "run", fake), redirect_stdout(out), \
                mock.patch.multiple(factory, end_priorities=mock.DEFAULT, release_paused=mock.DEFAULT,
                                    gather=mock.DEFAULT, tasks=mock.DEFAULT, read_word=mock.DEFAULT,
                                    snapshot=mock.Mock(return_value=snap), lander_queue=mock.Mock(return_value=[]),
                                    lander_pass=lander_pass):
            factory.drain(REPO, None)
        lander_pass.assert_called_once()
        self.assertEqual(out.getvalue().count("lander: stacks:"), 1)


class DissolveTests(World):
    def stacked(self, extra=None):
        listing = (0, json.dumps([stack(7, 1, 2)]), "")
        return {("gh", "api", "-H", V, f"{STACKS}?pull_request=1"): listing,
                ("gh", "api", "-H", V, f"{STACKS}?pull_request=2"): listing, UNSTACK: NO_CONTENT, **(extra or {})}

    def index(self, *prefix):
        return next(i for i, c in enumerate(self.fake.calls) if c[:len(prefix)] == list(prefix))

    def unstacks(self):
        return [c for c in self.fake.calls if c[-1].endswith("/unstack")]

    def test_a_stacked_pull_request_is_unstacked_before_it_is_merged(self):
        code, _, _ = self.merge("1", prs=[pr(1)], extra=self.stacked())
        self.assertEqual(code, 0)
        self.assertEqual(len(self.unstacks()), 1)
        self.assertLess(self.index("gh", "api", "-H", V, "-i"), self.index("gh", "pr", "merge"))
        self.assertLess(self.index("gh", "pr", "view"), self.index("gh", "api", "-H", V, "-i"))

    def test_a_dry_run_and_a_stopped_gate_unstack_nothing(self):
        code, _, _ = self.merge("1", "--dry-run", prs=[pr(1)], extra=self.stacked())
        self.assertEqual((code, self.unstacks()), (0, []))
        code, _, _ = self.merge("1", prs=[pr(1)], mergeable="CONFLICTING", extra=self.stacked())
        self.assertEqual((code, self.unstacks()), (1, []))
        code, _, _ = self.merge("1", prs=[pr(1)], checks=[], extra=self.stacked())
        self.assertEqual((code, self.unstacks()), (1, []))

    def test_a_merged_base_is_unstacked_before_the_retarget(self):
        extra = self.stacked({("gh", "pr", "list", "--head", "worktree-t1"): MERGED})
        code, _, _ = self.merge("2", prs=[pr(2, base="worktree-t1")], extra=extra)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.unstacks()), 1)
        self.assertLess(self.index("gh", "api", "-H", V, "-i"), self.index("gh", "pr", "edit"))

    def test_the_landing_unstacks_before_it_retargets(self):
        landing = factory.Landing.__new__(factory.Landing)
        landing.repo, landing.url, landing.pr_base, landing.target_branch = REPO, "https://x/pull/1", "dev", "main"
        fake = FakeRun({("gh", "api", "-H", V, f"{STACKS}?pull_request=1"): (0, json.dumps([stack(7, 1, 2)]), ""),
                        UNSTACK: NO_CONTENT, ("gh", "pr", "edit"): (0, "", "")})
        with mock.patch.object(factory, "run", fake):
            self.assertIsNone(landing.retarget())
        self.assertEqual([c[-1].rpartition("/")[2] if c[1] == "api" else c[1] for c in fake.calls],
                         ["stacks?pull_request=1", "unstack", "pr"])

    def test_a_failed_unstack_stops_before_any_edit_or_merge(self):
        for answer in ((1, "", "locked"), (0, "HTTP/2.0 200 OK\n\n{}", "")):
            with self.subTest(answer=answer):
                self.setUp()
                code, out, _ = self.merge("1", prs=[pr(1)], extra=self.stacked({UNSTACK: answer}))
                self.assertEqual(code, 1)
                self.assertIn("#1 stopped: gh api", out)
                self.assertEqual(self.gh_mutations(), [])
                self.setUp()
                extra = self.stacked({UNSTACK: answer, ("gh", "pr", "list", "--head", "worktree-t1"): MERGED})
                code, _, _ = self.merge("2", prs=[pr(2, base="worktree-t1")], extra=extra)
                self.assertEqual((code, self.gh_mutations()), (1, []))
        landing = factory.Landing.__new__(factory.Landing)
        landing.repo, landing.url, landing.pr_base, landing.target_branch = REPO, "https://x/pull/1", "dev", "main"
        fake = FakeRun({("gh", "api", "-H", V, f"{STACKS}?pull_request=1"): (0, json.dumps([stack(7, 1, 2)]), ""),
                        UNSTACK: (1, "", "locked")})
        with mock.patch.object(factory, "run", fake):
            self.assertIn("locked", landing.retarget())
        self.assertFalse(fake.ran("gh", "pr", "edit"))


if __name__ == "__main__":
    unittest.main()
