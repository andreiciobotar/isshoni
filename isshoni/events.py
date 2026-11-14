"""The event feed of the Hanami tab: transcripts, factory logs and gh as one JSON line per event (read-only).

History and follow share one reader: a file is read by byte offset, complete lines only, so history is the
first read and following is every read after it.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from . import kaizen, metrics, transcript

TOOL_KINDS = {
    "shell": ("Bash", "bash"),
    "read": ("Read", "Grep", "Glob", "WebFetch", "WebSearch", "ToolSearch"),
    "edit": ("Edit", "NotebookEdit"),
    "write": ("Write",),
    "dispatch": ("Agent", "Task", "Skill"),
    "message": ("SendMessage", "SubagentHandback", "AskUserQuestion"),
}
TOOL_KIND = {name: kind for kind, names in TOOL_KINDS.items() for name in names}
GH_FIELDS = "number,state,headRefName,createdAt,mergedAt,closedAt,statusCheckRollup"
CI_PENDING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED"}
CI_FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
WATCH_LINE = re.compile(r"(\S+) watch \S")


def _ev(ts, kind, **fields):
    """An event; empty fields are left out."""
    return {"ts": ts, "kind": kind, **{k: v for k, v in fields.items() if v not in ("", None)}}


def line(ev):
    """The event as its JSON line (without the newline)."""
    ts = ev["ts"].astimezone(timezone.utc)
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ts.microsecond // 1000)
    return json.dumps({**ev, "ts": stamp}, separators=(",", ":"), ensure_ascii=False)


def write(out, batch):
    out.write("".join(line(e) + "\n" for e in batch))
    out.flush()


def gh_prs(repo_path):
    """Every pull request of the repository with its check rollup; kaizen.GhError when gh cannot list them."""
    return kaizen.gh_prs(repo_path, GH_FIELDS)


def ci_state(rollup):
    """running, green or red from gh's statusCheckRollup; None when there are no checks."""
    states = []
    for c in rollup if isinstance(rollup, list) else []:
        if isinstance(c, dict):  # a check run has status and conclusion, a status context has state
            done = c.get("conclusion") if c.get("status") == "COMPLETED" else c.get("status")
            states.append(str(c.get("state") or done or "").upper())
    if not states:
        return None
    if CI_FAILED.intersection(states):
        return "red"
    return "running" if CI_PENDING.intersection(states) else "green"


def _verdict(role, text):
    """A reviewer's verdict from the text it returned; empty for other roles and for no verdict."""
    if role != "reviewer":
        return ""
    return "changes" if metrics._is_changes_needed(text) else "approve" if "APPROVE" in text else ""


class _Source:
    """A growing file read by byte offset; only complete lines are returned."""

    def __init__(self, path):
        self.path, self.offset = path, 0

    def read(self):
        try:
            with open(self.path, "rb") as f:
                if os.fstat(f.fileno()).st_size < self.offset:
                    self.offset = 0  # truncated or replaced: it is a new file
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        self.offset += end
        return data[:end].decode("utf-8", "replace").split("\n")[:-1]


class _Transcript(_Source):
    def __init__(self, path, session, agent=""):
        super().__init__(path)
        self.session, self.agent = session, agent
        self.role = "" if agent else "main"
        self.parent = ""
        self.started = not agent  # the spawn is the first line of a subagent file
        self.texts = {}  # message id -> text blocks seen so far (reviewers only)
        self.returned = set()
        self.done = False  # it has returned at least once


