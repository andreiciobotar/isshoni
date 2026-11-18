import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from test_factory import ROOT, FakeRun, factory
from test_merge import World, pr

V, STACKS = factory.STACKS_VERSION, factory.STACKS
REPO = SimpleNamespace(root=ROOT, default="main", tasks=[{"path": f"/w/{h}", "branch": h} for h in "abcdef"])
LIST = ("gh", "api", "-H", V, f"{STACKS}?per_page=100")
UNSTACK = ("gh", "api", "-H", V, "-i", "-X", "POST", f"{STACKS}/7/unstack")
NO_CONTENT = (0, "HTTP/2.0 204 No Content\n\n", "")
MERGED = (0, json.dumps([{"state": "MERGED", "url": "u", "headRefOid": "z", "baseRefName": "main"}]), "")


def node(n, base, head, fork=False):
    return {"number": n, "baseRefName": base, "headRefName": head, "isCrossRepository": fork}


def stack(number, *members):
    return {"number": number, "open": True, "pull_requests": [{"number": n, "state": "open"} for n in members]}


def posts(fake):
    return [c for c in fake.calls if "POST" in c]


class EnsureTests(unittest.TestCase):
    def ensure(self, snap, stacks=(), answers=None, repo=REPO):
        fake = FakeRun({LIST: (0, json.dumps(list(stacks)), ""), ("gh", "api", "-H", V): (0, "{}", ""), **(answers or {})})
        out = io.StringIO()
        with mock.patch.object(factory, "run", fake), redirect_stdout(out):
            factory.ensure_stacks(repo, snap)
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

    def test_a_chain_with_no_task_on_its_heads_gets_no_call(self):
        fake, _ = self.ensure([node(1, "main", "x"), node(2, "x", "y")])
        self.assertEqual(fake.calls, [])

    def test_a_chain_with_a_foreign_member_gets_no_post_even_beside_an_existing_stack(self):
        for foreign in range(3):
            heads = ["a", "b", "c"]
            heads[foreign] = "x"
            snap = [node(1, "main", heads[0]), node(2, heads[0], heads[1]), node(3, heads[1], heads[2])]
            for stacks in ([], [stack(5, 1, 2)]):
                with self.subTest(foreign=foreign, stacks=stacks):
                    fake, _ = self.ensure(snap, stacks)
                    self.assertEqual(posts(fake), [])

    def test_a_chain_with_a_member_of_another_number_than_its_tasks_recorded_one_gets_no_call(self):
        snap = [node(1, "main", "a"), node(2, "a", "b")]
        for recorded, posted in (("2", 1), ("9", 0)):
            with self.subTest(recorded=recorded):
                tasks = [{**t, "pr": recorded} if t["branch"] == "b" else t for t in REPO.tasks]
                fake, _ = self.ensure(snap, repo=SimpleNamespace(root=ROOT, default="main", tasks=tasks))
                self.assertEqual(len(posts(fake)), posted)

    def test_an_owned_chain_is_stacked_beside_a_foreign_chain(self):
        snap = [node(1, "main", "a"), node(2, "a", "b"), node(3, "main", "x"), node(4, "x", "y")]
        fake, _ = self.ensure(snap)
        self.assertEqual(posts(fake), [["gh", "api", "-H", V, STACKS, "-X", "POST", "-F", "pull_requests[]=1",
                                        "-F", "pull_requests[]=2"]])
        snap = [node(1, "main", "a"), node(2, "a", "b"), node(3, "b", "c"), node(4, "main", "x"), node(5, "x", "y")]
        fake, _ = self.ensure(snap, [stack(5, 1, 2)])
        self.assertEqual(posts(fake), [["gh", "api", "-H", V, f"{STACKS}/5/add", "-X", "POST", "-F", "pull_requests[]=3"]])

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

    def test_the_bottom_of_a_stack_is_not_unstacked_by_a_dry_run_or_a_stopped_gate(self):
        extra = self.stacked({("gh", "pr", "view"): (1, "", "boom")})
        code, _, _ = self.merge("1", "--dry-run", prs=[pr(1)], extra=extra)
        self.assertEqual((code, self.unstacks(), self.gh_mutations()), (1, [], []))
        code, _, _ = self.merge("1", prs=[pr(1)], extra=extra)
        self.assertEqual((code, self.unstacks(), self.gh_mutations()), (1, [], []))

    def test_a_stack_member_on_a_merged_base_is_left_alone(self):
        extra = self.stacked({("gh", "pr", "list", "--head", "worktree-t1"): MERGED})
        code, out, _ = self.merge("2", prs=[pr(2, base="worktree-t1")], extra=extra)
        self.assertEqual((code, self.unstacks(), self.gh_mutations()), (1, [], []))
        self.assertNotIn("base changed", out)

    def test_the_landing_unstacks_before_it_retargets(self):
        landing = factory.Landing.__new__(factory.Landing)
        landing.repo, landing.url, landing.pr_base, landing.target_branch = REPO, "https://x/pull/1", "dev", "main"
        fake = FakeRun({("gh", "api", "-H", V, f"{STACKS}?pull_request=1"): (0, json.dumps([stack(7, 1, 2)]), ""),
                        UNSTACK: NO_CONTENT, ("gh", "pr", "edit"): (0, "", "")})
        with mock.patch.object(factory, "run", fake):
            self.assertIsNone(landing.retarget())
        self.assertEqual([c[-1].rpartition("/")[2] if c[1] == "api" else c[1] for c in fake.calls],
                         ["stacks?pull_request=1", "unstack", "pr"])

    def test_a_failed_unstack_stops_the_landing_before_any_edit(self):
        landing = factory.Landing.__new__(factory.Landing)
        landing.repo, landing.url, landing.pr_base, landing.target_branch = REPO, "https://x/pull/1", "dev", "main"
        fake = FakeRun({("gh", "api", "-H", V, f"{STACKS}?pull_request=1"): (0, json.dumps([stack(7, 1, 2)]), ""),
                        UNSTACK: (1, "", "locked")})
        with mock.patch.object(factory, "run", fake):
            self.assertIn("locked", landing.retarget())
        self.assertFalse(fake.ran("gh", "pr", "edit"))


if __name__ == "__main__":
    unittest.main()
