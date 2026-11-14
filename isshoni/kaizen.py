"""Kaizen measures: is the factory getting better. Reads the factory's stores read-only (docs/factory.md, "Logs")."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import authoring, metrics, transcript
from .cli import fmt_duration

ROLES = ("lead", "tech-lead", "coder", "reviewer", "explorer", "lander", "other")
DEFECT_KINDS = ("tests", "reviewer", "lead", "ci", "human", "other")
MODES = ("single", "team")
SIZES = ("small", "medium", "large")  # a task without a stated size sorts last, as `None`
STOP_KINDS = ("QUESTION", "BLOCKED", "OVERTIME")
STATUSES = ("open", "accepted", "rejected", "done")
TREND_MIN = 10
MIX_WINDOW = 5
PR_CACHE_TTL = 600  # seconds a repository's pull request cache is trusted
PAGE_SIZE, MAX_PAGES, PAGE_TIMEOUT, SLOW_GH = 100, 10, 10, 2
ANATOMY_WINDOW = MIX_WINDOW
VERSION_MIN = 5
TOPICS = [(name, re.compile(pat, re.I)) for name, pat in (
    ("permissions", r"permission|denied|refus|sandbox|isolation|not allowed"),
    ("brief", r"\bbrief\b|\bspec\b|did not say|unstated|ambigu"),
    ("rules", r"\brule|lead\.md|role file|skill|coding-taste|instruction|CLAUDE\.md"),
    ("tools", r"\btool|command|\bgit\b|\bgh\b|bash|script|hook|sendmessage|worktree"),
    ("review", r"review|verdict"),
    ("time", r"time limit|overtime|slow|wait|minutes"),
)]


class GhError(Exception):
    """gh could not list the pull requests; the message is one line."""


# ---- reading the stores -------------------------------------------------------------

def _records(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def read_log(factory_dir, name):
    """The unsuffixed legacy file first, then the month files in name order."""
    d = glob.escape(factory_dir)
    paths = [os.path.join(factory_dir, name + ".jsonl")]
    paths += sorted(glob.glob(os.path.join(d, name + "-[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl")))
    return [r for p in paths for r in _records(p)]


def _str(rec, key):
    v = rec.get(key)
    return v if isinstance(v, str) and v else None


def _key(rec):
    repo, slug = _str(rec, "repo"), _str(rec, "slug")
    return (repo, slug) if repo and slug else None


def _name(repo_path):
    return os.path.basename(repo_path.rstrip("/")) or repo_path if repo_path else None


def _ts(rec):
    return transcript.parse_ts(rec.get("ts"))


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _seconds(later, earlier):
    return int(round((later - earlier).total_seconds()))


# ---- gh -----------------------------------------------------------------------------

def _log_gh(factory_dir, argv, cwd, seconds, rc, error):
    """One line in the factory's gh debug log for a slow or failed call; never raises."""
    now = datetime.now(timezone.utc)
    try:
        fd = os.open(os.path.join(factory_dir, "gh-%s.jsonl" % now.strftime("%Y-%m")),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _iso(now), "argv": argv, "cwd": cwd, "seconds": round(seconds, 1), "rc": rc,
                                "error": error or ""}) + "\n")
    except OSError:
        pass


def _gh_page(repo_path, factory_dir, page):
    """One page of the repository's pull requests, newest update first; GhError when gh cannot."""
    argv = ["gh", "api", "repos/{owner}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=%d&page=%d" % (PAGE_SIZE, page)]
    start, rc, error = time.monotonic(), None, None
    try:
        p = subprocess.run(argv, cwd=repo_path, capture_output=True, text=True, timeout=PAGE_TIMEOUT)
        rc = p.returncode
        if rc != 0:
            error = (p.stderr.strip().splitlines() or ["gh exited %d" % rc])[0]
        else:
            try:
                rows = json.loads(p.stdout)
            except ValueError:
                error = "gh printed no JSON"
            else:
                if not isinstance(rows, list):
                    error = "gh printed an unexpected document"
    except (OSError, subprocess.SubprocessError) as e:
        rc = 124 if isinstance(e, subprocess.TimeoutExpired) else 127  # as the factory logs them
        error = "gh unavailable: %s" % str(e).splitlines()[0] if str(e) else "gh unavailable"
    seconds = time.monotonic() - start
    if error or seconds > SLOW_GH:
        _log_gh(factory_dir, argv, repo_path, seconds, rc, error)
    if error:
        raise GhError(error)
    return rows