class Feed:
    """What happened since `cutoff`. `gh(repo_path)` lists a repository's pull requests (None: no gh)."""

    def __init__(self, projects_dir, factory_dir, cutoff, gh=None, follow=False, now=None):
        self.projects_dir, self.factory_dir = os.path.expanduser(projects_dir), os.path.expanduser(factory_dir)
        self.cutoff, self.gh, self.follow = cutoff, gh, follow
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.transcripts, self.logs = {}, {}
        self.tools, self.messages = set(), set()  # block and message ids seen in any transcript
        self.open_questions = set()
        self.repos = set()  # repositories named by a tasks record in the window
        self.sent, self.ci, self.warned = set(), {}, set()

    # ---- files ----------------------------------------------------------------------

    def discover(self):
        found = {}
        for p in glob.glob(os.path.join(glob.escape(self.projects_dir), "*", "*.jsonl")):
            found[p] = (os.path.basename(p)[:-len(".jsonl")], "")
        for p in glob.glob(os.path.join(glob.escape(self.projects_dir), "*", "*", "subagents", "agent-*.jsonl")):
            found[p] = (os.path.basename(os.path.dirname(os.path.dirname(p))), os.path.basename(p)[len("agent-"):-len(".jsonl")])
        for p, (session, agent) in found.items():
            if p not in self.transcripts:
                try:
                    fresh = os.path.getmtime(p) >= self.cutoff.timestamp()
                except OSError:
                    continue
                if fresh:
                    self.transcripts[p] = _Transcript(p, session, agent)
        for p in glob.glob(os.path.join(glob.escape(self.factory_dir), "tasks*.jsonl")):
            self.logs.setdefault(p, _Source(p))
        watch = os.path.join(self.factory_dir, "watch.log")
        self.logs.setdefault(watch, _Source(watch))

    def poll(self, discover=True):
        """Events from what was appended since the last poll, sorted by ts."""
        if discover:
            self.discover()
        out = []
        for t in list(self.transcripts.values()):
            out += self._transcript(t)
        records = []
        for p, src in self.logs.items():
            if os.path.basename(p) == "watch.log":
                out += self._watch(src)
            else:
                records += self._records(src)
        out += self._tasks(sorted(records, key=lambda r: r[0]))
        # a viewer that just arrived still sees what began before the cutoff and is not over: a question not yet
        # answered, an agent that has not returned
        waiting = {slug for _, slug in self.open_questions} if self.follow else ()
        running = {t.agent for t in self.transcripts.values() if t.agent and not t.done} if self.follow else ()
        keep = lambda e: (e["ts"] >= self.cutoff or (e["kind"] == "question" and e["slug"] in waiting)  # noqa: E731
                          or (e["kind"] == "spawn" and e["agent"] in running))
        return sorted(filter(keep, out), key=lambda e: e["ts"])

    def history(self):
        """The poll plus the first look at gh, sorted."""
        return sorted(self.poll() + self.poll_gh(), key=lambda e: e["ts"])

    # ---- transcripts ----------------------------------------------------------------

    def _transcript(self, t):
        out = []
        for text in t.read():
            try:
                obj = json.loads(text)
            except (ValueError, RecursionError):
                continue
            ts = transcript.parse_ts(obj.get("timestamp")) if isinstance(obj, dict) else None
            if ts is None:
                continue
            if not t.started:
                t.started = True
                meta = transcript._read_meta(t.path[:-len(".jsonl")] + ".meta.json")
                kind, parent = meta.get("agentType"), meta.get("parentAgentId")
                t.role = metrics.role(kind) if isinstance(kind, str) else ""
                t.parent = parent if isinstance(parent, str) else ""
                out.append(_ev(ts, "spawn", session=t.session, agent=t.agent, parent=t.parent, role=t.role))
            msg = obj.get("message")
            if obj.get("type") == "assistant" and isinstance(msg, dict):
                out += self._assistant(t, ts, msg, obj)
        return out

    def _assistant(self, t, ts, msg, obj):
        who = {"session": t.session, "agent": t.agent, "role": t.role}
        out = []
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str) and b["id"] not in self.tools:
                self.tools.add(b["id"])
                out.append(_ev(ts, "tool", tool=TOOL_KIND.get(b.get("name"), "other"), **who))
                if t.agent and b.get("name") == "SubagentHandback":
                    given = b.get("input")
                    given = given.get("message") if isinstance(given, dict) else None
                    out.append(_ev(ts, "return", verdict=_verdict(t.role, given if isinstance(given, str) else ""), **who))
                    t.done = True
        key = next((v for v in (msg.get("id"), obj.get("requestId"), obj.get("uuid")) if isinstance(v, str) and v), str(id(obj)))
        if msg.get("model") != "<synthetic>" and isinstance(msg.get("usage"), dict) and key not in self.messages:
            self.messages.add(key)
            u = transcript.usage_from(msg["usage"])
            out.append(_ev(ts, "tokens", **who, **{"in": u.input, "out": u.output, "cache_read": u.cache_read, "cache_write": u.cache_write}))
        if t.agent:
            if t.role == "reviewer":
                t.texts.setdefault(key, []).extend(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
            if msg.get("stop_reason") == "end_turn" and key not in t.returned:
                t.returned.add(key)
                t.done = True
                text = "\n".join(t.texts.pop(key, []))
                out.append(_ev(ts, "return", verdict=_verdict(t.role, text), **who))
        return out

    # ---- factory logs ---------------------------------------------------------------

    def _watch(self, src):
        ticks = (WATCH_LINE.match(text) for text in src.read())
        stamps = (transcript.parse_ts(m.group(1)) for m in ticks if m)
        return [_ev(ts, "watch") for ts in stamps if ts]

    def _records(self, src):
        out = []
        for text in src.read():
            try:
                rec = json.loads(text)
            except (ValueError, RecursionError):
                continue
            ts = transcript.parse_ts(rec.get("ts")) if isinstance(rec, dict) else None
            if ts:
                out.append((ts, rec))
        return out

    def _tasks(self, records):
        out = []
        for ts, rec in records:
            repo, slug, state = rec.get("repo"), rec.get("slug"), rec.get("state")
            if not (isinstance(repo, str) and isinstance(slug, str) and isinstance(state, str) and repo and slug and state):
                continue
            if ts >= self.cutoff or self.follow:  # a follower sees every open pull request, however long its repository was quiet
                self.repos.add(repo)
            if state == "blocked" and (repo, slug) not in self.open_questions:
                self.open_questions.add((repo, slug))
                out.append(_ev(ts, "question", slug=slug))
            elif state != "blocked" and (repo, slug) in self.open_questions:
                self.open_questions.remove((repo, slug))
                out.append(_ev(ts, "answer", slug=slug))
        return out

    # ---- gh -------------------------------------------------------------------------

    def poll_gh(self):
        """pr, merge and (following only) ci events for every repository seen so far."""
        out = []
        for repo in sorted(self.repos if self.gh else ()):
            try:
                prs = self.gh(repo)
            except kaizen.GhError as e:
                if repo not in self.warned:
                    self.warned.add(repo)
                    print("isshoni: gh: %s: %s" % (repo, e), file=sys.stderr)
                continue
            for p in prs:
                if isinstance(p, dict) and isinstance(p.get("number"), int):
                    out += self._pull_request(repo, p)
        return out

    def _pull_request(self, repo, p):
        n, state = p["number"], p.get("state")
        slug = str(p.get("headRefName") or "").removeprefix("worktree-")
        opened, merged, closed = (transcript.parse_ts(p.get(k)) for k in ("createdAt", "mergedAt", "closedAt"))
        name = os.path.basename(repo.rstrip("/"))  # pull request numbers repeat across repositories
        wanted = [("open", opened, _ev(opened, "pr", repo=name, pr=n, slug=slug, state="open") if opened else None)]
        if state == "MERGED" and merged:
            wanted.append(("merge", merged, _ev(merged, "merge", repo=name, pr=n)))
        elif state == "CLOSED" and closed:
            wanted.append(("closed", closed, _ev(closed, "pr", repo=name, pr=n, slug=slug, state="closed")))
        out = []
        for what, ts, ev in wanted:
            if ev and (repo, n, what) not in self.sent and (ts >= self.cutoff or (self.follow and what == "open" and state == "OPEN")):
                self.sent.add((repo, n, what))
                out.append(ev)
        ci = ci_state(p.get("statusCheckRollup")) if self.follow and state == "OPEN" else None
        if ci and self.ci.get((repo, n)) != ci:
            self.ci[(repo, n)] = ci
            out.append(_ev(self.now(), "ci", repo=name, pr=n, state=ci))
        return out


def follow(feed, emit, stop, tick=0.5, discover=2.0, gh=60.0):
    """Emit the history, a `live` marker, then what is appended, until `stop` is set. `emit(batch)` gets sorted
    event lists. Everything before the marker had happened when the stream started; what follows is news,
    however old its timestamp (gh is polled once a minute, and a watch tick's line lands when the tick ends)."""
    emit(feed.history() + [_ev(feed.now(), "live")])
    last_discover = last_gh = time.monotonic()
    while not stop.wait(tick):
        now = time.monotonic()
        due = now - last_discover >= discover
        last_discover = now if due else last_discover
        batch = feed.poll(discover=due)
        if now - last_gh >= gh:
            last_gh = now
            batch = sorted(batch + feed.poll_gh(), key=lambda e: e["ts"])
        if batch:
            emit(batch)
