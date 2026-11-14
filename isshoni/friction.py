"""The friction figures: what the pulse leaves out (docs/isshoni.md, "Kaizen measures"). Every one follows the cutoff, except the
Bash-call counts of the refusals, which cover whole runs. Nothing here runs git, gh or a model; kaizen.build calls `build`."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter

from . import kaizen, pulse
from .kaizen import CAUSES, ROLES, _dispatch_role, _iso, _key, _median, _name, _price, _ts

AFTER_CALLS = 3  # the calls after a refusal whose spend is shown
QUEUE_STEPS = {"updated": "updated", "rerun": "rerun", "sent-back": "sent_back", "stopped": "stopped"}
CONFLICT_STEPS = ("sent-back", "land-conflict")
ORDER = (*[r for r in ROLES if r != "other"], "dispatcher", "site_manager", "other")


def _share(n, d):
    return round(n / d, 6) if d else None


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _dispatcher(sessions, repos, prices, cutoff):
    """The dispatcher's main-thread calls (the relay's memory is its context): cost per call and the median context."""
    priced = [x for s in sessions if kaizen._spender(s, repos) == "dispatcher" for x in _price(s.run, prices, cutoff)]
    cost = sum(c for _, c, _ in priced)
    return {"calls": len(priced), "cost": round(cost, 6), "per_call": _share(cost, len(priced)),
            "context_median": _median(c.context for c, _, _ in priced), "unpriced_tokens": sum(t for *_, t in priced)}


def _rework(tasks):
    """What every call after the first review cost, lead included, against the tasks' whole cost."""
    costed = [t["cost"] for t in tasks if t["cost"]]
    after = [c["after_review"] for c in costed if c["after_review"] is not None]
    total = sum(c["total"] for c in costed)
    return {"cost": round(sum(after), 6), "task_cost": round(total, 6), "share": _share(sum(after), total),
            "per_task_median": _median(after), "n": len(after)}


def _runs(sessions, keys, repos, tech_sessions):
    """[(role, run)] of the task sessions and of the dispatcher and site manager sessions: main threads, then their dispatches."""
    for s in sessions:
        if (s.repo_path, s.task) in keys:
            main = "tech-lead" if s.id in tech_sessions else "lead"
        else:
            main = kaizen._spender(s, repos)
        if main:
            yield main, s.run
            for d in s.dispatches:
                yield _dispatch_role(d), d.run


def _refusals(runs, prices, cutoff):
    rows = {}
    for role, run in runs:
        if not run:
            continue
        row = rows.setdefault(role, {"role": role, "bash": 0, "guard": 0, "classifier": 0, "after_cost": 0.0})
        row["bash"] += sum(n for k, n in run.tools.items() if k == "Bash" or k.startswith("Bash:"))
        refused = [(ts, kind) for ts, kind in run.refused if cutoff is None or ts >= cutoff]
        if not refused:
            continue  # nothing to price
        calls = sorted(((c.ts, cost) for c, cost, _ in _price(run, prices) if c.ts), key=lambda x: x[0])
        stamps, after = [t for t, _ in calls], set()
        for ts, kind in refused:
            row[kind] += 1
            first = bisect_right(stamps, ts)
            after.update(range(first, first + AFTER_CALLS))  # a call in two windows counts once
        row["after_cost"] += sum(calls[i][1] for i in after if i < len(calls))
    out = [{"role": r["role"], "bash": r["bash"], "guard": r["guard"], "classifier": r["classifier"],
            "share": _share(r["guard"] + r["classifier"], r["bash"]), "after_cost": round(r["after_cost"], 6)}
           for r in rows.values() if r["bash"] or r["guard"] or r["classifier"]]
    return {"after_calls": AFTER_CALLS, "roles": sorted(out, key=lambda r: ORDER.index(r["role"]))}


def _wakeups(tasks, by_task, mine, prices, cutoff):
    """The prompts from the session after a hand-over, and what the session's calls cost from each until the next prompt
    from the session or the human (one from a subagent or a notification continues the same work)."""
    prompts = woken = 0
    cost = 0.0
    for t in tasks:
        key = (t["repo_path"], t["slug"])
        recs = by_task[key]
        events = kaizen._wakeups(recs, kaizen._handed_over(recs))
        counted = [e.get("from") == "session" and (cutoff is None or _ts(e) >= cutoff) for e in events]
        prompts += sum(counted)
        woken += any(counted)
        for s in mine.get(key, []):
            opened = sorted((_ts(e), c) for e, c in zip(events, counted) if e.get("session") == s.id)
            if not opened:
                continue  # nothing to price
            stamps = [ts for ts, _ in opened]
            for ts, spent, _ in kaizen._spend(s, prices):
                i = bisect_right(stamps, ts) if ts else 0
                cost += spent if i and opened[i - 1][1] else 0.0
    return {"prompts": prompts, "tasks": woken, "cost": round(cost, 6), "per_prompt": _share(cost, prompts)}


def _pr(r):
    return (r["repo"], r["pr"]) if isinstance(r.get("repo"), str) and _int(r.get("pr")) else None


