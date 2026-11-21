import json
import os
import unittest
from datetime import datetime, timezone

from isshoni import cli, friction, kaizen
from tests.test_kaizen import ALPHA, assistant, came_back, ev, loose, session
from tests.test_pulse import NOW, PulseStore

DAY = "2026-11-10"
OLD = "2026-11-02T10:00:00Z"  # a merge long before NOW: the pulse baseline reaches it, so the documents read the caches only (cached=True)
GUARD = "This session is isolated in the worktree /r/alpha/.claude/worktrees/x. Run it there. Refusing to run it: the guard."
CLASSIFIER = "Permission for this action was denied by the Claude Code auto mode classifier: too risky."


def at(hhmm, sec="00"):
    return "%sT%s:%s.000Z" % (DAY, hhmm, sec)


def use(ts, mid, tid=None, name="Bash", command="ls"):
    """A priced main-thread call (1M input tokens: 10.0 on the lead's model) that starts a tool call."""
    block = [{"type": "tool_use", "id": tid, "name": name, "input": {"command": command}}] if tid else None
    return assistant(ts, mid, tokens=1000000, content=block)


def result(ts, tid, text):
    return {"type": "user", "timestamp": ts, "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def append(path, lines):
    with open(path, "a") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")


class FrictionStore(PulseStore):
    def setUp(self):
        super().setUp()
        self.retros = []

    def lead_file(self, slug):
        return os.path.join(self.projects, "p-" + slug, "s-" + slug + ".jsonl")

    def document(self, cutoff=None):
        self.log("events-2026-11.jsonl", self.events)
        self.log("tasks-2026-11.jsonl", self.states)
        self.log("queue-2026-11.jsonl", self.queued)
        self.log("retros-2026-11.jsonl", self.retros)
        return self.build(lambda path: self.prs.get(path, []), now=NOW, cutoff=cutoff, cached=True)

    def friction(self, cutoff=None):
        return self.document(cutoff)["friction"]

    def lead(self, slug, dispatches=(), started="2026-11-01T00:00:00Z"):
        self.task(slug, 1, OLD, started=started)
        session(self.projects, ALPHA, slug, at("10:00", "05"), 1000000, dispatches)

    def roles(self, f):
        return {r["role"]: r for r in f["refusals"]["roles"]}


class DispatcherTests(FrictionStore):
    def setUp(self):
        super().setUp()
        self.task("early", 1, OLD)
        self.task("late", 2, OLD, started="2026-11-10T11:30:00Z")
        loose(self.projects, "disp", ALPHA, [(at("10:00"), "claude-fable-5-2", 1000000), (at("11:00"), "claude-fable-5-2", 3000000),
                                             (at("12:00"), "claude-fable-5-2", 2000000)],
              title="dispatcher-alpha", subagent=[(at("10:30"), "claude-fable-5-2", 5000000)])

    def test_the_main_thread_alone_costs_per_call_with_its_median_context(self):
        self.assertEqual(self.friction()["dispatcher"], {
            "calls": 3, "cost": 60.0, "per_call": 20.0, "context_median": 2000000.0, "unpriced_tokens": 0})  # the subagent's call is not one

    def test_only_calls_at_or_after_the_cutoff_count(self):
        d = self.friction(cutoff=datetime(2026, 11, 10, 11, tzinfo=timezone.utc))["dispatcher"]
        self.assertEqual((d["calls"], d["cost"], d["per_call"], d["context_median"]), (2, 50.0, 25.0, 2500000.0))

    def test_no_dispatcher_session_is_null_not_zero(self):
        os.remove(os.path.join(self.projects, "p-disp", "disp.jsonl"))
        self.assertEqual(self.friction()["dispatcher"], {"calls": 0, "cost": 0.0, "per_call": None, "context_median": None, "unpriced_tokens": 0})


class ReworkTests(FrictionStore):
    def setUp(self):
        super().setUp()
        self.lead("r1", dispatches=[("reviewer", "claude-opus-5-5", 1000000, "10:20", "10:25"), ("coder", "claude-haiku-5-5", 1000000, "10:30", "10:35")])
        append(self.lead_file("r1"), [use(at("10:40"), "late-lead")])  # the lead's own call after the review
        self.lead("r2")

    def test_after_review_holds_every_call_after_the_first_review_the_lead_included(self):
        costs = {t["slug"]: t["cost"] for t in self.document()["tasks"]}
        c = costs["r1"]
        self.assertEqual((c["total"], c["rework"], c["after_review"]), (26.0, 1.0, 11.0))  # coder 1 + lead 10; subagents only: 1
        self.assertIsNone(costs["r2"]["after_review"])

    def test_rework_is_over_the_tasks_with_a_cost(self):
        self.assertEqual(self.friction()["rework"], {"cost": 11.0, "task_cost": 36.0, "share": 0.305556, "per_task_median": 11.0, "n": 1})


class CausesTests(FrictionStore):
    def test_the_newest_retro_counts_by_cause_and_anything_else_is_unstated(self):
        self.task("c1", 1, OLD)
        self.task("c2", 2, OLD)
        self.retros = [
            ev("2026-11-03T10:00:00Z", "", slug="c1", defects=[{"cause": "code"}] * 5),  # superseded
            ev("2026-11-04T10:00:00Z", "", slug="c1", defects=[{"cause": "spec"}, {"cause": "spec"}, {"cause": "weird"}, {"caught_by": "lead"}, "x"]),
            ev("2026-11-04T10:00:00Z", "", slug="c2", defects=[{"cause": "conflict"}, {"cause": 7}])]
        self.assertEqual(self.friction()["causes"], {"spec": 2, "brief": 0, "conflict": 1, "scope": 0, "code": 0, "unstated": 3})


class RefusalTests(FrictionStore):
    def test_a_guard_and_a_classifier_refusal_are_parsed_from_the_transcript_and_a_window_overlap_counts_once(self):
        self.lead("t1")
        append(self.lead_file("t1"), [
            use(at("10:01"), "b1", "t1"), result(at("10:01", "01"), "t1", GUARD),
            use(at("10:02"), "b2", "t2"), result(at("10:02", "01"), "t2", "\n  " + CLASSIFIER),  # left whitespace is ignored
            use(at("10:03"), "b3", "t3", name="Read"), result(at("10:03", "01"), "t3", GUARD),  # not a Bash result
            use(at("10:04"), "b4", "t4"), result(at("10:04", "01"), "t4", "Error: " + GUARD),  # contains the text, does not start with it
            use(at("10:05"), "b5"), use(at("10:06"), "b6")])
        row = self.roles(self.friction())["lead"]
        # the calls after the refusals: 10:02, 10:03, 10:04 and 10:03, 10:04, 10:05: four calls at 10.0, not six
        self.assertEqual(row, {"role": "lead", "bash": 3, "guard": 1, "classifier": 1, "share": 0.666667, "after_cost": 40.0})

    def test_the_whole_run_counts_the_bash_calls_but_only_refusals_in_range_count(self):
        self.lead("t1", started="2026-11-10T10:02:00Z")
        append(self.lead_file("t1"), [use(at("10:01"), "b1", "t1"), result(at("10:01", "01"), "t1", GUARD), use(at("10:02"), "b2", "t2"),
                                      result(at("10:02", "01"), "t2", GUARD)])
        row = self.roles(self.friction(cutoff=datetime(2026, 11, 10, 10, 2, tzinfo=timezone.utc)))["lead"]
        self.assertEqual((row["bash"], row["guard"]), (2, 1))

    def test_a_dispatcher_session_with_no_call_in_range_adds_no_bash_calls(self):
        self.task("late", 2, OLD, started="2026-11-10T11:30:00Z")
        loose(self.projects, "disp", ALPHA, [(at("10:00"), "claude-fable-5-2", 1000000)], title="dispatcher-alpha")
        append(os.path.join(self.projects, "p-disp", "disp.jsonl"), [use(at("10:10"), "d1", "dt1")])
        self.assertEqual(self.roles(self.friction())["dispatcher"]["bash"], 1)
        self.assertNotIn("dispatcher", self.roles(self.friction(cutoff=datetime(2026, 11, 10, 11, tzinfo=timezone.utc))))

    def test_a_tech_lead_session_is_attributed_to_the_tech_lead(self):
        self.lead("t1")
        self.log("techlead-2026-11.jsonl", [came_back(at("11:00"), "resolved", slug="t1", session="s-t1")])
        append(self.lead_file("t1"), [use(at("10:01"), "b1", "t1"), result(at("10:01", "01"), "t1", GUARD)])
        row = self.roles(self.friction())
        self.assertEqual((row["tech-lead"]["bash"], row["tech-lead"]["guard"]), (1, 1))
        self.assertNotIn("lead", row)

    def test_a_dispatch_is_attributed_to_its_role_and_a_role_with_no_bash_call_is_left_out(self):
        self.lead("t1", dispatches=[("coder", "claude-haiku-5-5", 1000000, "10:30", "10:35"), ("explorer", "claude-haiku-5-5", 0, "10:40", "10:41")])
        append(os.path.join(self.projects, "p-t1", "s-t1", "subagents", "agent-a0.jsonl"), [
            assistant(at("10:31"), "cb1", "claude-haiku-5-5", 1000000, content=[{"type": "tool_use", "id": "ct1", "name": "Bash", "input": {"command": "git status"}}]),
            result(at("10:31", "01"), "ct1", GUARD)])
        roles = self.roles(self.friction())
        self.assertEqual((roles["coder"]["bash"], roles["coder"]["guard"], roles["coder"]["share"]), (1, 1, 1.0))
        self.assertEqual(list(roles), ["coder"])  # the lead made no Bash call, the explorer neither

    def test_the_dispatcher_and_its_dispatches_have_rows_in_the_order_of_the_roles(self):
        self.task("t1", 1, OLD)
        loose(self.projects, "disp", ALPHA, [(at("10:00"), "claude-fable-5-2", 1000000)], title="dispatcher-alpha", subagent=[(at("10:30"), "claude-haiku-5-5", 0)])
        append(os.path.join(self.projects, "p-disp", "disp.jsonl"), [use(at("10:10"), "d1", "dt1"), result(at("10:10", "01"), "dt1", CLASSIFIER)])
        append(os.path.join(self.projects, "p-disp", "disp", "subagents", "agent-a0.jsonl"), [use(at("10:31"), "e1", "et1"), result(at("10:31", "01"), "et1", GUARD)])
        self.assertEqual([(r["role"], r["guard"], r["classifier"]) for r in self.friction()["refusals"]["roles"]], [("explorer", 1, 0), ("dispatcher", 0, 1)])

    def test_the_transcript_keeps_the_time_and_kind_of_each_refusal(self):
        from isshoni import transcript
        self.lead("t1")
        append(self.lead_file("t1"), [use(at("10:01"), "b1", "t1"), result(at("10:01", "01"), "t1", CLASSIFIER)])
        s = transcript.load_session(self.lead_file("t1"), self.prices)
        self.assertEqual(s.run.refused, [(transcript.parse_ts(at("10:01", "01")), "classifier")])


class WakeupTests(FrictionStore):
    def test_a_session_prompt_opens_a_window_that_a_human_prompt_closes_and_a_subagent_prompt_does_not(self):
        self.lead("w1")
        self.states.append({"ts": "2026-11-10T10:30:00Z", "repo": ALPHA, "slug": "w1", "state": "handed-over"})
        prompt = lambda ts, who: self.events.append(ev(ts, "UserPromptSubmit", slug="w1", role="lead", session="s-w1", **{"from": who}))  # noqa: E731
        prompt("2026-11-10T10:35:00Z", "session")
        prompt("2026-11-10T11:30:00Z", "subagent")  # continues the work: no window of its own, does not close
        prompt("2026-11-10T12:00:00Z", "human")
        prompt("2026-11-10T09:00:00Z", "session")  # before the hand-over: not a wake-up
        append(self.lead_file("w1"), [use(at("10:20"), "a"), use(at("10:45"), "b"), use(at("11:40"), "c"), use(at("12:10"), "d"), use(at("13:00"), "e")])
        self.assertEqual(self.friction()["wakeups"], {"prompts": 1, "tasks": 1, "cost": 20.0, "per_prompt": 20.0})

    def test_no_prompt_is_null_not_zero(self):
        self.lead("w1")
        self.assertEqual(self.friction()["wakeups"], {"prompts": 0, "tasks": 0, "cost": 0.0, "per_prompt": None})


class QueueTests(FrictionStore):
    def test_attempts_up_to_the_merge_over_the_merged_pull_requests(self):
        self.lead("q1")
        for step, n, ts in (("updated", 1, "09:00"), ("rerun", 1, "09:10"), ("sent-back", 1, "09:20"), ("stopped", 1, "09:30"), ("merged", 1, "10:00"),
                            ("updated", 1, "11:00"),  # after the merge
                            ("land-updated", 1, "09:40"), ("land-conflict", 1, "09:41"),  # the lead's own landing
                            ("updated", 2, "09:00"),  # never merged
                            ("updated", 3, "07:00"), ("merged", 3, "08:00")):
            self.queue(step, n, "2026-11-15T%s:00Z" % ts)
        self.assertEqual(self.friction()["queue"], {"since": "2026-11-15T07:00:00Z", "merged": 2, "updated": 2, "rerun": 1, "sent_back": 1,
                                                    "stopped": 1, "per_merged": 2.5})

    def test_no_queue_log_is_null_and_no_merge_has_no_ratio(self):
        self.lead("q1")
        self.assertIsNone(self.friction()["queue"])
        self.queue("updated", 1, "2026-11-15T09:00:00Z")
        q = self.friction()["queue"]
        self.assertEqual((q["merged"], q["per_merged"]), (0, None))


class LateConflictTests(FrictionStore):
    def setUp(self):
        super().setUp()
        for n, slug in enumerate(("p1", "p2", "p3"), 1):
            self.task(slug, n, OLD)
        clean, bad = {"sha": "a" * 40, "conflicted": False, "files": []}, lambda *files: {"sha": "b" * 40, "conflicted": True, "files": list(files)}
        self.seed("merges", ALPHA, {"m1": {"pr": 1, "base_merges": [bad("a/b.go"), bad("a/b.go", "d.go"), clean]},
                                    "m2": {"pr": 2, "base_merges": [bad("a/b.go")]}})  # p3 (#3) is not in the cache

    def conflict(self, step, slug, files, **kw):
        self.queue(step, 1, "2026-11-15T09:00:00Z", slug=slug, files=files, **kw)

    def test_two_columns_from_the_log_and_from_the_cache_with_no_subprocess(self):
        self.conflict("sent-back", "p1", ["a/b.go", "c.go"], kind="conflict")
        self.conflict("land-conflict", "p1", ["a/b.go"])
        self.conflict("sent-back", "p1", ["z.go"], kind="tests")  # not a conflict
        self.conflict("sent-back", None, ["a/b.go"], kind="conflict")  # no task: in the files, not in the tasks
        lc = self.friction()["late_conflicts"]
        self.assertEqual(self.run.call_args_list, [])
        self.assertEqual(lc["tasks"], [{"repo": "alpha", "slug": "p1", "log": 2, "git": 2}, {"repo": "alpha", "slug": "p2", "log": 0, "git": 1}])
        self.assertEqual(lc["files"], [{"repo": "alpha", "file": "a/b.go", "log": 3, "git": 3}, {"repo": "alpha", "file": "c.go", "log": 1, "git": 0},
                                       {"repo": "alpha", "file": "d.go", "log": 0, "git": 1}])
        self.assertEqual((lc["log_tasks"], lc["git_tasks"], lc["git_prs"]), (1, 2, 2))

    def test_the_log_is_null_without_a_queue_log(self):
        lc = self.friction()["late_conflicts"]
        self.assertEqual(lc["tasks"][0], {"repo": "alpha", "slug": "p1", "log": None, "git": 2})
        self.assertEqual((lc["log_tasks"], lc["git_tasks"]), (None, 2))

    def test_an_unreadable_cache_is_no_conflict(self):
        kaizen._write_cache(kaizen.cache_path(self.factory, "merges", ALPHA), {"m": "x", "n": {"pr": 1, "base_merges": "y"}})
        self.assertEqual(self.friction()["late_conflicts"], {"tasks": [], "files": [], "log_tasks": None, "git_tasks": 0, "git_prs": 0})


class HeldTests(FrictionStore):
    def test_send_backs_pauses_and_priorities_per_task_with_mistyped_fields_ignored(self):
        self.lead("h1")
        self.lead("h2")
        self.queue("sent-back", 1, "2026-11-15T09:00:00Z", slug="h1", kind="tests")
        self.events += [ev("2026-11-12T09:00:00Z", "TaskPaused", slug="h1", waits_on=["h2"]), ev("2026-11-12T09:05:00Z", "TaskPaused", slug="h1"),
                        ev("2026-11-12T10:00:00Z", "TaskPrioritized", slug="h1", paused_minutes=14),
                        ev("2026-11-12T10:10:00Z", "TaskPrioritized", slug="h1", paused_minutes="9"),
                        ev("2026-11-12T10:20:00Z", "TaskPrioritized", slug="h1", paused_minutes=True)]
        self.assertEqual(self.friction()["held"], [{"repo": "alpha", "slug": "h1", "sent_back": 1, "paused": 2, "paused_minutes": 14, "prioritized": 3}])

    def test_no_record_is_an_empty_list(self):
        self.lead("h1")
        self.assertEqual(self.friction()["held"], [])


class RangeTests(FrictionStore):
    """Every figure follows the cutoff: a store built with one that drops a task gives other figures."""

    def test_a_cutoff_that_drops_a_task_changes_the_figures(self):
        self.lead("early", dispatches=[("reviewer", "claude-opus-5-5", 1000000, "10:20", "10:25")])
        self.task("late", 2, OLD, started="2026-11-10T11:30:00Z")
        loose(self.projects, "disp", ALPHA, [(at("10:00"), "claude-fable-5-2", 1000000), (at("12:00"), "claude-fable-5-2", 1000000)], title="dispatcher-alpha")
        self.retros = [ev("2026-11-03T10:00:00Z", "", slug="early", defects=[{"cause": "spec"}]), ev("2026-11-12T10:00:00Z", "", slug="late", defects=[{"cause": "code"}])]
        self.events += [ev("2026-11-05T09:00:00Z", "TaskPaused", slug="early"), ev("2026-11-12T09:00:00Z", "TaskPaused", slug="late")]
        self.queue("merged", 1, "2026-11-09T10:00:00Z")
        self.queue("merged", 2, "2026-11-15T10:00:00Z")
        whole = self.friction()
        cut = self.friction(cutoff=datetime(2026, 11, 10, 11, tzinfo=timezone.utc))
        for key in ("dispatcher", "rework", "causes", "queue", "held"):
            self.assertNotEqual(whole[key], cut[key], key)
        self.assertEqual((whole["queue"]["merged"], cut["queue"]["merged"]), (2, 1))
        self.assertEqual((whole["causes"]["spec"], cut["causes"]["spec"]), (1, 0))

    def test_a_cutoff_drops_the_wake_ups_and_the_conflicts_before_it(self):
        self.task("w", 1, OLD, handed="2026-11-10T09:00:00Z", started="2026-11-10T11:30:00Z")
        for ts in ("2026-11-10T10:00:00Z", "2026-11-10T12:00:00Z"):
            self.events.append(ev(ts, "UserPromptSubmit", slug="w", role="lead", session="s-w", **{"from": "session"}))
            self.queue("land-conflict", 1, ts, slug="w", files=["a.go"])
        cutoff = datetime(2026, 11, 10, 11, tzinfo=timezone.utc)
        self.assertEqual(self.friction()["wakeups"]["prompts"], 2)
        self.assertEqual(self.friction(cutoff)["wakeups"]["prompts"], 1)
        self.assertEqual(self.friction()["late_conflicts"]["tasks"][0]["log"], 2)
        self.assertEqual(self.friction(cutoff)["late_conflicts"]["tasks"][0]["log"], 1)


class DocumentTests(FrictionStore):
    def test_every_object_has_the_keys_of_the_contract_example(self):
        self.lead("d1", dispatches=[("reviewer", "claude-opus-5-5", 1000000, "10:20", "10:25")])
        append(self.lead_file("d1"), [use(at("10:01"), "b1", "t1"), result(at("10:01", "01"), "t1", GUARD)])
        self.queue("sent-back", 1, "2026-11-15T09:00:00Z", slug="d1", kind="conflict", files=["a.go"])
        self.events.append(ev("2026-11-12T09:00:00Z", "TaskPaused", slug="d1"))
        self.seed("merges", ALPHA, {"m": {"pr": 1, "base_merges": [{"sha": "b" * 40, "conflicted": True, "files": ["a.go"]}]}})
        f, example = self.friction(), TextTests.DOC
        self.assertEqual(list(f), list(example))
        for key in ("dispatcher", "rework", "causes", "wakeups", "queue", "refusals", "late_conflicts"):
            self.assertEqual(list(f[key]), list(example[key]), key)
        self.assertEqual(list(f["refusals"]["roles"][0]), list(example["refusals"]["roles"][0]))
        self.assertEqual(list(f["held"][0]), list(example["held"][0]))
        self.assertEqual(list(f["late_conflicts"]["tasks"][0]), ["repo", "slug", "log", "git"])
        self.assertEqual(list(f["late_conflicts"]["files"][0]), ["repo", "file", "log", "git"])
        self.assertEqual(list(f["causes"]), [*kaizen.CAUSES, "unstated"])
        self.assertEqual(f["refusals"]["after_calls"], friction.AFTER_CALLS)


class TextTests(unittest.TestCase):
    DOC = {
        "dispatcher": {"calls": 400, "cost": 50.5, "per_call": 0.12625, "context_median": 310000, "unpriced_tokens": 0},
        "rework": {"cost": 30.0, "task_cost": 120.0, "share": 0.25, "per_task_median": 2.5, "n": 10},
        "causes": {"spec": 2, "brief": 0, "conflict": 1, "scope": 0, "code": 2, "unstated": 20},
        "refusals": {"after_calls": 3, "roles": [{"role": "lead", "bash": 500, "guard": 10, "classifier": 2, "share": 0.024, "after_cost": 1.5}]},
        "wakeups": {"prompts": 30, "tasks": 12, "cost": 14.2, "per_prompt": 0.473333},
        "queue": {"since": "2026-11-15T09:00:00Z", "merged": 3, "updated": 2, "rerun": 0, "sent_back": 1, "stopped": 0, "per_merged": 1.0},
        "held": [{"repo": "alpha", "slug": "one", "sent_back": 2, "paused": 1, "paused_minutes": 14, "prioritized": 1}],
        "late_conflicts": {"tasks": [], "files": [], "log_tasks": 1, "git_tasks": 4, "git_prs": 9}}

    def test_one_line_per_figure(self):
        self.assertEqual(cli._friction_lines(self.DOC, "USD", 0.11), [
            "dispatcher: $0.13 per call (n=400 calls), median context 310.0k",
            "cost after the first review: 25% of task cost ($30.00 of $120, median $2.50 per task, n=10; subagents only 11%)",
            "defect causes: spec 2, brief 0, conflict 1, scope 0, code 2 (n=5 stated, 20 unstated)",
            "refused bash calls: lead 2.4% (12/500, next 3 calls $1.50)",
            "lead prompts after hand-over: 30 in 12 tasks, $14.20 after them, $0.47 per prompt",
            "queue attempts per merged PR: 1.00 (n=3: updates 2, re-runs 0, send-backs 1, stops 0), queue log since 2026-11-15T09:00:00Z",
            "queue send-backs and pauses: send-backs 2, pauses 1, paused 14 min, in 1 task",
            "late conflicts: 1 task in the queue log, 4 of 9 PRs in git history"])

    def test_unknown_is_a_dash_and_none_is_none_recorded(self):
        doc = {**self.DOC, "dispatcher": {**self.DOC["dispatcher"], "per_call": None, "context_median": None}, "queue": None, "held": [],
               "late_conflicts": {**self.DOC["late_conflicts"], "log_tasks": None}, "refusals": {"after_calls": 3, "roles": []}, "rework": {**self.DOC["rework"], "share": None, "per_task_median": None}}
        lines = cli._friction_lines(doc, "USD", None)
        self.assertEqual(lines[0], "dispatcher: - per call (n=400 calls), median context -")
        self.assertIn("- of task cost", lines[1])
        self.assertIn("subagents only -", lines[1])
        self.assertEqual((lines[3], lines[5], lines[6], lines[7]), (
            "refused bash calls: none recorded", "queue attempts per merged PR: no queue log yet", "queue send-backs and pauses: none recorded",
            "late conflicts: no queue log yet, 4 of 9 PRs in git history"))


if __name__ == "__main__":
    unittest.main()