def _from_rest(r):
    merged = r.get("merged_at")
    return {"number": r["number"], "url": r.get("html_url"), "state": "MERGED" if merged else str(r.get("state")).upper(),
            "headRefName": (r.get("head") or {}).get("ref"), "createdAt": r.get("created_at"), "mergedAt": merged}


def _refresh(repo_path, factory_dir, cache):
    """The cache brought up to date, page by page, down to its watermark; GhError discards the partial result."""
    prs, watermark = dict(cache["prs"]) if cache else {}, cache["watermark"] if cache else None
    newest = watermark
    for page in range(1, MAX_PAGES + 1):
        rows = [r for r in _gh_page(repo_path, factory_dir, page) if isinstance(r, dict) and isinstance(r.get("number"), int)]
        for r in rows:
            prs[str(r["number"])] = _from_rest(r)
        stamps = [str(r.get("updated_at") or "") for r in rows]
        newest = max([newest or ""] + stamps) or None
        if len(rows) < PAGE_SIZE or (watermark and min(stamps) <= watermark):
            break
    return {"as_of": time.time(), "watermark": newest, "prs": prs}


def _read_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    ok = isinstance(c, dict) and isinstance(c.get("as_of"), (int, float)) and isinstance(c.get("prs"), dict)
    return c if ok and isinstance(c.get("watermark"), (str, type(None))) else None


def _write_cache(path, cache):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".kaizen-prs-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def gh_prs(repo_path, factory_dir, cached=False):
    """(pull requests, epoch of the data, why it could not be refreshed or None), from the repository's disk cache,
    refreshed when older than PR_CACHE_TTL; GhError when there is no data at all."""
    if not os.path.isdir(repo_path):
        raise GhError("no such repository path")
    path = os.path.join(factory_dir, "cache", "kaizen-prs-%s.json" % hashlib.sha256(os.path.realpath(repo_path).encode()).hexdigest()[:16])
    cache = _read_cache(path)
    if cached:
        if not cache:
            raise GhError("not fetched yet")
        return list(cache["prs"].values()), cache["as_of"], None
    if cache and time.time() - cache["as_of"] < PR_CACHE_TTL:
        return list(cache["prs"].values()), cache["as_of"], None
    try:
        fresh = _refresh(repo_path, factory_dir, cache)
        _write_cache(path, fresh)
    except (GhError, OSError) as e:
        if not cache:
            raise GhError(str(e)) from e
        return list(cache["prs"].values()), cache["as_of"], str(e)
    return list(fresh["prs"].values()), fresh["as_of"], None


def _pr_for(prs, slug):
    mine = [p for p in prs if isinstance(p, dict) and p.get("headRefName") == "worktree-" + slug]
    if not mine:
        return None
    p = max(mine, key=lambda p: p.get("createdAt") if isinstance(p.get("createdAt"), str) else "")
    return {"number": p.get("number"), "url": p.get("url"), "state": p.get("state"),
            "opened": _iso(transcript.parse_ts(p.get("createdAt"))),
            "merged": _iso(transcript.parse_ts(p.get("mergedAt")))}


# ---- cost ---------------------------------------------------------------------------

def _dispatch_role(d):
    r = metrics.role(d.agent_type)
    return "explorer" if r in metrics.EXPLORER_TYPES else r if r in ROLES else "other"


def _cost(sessions, tech_sessions):
    """`tech_sessions`: ids of the sessions that were tech lead runs; their main thread is the tech lead's."""
    by_role = dict.fromkeys(ROLES, 0.0)
    unpriced = 0
    dispatches = []
    for s in sessions:
        main = transcript.sum_usage(s.main_usage_by_model)
        by_role["tech-lead" if s.id in tech_sessions else "lead"] += main.cost
        unpriced += main.unpriced
        for d in s.dispatches:
            u = d.usage
            by_role[_dispatch_role(d)] += u.cost
            unpriced += u.unpriced
            dispatches.append(d)
    reviews = sorted((d for d in dispatches if metrics.role(d.agent_type) == "reviewer" and d.status != "no-run"),
                     key=lambda d: d.start or transcript.EPOCH)
    rework = 0.0
    after = reviews and (reviews[0].returned_at or reviews[0].start)
    if after:
        rework = sum((d.usage.cost for d in dispatches
                      if metrics.role(d.agent_type) in ("coder", "reviewer") and d.start and d.start > after), 0.0)
    return {"total": round(sum(by_role.values()), 6), "unpriced_tokens": unpriced,
            "by_role": {k: round(v, 6) for k, v in by_role.items()}, "rework": round(rework, 6)}, len(reviews)


