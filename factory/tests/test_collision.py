"""The site manager sees collisions: the `collision` and `new-spec` criteria over real git, the `told` decision, its delivery
to a build run and the outcomes. Only `claude` and `gh` are faked."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import BATCH, DISPATCHER, FakeRun, factory, repo_answers
from test_health import COURIER_LINE, NOW, Harness, iso, lead_prompt, reply

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from isshoni import kaizen  # noqa: E402

REAL_RUN = factory.run
BODY = "".join(f"line {n}\n" for n in range(1, 41))
PR = {"state": "OPEN", "url": "https://github.com/o/r/pull/1", "headRefOid": "d" * 40, "baseRefName": "main", "isDraft": True}


def open_pr(number, branch, head):
    return {"number": number, "url": f"https://github.com/o/r/pull/{number}", "isDraft": True, "headRefName": branch, "headRefOid": head,
            "baseRefName": "main", "author": {"login": "u"}, "isCrossRepository": False, "latestReviews": [], "reviews": [], "comments": []}


def lines(text, at, to):
    """BODY with line `at` (1-based) replaced by `to`."""
    rows = text.splitlines(keepends=True)
    rows[at - 1] = f"{to}\n"
    return "".join(rows)


class Scratch(Harness):
    """A real repository with its remote, task worktrees under `.claude/worktrees`, and a faked `claude` and `gh`."""


    def setUp(self):
        super().setUp()
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.home / "repo").mkdir()
        self.root = str((self.home / "repo").resolve())
        remote = self.home / "remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(remote), cwd=self.home)
        self.git("init", "-q", "-b", "main", cwd=self.root)
        self.write(self.root, "f.txt", BODY)
        self.write(self.root, "g.txt", BODY)
        self.git("add", ".", cwd=self.root)
        self.git("commit", "-q", "-m", "init", cwd=self.root)
        self.git("remote", "add", "origin", str(remote), cwd=self.root)
        self.git("push", "-q", "-u", "origin", "main", cwd=self.root)
        self.git("remote", "set-head", "origin", "main", cwd=self.root)
        before = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, before)
        factory.write_lander(SimpleNamespace(root=self.root), {"health": NOW})  # every tick is between full passes
        self.prs, self.calls, self.leads = [], [], []
        self.answer = lambda: reply()

    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, where, name, text):
        Path(where, name).write_text(text)

    def task(self, slug, state="working", spec=None, age=0):
        """A worktree on `worktree-<slug>` declared `state`; with `spec`, its spec file, `age` seconds old."""
        path = f"{self.root}/.claude/worktrees/{slug}"
        self.git("worktree", "add", "-q", "-b", f"worktree-{slug}", path, "main", cwd=self.root)
        self.seed("tasks", [{"repo": self.root, "slug": slug, "state": state, "ts": iso(NOW - 600)}])
        if state == "working":  # a live lead, so `ended-undeclared` stays quiet
            self.leads.append({"sessionId": f"sid-{slug}", "name": slug, "cwd": path, "kind": "background", "pid": 7, "state": "working"})
        if spec:
            file = self.store / "specs" / "repo" / f"{slug}.md"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(spec)
            os.utime(file, (NOW - age, NOW - age))
        return path

    def commit(self, slug, name, text):
        path = f"{self.root}/.claude/worktrees/{slug}"
        self.write(path, name, text)
        self.git("add", name, cwd=path)
        self.git("commit", "-q", "-m", f"{slug} {name}", cwd=path)

    def main_moves(self, name, text):
        self.commit_root(name, text)
        self.git("push", "-q", "origin", "main", cwd=self.root)
        self.git("fetch", "-q", "origin", cwd=self.root)

    def commit_root(self, name, text):
        self.write(self.root, name, text)
        self.git("add", name, cwd=self.root)
        self.git("commit", "-q", "-m", f"main {name}", cwd=self.root)

    def cli(self, *argv):
        base = repo_answers([], [DISPATCHER | {"name": "dispatcher-repo", "cwd": self.root}, *self.leads])
        base[("claude", "-p")] = lambda: self.answer()
        base[BATCH] = (0, "[]", "")
        base[("gh", "pr", "list")] = lambda: (0, json.dumps([PR] if "--head" in self.fake.calls[-1] else self.prs), "")
        self.fake = FakeRun(base)
        route = lambda argv, **kw: REAL_RUN(argv, **kw) if argv[0] == "git" else self.fake(argv, **kw)  # noqa: E731
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(factory, "run", route), redirect_stdout(out), redirect_stderr(err):
            code = factory.main(list(argv))
        self.calls += self.fake.calls
        return code, out.getvalue(), err.getvalue()

    def tick(self):
        self.clock += 120
        return self.cli("watch")

    def fired(self, criterion=None):
        return [r for r in self.records() if "criterion" in r and criterion in (None, r["criterion"])]

    def claude_runs(self):
        return [c for c in self.calls if c[:2] == ["claude", "-p"]]

    def site_runs(self):
        return [c for c in self.claude_runs() if c[-1].startswith("Job: watch")]

    def build_runs(self):
        return [c for c in self.claude_runs() if c[-1].startswith("Task slug:")]

    def sees(self, decision="told", tell=None, reason="the other task rewrites these lines"):
        """The site manager's answers: `decision` on every firing it is sent, `tell` (default: the firing's slug)."""
        def answer():
            call = self.fake.calls[-1]
            if call[-1].startswith("Job:"):
                sent = json.loads(call[-1].partition("\n\n")[2])["firings"]
                return reply([{"id": f["id"], "decision": decision, "reason": reason, "looked": ["git diff"],
                               **({"tell": [f["slug"]] if tell is None else tell} if decision == "told" else {})} for f in sent])
            return 0, json.dumps({"result": "sent"}), ""
        self.answer = answer

    def taken(self, slug):
        """The courier's reply when the lead's session of `slug` took the line it was sent."""
        lead_prompt(COURIER_LINE.search(self.fake.calls[-1][-1])[1], cwd=f"{self.root}/.claude/worktrees/{slug}")
        return 0, json.dumps({"result": "sent"}), ""

    def build(self, slug, findings=(), outcome="built"):
        """One build run of `slug`, whose faked answer reports `findings`."""
        def answer():
            if self.fake.calls[-1][-1].startswith("Task slug:"):
                doc = {"outcome": outcome, "handover": "### Result\ndone", "findings": list(findings)}
                return 0, json.dumps({"subtype": "success", "session_id": "sid-b", "total_cost_usd": 1.0, "structured_output": doc}), ""
            return self.taken(slug)
        self.answer = answer
        self.spec(slug)
        return self.cli("build", slug, "--run", "--dispatcher", "dispatcher-repo")

    def spec(self, slug):
        file = self.store / "specs" / "repo" / f"{slug}.md"
        if not file.exists():
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("spec")

    def told_firing(self, fid, slug, criterion="collision", **kw):
        self.seed("firings", [{"ts": iso(self.clock - 60), "repo": self.root, "id": fid, "criterion": criterion, "escalate": "judgment",
                               "slug": slug, "subject": f"{slug}~x", "detail": "d", **kw},
                              {"ts": iso(self.clock - 30), "repo": self.root, "id": fid, "decision": "told", "by": "site-manager",
                               "reason": "merge the base first", "tell": [slug]}])


class CollisionTests(Scratch):
    def test_two_tasks_committed_to_the_same_lines_fire_once_and_the_finding_reaches_each_build_run(self):
        self.task("alpha")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.sees(tell=["alpha", "beta"])
        code, out, _ = self.tick()
        self.assertEqual(code, 0, out)
        [f] = self.fired()
        self.assertEqual((f["criterion"], f["slug"], f["subject"], f["files"]), ("collision", "alpha", "alpha~beta", ["f.txt"]))
        for part in ("alpha (working", "beta (working", "worktree-alpha", "worktree-beta", "f.txt: alpha changed lines 5, beta changed lines 5",
                     "f.txt (+1 -1)"):
            self.assertIn(part, f["detail"])
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertEqual((decision["decision"], decision["tell"], decision["by"]), ("told", ["alpha", "beta"], "site-manager"))
        self.assertEqual([c for c in self.fake.calls if c[:2] == ["claude", "-p"] and not c[-1].startswith("Job:")], [])  # nobody is messaged
        self.tick()
        self.assertEqual((len(self.site_runs()), len(self.fired())), (1, 1))  # decided: not fired again
        for slug in ("alpha", "beta"):
            self.build(slug)
            self.assertIn(f" The site manager found ({f['id']}): the other task rewrites these lines", self.build_runs()[-1][-1])
        self.assertEqual(sorted(r["delivered"] for r in self.records() if "delivered" in r), ["alpha", "beta"])
        self.build("alpha")
        self.assertNotIn("The site manager found", self.build_runs()[-1][-1])  # delivered once

    def test_a_decided_pair_fires_again_only_for_a_file_it_did_not_carry(self):
        self.task("alpha")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.sees()
        self.tick()
        self.commit("alpha", "f.txt", lines(BODY, 5, "again a"))  # the same file, the same lines
        self.tick()
        self.assertEqual(len(self.fired()), 1)
        self.commit("alpha", "g.txt", lines(BODY, 7, "from a"))
        self.commit("beta", "g.txt", lines(BODY, 7, "from b"))
        self.tick()
        [_, second] = self.fired()
        self.assertEqual(second["files"], ["f.txt", "g.txt"])
        self.assertEqual(len(self.site_runs()), 2)

    def test_the_same_large_file_changed_in_different_places_does_not_fire(self):
        self.task("alpha")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 3, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 37, "from b"))
        code, out, _ = self.tick()
        self.assertEqual((code, self.fired(), self.site_runs()), (0, [], []))
        self.assertIn("nothing new", out)

    def test_a_working_task_against_the_default_branch_and_what_the_default_branch_already_breaks_is_not_the_neighbours(self):
        self.task("alpha")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.commit_root("f.txt", lines(BODY, 5, "from main"))
        self.git("push", "-q", "origin", "main", cwd=self.root)
        self.git("fetch", "-q", "origin", cwd=self.root)
        self.tick()
        self.assertEqual([f["subject"] for f in self.fired()], ["alpha~main", "beta~main"])  # not alpha~beta: both already conflict with main there

    def test_a_handed_over_task_is_compared_with_working_ones_but_not_with_the_default_branch(self):
        self.task("alpha", state="handed-over")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.main_moves("f.txt", lines(BODY, 5, "from main"))
        self.tick()
        self.assertEqual([f["subject"] for f in self.fired()], ["beta~main"])

    def test_a_working_task_against_an_open_pull_request_that_is_no_task_and_a_head_git_cannot_read(self):
        self.task("alpha")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.git("worktree", "add", "-q", "-b", "elsewhere", f"{self.home}/elsewhere", "main", cwd=self.root)
        self.write(f"{self.home}/elsewhere", "f.txt", lines(BODY, 5, "from the pull request"))
        self.git("add", "f.txt", cwd=f"{self.home}/elsewhere")
        self.git("commit", "-q", "-m", "pr", cwd=f"{self.home}/elsewhere")
        head = self.git("rev-parse", "elsewhere", cwd=self.root)
        self.prs = [open_pr(7, "elsewhere", head), open_pr(8, "gone", "e" * 40),
                    open_pr(9, "worktree-alpha", self.git("rev-parse", "worktree-alpha", cwd=self.root))]
        self.tick()
        self.assertEqual([f["subject"] for f in self.fired()], ["alpha~#7"])

    def test_a_told_that_names_no_task_in_the_firing_is_not_a_decision(self):
        self.task("alpha")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        for tell in (["zzz"], [], ["alpha", "zzz"]):
            self.sees(tell=tell)
            factory.write_lander(SimpleNamespace(root=self.root), {"health": 0})  # a full pass sends the open firing again
            self.tick()
        self.assertEqual([r for r in self.records() if "decision" in r], [])
        self.assertEqual(len(self.fired()), 1)  # undecided: it comes back on a full pass, as today

    def test_a_suppressed_pair_is_decided_and_not_fired_again(self):
        self.task("alpha")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.sees("suppressed")
        self.tick()
        self.tick()
        self.assertEqual((len(self.fired()), len(self.site_runs())), (1, 1))

    def test_a_decided_firing_fires_again_on_the_next_tick_when_a_new_file_conflicts(self):
        self.task("alpha")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.sees()
        self.tick()
        self.commit("alpha", "g.txt", lines(BODY, 7, "from a"))
        self.commit("beta", "g.txt", lines(BODY, 7, "from b"))
        self.tick()
        self.assertEqual([f["files"] for f in self.fired()], [["f.txt"], ["f.txt", "g.txt"]])

    def test_a_decided_conflict_that_clears_and_comes_back_in_the_same_files_is_not_fired_again(self):
        self.task("alpha")
        self.task("beta")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.sees()
        self.tick()
        self.commit("beta", "f.txt", BODY)  # beta's net change is gone: no conflict
        self.tick()
        self.assertEqual([r.get("cleared") for r in self.records() if "cleared" in r], [True])
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.tick()
        self.assertEqual((len(self.fired()), len(self.site_runs())), (1, 1))

    def test_an_escalation_to_a_lead_on_a_collision_is_not_a_decision(self):
        path = self.task("alpha")
        self.task("beta")
        self.leads = [{"sessionId": "sid-a", "name": "alpha", "cwd": path, "kind": "background", "pid": 7, "state": "working"}]
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.commit("beta", "f.txt", lines(BODY, 5, "from b"))
        self.answer = lambda: reply([{"id": json.loads(self.fake.calls[-1][-1].partition("\n\n")[2])["firings"][0]["id"], "decision": "escalated",
                                      "to": "lead", "reason": "tell alpha", "looked": []}])
        self.tick()
        self.assertEqual((len(self.site_runs()), [r for r in self.records() if "decision" in r]), (1, []))

    def test_what_the_default_branch_already_breaks_is_not_a_task_and_pull_request_pair_s(self):
        self.task("alpha")
        self.commit("alpha", "f.txt", lines(BODY, 5, "from a"))
        self.git("worktree", "add", "-q", "-b", "elsewhere", f"{self.home}/elsewhere", "main", cwd=self.root)
        self.write(f"{self.home}/elsewhere", "f.txt", lines(BODY, 5, "from the pull request"))
        self.git("add", "f.txt", cwd=f"{self.home}/elsewhere")
        self.git("commit", "-q", "-m", "pr", cwd=f"{self.home}/elsewhere")
        self.prs = [open_pr(7, "elsewhere", self.git("rev-parse", "elsewhere", cwd=self.root))]
        self.main_moves("f.txt", lines(BODY, 5, "from main"))
        self.tick()
        self.assertEqual([f["subject"] for f in self.fired()], ["alpha~main"])  # not alpha~#7: both already conflict with main there


class NewSpecTests(Scratch):
    def test_the_newer_spec_fires_once_and_the_finding_is_delivered(self):
        self.task("alpha", spec="builds the thing", age=600)
        self.commit("alpha", "g.txt", lines(BODY, 20, "from a"))
        self.tick()
        self.assertEqual(self.fired(), [])  # alone
        self.task("beta", spec="rewrites the thing")
        self.sees(tell=["beta"], reason="a already changed g.txt line 20")
        self.tick()
        [f] = self.fired()
        self.assertEqual((f["criterion"], f["slug"], f["subject"], "files" in f), ("new-spec", "beta", "beta", False))
        for part in (str(self.store / "specs" / "repo" / "beta.md"), str(self.store / "specs" / "repo" / "alpha.md"), "alpha (working",
                     "g.txt (+1 -1)"):
            self.assertIn(part, f["detail"])
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertEqual((decision["decision"], decision["tell"]), ("told", ["beta"]))
        self.tick()
        self.assertEqual((len(self.fired()), len(self.site_runs())), (1, 1))
        self.build("beta")
        self.assertIn(f" The site manager found ({f['id']}): a already changed g.txt line 20", self.build_runs()[-1][-1])

    def test_two_specs_with_no_commits_are_looked_at_once_when_the_later_one_appears(self):
        self.task("alpha", spec="one", age=600)
        self.task("beta", spec="two", age=300)
        self.tick()
        self.assertEqual([f["slug"] for f in self.fired()], ["beta"])

    def test_a_task_without_a_spec_or_another_task_to_compare_fires_nothing(self):
        self.task("alpha")
        self.task("beta", spec="two")
        self.tick()
        self.assertEqual(self.fired(), [])

    def test_a_decided_new_spec_firing_that_was_cleared_is_not_fired_again_for_a_new_task(self):
        self.task("alpha", spec="builds the thing", age=600)
        self.commit("alpha", "g.txt", lines(BODY, 20, "from a"))
        self.task("beta", spec="rewrites the thing")
        self.seed("firings", [{"ts": iso(self.clock - 60), "repo": self.root, "id": "n1", "criterion": "new-spec", "escalate": "judgment",
                               "slug": "beta", "subject": "beta", "detail": "d"},
                              {"ts": iso(self.clock - 30), "repo": self.root, "id": "n1", "decision": "told", "by": "site-manager",
                               "reason": "r", "tell": ["beta"]},
                              {"ts": iso(self.clock - 20), "repo": self.root, "id": "n1", "cleared": True}])
        self.task("gamma")
        self.tick()
        self.assertEqual(([f["id"] for f in self.fired()], self.site_runs()), (["n1"], []))

    def test_a_handed_over_task_whose_branch_merged_is_not_in_flight_and_one_with_no_commit_is(self):
        self.task("alpha", state="handed-over", spec="builds the thing", age=600)
        self.commit("alpha", "g.txt", lines(BODY, 20, "from a"))
        self.task("beta")
        self.git("merge", "-q", "--no-edit", "worktree-alpha", cwd=self.root)
        self.git("push", "-q", "origin", "main", cwd=self.root)
        self.git("fetch", "-q", "origin", cwd=self.root)
        self.assertEqual(factory.in_flight(factory.Repo()), {"beta": "working"})

    def test_a_merged_task_gets_no_new_spec_look(self):
        self.task("alpha", state="handed-over", spec="builds the thing", age=600)
        self.commit("alpha", "g.txt", lines(BODY, 20, "from a"))
        self.git("merge", "-q", "--no-edit", "worktree-alpha", cwd=self.root)
        self.git("push", "-q", "origin", "main", cwd=self.root)
        self.git("fetch", "-q", "origin", cwd=self.root)
        self.task("beta", spec="rewrites the thing")
        self.tick()
        self.assertEqual(self.fired(), [])

    def test_a_working_task_fast_forwarded_to_the_default_branch_is_still_looked_at_and_may_be_told(self):
        self.task("alpha", spec="builds the thing", age=600)
        self.main_moves("g.txt", lines(BODY, 20, "from main"))
        self.git("merge", "-q", "--ff-only", "origin/main", cwd=f"{self.root}/.claude/worktrees/alpha")
        self.task("beta", spec="rewrites the thing")
        self.sees(tell=["alpha"], reason="beta rewrites the thing")
        self.tick()
        [f] = self.fired("new-spec")
        self.assertIn("alpha (working", f["detail"])
        [decision] = [r for r in self.records() if "decision" in r]
        self.assertEqual((decision["decision"], decision["tell"]), ("told", ["alpha"]))


class OutcomeTests(Scratch):
    def test_a_build_run_marks_a_delivered_finding_acted_or_noise(self):
        self.task("alpha")
        self.task("beta")
        self.told_firing("f1", "alpha")
        self.told_firing("f2", "alpha")
        self.told_firing("f3", "beta")
        self.build("alpha", findings=[{"id": "f1", "outcome": "acted", "note": "merged main first"},
                                  {"id": "f2", "outcome": "noise", "note": "other lines"},
                                  {"id": "f3", "outcome": "acted", "note": "not delivered to a: ignored"}])
        done = {f["id"]: (f["outcome"], f["delivered"]) for f in factory.fold(self.records())[0]}
        self.assertEqual(done, {"f1": ("acted", ["alpha"]), "f2": ("noise", ["alpha"]), "f3": (None, [])})
        [acted] = [r for r in self.records() if r.get("outcome") == "acted"]
        self.assertEqual((acted["by"], acted["slug"], acted["note"]), ("tech-lead", "alpha", "merged main first"))

    def test_noise_is_not_written_over_an_outcome_nor_acted_over_collided(self):
        self.task("alpha")
        self.told_firing("f1", "alpha")
        self.told_firing("f2", "alpha")
        self.seed("firings", [{"ts": iso(NOW), "repo": self.root, "id": "f1", "outcome": "acted"},
                              {"ts": iso(NOW), "repo": self.root, "id": "f2", "outcome": "collided"}])
        self.build("alpha", findings=[{"id": "f1", "outcome": "noise", "note": "x"}, {"id": "f2", "outcome": "acted", "note": "y"}])
        self.assertEqual({f["id"]: f["outcome"] for f in factory.fold(self.records())[0]}, {"f1": "acted", "f2": "collided"})

    def test_a_finding_that_arrives_during_a_run_waits_and_the_wake_line_says_so(self):
        self.task("alpha")
        run = self.build("alpha")
        self.assertEqual(run[0], 0)

        def arrives():
            call = self.fake.calls[-1]
            if call[-1].startswith("Task slug:"):
                self.told_firing("late", "alpha")
                return 0, json.dumps({"subtype": "success", "structured_output": {"outcome": "built", "handover": "ok"}}), ""
            return self.taken("alpha")
        self.answer = arrives
        self.cli("build", "alpha", "--run", "--dispatcher", "dispatcher-repo")
        wake = [c[-1] for c in self.claude_runs() if "build run 2 built" in c[-1]]
        self.assertTrue(wake and "1 site manager finding(s) arrived during the run: the next build run gets them." in wake[0], wake)
        self.assertEqual(len(factory.pending_findings(SimpleNamespace(root=self.root), "alpha")), 1)

    def test_a_queue_conflict_stop_on_a_told_task_in_the_findings_files_marks_it_collided(self):
        repo = SimpleNamespace(root=self.root)
        self.told_firing("f1", "alpha", files=["f.txt"])
        self.told_firing("f2", "alpha", files=["f.txt"])
        self.told_firing("f3", "alpha", criterion="new-spec")
        queue = [{"ts": iso(self.clock - 100), "repo": self.root, "slug": "alpha", "pr": 1, "head": "h", "step": "sent-back", "kind": "conflict",
                  "files": ["f.txt"]},  # before the decision: not this finding's
                 {"ts": iso(self.clock), "repo": self.root, "slug": "alpha", "pr": 1, "head": "h", "step": "sent-back", "kind": "ci"},
                 {"ts": iso(self.clock), "repo": self.root, "slug": "beta", "pr": 2, "head": "h", "step": "sent-back", "kind": "conflict",
                  "files": ["f.txt"]},
                 {"ts": iso(self.clock), "repo": self.root, "slug": "alpha", "pr": 1, "head": "h", "step": "sent-back", "kind": "conflict",
                  "files": ["f.txt"]}]
        self.seed("queue", queue[:1])
        factory.settle_collisions({"repo": repo})
        self.assertEqual([f["outcome"] for f in factory.fold(self.records())[0]], [None, None, None])
        self.seed("queue", queue[1:3])
        factory.settle_collisions({"repo": repo})
        self.assertEqual([f["outcome"] for f in factory.fold(self.records())[0]], [None, None, None])
        self.seed("queue", queue[3:])
        factory.settle_collisions({"repo": repo})
        self.assertEqual([f["outcome"] for f in factory.fold(self.records())[0]], ["collided", "collided", None])

    def test_collided_is_reached_by_a_full_pass_watch_tick(self):
        self.told_firing("f1", "alpha", files=["f.txt"])
        self.seed("queue", [{"ts": iso(self.clock), "repo": self.root, "slug": "alpha", "pr": 1, "head": "h", "step": "sent-back",
                             "kind": "conflict", "files": ["f.txt"]}])
        factory.write_lander(SimpleNamespace(root=self.root), {"health": 0})  # the next tick is a full pass
        self.tick()
        self.assertEqual([f["outcome"] for f in factory.fold(self.records())[0]], ["collided"])

    def test_firings_prints_the_counts_and_told_is_not_an_escalation(self):
        for fid in ("t1", "t2", "t3", "t4", "t5"):
            self.told_firing(fid, "alpha")
        self.seed("firings", [{"ts": iso(NOW), "repo": self.root, "id": "t1", "outcome": "acted"},
                              {"ts": iso(NOW), "repo": self.root, "id": "t2", "outcome": "noise"},
                              {"ts": iso(NOW), "repo": self.root, "id": "t3", "outcome": "collided"},
                              {"ts": iso(NOW), "repo": self.root, "id": "t1", "delivered": "alpha", "build_run": 1},
                              {"ts": iso(NOW), "repo": self.root, "id": "t2", "delivered": "alpha", "build_run": 1}])
        out = self.cli("firings")[1]
        self.assertTrue(out.rstrip().endswith("told 5 (acted 1, noise 1, collided 1, no outcome 2, undelivered 3)"), out)
        s = json.loads(self.cli("firings", "--json")[1])["summary"]
        self.assertEqual((s["escalated"], s["precision"], s["recall"], s["told_acted"], s["told_undelivered"]), (0, None, None, 1, 3))

    def test_the_dashboards_site_manager_counts_ignore_told_records(self):
        base = [{"ts": "2026-11-10T10:00:00Z", "repo": self.root, "id": "f1", "criterion": "quiet", "slug": "alpha", "subject": "alpha"},
                {"ts": "2026-11-10T10:01:00Z", "repo": self.root, "id": "f1", "decision": "escalated", "by": "watch"}]
        told = [{"ts": "2026-11-10T10:02:00Z", "repo": self.root, "id": "f2", "criterion": "collision", "slug": "alpha", "subject": "alpha~beta",
                 "files": ["f.txt"]},
                {"ts": "2026-11-10T10:03:00Z", "repo": self.root, "id": "f2", "decision": "told", "tell": ["alpha"], "reason": "r"},
                {"ts": "2026-11-10T10:04:00Z", "repo": self.root, "id": "f2", "delivered": "alpha", "build_run": 1},
                {"ts": "2026-11-10T10:05:00Z", "repo": self.root, "id": "f2", "outcome": "acted", "by": "tech-lead"}]
        self.assertEqual(kaizen._site_manager(base + told, None), kaizen._site_manager(base, None))


if __name__ == "__main__":
    unittest.main()
