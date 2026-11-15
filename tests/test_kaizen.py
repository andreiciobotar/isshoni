import contextlib
import glob
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.support import AGENTS, PRICES_PATH, ROOT
from isshoni import authoring, cli, kaizen, metrics, prices as price_file, transcript

GROUPING = os.path.join(ROOT, "tests", "fixtures", "grouping")
PRICES_TOML = """currency = "USD"
as_of = "2026-01-01"
source = "test"
""" + "".join("""
[models.%s]
input = %s
output = 1
cache_write_5m = 1
cache_write_1h = 1
cache_read = 1
""" % m for m in (("claude-fable-5-2", 10), ("claude-opus-5-5", 5), ("claude-haiku-5-5", 1)))  # sonnet: no price
DAY = "2026-11-10"
ALPHA, BETA = "/r/alpha", "/r/beta"


def write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for ln in lines:
            f.write(ln if isinstance(ln, str) else json.dumps(ln))
            f.write("\n")


def ev(ts, event, repo=ALPHA, slug="a1", **kw):
    return {"ts": ts, "repo": repo, "slug": slug, "event": event, "version": "0.2.0", **kw}


def assistant(ts, mid, model="claude-fable-5-2", tokens=0, content=None, cwd="/x"):
    m = {"id": mid, "model": model, "role": "assistant", "content": content or [{"type": "text", "text": "x"}]}
    if tokens:
        m["usage"] = {"input_tokens": tokens}
    return {"type": "assistant", "uuid": "u-" + mid, "timestamp": ts, "requestId": "r-" + mid, "message": m, "cwd": cwd}


def writes(who, calls):
    """tool_use blocks for the (tool, input) pairs `calls` of `who`."""
    return [{"type": "tool_use", "id": "w-%s-%d" % (who, i), "name": n, "input": inp} for i, (n, inp) in enumerate(calls)]


def session(projects, repo, slug, start, lead_tokens, dispatches=(), written=None):
    """A transcript in worktree `slug`; dispatches are (agent_type, model, tokens, start, end[, no_file]) on DAY.
    `written`: "lead" or an agent type -> the (Edit or Write, input) pairs its first call makes."""
    written = written or {}
    wt = "%s/.claude/worktrees/%s" % (repo, slug)
    sid = "s-" + slug
    path = os.path.join(projects, "p-" + slug, sid + ".jsonl")
    lines = [{"type": "worktree-state", "sessionId": sid, "worktreeSession": {
        "originalCwd": wt, "worktreePath": wt, "worktreeName": slug, "worktreeBranch": "worktree-" + slug}},
        assistant(start, "lead-" + slug, tokens=lead_tokens, cwd=wt,
                  content=[{"type": "text", "text": "x"}, *writes("%s-lead" % slug, written.get("lead", ()))])]
    for i, (agent, model, tokens, t0, t1, *no_file) in enumerate(dispatches):
        tid, aid = "toolu-%s-%d" % (slug, i), "a%d" % i
        lines.append(assistant("%sT%s:00.000Z" % (DAY, t0), "d-%s-%d" % (slug, i), cwd=wt, content=[
            {"type": "tool_use", "id": tid, "name": "Agent",
             "input": {"description": "d%d" % i, "prompt": "p%d" % i, "subagent_type": agent}}]))
        if not no_file:
            base = os.path.join(projects, "p-" + slug, sid, "subagents", "agent-" + aid)
            write(base + ".jsonl", [assistant("%sT%s:01.000Z" % (DAY, t0), "s-%s-%d" % (slug, i), model, tokens, cwd=wt, content=[
                {"type": "text", "text": "x"}, *writes("%s-%d" % (slug, i), written.get(agent, ()))]),
                                    assistant("%sT%s:00.000Z" % (DAY, t1), "e-%s-%d" % (slug, i), model, 0, cwd=wt)])
            write(base + ".meta.json", [{"agentType": agent, "toolUseId": tid, "spawnDepth": 1}])
    write(path, lines)


def loose(projects, name, repo, calls, title=None, prompt=None, subagent=()):
    """A transcript outside any task, in `repo`; calls are (timestamp, model, tokens). Named by `title`, started by `prompt`.
    `subagent`: the calls (same triple) of one subagent the first call dispatches."""
    lines = [{"type": "custom-title", "customTitle": title}] if title else []
    lines += [{"type": "user", "timestamp": calls[0][0], "cwd": repo, "message": {"content": prompt}}] if prompt else []
    lines += [assistant(ts, "%s-%d" % (name, i), model, tokens, cwd=repo) for i, (ts, model, tokens) in enumerate(calls)]
    base = os.path.join(projects, "p-" + name)
    if subagent:
        tool = [{"type": "tool_use", "id": "toolu-" + name, "name": "Agent", "input": {"description": "d", "prompt": "p", "subagent_type": "explorer"}}]
        first = next(l for l in lines if l["type"] == "assistant")
        first["message"]["content"] = first["message"]["content"] + tool
        sub = os.path.join(base, name, "subagents", "agent-a0")
        write(sub + ".jsonl", [assistant(ts, "%s-sub-%d" % (name, i), model, tokens, cwd=repo) for i, (ts, model, tokens) in enumerate(subagent)])
        write(sub + ".meta.json", [{"agentType": "explorer", "toolUseId": "toolu-" + name, "spawnDepth": 1}])
    write(os.path.join(base, name + ".jsonl"), lines)


def pr(number, branch, opened, merged=None, state="MERGED"):
    return {"number": number, "url": "https://gh/pull/%d" % number, "state": state, "headRefName": branch,
            "createdAt": opened, "mergedAt": merged}