# ---- tasks --------------------------------------------------------------------------

def _stops(events):
    """Stops in event order: a lead's QUESTION/BLOCKED/OVERTIME and a denied permission."""
    out = []
    for i, e in enumerate(events):
        key, ts = _key(e), _ts(e)
        if key is None or ts is None:
            continue
        kind, waited, why = None, None, None
        if e.get("event") == "PostToolUse" and e.get("tool_name") == "SendMessage" and e.get("kind") in STOP_KINDS:
            kind, why = e["kind"], _str(e, "summary")
            session = _str(e, "session")
            for nxt in events[i + 1:]:
                nts = _ts(nxt)
                if (session and nts and nxt.get("event") == "UserPromptSubmit" and nxt.get("session") == session
                        and nxt.get("from") in ("session", "human")):
                    waited = max(0, _seconds(nts, ts))
                    break
        elif e.get("event") == "PermissionDenied":
            kind = "DENIED"
            why = ": ".join(x for x in (_str(e, "tool_name"), _str(e, "reason")) if x) or None
        if kind:
            out.append({"ts": _iso(ts), "repo": _name(key[0]), "slug": key[1], "kind": kind,
                        "waited_s": waited, "why": why, "_key": key})
    return sorted(out, key=lambda s: s["ts"])


def _defects(retro):
    counts = dict.fromkeys(DEFECT_KINDS, 0)
    for d in retro.get("defects") if isinstance(retro.get("defects"), list) else []:
        if isinstance(d, dict):
            c = d.get("caught_by")
            counts[c if c in DEFECT_KINDS else "other"] += 1
    return counts


def _first_ts(records):
    stamped = [(t, r) for r in records for t in [_ts(r)] if t]
    return min(stamped, key=lambda x: x[0]) if stamped else None


def _role_facts(sessions, prices, tech_sessions):
    """What each role that ran in the task did, from the anatomy's blocks over kaizen's own role buckets."""
    from . import anatomy  # anatomy reads this module's logs: import it where it is used
    runs = [("tech-lead" if s.id in tech_sessions else "lead", s.run) for s in sessions if s.run] + [
        (_dispatch_role(d), d.run) for s in sessions for d in s.dispatches if d.run]
    out = {}
    for role, r in anatomy.by_role(runs, prices).items():
        b = r.block()
        out[role] = {"calls": b["calls"], "peak": b["context"]["peak"], "tools": sum(b["tools"].values()),
                     "dispatches": b["dispatches"], "cost": b["cost"], "written": b["written"]}
    return out


def _authoring(roles):
    """Who wrote the product code and tests of a task: lines and calls per role, and the lead's share of the lines."""
    part = lambda key: {r: sum(roles[r]["written"][k][key] for k in authoring.CODE) if r in roles else 0 for r in ROLES}  # noqa: E731
    lines = part("lines")
    total = sum(lines.values())
    return {"lines": lines, "calls": part("calls"), "lead_share": round(lines["lead"] / total, 6) if total else None}


def _pooled(tasks):
    """The authoring of some tasks together: each role's part of the summed lines, not a median (most tasks have a lead share of zero)."""
    lines = {r: sum(t["authoring"]["lines"][r] for t in tasks) for r in ROLES}
    wrote = [t for t in tasks if sum(t["authoring"]["lines"].values())]
    return {"tasks": len(wrote), "lead_tasks": sum(1 for t in wrote if t["authoring"]["calls"]["lead"]), "lines": lines,
            "share": {r: round(n / sum(lines.values()), 6) for r, n in lines.items()} if wrote else None}


def _handed_over(recs):
    """When the task was first declared handed over, or None."""
    return min((ts for r in recs["tasks"] if r.get("state") == "handed-over" for ts in [_ts(r)] if ts), default=None)


def _wakeups(recs):
    """Prompts to the lead, from the session or the human, after the task's first hand-over."""
    handed = _handed_over(recs)
    return sum(1 for e in recs["events"] for ts in [_ts(e)] if handed and ts and ts > handed
               and e.get("event") == "UserPromptSubmit" and e.get("role") == "lead" and e.get("from") in ("session", "human"))


