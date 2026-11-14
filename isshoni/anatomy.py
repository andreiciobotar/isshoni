"""The cost anatomy of a task per role: calls, contexts, cost by token class, tools, what filled the context."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from . import metrics, transcript
from .kaizen import read_log
from .prices import RATES, write_tiers

TOKENS = ("input", "output", "thinking", "cache_write_5m", "cache_write_1h", "cache_read")


class _Role:
    """Everything a role did over some runs; `block` renders it."""

    def __init__(self):
        self.runs = 0
        self.models = set()
        self.calls = 0
        self.first, self.peak, self.contexts = [], [], []
        self.tokens = dict.fromkeys(TOKENS, 0)
        self.cost = dict.fromkeys(RATES, 0.0)
        self.unpriced = 0
        self.tools = {}
        self.dispatches = 0
        self.fill = {"base": 0, "own_output": 0, "inbound": 0, "compactions": 0}
        self.estimate = {}

    def add(self, run, prices):
        self.runs += 1
        self.dispatches += run.dispatches
        for kind, n in run.tools.items():
            self.tools[kind] = self.tools.get(kind, 0) + n
        prev = None
        for c in run.calls:
            self.models.add(c.model)
            u = transcript.usage_from(c.raw)
            w5, w1 = write_tiers(c.raw)
            for k, n in (("input", u.input), ("output", u.output), ("thinking", c.thinking),
                         ("cache_write_5m", w5), ("cache_write_1h", w1), ("cache_read", u.cache_read)):
                self.tokens[k] += n
            parts = prices.parts(c.model, c.raw)
            if parts is None:
                self.unpriced += c.context + c.output
            else:
                for k, v in parts.items():
                    self.cost[k] += v
            self.contexts.append(c.context)
            self._fill(prev, c)
            prev = c
        self.calls += len(run.calls)
        if run.calls:
            self.first.append(run.calls[0].context)
            self.peak.append(max(c.context for c in run.calls))

    def _fill(self, prev, c):
        if prev is None:
            self.fill["base"] += c.context
            return
        delta = c.context - prev.context
        if delta < 0:
            self.fill["compactions"] += 1
            return
        own = min(prev.output, delta)
        inbound = delta - own
        self.fill["own_output"] += own
        self.fill["inbound"] += inbound
        chars = sum(c.inbound.values())
        if not chars:
            self.estimate["unaccounted"] = self.estimate.get("unaccounted", 0) + inbound
        for src, n in c.inbound.items():
            self.estimate[src] = self.estimate.get(src, 0) + round(inbound * n / chars)

    def block(self):
        med = lambda v: None if not v else round(metrics.percentile(v, 50))  # noqa: E731
        cost = {k: round(v, 6) for k, v in self.cost.items()}
        cost = {"total": round(sum(self.cost.values()), 6), **cost}
        return {
            "runs": self.runs,
            "models": sorted(self.models),
            "calls": self.calls,
            "context": {"first": med(self.first), "median": med(self.contexts), "peak": med(self.peak),
                        "max": max(self.contexts, default=None)},
            "tokens": dict(self.tokens),
            "cost": cost,
            "tools": dict(sorted(self.tools.items(), key=lambda kv: (-kv[1], kv[0]))),
            "dispatches": self.dispatches,
            "fill": {**self.fill, "inbound_estimate": dict(sorted(self.estimate.items(), key=lambda kv: (-kv[1], kv[0])))},
        }


def _roles(runs, prices):
    """role -> _Role over [(role, Run)]."""
    out = {}
    for name, run in runs:
        out.setdefault(name, _Role()).add(run, prices)
    return out


def _versions(factory_dir):
    """(repository path, slug) -> the version of the earliest record that carries one."""
    found = {}
    for store in ("events", "retros", "tasks"):
        for r in read_log(factory_dir, store):
            ts, v = transcript.parse_ts(r.get("ts")), r.get("version")
            if ts and isinstance(v, str) and isinstance(r.get("repo"), str) and isinstance(r.get("slug"), str):
                k = (r["repo"], r["slug"])
                if k not in found or ts < found[k][0]:
                    found[k] = (ts, v)
    return {k: v for k, (_, v) in found.items()}


def _version_key(v):
    """Numeric parts compare as numbers, others as text; no version last."""
    return (v is None, [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"[.\-+]", v or "")])


def _summary(roles):
    return {"cost": round(sum(sum(r.cost.values()) for r in roles.values()), 6),
            "unpriced_tokens": sum(r.unpriced for r in roles.values()),
            "roles": {n: r.block() for n, r in sorted(roles.items())}}


def build(sessions, prices, factory_dir, now=None):
    factory_dir = os.path.expanduser(factory_dir)
    versions = _versions(factory_dir)
    groups = {}
    for s in sessions:
        if s.task:
            groups.setdefault((s.repo_path, s.task), []).append(s)
    tasks = []
    for (repo_path, task), ss in groups.items():
        runs = [("lead", s.run) for s in ss if s.run] + [
            (metrics.role(d.agent_type), d.run) for s in ss for d in s.dispatches if d.run]
        t = metrics.totals(ss)
        tasks.append({"repo": ss[0].repo, "repo_path": repo_path, "task": task, "version": versions.get((repo_path, task)),
                      "start": t["start"], "end": t["end"], **_summary(_roles(runs, prices)), "_runs": runs})
    tasks.sort(key=lambda t: (t["start"] is not None, t["start"] or "", t["task"], t["repo_path"] or ""))
    by_version = {}
    for t in tasks:
        by_version.setdefault(t["version"], []).append(t)
    rolled = []
    for v, ts in sorted(by_version.items(), key=lambda kv: _version_key(kv[0])):
        roles = _roles([r for t in ts for r in t["_runs"]], prices)
        rolled.append({"version": v, "tasks": len(ts), **_summary(roles)})
    for t in tasks:
        del t["_runs"]
    now = now or datetime.now(timezone.utc)
    return {"generated": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "factory_dir": factory_dir,
            "prices": prices.to_dict(), "tasks": tasks, "versions": rolled}