class Store(unittest.TestCase):
    """A factory store and transcripts in a temp dir: `self.factory`, `self.projects`, `self.prices`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.factory = os.path.join(tmp.name, "factory")
        self.projects = os.path.join(tmp.name, "projects")
        os.makedirs(self.factory)
        os.makedirs(self.projects)
        path = os.path.join(tmp.name, "prices.toml")
        write(path, [PRICES_TOML])
        self.prices = price_file.load(path)

    def log(self, name, lines):
        write(os.path.join(self.factory, name), lines)

    def build(self, gh, cutoff=None, sessions=None, now=datetime(2026, 11, 14, 1, 2, 3, tzinfo=timezone.utc), cached=False):
        loaded = []

        def load(load_from=None):
            for p in transcript.discover_sessions(self.projects):
                s = transcript.load_session(p, self.prices)
                metrics.analyze(s, AGENTS, 0.5)
                loaded.append(s)
            return loaded

        with mock.patch.object(kaizen, "gh_prs", side_effect=fresh(gh)):
            return kaizen.build(self.factory, sessions or load, self.prices, cutoff, now=now, cached=cached)


def gh_alpha_only(path):
    if path == ALPHA:
        return [pr(3, "worktree-a1", "2026-11-10T09:00:00Z", None, "CLOSED"),  # an older attempt: the newest wins
                pr(5, "worktree-a1", "%sT10:50:00Z" % DAY, "%sT11:00:00Z" % DAY),
                pr(6, "worktree-other", "%sT10:00:00Z" % DAY, "%sT10:10:00Z" % DAY), "junk"]
    raise kaizen.GhError("not a GitHub repository")


class FixtureStore(Store):
    """Four tasks: a1 (everything), a0 (legacy files, no TaskStarted), b1 (declared state only), c1 (session only)."""

    def setUp(self):
        super().setUp()
        self.log("events.jsonl", [
            "not json", "[1, 2]", "{}", '{"ts": "2026-11-09T08:00:00Z", "repo": "/r/alpha"}',
            {"ts": "2026-11-09T08:00:00Z", "slug": "zz", "event": "SessionStart"},
            {"ts": "2026-11-09T08:00:00Z", "repo": ALPHA, "slug": "a0", "event": "SessionStart", "version": "0.1.0"}])
        self.log("events-2026-11.jsonl", [
            ev("%sT10:00:00Z" % DAY, "TaskStarted"),
            ev("%sT10:12:00Z" % DAY, "PostToolUse", tool_name="SendMessage", kind="QUESTION", session="s1"),
            ev("%sT10:13:00Z" % DAY, "UserPromptSubmit", session="s1", **{"from": "subagent"}),
            ev("%sT10:14:00Z" % DAY, "UserPromptSubmit", session="s1", **{"from": "human"}),
            ev("%sT10:15:00Z" % DAY, "Notification", notification_type="idle_prompt", session="s1"),
            ev("%sT10:16:00Z" % DAY, "PostToolUse", tool_name="SendMessage", kind="BLOCKED", session="s2"),
            ev("%sT10:17:00Z" % DAY, "PostToolUse", tool_name="SendMessage", kind="DONE", session="s2"),
            ev("%sT10:18:00Z" % DAY, "PermissionDenied", tool_name="Bash", reason="sandbox"),
            ev("%sT10:19:00Z" % DAY, "PermissionDenied", tool_name="Edit"),
            {"ts": "%sT10:20:00Z" % DAY, "repo": ALPHA, "event": "PermissionDenied", "tool_name": "Bash"},
            ev("%sT12:00:00Z" % DAY, "SessionStart", slug="c1")])
        self.log("retros.jsonl", [{"ts": "2026-11-09T09:00:00Z", "repo": ALPHA, "slug": "a0", "version": "0.2.0",
                                   "defects": [{"caught_by": "lead"}], "slowed": ["a0 slow"]}])
        self.log("retros-2026-11.jsonl", [
            ev("%sT12:00:00Z" % DAY, "", defects=[{"caught_by": "human"}], slowed=["old"]),
            ev("%sT13:00:00Z" % DAY, "", defects=[{"caught_by": "reviewer"}, {"caught_by": "reviewer"}, {"caught_by": "tests"},
                                                   {"caught_by": "weird"}, "str", {}], slowed=["slow two", 7, "slow three"])])
        self.log("tasks.jsonl", [
            {"ts": "%sT10:55:00Z" % DAY, "repo": ALPHA, "slug": "a1", "state": "handed-over", "deliverable": "x"}])
        self.log("tasks-2026-11.jsonl", [
            {"ts": "%sT10:01:00Z" % DAY, "repo": ALPHA, "slug": "a1", "state": "working"},
            {"ts": "%sT10:56:00Z" % DAY, "repo": ALPHA, "slug": "a1"},
            {"ts": "%sT09:00:00Z" % DAY, "repo": BETA, "slug": "b1", "state": "acknowledged"}])
        fb = lambda i, ts, text, **kw: {"ts": "2026-11-11T%s:00Z" % ts, "repo": ALPHA, "slug": "a1", "role": "reviewer",  # noqa: E731
                                        "id": i, "text": text, "status": "open", **kw}
        self.log("feedback-2026-11.jsonl", [
            fb("e1", "10:00", "The brief did not say which file"), fb("e1", "10:05", "same id again: permission"),
            fb("e2", "10:10", "Permission denied on Bash in the sandbox"),
            fb("e3", "10:20", "DECISION: used a tool"), fb("e4", "10:30", "Nothing special"),
            fb("e5", "10:40", "Used a bash script", version="0.3.0"),
            {"ts": "2026-11-11T10:50:00Z", "id": "e1", "status": "accepted"},
            {"ts": "2026-11-11T11:00:00Z", "id": "e1", "status": "done", "pr": "https://x/pull/1"},
            {"ts": "2026-11-11T10:55:00Z", "id": "e1", "status": "rejected"},
            {"ts": "2026-11-11T11:05:00Z", "id": "e3", "status": "accepted"},
            {"ts": "2026-11-11T11:05:00Z", "id": "zz", "status": "done"}, {"text": "no id"}, {"id": "e9", "text": 5}])
        session(self.projects, ALPHA, "a1", "%sT10:00:05.000Z" % DAY, 1000000, [
            ("general-purpose", "claude-haiku-5-5", 1000000, "10:01", "10:02"),
            ("explorer", "claude-haiku-5-5", 1000000, "10:02", "10:03"),
            ("Explore", "claude-haiku-5-5", 1000000, "10:03", "10:04"),
            ("factory:coder", "claude-haiku-5-5", 1000000, "10:05", "10:10"),
            ("factory:reviewer", "claude-opus-5-5", 1000000, "10:20", "10:25"),
            ("coder", "claude-haiku-5-5", 2000000, "10:30", "10:35"),
            ("coder", "claude-sonnet-5-5", 1000000, "10:40", "10:41"),
            ("reviewer", "claude-opus-5-5", 0, "10:45", "10:45", "no file"),
            ("factory:lander", "claude-haiku-5-5", 1000000, "10:46", "10:47")],  # after the last review: no rework
            written={"lead": [("Write", {"file_path": ALPHA + "/.claude/worktrees/a1/isshoni/x.py", "content": "a\nb\nc\n"}),
                              ("Edit", {"file_path": ALPHA + "/.claude/worktrees/a1/docs/y.md", "old_string": "x", "new_string": "x\ny"})],
                     "factory:coder": [("Write", {"file_path": ALPHA + "/.claude/worktrees/a1/tests/test_x.py", "content": "t\n" * 10}),
                                       ("Edit", {"file_path": ALPHA + "/.claude/worktrees/a1/isshoni/x.py",
                                                 "old_string": "a\nb", "new_string": "a\nb\nc\nd"})]})
        session(self.projects, ALPHA, "c1", "%sT12:30:00.000Z" % DAY, 500000)
        loose(self.projects, "disp", ALPHA, [("%sT10:00:00.000Z" % DAY, "claude-fable-5-2", 100000)], title="dispatcher-alpha")
        loose(self.projects, "site", ALPHA, [("%sT10:30:00.000Z" % DAY, "claude-haiku-5-5", 1000000)],
              prompt="Job: watch. Repo: %s. The dispatcher's session name is dispatcher-alpha." % ALPHA)
        self.doc = self.build(gh_alpha_only)
        self.by_slug = {t["slug"]: t for t in self.doc["tasks"]}


class TaskTests(FixtureStore):
    def test_tasks_are_listed_by_start_with_nulls_first(self):
        self.assertEqual([t["slug"] for t in self.doc["tasks"]], ["a0", "b1", "a1", "c1"])

    def test_identity_version_started_state(self):
        a1, a0, b1, c1 = (self.by_slug[s] for s in ("a1", "a0", "b1", "c1"))
        self.assertEqual((a1["repo"], a1["repo_path"], a1["version"], a1["started"], a1["state"]),
                         ("alpha", ALPHA, "0.2.0", "%sT10:00:00Z" % DAY, "handed-over"))
        self.assertEqual((a0["version"], a0["started"], a0["state"]), ("0.1.0", None, None))  # earliest record's version
        self.assertEqual((b1["repo"], b1["version"], b1["started"], b1["state"]), ("beta", None, None, "acknowledged"))
        self.assertEqual((c1["version"], c1["started"]), ("0.2.0", "%sT12:30:00Z" % DAY))  # from its session

    def test_authoring_counts_product_and_tests_by_role(self):
        zero = dict.fromkeys(kaizen.ROLES, 0)
        self.assertEqual(self.by_slug["a1"]["authoring"], {
            "lines": {**zero, "lead": 3, "coder": 12},  # the lead's docs line and the Edit's repeated context are not counted
            "calls": {**zero, "lead": 1, "coder": 2}, "lead_share": 0.2})
        self.assertEqual(self.by_slug["c1"]["authoring"], {"lines": zero, "calls": zero, "lead_share": None})

    def test_pull_request_and_times(self):
        a1 = self.by_slug["a1"]
        self.assertEqual(a1["pr"], {"number": 5, "url": "https://gh/pull/5", "state": "MERGED",
                                    "opened": "%sT10:50:00Z" % DAY, "merged": "%sT11:00:00Z" % DAY})
        self.assertEqual((a1["landed"], a1["brief_to_pr_s"], a1["pr_to_merge_s"]), (True, 3000, 600))
        for s in ("a0", "b1", "c1"):
            t = self.by_slug[s]
            self.assertEqual((t["pr"], t["landed"], t["brief_to_pr_s"], t["pr_to_merge_s"]), (None, False, None, None))

    def test_cost_by_role_rework_unpriced(self):
        self.assertEqual(self.by_slug["a1"]["cost"], {
            "total": 22.0, "unpriced_tokens": 1000000,
            "by_role": {"lead": 10.0, "tech-lead": 0.0, "coder": 3.0, "reviewer": 5.0, "explorer": 2.0, "lander": 1.0, "other": 1.0},
            "rework": 2.0, "after_review": 3.0})  # the coder's 2 and the lander's 1 after the review; the lead's call came before it
        self.assertEqual(self.by_slug["c1"]["cost"]["total"], 5.0)
        self.assertEqual(self.by_slug["c1"]["cost"]["rework"], 0.0)
        self.assertIsNone(self.by_slug["c1"]["cost"]["after_review"])  # no review
        self.assertIsNone(self.by_slug["a0"]["cost"])

    def test_review_rounds_and_first_pass(self):
        a1, c1, a0 = (self.by_slug[s] for s in ("a1", "c1", "a0"))
        self.assertEqual((a1["review_rounds"], a1["first_pass"]), (1, True))  # the no-run reviewer is not a round
        self.assertEqual((c1["review_rounds"], c1["first_pass"]), (0, False))  # skipping the review is not first-pass
        self.assertEqual((a0["review_rounds"], a0["first_pass"]), (None, None))

    def test_stops(self):
        self.assertEqual([(t["slug"], t["stops"], t["stopped_s"]) for t in self.doc["tasks"]],
                         [("a0", 0, 0), ("b1", 0, 0), ("a1", 4, 120), ("c1", 0, 0)])
        self.assertEqual(self.doc["stops"], [
            {"ts": "%sT10:12:00Z" % DAY, "repo": "alpha", "slug": "a1", "kind": "QUESTION", "waited_s": 120, "why": None},
            {"ts": "%sT10:16:00Z" % DAY, "repo": "alpha", "slug": "a1", "kind": "BLOCKED", "waited_s": None, "why": None},
            {"ts": "%sT10:18:00Z" % DAY, "repo": "alpha", "slug": "a1", "kind": "DENIED", "waited_s": None, "why": "Bash: sandbox"},
            {"ts": "%sT10:19:00Z" % DAY, "repo": "alpha", "slug": "a1", "kind": "DENIED", "waited_s": None, "why": "Edit"}])

    def test_defects_come_from_the_newest_retro(self):
        zero = dict.fromkeys(("tests", "reviewer", "lead", "ci", "human", "other"), 0)
        self.assertEqual(self.by_slug["a1"]["defects"], {**zero, "tests": 1, "reviewer": 2, "other": 2})
        self.assertEqual(self.by_slug["a0"]["defects"], {**zero, "lead": 1})
        self.assertIsNone(self.by_slug["c1"]["defects"])

    def test_slowed_newest_retro_first(self):
        self.assertEqual([(s["ts"], s["slug"], s["text"]) for s in self.doc["slowed"]], [
            ("%sT13:00:00Z" % DAY, "a1", "slow two"), ("%sT13:00:00Z" % DAY, "a1", "slow three"),
            ("%sT12:00:00Z" % DAY, "a1", "old"), ("2026-11-09T09:00:00Z", "a0", "a0 slow")])

    def test_versions_by_earliest_record_in_any_store(self):
        self.assertEqual(self.doc["versions"], [
            {"version": "0.1.0", "first_seen": "2026-11-09T08:00:00Z"},
            {"version": "0.2.0", "first_seen": "2026-11-09T09:00:00Z"},
            {"version": "0.3.0", "first_seen": "2026-11-11T10:40:00Z"}])


class DocumentTests(FixtureStore):
    def test_header_repos_and_gh_failure(self):
        d = self.doc
        self.assertEqual((d["generated"], d["factory_dir"], d["store_found"]), ("2026-11-14T01:02:03Z", self.factory, True))
        self.assertEqual(d["prices"], self.prices.to_dict())
        self.assertEqual(d["repos"], [{"repo": "alpha", "repo_path": ALPHA, "prs": "ok", "prs_as_of": AS_OF_ISO, "prs_error": None},
                                      {"repo": "beta", "repo_path": BETA, "prs": "not a GitHub repository", "prs_as_of": None,
                                       "prs_error": "not a GitHub repository"}])

    def test_the_document_carries_no_private_key(self):
        self.assertNotIn("_roles", json.dumps(self.doc))
        self.assertEqual([k for t in self.doc["tasks"] for k in t if k.startswith("_")], [])
        self.assertEqual([(v["version"], v["tasks"], v["roles"]["lead"] and v["roles"]["lead"]["n"])
                          for v in self.doc["summary"]["anatomy"]["versions"]], [("0.1.0", 0, None), ("0.2.0", 1, 1), ("0.3.0", 0, None)])

    def test_summary(self):
        self.assertEqual({k: v for k, v in self.doc["summary"].items() if k not in ("anatomy", "by_mode")}, {
            "tasks": 4, "landed": 1, "in_flight": 2,  # b1 is acknowledged
            "cost": {"median": 22.0, "n": 1}, "brief_to_pr_s": {"median": 3000.0, "n": 1},
            "pr_to_merge_s": {"median": 600.0, "n": 1}, "review_rounds": {"median": 1.0, "n": 1},
            "first_pass": {"yes": 1, "of": 1}, "rework_share": 0.090909,
            "by_role": {"lead": 10.0, "tech-lead": 0.0, "coder": 3.0, "reviewer": 5.0, "explorer": 2.0, "lander": 1.0, "other": 1.0},
            "mix": {"window": 5, "points": []},
            "time": {"building_s": 2880, "stopped_s": 120, "merge_wait_s": 600},
            "defects": {"tests": 1, "reviewer": 2, "lead": 1, "ci": 0, "human": 0, "other": 2},
            "trend_min": 10, "trend": None,
            "overhead": {"dispatcher": 1.0, "site_manager": 1.0, "sessions": 2, "unpriced_tokens": 0, "landed": 1,
                         "per_landed": 2.0}})
        self.assertEqual(self.doc["rewards"], [])

    def test_outbox_folds_entries_and_topics(self):
        o = self.doc["outbox"]
        self.assertEqual(o["issues"], {"open": 3, "accepted": 0, "rejected": 0, "done": 1})
        self.assertEqual(o["decisions"], {"open": 0, "accepted": 1, "rejected": 0, "done": 0})
        self.assertEqual(o["topics"], [{"topic": "permissions", "open": 1}, {"topic": "tools", "open": 1}, {"topic": "other", "open": 1}])
        self.assertEqual([(e["id"], e["kind"], e["status"], e["topic"], e["pr"]) for e in o["entries"]], [
            ("e5", "issue", "open", "tools", None), ("e4", "issue", "open", "other", None),
            ("e3", "decision", "accepted", None, None), ("e2", "issue", "open", "permissions", None),
            ("e1", "issue", "done", "brief", "https://x/pull/1")])  # the latest ts wins, not the last line
        self.assertEqual(o["entries"][-1], {"id": "e1", "ts": "2026-11-11T10:00:00Z", "repo": "alpha", "slug": "a1",
                                           "role": "reviewer", "kind": "issue", "status": "done", "topic": "brief",
                                           "text": "The brief did not say which file", "pr": "https://x/pull/1"})

    def test_topics_first_match_in_table_order(self):
        for text, topic in (("sandbox", "permissions"), ("an ambiguous Spec", "brief"), ("the lead.md rule", "rules"),
                            ("run the git command", "tools"), ("verdict", "review"), ("wasted minutes", "time"),
                            ("brief and permission", "permissions"), ("nothing", "other")):
            self.assertEqual(kaizen._topic(text), topic, text)

    def test_since_keeps_tasks_started_at_or_after(self):
        d = self.build(gh_alpha_only, cutoff=datetime(2026, 11, 10, 12, 30, 0, tzinfo=timezone.utc))
        self.assertEqual([t["slug"] for t in d["tasks"]], ["c1"])  # at the cutoff is kept; null starts are not
        self.assertEqual((d["stops"], d["slowed"], d["repos"]), ([], [], [{"repo": "alpha", "repo_path": ALPHA, "prs": "ok", "prs_as_of": AS_OF_ISO, "prs_error": None}]))
        self.assertEqual(d["summary"]["tasks"], 1)
        d = self.build(gh_alpha_only, cutoff=datetime(2026, 11, 10, 12, 30, 1, tzinfo=timezone.utc))
        self.assertEqual((d["tasks"], d["repos"]), ([], []))
        self.assertEqual(len(d["outbox"]["entries"]), 5)
        d = self.build(gh_alpha_only, cutoff=datetime(2026, 11, 11, 10, 30, 0, tzinfo=timezone.utc))
        self.assertEqual([e["id"] for e in d["outbox"]["entries"]], ["e5", "e4"])  # entries by their own time

    def test_gh_failing_everywhere(self):
        def fail(path):
            raise kaizen.GhError("gh unavailable")
        d = self.build(fail)
        self.assertEqual([r["prs"] for r in d["repos"]], ["gh unavailable"] * 2)
        self.assertEqual([(t["pr"], t["landed"]) for t in d["tasks"]], [(None, False)] * 4)
        self.assertEqual((d["summary"]["landed"], d["summary"]["in_flight"], d["summary"]["cost"]), (0, 3, {"median": None, "n": 0}))


def shapes(d):
    """The key sets of every kind of object in a kaizen document, by name."""
    out = {}

    def add(name, objs):
        for o in objs:
            if isinstance(o, dict):
                out.setdefault(name, set()).add(frozenset(o))

    s, o, tasks = d["summary"], d["outbox"], d["tasks"]
    add("doc", [d]), add("repo", d["repos"]), add("version", d["versions"]), add("task", tasks)
    add("pr", [t["pr"] for t in tasks]), add("cost", [t["cost"] for t in tasks])
    add("by_role", [t["cost"]["by_role"] for t in tasks if t["cost"]] + [s["by_role"]] + [p["share"] for p in s["mix"]["points"]])
    add("mix", [s["mix"]]), add("point", s["mix"]["points"])
    a = s["anatomy"]
    add("anatomy", [a]), add("anatomy_point", a["points"]), add("anatomy_version", a["versions"])
    add("anatomy_roles", [p["roles"] for p in a["points"]] + [v["roles"] for v in a["versions"]])
    add("anatomy_point_role", [r for p in a["points"] for r in p["roles"].values()])
    add("anatomy_version_role", [r for v in a["versions"] for r in v["roles"].values()])
    add("anatomy_classes", [r["classes"] for v in a["versions"] for r in v["roles"].values() if r and r["classes"]])
    add("defects", [t["defects"] for t in tasks] + [s["defects"]] + [r["defects"] for r in s["by_mode"]])
    add("by_mode", s["by_mode"])
    add("summary", [s]), add("time", [s["time"]]), add("first_pass", [s["first_pass"]]), add("trend", [s["trend"]])
    add("median", [s[m] for m in ("cost", "brief_to_pr_s", "pr_to_merge_s", "review_rounds")])
    add("stop", d["stops"]), add("slowed", d["slowed"]), add("reward", d["rewards"])
    add("outbox", [o]), add("counts", [o["issues"], o["decisions"]]), add("topic", o["topics"]), add("entry", o["entries"])
    add("site_manager", [d["site_manager"]]), add("tech_lead", [d["tech_lead"]])
    add("authoring", [t["authoring"] for t in tasks]), add("authoring_roles", [t["authoring"][k] for t in tasks for k in ("lines", "calls")])
    add("pooled", [x["authoring"] for x in a["points"] + a["versions"]])
    add("pooled_roles", [x["authoring"]["lines"] for x in a["points"] + a["versions"]])
    add("pooled_share", [x["authoring"]["share"] for x in a["points"] + a["versions"]])
    add("overhead", [s["overhead"]])
    days = [d["pulse"]["today"], d["pulse"]["baseline"], *d["pulse"]["days"]]
    add("pulse", [d["pulse"]]), add("pulse_day", days), add("pulse_cost", [x["cost"] for x in days])
    add("pulse_sizes", [x["first_pass"] for x in days]), add("pulse_first_pass", [f for x in days for f in x["first_pass"].values()])
    add("pulse_handover", [x["handover"] for x in days]), add("pulse_p90", [h for x in days for h in list(x["handover"].values())[:3]])
    add("pulse_conflicts", [x["conflicts"] for x in days]), add("pulse_verdicts", [d["pulse"]["verdicts"]])
    f = d["friction"]
    add("friction", [f]), add("friction_dispatcher", [f["dispatcher"]]), add("friction_rework", [f["rework"]]), add("friction_causes", [f["causes"]])
    add("friction_refusals", [f["refusals"]]), add("friction_role", f["refusals"]["roles"]), add("friction_wakeups", [f["wakeups"]])
    add("friction_queue", [f["queue"]]), add("friction_held", f["held"]), add("friction_late", [f["late_conflicts"]])
    add("friction_late_task", f["late_conflicts"]["tasks"]), add("friction_late_file", f["late_conflicts"]["files"])
    return out


class DashboardFixtureTests(FixtureStore):
    """The dashboard's made-up fixture has the shape the command emits."""

    def test_fixture_matches_the_command(self):
        with open(os.path.join(ROOT, "internal", "tui", "testdata", "kaizen-fixture.json")) as f:
            fixture_doc = json.load(f)
        fixture = shapes(fixture_doc)
        ours = shapes(self.doc)
        self.assertEqual({n for n, v in fixture.items() if len(v) != 1}, set())
        self.assertLessEqual({"doc", "repo", "version", "task", "pr", "cost", "by_role", "defects", "summary", "time",
                              "first_pass", "median", "stop", "slowed", "outbox", "counts", "topic", "entry",
                              "site_manager", "tech_lead", "pulse", "pulse_day", "pulse_cost", "pulse_sizes", "pulse_first_pass",
                              "pulse_handover", "pulse_p90", "pulse_conflicts", "pulse_verdicts", "mix", "authoring", "authoring_roles", "pooled", "pooled_roles", "pooled_share",
                              "overhead", "by_mode"}, set(ours))
        for name in ours:
            self.assertEqual(fixture[name], ours[name], name)
        landed = [t for t in fixture_doc["tasks"] if t["landed"]]
        self.assertEqual((fixture_doc["roles"], fixture_doc["summary"]["mix"]), (list(kaizen.ROLES), kaizen._mix(landed)))
        self.assertEqual(fixture["point"], {frozenset(kaizen._mix(mixed([0.5] * 5))["points"][0])})
        self.assertEqual(fixture["trend"], {frozenset(kaizen._trend([task("t", "2026-11-01T00:00:00Z")] * 10))})
        self.assertEqual(fixture["reward"], {frozenset(kaizen._rewards(days(9, 8, 7, 6, 5))[0])})