def _task(key, recs, sessions, prices, tech_sessions):
    """recs: this task's records by store."""
    repo, slug = key
    started_rec = next((r for r in recs["events"] if r.get("event") == "TaskStarted" and _ts(r)), None)
    started = _ts(started_rec) if started_rec else None
    last_start = next((r for r in reversed(recs["events"]) if r.get("event") == "TaskStarted"), {})
    if started is None:
        firsts = [s.first_ts for s in sessions if s.first_ts]
        started = min(firsts) if firsts else None
    earliest = _first_ts([r for r in recs["all"] if _str(r, "version")])
    version = _str(started_rec, "version") if started_rec and _str(started_rec, "version") else (
        _str(earliest[1], "version") if earliest else None)
    states = [r["state"] for r in recs["tasks"] if _str(r, "state")]
    t = {"repo": _name(repo), "repo_path": repo, "slug": slug, "version": version, "started": started,
         "state": states[-1] if states else None, "pr": None, "landed": False,
         "mode": last_start.get("mode") if last_start.get("mode") in MODES else "single",
         "brief_to_pr_s": None, "pr_to_merge_s": None, "cost": None, "review_rounds": None, "first_pass": None,
         "_roles": _role_facts(sessions, prices, tech_sessions), "_wakeups": _wakeups(recs),
         "_size": last_start.get("size") if last_start.get("size") in SIZES else None, "_handover_s": None}
    handed = _handed_over(recs)
    if handed and started and handed >= started:
        t["_handover_s"] = _seconds(handed, started)
    t["authoring"] = _authoring(t["_roles"])
    if sessions:
        t["cost"], t["review_rounds"] = _cost(sessions, tech_sessions)
        t["first_pass"] = t["review_rounds"] == 1
    t["stops"] = len(recs["stops"])
    t["stopped_s"] = sum(s["waited_s"] for s in recs["stops"] if s["waited_s"] is not None)
    retros = sorted(recs["retros"], key=lambda r: _ts(r) or transcript.EPOCH)
    t["defects"] = _defects(retros[-1]) if retros else None
    return t


def _apply_pr(t, pr):
    t["pr"] = pr
    t["landed"] = bool(pr and pr["merged"])
    if pr:
        opened, merged = transcript.parse_ts(pr["opened"]), transcript.parse_ts(pr["merged"])
        if opened and t["started"] and opened >= t["started"]:
            t["brief_to_pr_s"] = _seconds(opened, t["started"])
        if opened and merged:
            t["pr_to_merge_s"] = _seconds(merged, opened)


# ---- summary ------------------------------------------------------------------------

def _measures(t):
    return {"cost": t["cost"]["total"] if t["cost"] else None, "brief_to_pr_s": t["brief_to_pr_s"],
            "pr_to_merge_s": t["pr_to_merge_s"], "review_rounds": t["review_rounds"]}


def _median(values):
    vals = [v for v in values if v is not None]
    m = metrics.percentile(vals, 50)
    return None if m is None else round(m, 6)


def _trend(landed):
    if len(landed) < TREND_MIN:
        return None
    k = len(landed) // 2
    latest, earlier = [_measures(t) for t in landed[-k:]], [_measures(t) for t in landed[:-k]]
    out = {"latest_n": k, "earlier_n": len(landed) - k}
    for m in ("cost", "brief_to_pr_s", "pr_to_merge_s", "review_rounds"):
        out[m] = {"latest": _median(x[m] for x in latest), "earlier": _median(x[m] for x in earlier)}
    return out


def _mix(landed):
    """Each role's share of a task's cost over time: per landed task with a cost, in start order,
    the median share over the MIX_WINDOW tasks ending at it. Medians need not sum to 1."""
    costed = [t for t in landed if t["cost"] and t["cost"]["total"] > 0]
    shares = [{r: t["cost"]["by_role"][r] / t["cost"]["total"] for r in ROLES} for t in costed]
    return {"window": MIX_WINDOW, "points": [
        {"slug": t["slug"], "version": t["version"],
         "share": {r: _median(s[r] for s in shares[i + 1 - MIX_WINDOW:i + 1]) for r in ROLES}}
        for i, t in enumerate(costed) if i + 1 >= MIX_WINDOW]}