def _queue(queue, cutoff):
    """Queue attempts per pull request merged in the range: the steps before its merge. None without a queue log."""
    if not queue:
        return None
    merged = {}
    for r in queue:  # oldest first
        if r.get("step") == "merged" and _pr(r) and (cutoff is None or _ts(r) >= cutoff):
            merged.setdefault(_pr(r), _ts(r))
    steps = Counter(QUEUE_STEPS[r["step"]] for r in queue
                    if r.get("step") in QUEUE_STEPS and _pr(r) in merged and _ts(r) <= merged[_pr(r)])
    return {"since": _iso(_ts(queue[0])), "merged": len(merged), **{k: steps[k] for k in QUEUE_STEPS.values()},
            "per_merged": _share(sum(steps.values()), len(merged))}


def _held(by_task, queue, cutoff):
    """Per task, the queue's send-backs and the pause records (written by another task: they may be absent)."""
    rows = {}
    row = lambda key: rows.setdefault(key, {"repo": _name(key[0]), "slug": key[1], "sent_back": 0, "paused": 0,  # noqa: E731
                                            "paused_minutes": 0, "prioritized": 0})
    in_range = lambda r: cutoff is None or bool(_ts(r) and _ts(r) >= cutoff)  # noqa: E731
    for r in queue:
        if r.get("step") == "sent-back" and _key(r) and in_range(r):
            row(_key(r))["sent_back"] += 1
    for key, recs in by_task.items():
        for e in recs["events"]:
            if e.get("event") == "TaskPaused" and in_range(e):
                row(key)["paused"] += 1
            elif e.get("event") == "TaskPrioritized" and in_range(e):
                row(key)["prioritized"] += 1
                row(key)["paused_minutes"] += e["paused_minutes"] if _int(e.get("paused_minutes")) else 0
    held = [r for r in rows.values() if r["sent_back"] or r["paused"] or r["prioritized"]]
    return sorted(held, key=lambda r: (-(r["sent_back"] + r["paused"] + r["prioritized"]), r["repo"], r["slug"]))


def _late_conflicts(factory_dir, tasks, queue, cutoff):
    """Conflicts per task and per file from two sources that overlap and are never added: the queue log, and the conflicted base
    merges git history gave for the filtered tasks' pull requests (only those the pulse already looked up)."""
    log_task, log_file, git_task, git_file = Counter(), Counter(), Counter(), Counter()
    for r in queue:
        if (r.get("step") == "land-conflict" or (r.get("step") == "sent-back" and r.get("kind") == "conflict")) \
                and (cutoff is None or _ts(r) >= cutoff):
            if _key(r):
                log_task[_key(r)] += 1
            for f in r["files"] if isinstance(r.get("files"), list) else []:
                if isinstance(f, str) and isinstance(r.get("repo"), str):
                    log_file[(r["repo"], f)] += 1
    found = set()
    for repo in {t["repo_path"] for t in tasks}:
        cache = pulse.read_merges(factory_dir, repo)
        by_pr = {e["pr"]: e for e in cache.values() if isinstance(e, dict) and _int(e.get("pr")) and isinstance(e.get("base_merges"), list)}
        for t in tasks:
            entry = by_pr.get(t["pr"]["number"]) if t["repo_path"] == repo and t["pr"] else None
            if entry:
                found.add((repo, t["slug"]))
                for b in entry["base_merges"]:
                    if isinstance(b, dict) and b.get("conflicted") is True:
                        git_task[(repo, t["slug"])] += 1
                        for f in b["files"] if isinstance(b.get("files"), list) else []:
                            git_file[(repo, f)] += 1

    def rows(logs, gits, name):
        out = [{"repo": _name(k[0]), name: k[1], "log": logs[k] if queue else None, "git": gits[k]} for k in logs.keys() | gits.keys()]
        return sorted(out, key=lambda r: (-max(r["log"] or 0, r["git"]), r["repo"], r[name]))

    return {"tasks": rows(log_task, git_task, "slug"), "files": rows(log_file, git_file, "file"),
            "log_tasks": len(log_task) if queue else None, "git_tasks": len(git_task), "git_prs": len(found)}


def build(factory_dir, tasks, by_task, sessions, prices, cutoff, queue, tech_sessions):
    """The `friction` of the document. `tasks`: those kept by the cutoff, with their pull request and cost; `by_task`: every task's
    records by store; `queue`: `pulse.read_queue`'s records; `tech_sessions`: ids of the tech lead runs."""
    keys = {(t["repo_path"], t["slug"]) for t in tasks}
    repos = {k[0] for k in keys}
    mine = {}
    for s in sessions:
        mine.setdefault((s.repo_path, s.task), []).append(s)
    return {
        "dispatcher": _dispatcher(sessions, repos, prices, cutoff),
        "rework": _rework(tasks),
        "causes": {k: sum(t["_causes"][k] for t in tasks if t["_causes"]) for k in (*CAUSES, "unstated")},
        "refusals": _refusals(_runs(sessions, keys, repos, tech_sessions), prices, cutoff),
        "wakeups": _wakeups(tasks, by_task, mine, prices, cutoff),
        "queue": _queue(queue, cutoff),
        "held": _held(by_task, queue, cutoff),
        "late_conflicts": _late_conflicts(factory_dir, tasks, queue, cutoff)}