class StopAndTimeTests(Store):
    def test_overtime_answered_in_its_own_session_offsets_and_a_pr_older_than_the_start(self):
        self.log("events-2026-11.jsonl", [
            ev("2026-11-10T11:00:00+01:00", "TaskStarted"),
            ev("%sT10:10:00Z" % DAY, "PostToolUse", tool_name="SendMessage", kind="OVERTIME", session="s1"),
            ev("%sT10:11:00Z" % DAY, "UserPromptSubmit", session="other", **{"from": "session"}),
            ev("%sT10:13:00Z" % DAY, "UserPromptSubmit", session="s1", **{"from": "session"})])
        d = self.build(lambda p: [pr(1, "worktree-a1", "%sT09:00:00Z" % DAY, "%sT09:30:00Z" % DAY)])
        t = d["tasks"][0]
        self.assertEqual(t["started"], "%sT10:00:00Z" % DAY)
        self.assertEqual(d["stops"], [{"ts": "%sT10:10:00Z" % DAY, "repo": "alpha", "slug": "a1", "kind": "OVERTIME",
                                       "waited_s": 180, "why": None}])
        self.assertEqual((t["landed"], t["brief_to_pr_s"], t["pr_to_merge_s"]), (True, None, 1800))

    def test_why_is_the_summary_the_hook_stored(self):
        stop = lambda minute, kind, **kw: ev("%sT10:%02d:00Z" % (DAY, minute), "PostToolUse", tool_name="SendMessage",  # noqa: E731
                                             kind=kind, session="s1", **kw)
        self.log("events-2026-11.jsonl", [
            stop(1, "QUESTION", summary="which of the two schemas?"), stop(2, "BLOCKED", summary="push denied"),
            stop(3, "OVERTIME", summary="review is on its third round"),
            stop(4, "QUESTION"), stop(5, "QUESTION", summary=""), stop(6, "QUESTION", summary=7),  # older or odd records
            stop(7, "DONE", summary="not a stop")])
        self.assertEqual([(s["kind"], s["why"]) for s in self.build(lambda p: [])["stops"]], [
            ("QUESTION", "which of the two schemas?"), ("BLOCKED", "push denied"), ("OVERTIME", "review is on its third round"),
            ("QUESTION", None), ("QUESTION", None), ("QUESTION", None)])


def fired(fid, ts, repo=ALPHA):
    return {"ts": ts, "repo": repo, "id": fid, "criterion": "quiet", "escalate": "judge", "slug": "a1", "subject": "a1", "detail": "d"}


def then(fid, hhmm, repo=ALPHA, **kw):
    """A later record of a firing on DAY: its decision, outcome or clearing."""
    return {"ts": "%sT%s:00Z" % (DAY, hhmm), "repo": repo, "id": fid, **kw}