def _anatomy(landed, versions):
    """What drives a task's cost, per role: the median over the ANATOMY_WINDOW tasks ending at each landed task with
    a cost, and per version the median over its landed tasks. A role that ran in too few tasks is null."""
    costed = [t for t in landed if t["cost"] and t["cost"]["total"] > 0]
    ran = lambda ts, r: [t["_roles"][r] for t in ts if r in t["_roles"]]  # noqa: E731
    med = lambda xs, k: _median(x[k] for x in xs)  # noqa: E731
    points = []
    for i, t in enumerate(costed):
        if i + 1 < ANATOMY_WINDOW:
            continue
        window = costed[i + 1 - ANATOMY_WINDOW:i + 1]
        roles = {}
        for r in ROLES:
            xs = ran(window, r)
            roles[r] = {k: med(xs, k) for k in ("calls", "peak", "tools")} if len(xs) >= ANATOMY_WINDOW // 2 + 1 else None
        points.append({"slug": t["slug"], "version": t["version"], "roles": roles, "authoring": _pooled(window)})
    out = []
    for v in versions:
        mine = [t for t in costed if t["version"] == v]
        roles = {}
        for r in ROLES:
            tasks = [t for t in mine if r in t["_roles"]]
            xs = [t["_roles"][r] for t in tasks]
            if not xs:
                roles[r] = None
                continue
            classes = {k: sum(x["cost"][k] for x in xs) for k in ("input", "output", "cache_read")}
            classes["cache_write"] = sum(x["cost"]["cache_write_5m"] + x["cost"]["cache_write_1h"] for x in xs)
            whole = sum(classes.values())
            roles[r] = {
                "n": len(xs),
                "share": _median(t["cost"]["by_role"][r] / t["cost"]["total"] for t in tasks),
                "cost": _median(t["cost"]["by_role"][r] for t in tasks),
                **{k: med(xs, k) for k in ("calls", "peak", "tools", "dispatches")},
                "tools_per_dispatch": _median(x["tools"] / x["dispatches"] for x in xs if x["dispatches"] > 0),
                "classes": {k: round(classes[k] / whole, 6) for k in ("input", "output", "cache_write", "cache_read")} if whole else None}
        out.append({"version": v, "tasks": len(mine), "roles": roles, "authoring": _pooled(mine)})
    return {"window": ANATOMY_WINDOW, "min": VERSION_MIN, "points": points, "versions": out}


def _rewards(landed):
    landed = sorted(landed, key=lambda t: (t["pr"]["merged"], t["slug"]))
    if not landed:
        return []
    out, last = [], landed[-1]
    streak = 0
    for t in reversed(landed):
        if t["first_pass"] is not True:
            break
        streak += 1
    if streak >= 3:
        out.append({"kind": "first_pass_streak", "slug": last["slug"], "text": "%d landed first-pass in a row" % streak})
    for kind, measure, text in (
            ("record_low_cost", "cost", lambda v: "record low cost: $%.2f" % v),
            ("record_fast", "brief_to_pr_s", lambda v: "fastest brief to pull request: " + fmt_duration(v * 1000))):
        vals = [_measures(t)[measure] for t in landed]
        have = [v for v in vals if v is not None]
        if len(have) >= 5 and vals[-1] is not None and vals[-1] < min(have[:-1]):
            out.append({"kind": kind, "slug": last["slug"], "text": text(vals[-1])})
    return out


def _summary(tasks, versions):
    landed = [t for t in tasks if t["landed"]]
    ms = [_measures(t) for t in landed]
    med = lambda m: {"median": _median(x[m] for x in ms), "n": sum(1 for x in ms if x[m] is not None)}  # noqa: E731
    costed = [t["cost"] for t in landed if t["cost"]]
    total = sum(c["total"] for c in costed)
    first_pass = [t["first_pass"] for t in landed if t["first_pass"] is not None]
    defects = [t["defects"] for t in tasks if t["defects"]]
    return {
        "tasks": len(tasks), "landed": len(landed),
        "in_flight": sum(1 for t in tasks if not t["landed"] and t["state"] not in ("acknowledged", "dismissed")),
        "cost": med("cost"), "brief_to_pr_s": med("brief_to_pr_s"), "pr_to_merge_s": med("pr_to_merge_s"),
        "review_rounds": med("review_rounds"),
        "first_pass": {"yes": sum(first_pass), "of": len(first_pass)},
        "rework_share": round(sum(c["rework"] for c in costed) / total, 6) if total else None,
        "by_role": {r: round(sum(c["by_role"][r] for c in costed), 6) for r in ROLES}, "mix": _mix(landed),
        "anatomy": _anatomy(landed, versions),
        "time": {"building_s": sum(max(0, t["brief_to_pr_s"] - t["stopped_s"]) for t in landed if t["brief_to_pr_s"] is not None),
                 "stopped_s": sum(t["stopped_s"] for t in landed),
                 "merge_wait_s": sum(t["pr_to_merge_s"] for t in landed if t["pr_to_merge_s"] is not None)},
        "defects": {k: sum(d[k] for d in defects) for k in DEFECT_KINDS},
        "trend_min": TREND_MIN, "trend": _trend(landed),
    }


def _by_mode(landed):
    """One row per (mode, stated size) holding a landed task: the medians of what the tasks measured, not a pooled sum."""
    rows = []
    for size in (*SIZES, None):
        for mode in MODES:
            ts = [t for t in landed if t["mode"] == mode and t["_size"] == size]
            if not ts:
                continue
            roles = [t["_roles"]["lead"]["calls"] for t in ts if "lead" in t["_roles"]]
            defects = [t["defects"] for t in ts if t["defects"]]
            rows.append({
                "mode": mode, "size": size, "n": len(ts),
                "cost_median": _median(t["cost"]["total"] for t in ts if t["cost"]),
                "lead_share_median": _median(t["authoring"]["lead_share"] for t in ts),
                "lead_calls_median": _median(roles),
                "handover_s_median": _median(t["_handover_s"] for t in ts),
                "first_pass": sum(t["first_pass"] is True for t in ts),
                "review_rounds_median": _median(t["review_rounds"] for t in ts),
                "defects": {k: sum(d[k] for d in defects) for k in DEFECT_KINDS} if defects else None,
                "lead_wakeups": sum(t["_wakeups"] for t in ts),
                "build_runs_median": _median(t["_build_runs"] for t in ts) if mode == "team" else None})
    return rows


_SITE_MANAGER = re.compile(r"Job: (watch|digest)\. Repo: ")


def _overhead(sessions, tasks, landed, cutoff, prices):
    """What the dispatcher and the site manager spent in the period (the calls of their sessions at or after the cutoff),
    per task landed in it (`landed`). Only sessions without a task, in a repository of the document, count."""
    repos = {t["repo_path"] for t in tasks}
    spent = {"dispatcher": 0.0, "site_manager": 0.0}
    unpriced = counted = 0
    for s in sessions:
        if s.task or s.repo_path not in repos:
            continue
        who = ("dispatcher" if s.name == "dispatcher-" + _name(s.repo_path)
               else "site_manager" if _SITE_MANAGER.match(s.first_prompt) else None)
        if who is None:
            continue
        counted += 1
        for run in [s.run] + [d.run for d in s.dispatches]:
            for c in run.calls if run else ():
                if cutoff is not None and not (c.ts and c.ts >= cutoff):
                    continue
                parts = prices.parts(c.model, c.raw)
                if parts is None:
                    unpriced += c.context + c.output
                else:
                    spent[who] += sum(parts.values())
    return {**{k: round(v, 6) for k, v in spent.items()}, "sessions": counted, "unpriced_tokens": unpriced, "landed": landed,
            "per_landed": round(sum(spent.values()) / landed, 6) if landed else None}


# ---- outbox -------------------------------------------------------------------------

def _topic(text):
    return next((name for name, rx in TOPICS if rx.search(text)), "other")