class SiteManagerTests(Store):
    EMPTY = {"escalated": 0, "acted": 0, "ignored": 0, "no_outcome": 0, "suppressed": 0, "paused": 0, "extended": 0,
             "kept": 0, "overran": 0, "missed": 0, "missed_suppressed": 0, "missed_never_fired": 0, "precision": None, "recall": None}

    def record(self, cutoff=None):
        return self.build(lambda p: [], cutoff=cutoff)["site_manager"]

    def test_no_log_and_an_empty_log(self):
        self.assertEqual(self.record(), self.EMPTY)
        self.log("firings.jsonl", [])
        self.assertEqual(self.record(), self.EMPTY)

    def test_fired_without_a_decision_or_an_outcome_gives_no_ratio(self):
        self.log("firings-2026-11.jsonl", [fired("f1", "2026-11-10T10:00:00Z"), fired("f2", "2026-11-10T10:01:00Z"),
                                           then("f2", "10:02", decision="escalated", by="watch")])
        self.assertEqual(self.record(), {**self.EMPTY, "escalated": 1, "no_outcome": 1})

    def test_counts_and_ratios_over_every_repository_and_month_file(self):
        self.log("firings.jsonl", [  # the legacy file and the month files are one log
            "not json", {"ts": "2026-11-10T09:00:00Z", "repo": ALPHA, "id": 7, "criterion": "quiet"},
            then("zz", "09:01", outcome="acted"),
            *(fired("f%d" % i, "2026-11-10T10:%02d:00Z" % i) for i in range(1, 5)),
            fired("f1", "2026-11-10T10:08:00Z", BETA),  # the same id in another repository is another firing
            then("f1", "11:01", decision="escalated"), then("f1", "11:02", decision="suppressed"),  # the first decision stands
            then("f2", "11:03", decision="escalated"), then("f3", "11:04", decision="escalated")])
        self.log("firings-2026-11.jsonl", [
            then("f1", "12:09", outcome="acted"),
            then("f4", "11:05", decision="escalated"), then("f5", "11:06", decision="escalated"),
            then("f6", "11:07", decision="suppressed"), then("f7", "11:08", decision="suppressed"),
            then("f1", "11:09", BETA, decision="escalated"), then("f1", "12:00", BETA, outcome="ignored"),
            then("f2", "12:01", outcome="ignored"), then("f2", "12:02", outcome="acted"),  # the last outcome stands
            then("f3", "12:03", outcome="acted"),
            then("f6", "12:05", outcome="acted"),  # an outcome on a suppressed firing counts nowhere
            then("f1", "12:10", cleared=True),
            {"ts": "2026-11-10T12:11:00Z", "id": "f4", "outcome": "acted"},  # no repo: the factory counts it nowhere
            {"ts": "2026-11-10T12:12:00Z", "miss": "no repo"},
            {"ts": "2026-11-10T13:00:00Z", "repo": ALPHA, "miss": "a lead was stuck", "slug": "a1", "firing": "f6"},
            {"ts": "2026-11-10T13:01:00Z", "repo": BETA, "miss": "nothing fired", "slug": None, "firing": None},
            # carried into the new month after its first record: older than the lines above, so the fold is by ts
            *(fired("f%d" % i, "2026-11-10T10:%02d:00Z" % i) for i in range(5, 8))])
        self.assertEqual(self.record(), {
            "escalated": 6, "acted": 3, "ignored": 1, "no_outcome": 2, "suppressed": 2, "paused": 0, "extended": 0, "kept": 0, "overran": 0, "missed": 2,
            "missed_suppressed": 1, "missed_never_fired": 1, "precision": 0.75, "recall": 0.6})

    def test_extended_firings_count_with_their_outcome_and_an_unknown_decision_is_not_an_error(self):
        self.log("firings-2026-11.jsonl", [
            *(fired("f%d" % i, "2026-11-10T10:%02d:00Z" % i) for i in range(1, 6)),
            then("f1", "11:01", decision="extended", minutes=20), then("f1", "11:02", outcome="kept"),
            then("f2", "11:03", decision="extended", minutes=20), then("f2", "11:04", outcome="overran"),
            then("f3", "11:05", decision="extended", minutes=20),
            then("f4", "11:06", decision="postponed"),
            then("f5", "11:07", decision="escalated", by="watch"), then("f5", "11:08", outcome="kept")])
        self.assertEqual(self.record(), {**self.EMPTY, "escalated": 1, "no_outcome": 1, "extended": 3, "kept": 1, "overran": 1})

    def test_since_keeps_firings_and_misses_by_their_own_time(self):
        self.log("firings-2026-11.jsonl", [
            fired("old", "2026-11-10T10:00:00Z"), fired("new", "2026-11-10T12:00:00Z"),
            then("old", "12:01", decision="escalated"), then("old", "12:02", outcome="ignored"),
            then("new", "12:03", decision="escalated"), then("new", "12:04", outcome="acted"),
            {"ts": "2026-11-10T11:59:59Z", "repo": ALPHA, "miss": "before"},
            {"ts": "2026-11-10T12:00:00Z", "repo": ALPHA, "miss": "at the cutoff"}, {"repo": ALPHA, "miss": "no time"}])
        self.assertEqual(self.record(datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)), {
            **self.EMPTY, "escalated": 1, "acted": 1, "missed": 1, "missed_never_fired": 1, "precision": 1.0, "recall": 0.5})
        self.assertEqual(self.record(), {**self.EMPTY, "escalated": 2, "acted": 1, "ignored": 1, "missed": 3,
                                         "missed_never_fired": 3, "precision": 0.5, "recall": 0.25})

    def test_agrees_with_the_factory_on_the_same_log(self):
        """The one place the two folds are compared: `factory firings --json` run on a store holding one repository."""
        home = os.path.dirname(self.factory)
        repo = os.path.realpath(os.path.join(home, "alpha"))
        subprocess.run(["git", "init", "-q", repo], check=True, timeout=60)
        self.factory = os.path.join(home, ".claude", "factory")
        self.log("firings.jsonl", [
            fired("f1", "2026-11-10T10:00:00Z", repo), fired("f2", "2026-11-10T10:01:00Z", repo),
            fired("f3", "2026-11-10T10:02:00Z", repo), fired("f4", "2026-11-10T10:03:00Z", repo),
            fired("f5", "2026-11-10T10:04:00Z", repo), fired("f6", "2026-11-10T10:05:00Z", repo),
            {"ts": "2026-11-10T10:30:00Z", "repo": repo, "id": "f5", "decision": "escalated", "to": "lead"},
            {"ts": "2026-11-10T10:31:00Z", "repo": repo, "id": "f5", "outcome": "ignored"},
            {"ts": "2026-11-10T10:40:00Z", "repo": repo, "id": "f5", "cleared": True},
            {"ts": "2026-11-10T10:41:00Z", "repo": repo, "id": "f6", "decision": "escalated"},  # f5 fired again, to the dispatcher
            {"ts": "2026-11-10T11:00:00Z", "repo": repo, "id": "f1", "decision": "escalated"},
            {"ts": "2026-11-10T11:01:00Z", "repo": repo, "id": "f2", "decision": "suppressed", "reason": "r"},
            {"ts": "2026-11-10T11:02:00Z", "repo": repo, "id": "f3", "decision": "escalated"},
            {"ts": "2026-11-10T11:03:00Z", "repo": repo, "id": "f4", "decision": "escalated"},
            {"ts": "2026-11-10T12:00:00Z", "repo": repo, "id": "f1", "outcome": "acted"},
            {"ts": "2026-11-10T12:01:00Z", "repo": repo, "id": "f3", "outcome": "ignored"},
            {"ts": "2026-11-10T13:00:00Z", "repo": repo, "miss": "m", "slug": None, "firing": "f2"},
            {"ts": "2026-11-10T13:01:00Z", "repo": repo, "miss": "n", "slug": None, "firing": None}])
        p = subprocess.run([os.path.join(ROOT, "factory", "bin", "factory"), "firings", "--json"], cwd=repo,
                           env={**os.environ, "HOME": home, "FACTORY_HOME": os.path.join(home, ".claude", "factory")}, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        theirs = json.loads(p.stdout)["summary"]
        self.assertEqual((theirs["escalated"], theirs["missed"]), (4, 2))
        theirs = {k: v for k, v in theirs.items() if not k.startswith("told")}  # the dashboard does not count `told`
        theirs.update({k: round(theirs[k], 6) for k in ("precision", "recall")})
        self.assertEqual(self.record(), theirs)


def came_back(ts, outcome, cost=None, slug="a1", session=None, repo=ALPHA):
    return {"ts": ts, "repo": repo, "slug": slug, "role": "tech-lead", "model": "opus", "version": "0.6.0", "session": session,
            "pr": 1, "evidence": "it conflicts", "outcome": outcome, "reason": None, "note": None, "noted": True,
            "head_before": None, "head_after": None, "cost_usd": cost, "seconds": 60}


class TechLeadTests(Store):
    EMPTY = {"came_back": 0, "resolved": 0, "escalated": 0, "stopped": 0, "unstarted": 0, "cost": None,
             "cost_resolved": None, "landed": 0, "lead_wakeups": 0}

    def record(self, gh=lambda p: [], cutoff=None):
        return self.build(gh, cutoff=cutoff)["tech_lead"]

    def test_a_store_without_the_log_gives_zero_counts_and_unknown_costs(self):
        self.assertEqual(self.record(), self.EMPTY)

    def test_each_outcome_counts_and_costs_sum_what_is_priced(self):
        t = "%sT12:00:00Z" % DAY
        self.log("techlead.jsonl", [came_back(t, "resolved", 1.5)])  # the legacy file and the month files are one log
        self.log("techlead-2026-11.jsonl", [
            came_back(t, "resolved", 2.0), came_back(t, "resolved", None), came_back(t, "escalated", 0.25),
            came_back(t, "stopped", 3.0), came_back(t, "unstarted"), came_back(t, "paused"), "not json"])  # a paused start is not counted
        self.assertEqual(self.record(), {**self.EMPTY, "came_back": 6, "resolved": 3, "escalated": 1, "stopped": 1,
                                         "unstarted": 1, "cost": 6.75, "cost_resolved": 3.5})

    def test_no_priced_record_is_unpriced_not_zero(self):
        self.log("techlead.jsonl", [came_back("%sT12:00:00Z" % DAY, "resolved"), came_back("%sT12:00:00Z" % DAY, "unstarted")])
        d = self.record()
        self.assertEqual((d["came_back"], d["cost"], d["cost_resolved"]), (2, None, None))

    def test_cost_resolved_is_null_when_only_other_outcomes_are_priced(self):
        self.log("techlead.jsonl", [came_back("%sT12:00:00Z" % DAY, "escalated", 1.0)])
        d = self.record()
        self.assertEqual((d["cost"], d["cost_resolved"]), (1.0, None))

    def test_since_keeps_records_by_their_own_time(self):
        self.log("techlead.jsonl", [came_back("%sT11:59:59Z" % DAY, "resolved", 1.0), came_back("%sT12:00:00Z" % DAY, "resolved", 2.0),
                                    {**came_back("x", "resolved", 4.0), "ts": None}])
        d = self.record(cutoff=datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual((d["came_back"], d["cost"]), (1, 2.0))

    def test_lead_wakeups_count_after_the_first_hand_over_only_role_lead_from_session_or_human_on_landed_tasks(self):
        wake = lambda hhmm, slug="a1", role="lead", who="human", event="UserPromptSubmit": ev(  # noqa: E731
            "%sT%s:00Z" % (DAY, hhmm), event, slug=slug, role=role, **{"from": who})
        extra = [
            wake("10:29"),  # before the hand-over
            wake("10:31"), wake("10:32", who="session"),  # these two count
            wake("10:33", who="subagent"), wake("10:34", who="tech-lead"),  # not from the session or the human
            wake("10:35", role="tech-lead"), wake("10:36", role="coder"),  # not the lead
            wake("10:37", event="Notification"),
            ev("%sT10:38:00Z" % DAY, "UserPromptSubmit", **{"from": "human"}),  # no role
            wake("10:39", slug="a2")]  # a task that did not land
        self.log("tasks-2026-11.jsonl", [{"ts": "%sT10:30:00Z" % DAY, "repo": ALPHA, "slug": "a1", "state": "handed-over"},
                                         {"ts": "%sT10:20:00Z" % DAY, "repo": ALPHA, "slug": "a1", "state": "working"},
                                         {"ts": "%sT10:50:00Z" % DAY, "repo": ALPHA, "slug": "a1", "state": "handed-over"},
                                         {"ts": "%sT10:00:00Z" % DAY, "repo": ALPHA, "slug": "a2", "state": "handed-over"}])
        self.log("events-2026-11.jsonl", [ev("%sT10:00:00Z" % DAY, "TaskStarted"), ev("%sT10:00:00Z" % DAY, "TaskStarted", slug="a2")]
                 + extra)
        gh = lambda p: [pr(1, "worktree-a1", "%sT10:20:00Z" % DAY, "%sT11:00:00Z" % DAY),  # noqa: E731
                        pr(2, "worktree-a2", "%sT10:20:00Z" % DAY, None, "OPEN")]
        d = self.record(gh)
        self.assertEqual((d["landed"], d["lead_wakeups"]), (1, 2))

    def test_a_task_never_handed_over_has_no_wakeups(self):
        self.log("events-2026-11.jsonl", [ev("%sT10:00:00Z" % DAY, "TaskStarted"),
                                          ev("%sT10:31:00Z" % DAY, "UserPromptSubmit", role="lead", **{"from": "human"})])
        d = self.record(lambda p: [pr(1, "worktree-a1", "%sT10:20:00Z" % DAY, "%sT11:00:00Z" % DAY)])
        self.assertEqual((d["landed"], d["lead_wakeups"]), (1, 0))

    def test_the_session_of_a_record_has_its_main_cost_under_tech_lead_and_its_dispatches_under_their_roles(self):
        self.log("events-2026-11.jsonl", [ev("%sT10:00:00Z" % DAY, "TaskStarted")])
        session(self.projects, ALPHA, "a1", "%sT10:00:05.000Z" % DAY, 1000000, [("coder", "claude-haiku-5-5", 1000000, "10:05", "10:10")])
        roles = lambda: self.build(lambda p: [])["tasks"][0]["cost"]["by_role"]  # noqa: E731
        self.assertEqual((roles()["lead"], roles()["tech-lead"], roles()["coder"]), (10.0, 0.0, 1.0))
        self.log("techlead.jsonl", [came_back("%sT12:00:00Z" % DAY, "resolved", 1.0, session="s-a1")])
        by_role = roles()
        self.assertEqual((by_role["lead"], by_role["tech-lead"], by_role["coder"]), (0.0, 10.0, 1.0))

    def test_the_role_facts_of_a_tech_lead_session_are_filed_under_tech_lead_not_lead(self):
        self.log("events-2026-11.jsonl", [ev("%sT10:00:00Z" % DAY, "TaskStarted")])
        session(self.projects, ALPHA, "a1", "%sT10:00:05.000Z" % DAY, 1000000, [("coder", "claude-haiku-5-5", 1000000, "10:05", "10:10")])
        self.log("techlead.jsonl", [came_back("%sT12:00:00Z" % DAY, "resolved", 1.0, session="s-a1")])
        with mock.patch.object(kaizen, "_authoring", wraps=kaizen._authoring) as authoring:
            self.build(lambda p: [])
        roles = authoring.call_args.args[0]
        self.assertEqual(sorted(roles), ["coder", "tech-lead"])


class ModeTests(Store):
    """Five landed tasks: s1, s2 single small; t1 team small; t2 team medium, no transcript; o1 started before modes existed."""

    def setUp(self):
        super().setUp()
        at = lambda hhmm: "%sT%s:00Z" % (DAY, hhmm)  # noqa: E731
        slugs = ("s1", "s2", "t1", "t2", "o1", "h1", "u1")
        self.log("events-2026-11.jsonl", [
            ev(at("10:00"), "TaskStarted", slug="s1", mode="single", size="small"),
            ev(at("10:00"), "TaskStarted", slug="s2", mode="single", size="small"),
            ev(at("10:00"), "TaskStarted", slug="t1", mode="team", size="small"),
            ev(at("09:59"), "TaskStarted", slug="t2", mode="single", size="medium"),  # started again, in the other mode
            ev(at("10:00"), "TaskStarted", slug="t2", mode="team", size="medium"),
            ev(at("10:00"), "TaskStarted", slug="h1", mode="single", size="large"),
            ev(at("10:00"), "TaskStarted", slug="u1", mode="single", size="large"),
            ev(at("10:00"), "TaskStarted", slug="o1"),
            ev(at("10:20"), "UserPromptSubmit", slug="t1", role="lead", **{"from": "human"}),  # after the hand-over
            ev(at("10:05"), "UserPromptSubmit", slug="t1", role="lead", **{"from": "human"})])  # before it
        self.log("tasks-2026-11.jsonl", [{"ts": at(hhmm), "repo": ALPHA, "slug": s, "state": "handed-over"}
                                         for s, hhmm in (("s1", "10:30"), ("s2", "11:00"), ("t1", "10:10"), ("o1", "10:40"),
                                                          ("h1", "09:00"))])  # h1 was handed over before it started
        self.log("retros-2026-11.jsonl", [ev(at("12:00"), "", slug="t1", defects=[{"caught_by": "reviewer"}] * 4 + [{"caught_by": "lead"}])])
        self.log("techlead-2026-11.jsonl", [{**came_back(at("12:00"), "built", 2.0, slug="t1"), "kind": "build"},
                                            {**came_back(at("12:00"), "stopped", slug="t1"), "kind": "build"},
                                            {**came_back(at("12:00"), "built", slug="t2"), "kind": "build"},
                                            {**came_back(at("12:00"), "built", slug="t2"), "kind": "build"},
                                            {**came_back(at("12:00"), "built", slug="t2"), "kind": "build"},
                                            {**came_back(at("12:00"), "built", 1.0, slug="h1", session="s-h1"), "kind": "build"}])
        session(self.projects, ALPHA, "s1", "%sT10:00:05.000Z" % DAY, 1000000,
                written={"lead": [("Write", {"file_path": ALPHA + "/.claude/worktrees/s1/isshoni/x.py", "content": "a\nb\nc\n"})]})
        session(self.projects, ALPHA, "s2", "%sT10:00:05.000Z" % DAY, 3000000)
        session(self.projects, ALPHA, "h1", "%sT10:00:05.000Z" % DAY, 1000000)  # a build run's session
        session(self.projects, ALPHA, "u1", "%sT10:00:05.000Z" % DAY, 0, [("coder", "claude-sonnet-5-5", 1000000, "10:05", "10:06")])  # unpriced
        session(self.projects, ALPHA, "t1", "%sT10:00:05.000Z" % DAY, 200000, [("reviewer", "claude-opus-5-5", 0, "10:05", "10:06")])
        prs = [pr(i, "worktree-" + s, at("10:50"), at("11:30")) for i, s in enumerate(slugs)]
        self.doc = self.build(lambda p: prs)
        self.rows = {(r["mode"], r["size"]): r for r in self.doc["summary"]["by_mode"]}

    def test_a_task_has_its_last_started_mode_and_single_when_none_is_recorded(self):
        self.assertEqual({t["slug"]: t["mode"] for t in self.doc["tasks"]}, {"s1": "single", "s2": "single", "t1": "team", "t2": "team", "o1": "single",
                                                                                      "h1": "single", "u1": "single"})

    def test_rows_are_by_size_then_mode_and_only_where_a_task_landed(self):
        self.assertEqual(list(self.rows), [("single", "small"), ("team", "small"), ("team", "medium"), ("single", "large"), ("single", None)])

    def test_a_row_holds_medians_and_counts_of_its_tasks(self):
        self.assertEqual(self.rows[("single", "small")], {
            "mode": "single", "size": "small", "n": 2, "cost_median": 20.0, "lead_share_median": 1.0, "lead_calls_median": 1.0,
            "handover_s_median": 2700.0, "first_pass": 0, "review_rounds_median": 0.0, "defects": None, "lead_wakeups": 0,
            "build_runs_median": None})
        self.assertEqual(self.rows[("team", "small")], {
            "mode": "team", "size": "small", "n": 1, "cost_median": 2.0, "lead_share_median": None, "lead_calls_median": 2.0,
            "handover_s_median": 600.0, "first_pass": 1, "review_rounds_median": 1.0,
            "defects": {"tests": 0, "reviewer": 4, "lead": 1, "ci": 0, "human": 0, "other": 0}, "lead_wakeups": 1,
            "build_runs_median": 2.0})

    def test_a_hand_over_before_the_start_is_unknown_and_a_task_with_only_unpriced_tokens_has_no_cost_in_the_median(self):
        row = self.rows[("single", "large")]
        self.assertEqual((row["n"], row["handover_s_median"], row["cost_median"]), (2, None, 10.0))  # h1: 10.0; u1: unpriced, not 0

    def test_the_cost_of_a_build_run_session_is_the_tech_leads(self):
        by_role = {t["slug"]: t["cost"]["by_role"] for t in self.doc["tasks"] if t["cost"]}
        self.assertEqual((by_role["h1"]["tech-lead"], by_role["h1"]["lead"]), (10.0, 0.0))
        self.assertEqual((by_role["t1"]["tech-lead"], by_role["t1"]["lead"]), (0.0, 2.0))

    def test_what_is_not_known_is_null_not_zero(self):
        self.assertEqual(self.rows[("team", "medium")], {
            "mode": "team", "size": "medium", "n": 1, "cost_median": None, "lead_share_median": None, "lead_calls_median": None,
            "handover_s_median": None, "first_pass": 0, "review_rounds_median": None, "defects": None, "lead_wakeups": 0,
            "build_runs_median": 3.0})

    def test_a_task_that_is_not_landed_is_in_no_row(self):
        prs = [pr(0, "worktree-t1", "%sT10:50:00Z" % DAY, None, "OPEN")]
        d = self.build(lambda p: prs)
        self.assertEqual(d["summary"]["by_mode"], [])

    def test_build_records_are_not_merge_queue_returns(self):
        self.log("techlead-2026-11.jsonl", [came_back("%sT12:00:00Z" % DAY, "resolved", 1.0, slug="t1"),  # no kind: a merge record
                                            {**came_back("%sT12:00:00Z" % DAY, "stopped", 9.0, slug="t1"), "kind": "build"},
                                            {**came_back("%sT12:00:00Z" % DAY, "unstarted", 9.0, slug="t1"), "kind": "merge"}])
        d = self.build(lambda p: [])["tech_lead"]
        self.assertEqual((d["came_back"], d["resolved"], d["stopped"], d["unstarted"], d["cost"], d["cost_resolved"]), (2, 1, 0, 1, 10.0, 1.0))

    def test_the_text_table_puts_single_and_team_of_a_size_side_by_side(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_modes(self.doc["summary"]["by_mode"], "USD")
        self.assertEqual(out.getvalue(), "\n".join([
            "landed tasks: single 5, team 2",
            "size    mode    n  cost USD  lead share  lead calls  hand-over min  first pass  rounds  defects             wakeups  builds",
            "small   single  2     20.00        100%           1             45         0/2       0  -                         0       -",
            "small   team    1      2.00           -           2             10         1/1       1  reviewer 4, lead 1        1       2",
            "medium  team    1         -           -           -              -         0/1       -  -                         0       3",
            "large   single  2     10.00           -           2              -         0/2       0  -                         0       -",
            "none    single  1         -           -           -             40         0/1       -  -                         0       -", ""]))

    def test_no_row_says_so(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_modes([], "USD")
        self.assertEqual(out.getvalue(), "no landed task carries a mode yet\n")


class SmallTests(Store):
    def test_missing_directory_and_empty_directory(self):
        calls = []
        for path, found in ((os.path.join(self.factory, "nope"), False), (self.factory, True)):
            self.factory = path
            d = self.build(lambda p: calls.append(p), sessions=lambda since: calls.append("loaded"))
            self.assertEqual((d["store_found"], d["tasks"], d["repos"], d["versions"], d["stops"], d["slowed"], d["rewards"]),
                             (found, [], [], [], [], [], []))
            self.assertEqual(d["outbox"]["entries"], [])
            self.assertEqual((d["summary"]["tasks"], d["summary"]["trend"]), (0, None))
        self.assertEqual(calls, [])  # no tasks: no transcripts loaded, no gh

    def test_legacy_file_first_then_months_in_name_order(self):
        for name, n in (("events-2026-11.jsonl", 3), ("events.jsonl", 1), ("events-2026-10.jsonl", 2), ("events-2026-1.jsonl", 9),
                        ("events-old.jsonl", 9)):
            self.log(name, ['{"n": %d}' % n, "garbage", '{"n": %d}' % (n * 10)])
        self.assertEqual([r["n"] for r in kaizen.read_log(self.factory, "events")], [1, 10, 2, 20, 3, 30])

    def test_field_poor_records_still_make_a_task(self):
        self.log("events.jsonl", [{"repo": ALPHA, "slug": "bare"}, {"repo": ALPHA, "slug": "bare", "ts": 5, "version": 3}])
        d = self.build(lambda p: [])
        self.assertEqual(len(d["tasks"]), 1)
        t = d["tasks"][0]
        self.assertEqual((t["slug"], t["version"], t["started"], t["state"], t["cost"], t["defects"], t["stops"]),
                         ("bare", None, None, None, None, None, 0))
        self.assertEqual(d["versions"], [])

    def _landed(self, n):
        """n landed tasks without transcripts: task i has brief_to_pr 60*(i+1) s and pr_to_merge 10*(i+1) s."""
        prs = []
        for i in range(n):
            opened = datetime(2026, 11, 10, i, i + 1, 0, tzinfo=timezone.utc)
            merged = opened + timedelta(seconds=10 * (i + 1))
            prs.append(pr(i, "worktree-t%02d" % i, opened.strftime("%Y-%m-%dT%H:%M:%SZ"), merged.strftime("%Y-%m-%dT%H:%M:%SZ")))
        self.log("events.jsonl", [{"ts": "2026-11-10T%02d:00:00Z" % i, "repo": ALPHA, "slug": "t%02d" % i, "event": "TaskStarted"}
                                  for i in range(n)])
        return self.build(lambda p: prs)

    def test_trend_needs_ten_landed_tasks(self):
        d = self._landed(9)
        self.assertEqual((d["summary"]["landed"], d["summary"]["trend_min"], d["summary"]["trend"]), (9, 10, None))
        d = self._landed(10)
        self.assertEqual(d["summary"]["trend"], {
            "latest_n": 5, "earlier_n": 5,
            "cost": {"latest": None, "earlier": None}, "review_rounds": {"latest": None, "earlier": None},
            "brief_to_pr_s": {"latest": 480.0, "earlier": 180.0},  # tasks 5..9 vs 0..4: 360..600 and 60..300
            "pr_to_merge_s": {"latest": 80.0, "earlier": 30.0}})
        self.assertEqual(self._landed(11)["summary"]["trend"]["latest_n"], 5)
        self.assertEqual(self._landed(11)["summary"]["trend"]["earlier_n"], 6)


def task(slug, merged, cost=5.0, brief=600, first_pass=True):
    return {"slug": slug, "pr": {"merged": merged}, "first_pass": first_pass, "brief_to_pr_s": brief,
            "cost": None if cost is None else {"total": cost}, "review_rounds": 1, "pr_to_merge_s": 60}


def days(*values, **kw):
    return [task("t%d" % i, "2026-11-%02dT00:00:00Z" % (i + 1), v, **kw) for i, v in enumerate(values)]


def mixed(lead_shares, version="0.1.0", start=0):
    """Landed tasks costing 10 each: the lead has the share given, the coder the rest."""
    zero = dict.fromkeys(kaizen.ROLES, 0.0)
    return [{"slug": "m%d" % (start + i), "version": version, "landed": True,
             "cost": {"total": 10.0, "by_role": {**zero, "lead": 10.0 * x, "coder": 10.0 * (1 - x)}}}
            for i, x in enumerate(lead_shares)]


class MixTests(unittest.TestCase):
    def lead(self, landed):
        return [(p["slug"], p["version"], p["share"]["lead"]) for p in kaizen._mix(landed)["points"]]

    def test_no_points_below_the_window(self):
        self.assertEqual(kaizen._mix(mixed([0.5] * 4)), {"window": 5, "points": []})
        self.assertEqual(kaizen._mix([]), {"window": 5, "points": []})

    def test_a_point_is_the_median_share_of_the_five_tasks_ending_at_it(self):
        landed = mixed([0.9, 0.1, 0.8, 0.7, 0.6, 0.2])
        self.assertEqual(self.lead(landed), [("m4", "0.1.0", 0.7), ("m5", "0.1.0", 0.6)])
        point = kaizen._mix(landed)["points"][0]
        self.assertEqual(list(point), ["slug", "version", "share"])
        self.assertEqual(point["share"], {"lead": 0.7, "tech-lead": 0.0, "coder": 0.3, "reviewer": 0.0, "explorer": 0.0, "lander": 0.0, "other": 0.0})
        self.assertTrue(all(type(v) is float for v in point["share"].values()))

    def test_medians_are_per_role_and_need_not_sum_to_one(self):
        zero = dict.fromkeys(kaizen.ROLES, 0.0)
        landed = [{"slug": "s%d" % i, "version": None, "cost": {"total": 10.0, "by_role": {**zero, **by}}} for i, by in enumerate((
            {"lead": 10.0}, {"lead": 10.0}, {"coder": 10.0}, {"coder": 10.0}, {"reviewer": 10.0}))]
        self.assertEqual(kaizen._mix(landed)["points"], [{"slug": "s4", "version": None, "share": zero}])

    def test_tasks_without_a_cost_take_no_place_in_the_window(self):
        landed = mixed([0.9, 0.1, 0.8, 0.7, 0.6])
        landed[1:1] = [{"slug": "none", "version": "0.1.0", "cost": None},
                       {"slug": "free", "version": "0.1.0", "cost": {"total": 0.0, "by_role": dict.fromkeys(kaizen.ROLES, 0.0)}}]
        self.assertEqual(self.lead(landed), [("m4", "0.1.0", 0.7)])

    def test_a_shift_at_a_version_shows_in_full_three_tasks_after_it(self):
        landed = mixed([0.8] * 6) + mixed([0.4] * 5, "0.2.0", 6)
        self.assertEqual(self.lead(landed), [
            ("m4", "0.1.0", 0.8), ("m5", "0.1.0", 0.8), ("m6", "0.2.0", 0.8), ("m7", "0.2.0", 0.8),
            ("m8", "0.2.0", 0.4), ("m9", "0.2.0", 0.4), ("m10", "0.2.0", 0.4)])


def facts(calls, peak, tools, dispatches, **cost):
    return {"calls": calls, "peak": peak, "tools": tools, "dispatches": dispatches, "written": written(),
            "cost": {"input": 0.0, "output": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0, "cache_read": 0.0, **cost}}


def written(**kinds):
    """A role's `written` block: kind -> (calls, lines), the other kinds empty."""
    return {k: dict(zip(("calls", "lines"), kinds.get(k, (0, 0)))) for k in authoring.KINDS}


def at(slug, version, lead, other=None, total=10.0, code=None):
    """A landed task costing `total`; the lead has the share and the facts given. `code`: role -> (calls, lines) of product code."""
    by_role = {**dict.fromkeys(kaizen.ROLES, 0.0), "lead": total * lead[0], **{r: total * x[0] for r, x in (other or {}).items()}}
    roles = {"lead": lead[1], **{r: x[1] for r, x in (other or {}).items()}}
    for r, (calls, lines) in (code or {}).items():
        roles[r] = {**roles.get(r, facts(0, 0, 0, 0)), "written": written(product=(calls, lines))}
    return {"slug": slug, "version": version, "landed": True, "cost": {"total": total, "by_role": by_role},
            "_roles": roles, "authoring": kaizen._authoring(roles)}


class AnatomyTests(unittest.TestCase):
    def tasks(self):
        """0.1.0: six tasks, the lead's calls 10..60 and a coder in the first three only; 0.2.0: two tasks."""
        old = [at("o%d" % i, "0.1.0", (0.8, facts(10 * (i + 1), 100000 + i, 4 * i, 2 * (i % 2),
                                                  input=1.0, output=1.0, cache_write_5m=1.0, cache_write_1h=1.0)),
                 {"coder": (0.2, facts(3 * i, 5000, 7, 0))} if i < 3 else None) for i in range(6)]
        new = [at("n%d" % i, "0.2.0", (0.4, facts(5, 50000, 1, 3, cache_read=2.0))) for i in range(2)]
        return old + new

    def test_a_point_needs_the_role_in_the_majority_of_the_window(self):
        out = kaizen._anatomy(self.tasks(), ["0.1.0", "0.2.0", "0.3.0"])
        self.assertEqual((out["window"], out["min"]), (5, 5))
        self.assertEqual([p["slug"] for p in out["points"]], ["o4", "o5", "n0", "n1"])
        first, second = out["points"][0]["roles"], out["points"][1]["roles"]
        self.assertEqual(first["lead"], {"calls": 30.0, "peak": 100002.0, "tools": 8.0})  # medians of the five
        self.assertEqual(first["coder"], {"calls": 3.0, "peak": 5000.0, "tools": 7.0})  # in 3 of 5: counts, of those 3
        self.assertIsNone(second["coder"])  # in 2 of 5: not enough
        self.assertEqual(list(out["points"][0]), ["slug", "version", "roles", "authoring"])
        self.assertEqual(list(first), list(kaizen.ROLES))
        self.assertIsNone(first["lander"])

    def test_a_version_is_the_median_over_its_landed_tasks(self):
        v = kaizen._anatomy(self.tasks(), ["0.1.0", "0.2.0", "0.3.0"])["versions"]
        self.assertEqual([(x["version"], x["tasks"]) for x in v], [("0.1.0", 6), ("0.2.0", 2), ("0.3.0", 0)])
        lead = v[0]["roles"]["lead"]
        self.assertEqual((lead["n"], lead["share"], lead["cost"], lead["calls"], lead["tools"], lead["dispatches"]),
                         (6, 0.8, 8.0, 35.0, 10.0, 1.0))
        self.assertEqual(lead["tools_per_dispatch"], 6.0)  # tasks with dispatches only: 4/2, 12/2, 20/2 -> median of 2, 6, 10
        self.assertEqual(v[0]["roles"]["coder"]["n"], 3)
        self.assertEqual(v[0]["roles"]["coder"]["share"], 0.2)

    def test_a_role_that_did_not_run_and_a_version_without_tasks_are_null(self):
        v = kaizen._anatomy(self.tasks(), ["0.1.0", "0.2.0", "0.3.0"])["versions"]
        self.assertIsNone(v[1]["roles"]["coder"])
        self.assertEqual(v[2]["roles"], dict.fromkeys(kaizen.ROLES))
        self.assertEqual(list(v[0]["roles"]), list(kaizen.ROLES))

    def test_tools_per_dispatch_is_null_without_dispatches_and_classes_sum_to_one(self):
        v = kaizen._anatomy([at("a", "0.1.0", (1.0, facts(1, 1, 3, 0, input=1.0, output=1.0, cache_write_5m=1.0, cache_write_1h=2.0)))],
                            ["0.1.0"])["versions"][0]["roles"]["lead"]
        self.assertIsNone(v["tools_per_dispatch"])
        self.assertEqual(v["classes"], {"input": 0.2, "output": 0.2, "cache_write": 0.6, "cache_read": 0.0})
        free = kaizen._anatomy([at("a", "0.1.0", (1.0, facts(1, 1, 3, 0)))], ["0.1.0"])["versions"][0]["roles"]["lead"]
        self.assertIsNone(free["classes"])  # nothing was spent
        for x in kaizen._anatomy(self.tasks(), ["0.1.0", "0.2.0"])["versions"]:
            for r in x["roles"].values():
                if r and r["classes"]:
                    self.assertAlmostEqual(sum(r["classes"].values()), 1.0, places=5)

    def test_tasks_without_a_cost_or_landing_take_no_part(self):
        def whole(t, landed=True):
            cost = t["cost"] and {**t["cost"], "rework": 0.0}
            return {**t, "cost": cost, "landed": landed, "state": "done", "stopped_s": 0, "defects": None, "first_pass": True,
                    "review_rounds": 1, "brief_to_pr_s": 60, "pr_to_merge_s": 60, "pr": {"merged": "2026-11-01T00:00:00Z"}}
        no_cost = whole({**at("none", "0.1.0", (1.0, facts(1, 1, 1, 1))), "cost": None})
        free = whole(at("free", "0.1.0", (1.0, facts(1, 1, 1, 1)), total=0.0))
        running = whole(at("running", "0.1.0", (0.5, facts(1, 1, 1, 1))), landed=False)
        old = [whole(t) for t in self.tasks()]
        out = kaizen._summary([no_cost, free, running, *old], ["0.1.0", "0.2.0"])["anatomy"]
        self.assertEqual([p["slug"] for p in out["points"]], ["o4", "o5", "n0", "n1"])
        self.assertEqual([v["tasks"] for v in out["versions"]], [6, 2])


class AuthoringTests(unittest.TestCase):
    def zero(self, **kw):
        return {**dict.fromkeys(kaizen.ROLES, 0), **kw}

    def test_a_task_counts_product_and_tests_of_every_role_and_nothing_else(self):
        roles = {"lead": {"written": written(product=(1, 10), tests=(2, 20), docs=(5, 500), scratch=(9, 900))},
                 "coder": {"written": written(tests=(1, 30))}, "lander": {"written": written()}}
        self.assertEqual(kaizen._authoring(roles), {"lines": self.zero(lead=30, coder=30), "calls": self.zero(lead=3, coder=1),
                                                    "lead_share": 0.5})
        self.assertEqual(kaizen._authoring({}), {"lines": self.zero(), "calls": self.zero(), "lead_share": None})

    def tasks(self, *by_task):
        """Landed tasks from [{role: (calls, lines)}]."""
        return [at("t%d" % i, "0.1.0", (0.5, facts(1, 1, 1, 0)), code=code) for i, code in enumerate(by_task)]

    def test_a_version_pools_lines_not_shares(self):
        got = kaizen._pooled(self.tasks({"lead": (1, 100)}, {"coder": (3, 300)}, {"lead": (0, 0), "coder": (1, 100)}, {}))
        self.assertEqual(got, {"tasks": 3, "lead_tasks": 1, "lines": self.zero(lead=100, coder=400),
                               "share": {**dict.fromkeys(kaizen.ROLES, 0.0), "lead": 0.2, "coder": 0.8}})

    def test_nobody_wrote_a_line_has_no_share(self):
        self.assertEqual(kaizen._pooled(self.tasks({}, {"lead": (2, 0)})), {
            "tasks": 0, "lead_tasks": 0, "lines": self.zero(), "share": None})
        self.assertEqual(kaizen._pooled([])["share"], None)

    def test_the_summary_pools_each_version_and_each_window(self):
        old = [at("o%d" % i, "0.1.0", (0.8, facts(1, 1, 1, 0)), code={"lead": (1, 80), "coder": (1, 20)}) for i in range(5)]
        new = [at("n%d" % i, "0.2.0", (0.4, facts(1, 1, 1, 0)), code={"coder": (1, 100)}) for i in range(5)]
        out = kaizen._anatomy(old + new, ["0.1.0", "0.2.0"])
        self.assertEqual([v["authoring"]["share"]["lead"] for v in out["versions"]], [0.8, 0.0])
        self.assertEqual([p["authoring"]["share"]["lead"] for p in out["points"]], [0.8, 0.64, 0.48, 0.32, 0.16, 0.0])
        self.assertEqual(out["versions"][0]["authoring"]["lead_tasks"], 5)


class OverheadTests(Store):
    def setUp(self):
        super().setUp()
        self.log("events.jsonl", [ev("%sT10:00:00Z" % DAY, "TaskStarted")])
        self.merged = [pr(1, "worktree-a1", "%sT10:50:00Z" % DAY, "%sT11:00:00Z" % DAY)]

    def overhead(self, cutoff=None, prs=None):
        return self.build(lambda p: self.merged if prs is None else prs, cutoff)["summary"]["overhead"]

    def at(self, hhmm):
        return "%sT%s:00.000Z" % (DAY, hhmm)

    def test_the_dispatcher_by_name_and_the_site_manager_by_prompt(self):
        fable, haiku = "claude-fable-5-2", "claude-haiku-5-5"
        loose(self.projects, "disp", ALPHA, [(self.at("10:00"), fable, 100000), (self.at("10:05"), fable, 50000)], title="dispatcher-alpha")
        loose(self.projects, "site", ALPHA, [(self.at("10:30"), haiku, 1000000)],
              prompt="Job: digest. Repo: %s. The dispatcher's session name is dispatcher-alpha." % ALPHA)
        loose(self.projects, "other", ALPHA, [(self.at("10:40"), fable, 100000)], title="notes")  # task-less, neither
        loose(self.projects, "plain", ALPHA, [(self.at("10:41"), fable, 100000)], prompt="Job: watching. Repo: x")
        loose(self.projects, "beta", BETA, [(self.at("10:42"), fable, 100000)], title="dispatcher-beta")  # no task of beta's in the document
        loose(self.projects, "wrong", ALPHA, [(self.at("10:43"), fable, 100000)], title="dispatcher-beta")
        self.assertEqual(self.overhead(), {"dispatcher": 1.5, "site_manager": 1.0, "sessions": 2, "unpriced_tokens": 0,
                                           "landed": 1, "per_landed": 2.5})

    def test_unpriced_tokens_are_reported(self):
        loose(self.projects, "disp", ALPHA, [(self.at("10:00"), "claude-sonnet-5-5", 7000)], title="dispatcher-alpha")
        self.assertEqual(self.overhead()["unpriced_tokens"], 7000)

    def test_the_cutoff_cuts_the_calls_of_one_long_session(self):
        fable = "claude-fable-5-2"
        loose(self.projects, "disp", ALPHA, [("%sT08:00:00.000Z" % DAY, fable, 100000), (None, fable, 100000),
                                             (self.at("10:00"), fable, 100000), (self.at("11:00"), fable, 200000)], title="dispatcher-alpha")
        cutoff = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(self.overhead(cutoff)["dispatcher"], 3.0)  # the 10:00 and 11:00 calls, at the cutoff included
        self.assertEqual(self.overhead()["dispatcher"], 5.0)  # no cutoff: every call, one without a time too

    def test_a_subagent_of_the_dispatcher_is_cut_at_the_cutoff_too(self):
        fable = "claude-fable-5-2"
        loose(self.projects, "disp", ALPHA, [(self.at("10:00"), fable, 100000)], title="dispatcher-alpha",
              subagent=[("%sT09:00:00.000Z" % DAY, fable, 400000), (self.at("10:30"), fable, 200000)])
        cutoff = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(self.overhead(cutoff)["dispatcher"], 3.0)  # its own 10:00 call and the subagent's 10:30 one, not the 09:00 one
        self.assertEqual(self.overhead()["dispatcher"], 7.0)

    def test_nothing_landed_has_no_per_landed(self):
        loose(self.projects, "disp", ALPHA, [(self.at("10:00"), "claude-fable-5-2", 100000)], title="dispatcher-alpha")
        self.assertEqual(self.overhead(prs=[]), {"dispatcher": 1.0, "site_manager": 0.0, "sessions": 1, "unpriced_tokens": 0,
                                                 "landed": 0, "per_landed": None})


class RewardTests(unittest.TestCase):
    def kinds(self, landed):
        return [(r["kind"], r["slug"], r["text"]) for r in kaizen._rewards(landed)]

    def test_first_pass_streak(self):
        three = [task("t%d" % i, "2026-11-%02dT00:00:00Z" % (i + 1)) for i in range(5)]
        self.assertEqual(self.kinds(three), [("first_pass_streak", "t4", "5 landed first-pass in a row")])
        three[1]["first_pass"] = False  # a streak of 3 at the end still counts
        self.assertEqual(self.kinds(three), [("first_pass_streak", "t4", "3 landed first-pass in a row")])
        three[4]["first_pass"] = False  # broken by the latest task
        self.assertEqual(self.kinds(three), [])
        three[4]["first_pass"] = None  # no transcripts: not first-pass
        self.assertEqual(self.kinds(three), [])
        self.assertEqual(self.kinds(three[:2]), [])  # fewer than 3

    def test_streak_follows_merge_order_not_list_order(self):
        landed = [task("old", "2026-11-01T00:00:00Z", first_pass=False)] + \
                 [task("n%d" % i, "2026-11-0%dT00:00:00Z" % (i + 2)) for i in range(3)]
        self.assertEqual(self.kinds(list(reversed(landed))), [("first_pass_streak", "n2", "3 landed first-pass in a row")])

    def test_record_low_cost(self):
        base = days(9, 8, 7, 6, 5, first_pass=False)
        self.assertEqual(self.kinds(base), [("record_low_cost", "t4", "record low cost: $5.00")])
        self.assertEqual(self.kinds(days(9, 8, 7, 6, 6, first_pass=False)), [])  # a tie is not a record
        self.assertEqual(self.kinds(days(9, 8, 7, 5, 6, first_pass=False)), [])  # not the latest
        self.assertEqual(self.kinds(days(9, 8, 7, 6, first_pass=False)), [])  # fewer than 5 with a cost
        self.assertEqual(self.kinds(days(9, 8, 7, 6, None, first_pass=False)), [])  # the latest has no cost
        self.assertEqual(self.kinds(days(9, None, 8, 7, 6, 5, first_pass=False))[0][2], "record low cost: $5.00")

    def test_record_fast(self):
        landed = days(*[5] * 5, first_pass=False)
        for t, brief in zip(landed, (900, 800, 700, 600, 125)):
            t["brief_to_pr_s"] = brief
        self.assertEqual(self.kinds(landed), [("record_fast", "t4", "fastest brief to pull request: 2m05s")])
        landed[4]["brief_to_pr_s"] = 600  # a tie
        self.assertEqual(self.kinds(landed), [])
        landed[4]["brief_to_pr_s"] = 1000
        self.assertEqual(self.kinds(landed), [])

    def test_all_three_and_none(self):
        landed = days(9, 8, 7, 6, 5)
        for t, brief in zip(landed, (900, 800, 700, 600, 500)):
            t["brief_to_pr_s"] = brief
        self.assertEqual([r["kind"] for r in kaizen._rewards(landed)], ["first_pass_streak", "record_low_cost", "record_fast"])
        self.assertEqual(kaizen._rewards([]), [])


def rest(number, head, updated, merged=None, state="closed"):
    return {"number": number, "html_url": "https://x/pull/%d" % number, "state": state, "merged_at": merged,
            "created_at": "2026-11-01T00:00:00Z", "updated_at": updated, "head": {"ref": head}}


def reply(rows=(), rc=0, err=""):
    return subprocess.CompletedProcess([], rc, json.dumps(list(rows)) if rc == 0 else "", err)


def full_page(first, updated):
    return [rest(i, "b%d" % i, updated) for i in range(first, first + 100)]


AS_OF, AS_OF_ISO = 1794668400.0, "2026-11-14T15:00:00Z"
WATERMARK = "2026-11-10T00:00:00Z"
CACHED = {"number": 1, "url": "u", "state": "OPEN", "headRefName": "worktree-a", "createdAt": "c", "mergedAt": None}


def fresh(gh):
    """A stand-in for kaizen.gh_prs that answers from a `path -> pull requests` function."""
    return lambda path, factory_dir, cached=False: (gh(path), AS_OF, None)


class GhTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.factory = tmp.name
        self.cache = os.path.join(self.factory, "cache", "kaizen-prs-%s.json" % hashlib.sha256(os.path.realpath(ROOT).encode()).hexdigest()[:16])

    def gh(self, *results, **kw):
        """gh_prs on ROOT with gh answering `results` in turn (an exception is raised, a reply is returned)."""
        with mock.patch.object(kaizen.subprocess, "run", side_effect=list(results)) as self.run:
            return kaizen.gh_prs(ROOT, self.factory, **kw)

    def seed(self, age):
        kaizen._write_cache(self.cache, {"as_of": time.time() - age, "watermark": WATERMARK, "prs": {"1": CACHED}})

    def logged(self):
        lines = []
        for p in glob.glob(os.path.join(self.factory, "gh-*.jsonl")):
            with open(p) as f:
                lines += f.readlines()
        return [json.loads(line) for line in lines]

    def test_cold_fetch_pages_until_a_short_page_and_maps_the_fields(self):
        short = [rest(7, "worktree-x", "2026-11-09T00:00:00Z", merged="2026-11-09T01:00:00Z"),
                 rest(8, "worktree-y", "2026-11-09T00:00:00Z", state="open")]
        prs, as_of, err = self.gh(reply(full_page(100, "2026-11-10T00:00:00Z")), reply(short))
        argv = [c.args[0] for c in self.run.call_args_list]
        self.assertEqual([a[:2] for a in argv], [["gh", "api"]] * 2)
        self.assertEqual([a[2] for a in argv], ["repos/{owner}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100&page=%d" % n
                                                 for n in (1, 2)])
        self.assertEqual({(c.kwargs["cwd"], c.kwargs["timeout"]) for c in self.run.call_args_list}, {(ROOT, 10)})
        by = {p["number"]: p for p in prs}
        self.assertEqual(len(prs), 102)
        self.assertEqual(by[7], {"number": 7, "url": "https://x/pull/7", "state": "MERGED", "headRefName": "worktree-x",
                                 "createdAt": "2026-11-01T00:00:00Z", "mergedAt": "2026-11-09T01:00:00Z"})
        self.assertEqual((by[8]["state"], by[100]["state"], err), ("OPEN", "CLOSED", None))
        cached = kaizen._read_cache(self.cache)
        self.assertEqual((cached["watermark"], cached["as_of"] == as_of, len(cached["prs"])), (WATERMARK, True, 102))

    def test_incremental_stops_at_the_watermark_and_merges_an_updated_pull_request(self):
        self.seed(age=700)
        page1 = full_page(100, "2026-11-12T00:00:00Z")[:99] + [rest(1, "worktree-a", "2026-11-12T00:00:00Z", merged="2026-11-12T01:00:00Z")]
        page2 = full_page(300, "2026-11-11T00:00:00Z")[:98] + [rest(2, "z", WATERMARK), rest(3, "y", WATERMARK)]
        prs, _, err = self.gh(reply(page1), reply(page2))  # a full page, but not newer than the watermark
        self.assertEqual(self.run.call_count, 2)
        by = {p["number"]: p for p in prs}
        self.assertEqual((by[1]["state"], by[1]["mergedAt"], len(prs), err), ("MERGED", "2026-11-12T01:00:00Z", 200, None))
        self.assertEqual(kaizen._read_cache(self.cache)["watermark"], "2026-11-12T00:00:00Z")

    def test_a_cold_fetch_stops_after_ten_pages(self):
        self.gh(*[reply(full_page(100 * n, "2026-11-10T00:00:00Z")) for n in range(1, 12)])
        self.assertEqual(self.run.call_count, 10)

    def test_a_young_cache_makes_no_call(self):
        self.seed(age=100)
        prs, as_of, err = self.gh()
        self.assertEqual((prs, err, self.run.called), ([CACHED], None, False))
        self.assertAlmostEqual(as_of, time.time() - 100, delta=5)

    def test_cached_makes_no_call_even_when_the_cache_is_old(self):
        self.seed(age=99999)
        prs, _, err = self.gh(cached=True)
        self.assertEqual((prs, err, self.run.called), ([CACHED], None, False))

    def test_cached_without_a_cache_is_not_fetched_yet(self):
        with self.assertRaises(kaizen.GhError) as cm:
            self.gh(cached=True)
        self.assertEqual((str(cm.exception), self.run.called), ("not fetched yet", False))

    def test_a_failed_refresh_keeps_and_uses_the_cache(self):
        self.seed(age=700)
        for failure, reason in ((reply(rc=1, err="HTTP 403: rate limited\nmore"), "HTTP 403: rate limited"),
                                (subprocess.TimeoutExpired("gh", 10), "gh unavailable: Command 'gh' timed out after 10 seconds")):
            prs, as_of, err = self.gh(failure)
            self.assertEqual((prs, err), ([CACHED], reason))
            self.assertAlmostEqual(as_of, time.time() - 700, delta=5)
        self.assertEqual(kaizen._read_cache(self.cache)["watermark"], WATERMARK)

    def test_a_partial_refresh_is_discarded(self):
        self.seed(age=700)
        prs, _, err = self.gh(reply(full_page(100, "2026-11-12T00:00:00Z")), reply(rc=1, err="HTTP 502"))
        self.assertEqual((prs, err), ([CACHED], "HTTP 502"))
        self.assertEqual(len(kaizen._read_cache(self.cache)["prs"]), 1)

    def test_recovery_clears_the_error(self):
        self.seed(age=700)
        self.assertIsNotNone(self.gh(reply(rc=1, err="HTTP 502"))[2])
        self.assertIsNone(self.gh(reply([rest(1, "worktree-a", "2026-11-12T00:00:00Z")]))[2])

    def test_failures_without_a_cache_become_one_line_reasons(self):
        cases = ((FileNotFoundError(2, "No such file or directory: 'gh'"), "gh unavailable: [Errno 2]"),
                 (subprocess.TimeoutExpired("gh", 10), "gh unavailable: Command 'gh' timed out after 10 seconds"),
                 (reply(rc=1, err="none of the git remotes\nsecond line"), "none of the git remotes"),
                 (reply(rc=4), "gh exited 4"),
                 (subprocess.CompletedProcess([], 0, "not json", ""), "gh printed no JSON"),
                 (subprocess.CompletedProcess([], 0, "{}", ""), "gh printed an unexpected document"))
        for result, reason in cases:
            with self.assertRaises(kaizen.GhError) as cm:
                self.gh(result)
            self.assertTrue(str(cm.exception).startswith(reason), (str(cm.exception), reason))
            self.assertNotIn("\n", str(cm.exception))
        self.assertFalse(os.path.exists(self.cache))

    def test_a_missing_repository_path_is_not_asked(self):
        with mock.patch.object(kaizen.subprocess, "run") as run, self.assertRaises(kaizen.GhError) as cm:
            kaizen.gh_prs(os.path.join(ROOT, "no-such-dir"), self.factory)
        self.assertEqual((str(cm.exception), run.called), ("no such repository path", False))

    @mock.patch.object(kaizen, "PR_CACHE_TTL", 0)
    def test_slow_and_failed_calls_are_logged_and_fast_ones_are_not(self):
        self.gh(reply())
        self.assertEqual(self.logged(), [])
        with mock.patch.object(kaizen.time, "monotonic", side_effect=[0, 3.5]):
            self.gh(reply())
        (slow,) = self.logged()
        self.assertEqual((slow["rc"], slow["seconds"], slow["error"], slow["cwd"], slow["argv"][:2]), (0, 3.5, "", ROOT, ["gh", "api"]))
        self.assertEqual(sorted(slow), ["argv", "cwd", "error", "rc", "seconds", "ts"])
        self.gh(reply(rc=1, err="HTTP 403"))
        self.assertEqual([(r["rc"], r["error"]) for r in self.logged()], [(0, ""), (1, "HTTP 403")])
        (path,) = glob.glob(os.path.join(self.factory, "gh-*.jsonl"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.gh(kaizen.subprocess.TimeoutExpired("gh", 10))  # the cache answers; logged as the factory logs a timeout
        self.assertEqual(self.logged()[-1]["rc"], 124)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class CliTests(Store):
    """The command end to end against a fixture store and the existing grouping transcripts (repo /r/alpha, task ci)."""

    def setUp(self):
        super().setUp()
        self.log("events-2026-01.jsonl", [{"ts": "2026-01-06T08:59:00Z", "repo": ALPHA, "slug": "ci", "version": "0.2.0",
                                           "event": "TaskStarted"}])
        self.args = ("--factory-dir", self.factory, "--projects-dir", GROUPING, "--prices", PRICES_PATH, "--agents-dir", AGENTS)

    def gh(self, path):
        return [pr(1, "worktree-ci", "2026-01-06T09:30:00Z", "2026-01-06T09:40:00Z")]

    def test_json_end_to_end(self):
        with mock.patch.object(kaizen, "gh_prs", side_effect=fresh(self.gh)):
            rc, out, _ = run_cli("kaizen", "--json", *self.args)
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual([t["slug"] for t in d["tasks"]], ["ci"])
        t = d["tasks"][0]
        self.assertEqual((t["started"], t["landed"], t["brief_to_pr_s"], t["pr_to_merge_s"], t["review_rounds"], t["first_pass"]),
                         ("2026-01-06T08:59:00Z", True, 1860, 600, 1, True))
        self.assertEqual(t["cost"], {"total": 0.004366, "unpriced_tokens": 14, "rework": 0.0,
                                     "after_review": 0.0, "by_role": {"lead": 0.004227, "tech-lead": 0.0, "coder": 0.0, "reviewer": 0.000133, "explorer": 6e-06,
                                                 "lander": 0.0, "other": 0.0}})
        self.assertEqual(d["prices"], price_file.load(PRICES_PATH).to_dict())
        self.assertEqual(d["summary"]["landed"], 1)

    def test_since_and_plain_summary(self):
        with mock.patch.object(kaizen, "gh_prs", side_effect=fresh(self.gh)):
            rc, out, _ = run_cli("kaizen", "--json", "--since", "1h", *self.args)
            self.assertEqual((rc, json.loads(out)["tasks"]), (0, []))
            rc, out, _ = run_cli("kaizen", "--json", "--since", "2026-01-06T08:59:00", *self.args)
            self.assertEqual(len(json.loads(out)["tasks"]), 1)
            rc, out, _ = run_cli("kaizen", *self.args)
        self.assertEqual(rc, 0)
        self.assertIn("tasks 1, landed 1, in flight 0", out)
        self.assertRegex(out, r"ci +0\.2\.0 +landed +31m00s +10m00s +\$0\.0044\* +1 +0")

    def test_repos_say_how_old_their_pull_requests_are_and_why_they_could_not_refresh(self):
        with mock.patch.object(kaizen, "gh_prs", side_effect=fresh(self.gh)):
            _, out, _ = run_cli("kaizen", "--json", *self.args)
        self.assertEqual(json.loads(out)["repos"], [{"repo": "alpha", "repo_path": ALPHA, "prs": "ok",
                                                     "prs_as_of": AS_OF_ISO, "prs_error": None}])
        stale = lambda path, factory_dir, cached=False: (self.gh(path), time.time() - 720, "HTTP 403")  # noqa: E731
        with mock.patch.object(kaizen, "gh_prs", side_effect=stale):
            _, out, _ = run_cli("kaizen", "--json", *self.args)
            repo = json.loads(out)["repos"][0]
            self.assertEqual((repo["prs"], repo["prs_error"]), ("ok", "HTTP 403"))
            _, out, _ = run_cli("kaizen", *self.args)
        self.assertRegex(out, r"pull requests for alpha as of 12m0\ds ago: HTTP 403\n")

    def test_cached_never_asks_gh_and_without_a_cache_says_not_fetched_yet(self):
        def no_cache(path, factory_dir, cached=False):
            self.assertTrue(cached)
            raise kaizen.GhError("not fetched yet")
        with mock.patch.object(kaizen, "gh_prs", side_effect=no_cache), mock.patch.object(kaizen.subprocess, "run") as run:
            _, out, _ = run_cli("kaizen", "--json", "--cached", *self.args)
            self.assertEqual(json.loads(out)["repos"], [{"repo": "alpha", "repo_path": ALPHA, "prs": "not fetched yet",
                                                         "prs_as_of": None, "prs_error": None}])
            _, out, _ = run_cli("kaizen", "--cached", *self.args)
        self.assertIn("pull requests unavailable for alpha: not fetched yet", out)
        self.assertFalse(run.called)

    def test_missing_directory_exits_zero_with_empty_lists(self):
        rc, out, _ = run_cli("kaizen", "--json", "--factory-dir", "/nonexistent-factory", "--projects-dir", GROUPING,
                             "--prices", PRICES_PATH)
        d = json.loads(out)
        self.assertEqual((rc, d["store_found"], d["tasks"], d["repos"], d["stops"], d["outbox"]["entries"]), (0, False, [], [], [], []))
        rc, out, _ = run_cli("kaizen", "--factory-dir", "/nonexistent-factory", "--projects-dir", GROUPING, "--prices", PRICES_PATH)
        self.assertEqual((rc, out), (0, "no factory tasks in /nonexistent-factory\n"))

    def test_modes_prints_the_table_and_honours_the_options(self):
        with mock.patch.object(kaizen, "gh_prs", side_effect=fresh(self.gh)):
            rc, out, _ = run_cli("kaizen", "--modes", *self.args)
            self.assertEqual(rc, 0)
            self.assertRegex(out, r"^landed tasks: single 1, team 0\nsize +mode.*\nnone +single +1 +0\.00 ")
            self.assertEqual(run_cli("kaizen", "--modes", "--since", "1h", *self.args)[1], "no landed task carries a mode yet\n")

    def test_document_keys(self):
        with mock.patch.object(kaizen, "gh_prs", side_effect=fresh(self.gh)):
            _, out, _ = run_cli("kaizen", "--json", *self.args)
        d = json.loads(out)
        self.assertEqual(list(d), ["generated", "factory_dir", "store_found", "prices", "roles", "repos", "versions", "tasks", "summary",
                                   "stops", "slowed", "rewards", "outbox", "site_manager", "tech_lead", "pulse", "friction"])
        self.assertEqual(d["site_manager"], SiteManagerTests.EMPTY)
        self.assertEqual(d["tech_lead"], {**TechLeadTests.EMPTY, "landed": 1})
        self.assertEqual(list(d["tasks"][0]), ["repo", "repo_path", "slug", "version", "started", "size", "state", "pr", "landed", "mode",
                                               "brief_to_pr_s", "pr_to_merge_s", "cost", "review_rounds", "first_pass", "authoring", "stops",
                                               "stopped_s", "defects"])
        self.assertEqual(list(d["summary"]), ["tasks", "landed", "in_flight", "cost", "brief_to_pr_s", "pr_to_merge_s",
                                              "review_rounds", "first_pass", "rework_share", "by_role", "mix", "anatomy", "time",
                                              "defects",
                                              "trend_min", "trend", "overhead", "by_mode"])
        self.assertEqual(list(d["outbox"]), ["issues", "decisions", "topics", "entries"])


if __name__ == "__main__":
    unittest.main()