def _outbox(feedback, cutoff):
    entries, changes = {}, {}
    for r in feedback:
        fid = _str(r, "id")
        if not fid:
            continue
        if isinstance(r.get("text"), str):
            entries.setdefault(fid, r)
        else:
            changes.setdefault(fid, []).append(r)
    out = []
    for fid, r in entries.items():
        ts = _ts(r)
        if cutoff is not None and not (ts and ts >= cutoff):
            continue
        status, pr = _str(r, "status") or "open", None
        latest = None
        for c in changes.get(fid, []):  # the latest ts wins, a tie goes to the later line
            if latest is None or (_ts(c) or transcript.EPOCH) >= (_ts(latest) or transcript.EPOCH):
                latest = c
        if latest:
            status, pr = _str(latest, "status") or status, _str(latest, "pr")
        text = r["text"]
        decision = text.startswith("DECISION:")
        out.append({"id": fid, "ts": _iso(ts), "repo": _name(_str(r, "repo")), "slug": _str(r, "slug"),
                    "role": _str(r, "role"), "kind": "decision" if decision else "issue", "status": status,
                    "topic": None if decision else _topic(text), "text": text, "pr": pr})
    out.reverse()
    out.sort(key=lambda e: e["ts"] or "", reverse=True)
    counts = {k: {s: sum(1 for e in out if e["kind"] == k[:-1] and e["status"] == s) for s in STATUSES}
              for k in ("issues", "decisions")}
    topics = {}
    for e in out:
        if e["kind"] == "issue" and e["status"] == "open":
            topics[e["topic"]] = topics.get(e["topic"], 0) + 1
    order = [n for n, _ in TOPICS] + ["other"]
    ranked = sorted(topics, key=lambda n: (n == "other", -topics[n], order.index(n)))
    return {**counts, "topics": [{"topic": n, "open": topics[n]} for n in ranked], "entries": out}


# ---- the site manager ---------------------------------------------------------------

def _site_manager(records, cutoff):
    """The site manager's record, folded and counted as `factory firings` does, over every repository."""
    firings, misses = {}, 0
    suppressed_first = 0
    for r in sorted(records, key=lambda r: str(r.get("ts"))):  # a carried record sits in a newer file than its ts
        if not _str(r, "repo"):
            continue  # the factory reads the log by repository: such a record counts in none
        fid = (r["repo"], r["id"]) if isinstance(r.get("id"), str) else None
        f = firings.get(fid)
        if "miss" in r or (fid and "criterion" in r):
            ts = _ts(r)
            if cutoff is not None and not (ts and ts >= cutoff):
                continue
            if "miss" in r:
                misses += 1
                suppressed_first += bool(r.get("firing"))
            else:
                firings[fid] = {"decision": None, "to": None, "outcome": None}
        elif f and "decision" in r and f["decision"] is None:
            f["decision"], f["to"] = r["decision"], r.get("to")
        elif f and "outcome" in r:
            f["outcome"] = r["outcome"]
    escalated = [f for f in firings.values() if f["decision"] == "escalated" and f["to"] != "lead"]  # a lead's message is not the dispatcher's
    acted = sum(f["outcome"] == "acted" for f in escalated)
    ignored = sum(f["outcome"] == "ignored" for f in escalated)
    ratio = lambda n, d: round(n / d, 6) if d else None  # noqa: E731
    return {"escalated": len(escalated), "acted": acted, "ignored": ignored,
            "no_outcome": len(escalated) - acted - ignored,
            "suppressed": sum(f["decision"] == "suppressed" for f in firings.values()), "missed": misses,
            "missed_suppressed": suppressed_first, "missed_never_fired": misses - suppressed_first,
            "precision": ratio(acted, acted + ignored), "recall": ratio(acted, acted + misses)}


# ---- the tech lead ------------------------------------------------------------------

def _tech_lead(records, cutoff, tasks):
    """What came back from the merge queue in the window (a record of a build run is not one of those), how it ended, and
    how often a landed task's lead was woken after the hand-over. A cost is the sum of the priced records, null when none is: never zero."""
    mine = [r for r in records if r.get("kind") in (None, "merge") and (cutoff is None or (_ts(r) and _ts(r) >= cutoff))]

    def spent(rs):
        costs = [r["cost_usd"] for r in rs if isinstance(r.get("cost_usd"), (int, float)) and not isinstance(r["cost_usd"], bool)]
        return round(sum(costs), 6) if costs else None

    landed = [t for t in tasks if t["landed"]]
    outcome = lambda o: sum(r.get("outcome") == o for r in mine)  # noqa: E731
    return {"came_back": len(mine), "resolved": outcome("resolved"), "escalated": outcome("escalated"),
            "stopped": outcome("stopped"), "unstarted": outcome("unstarted"), "cost": spent(mine),
            "cost_resolved": spent([r for r in mine if r.get("outcome") == "resolved"]),
            "landed": len(landed), "lead_wakeups": sum(t["_wakeups"] for t in landed)}


# ---- the document -------------------------------------------------------------------

def build(factory_dir, load_sessions, prices, cutoff=None, now=None, cached=False):
    """The kaizen document. `load_sessions()` is called once, and only when the store has tasks.
    `cached`: pull requests come from the disk cache and gh is never run."""
    factory_dir = os.path.expanduser(factory_dir)
    logs = {n: read_log(factory_dir, n) for n in ("events", "retros", "feedback", "tasks")}
    logs["tasks"].sort(key=lambda r: str(r.get("ts")))  # a carried record sits in a newer file than its ts
    stops = _stops(logs["events"])
    by_task = {}
    for store in ("events", "retros", "tasks"):
        for r in logs[store]:
            if _key(r):
                by_task.setdefault(_key(r), {"all": [], "events": [], "retros": [], "tasks": [], "stops": []})[store].append(r)
    versions = {}
    for store in logs.values():
        for r in store:
            if _key(r) in by_task:
                by_task[_key(r)]["all"].append(r)
            v, t = _str(r, "version"), _ts(r)
            if v and t and (v not in versions or t < versions[v]):
                versions[v] = t
    for s in stops:
        by_task[s["_key"]]["stops"].append(s)
    sessions = load_sessions() if by_task else []
    mine = {}
    for s in sessions:
        mine.setdefault((s.repo_path, s.task), []).append(s)
    tech = read_log(factory_dir, "techlead")
    tech_sessions = {r["session"] for r in tech if _str(r, "session")}
    tasks = [_task(k, recs, mine.get(k, []), prices, tech_sessions) for k, recs in by_task.items()]
    builds = Counter(_key(r) for r in tech if r.get("kind") == "build")
    for t in tasks:
        t["_build_runs"] = builds[(t["repo_path"], t["slug"])]
    if cutoff is not None:
        tasks = [t for t in tasks if t["started"] and t["started"] >= cutoff]
    tasks.sort(key=lambda t: (t["started"] is not None, t["started"] or transcript.EPOCH, t["slug"], t["repo_path"]))
    def load_prs(path):
        try:
            return gh_prs(path, factory_dir, cached)
        except GhError as e:
            return [], None, str(e)

    paths = sorted({t["repo_path"] for t in tasks})
    with ThreadPoolExecutor(max(len(paths), 1)) as pool:
        loaded = list(pool.map(load_prs, paths))
    repos = []
    for path, (prs, as_of, error) in zip(paths, loaded):
        for t in tasks:
            if t["repo_path"] == path:
                _apply_pr(t, _pr_for(prs, t["slug"]))
        data = as_of is not None
        repos.append({"repo": _name(path), "repo_path": path, "prs": "ok" if data else error,
                      "prs_as_of": _iso(datetime.fromtimestamp(as_of, timezone.utc)) if data else None,
                      "prs_error": None if cached or not error else error})
    kept = {(t["repo_path"], t["slug"]) for t in tasks}
    slowed = [{"ts": _iso(_ts(r)), "repo": _name(r["repo"]), "slug": r["slug"], "text": x}
              for k in sorted(kept) for r in by_task[k]["retros"] for x in (r.get("slowed") if isinstance(r.get("slowed"), list) else [])
              if isinstance(x, str)]
    slowed.sort(key=lambda s: s["ts"] or "", reverse=True)
    ordered = [v for v, _ in sorted(versions.items(), key=lambda x: (x[1], x[0]))]
    summary = _summary(tasks, ordered)
    summary["overhead"] = _overhead(sessions, tasks, summary["landed"], cutoff, prices)
    summary["by_mode"] = _by_mode([t for t in tasks if t["landed"]])
    tech_lead = _tech_lead(tech, cutoff, tasks)
    for t in tasks:
        t["started"] = _iso(t["started"])
        for private in [k for k in t if k.startswith("_")]:
            del t[private]
    return {
        "generated": _iso(now or datetime.now(timezone.utc)), "factory_dir": factory_dir,
        "store_found": os.path.isdir(factory_dir), "prices": prices.to_dict(), "roles": list(ROLES), "repos": repos,
        "versions": [{"version": v, "first_seen": _iso(versions[v])} for v in ordered],
        "tasks": tasks, "summary": summary,
        "stops": [{k: v for k, v in s.items() if k != "_key"} for s in stops if s["_key"] in kept],
        "slowed": slowed, "rewards": _rewards([t for t in tasks if t["landed"]]),
        "outbox": _outbox(logs["feedback"], cutoff),
        "site_manager": _site_manager(read_log(factory_dir, "firings"), cutoff),
        "tech_lead": tech_lead,
    }
