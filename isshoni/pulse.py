"""The pulse: four figures for today, for the baseline (the whole days before it) and for each day (docs/isshoni.md, "Kaizen measures").
Its readers (the queue log, the merges and approvals caches) are plain functions; kaizen.build calls `build`."""
from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone

from . import kaizen, metrics, transcript
from .kaizen import SIZES, GhError, _iso, _seconds, _ts

GIT_TIMEOUT = 10
GH_WORKERS = 8
_MERGE_SUBJECT = re.compile(r"Merge pull request #(\d+) ")
PULSE_MIN_N = 5  # a verdict needs this many pull requests on each side
_FAILED = object()  # a lookup that failed: not cached, not in the split


class GitError(Exception):
    """git could not answer; the message is one line."""


# ---- readers ------------------------------------------------------------------------

def read_queue(factory_dir):
    """The `queue` log's records that carry a time, oldest first."""
    return sorted((r for r in kaizen.read_log(factory_dir, "queue") if _ts(r)), key=_ts)


def _read_map(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def read_merges(factory_dir, repo_path):
    """`{merge commit sha: {"pr": int, "base_merges": [{"sha", "conflicted", "files"}]}}` of a repository."""
    return _read_map(kaizen.cache_path(factory_dir, "merges", repo_path))


def read_approvals(factory_dir, repo_path):
    """`{"<pr number>": approval time (ISO str) or None}` of a repository: None means looked up, none found."""
    return _read_map(kaizen.cache_path(factory_dir, "approvals", repo_path))


def _write(factory_dir, kind, repo_path, cache):
    try:
        kaizen._write_cache(kaizen.cache_path(factory_dir, kind, repo_path), cache, prefix=".kaizen-%s-" % kind)
    except OSError:
        pass  # the figures stand; the next load computes them again


# ---- git ----------------------------------------------------------------------------

def _git(repo_path, *args, ok=(0,)):
    """(exit code, stdout) of `git -C repo_path args`; GitError when git cannot run or exits with a code not in `ok`."""
    try:
        p = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError("git unavailable: %s" % (str(e).splitlines() or ["failed"])[0]) from e
    if p.returncode not in ok:
        raise GitError((p.stderr.strip().splitlines() or ["git exited %d" % p.returncode])[0])
    return p.returncode, p.stdout


def _merge_commits(repo_path):
    """{pull request number: merge commit} for the `Merge pull request #N ` commits on the first-parent line of origin's HEAD."""
    out = _git(repo_path, "log", "--no-color", "--first-parent", "--merges", "--format=%H%x01%s", "refs/remotes/origin/HEAD")[1]
    found = {}
    for line in out.splitlines():
        sha, _, subject = line.partition("\x01")
        m = _MERGE_SUBJECT.match(subject)
        if m:
            found.setdefault(int(m.group(1)), sha)
    return found


def _remerge(repo_path, sha):
    """(conflicted, files) of a base merge: a `remerge CONFLICT` line in its remerge diff, and the files of the sections holding one."""
    out = _git(repo_path, "show", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--remerge-diff", "--format=", sha)[1]
    files, current = [], None
    for line in out.splitlines():
        if line.startswith("diff --git "):
            current = line.rsplit(" b/", 1)[-1]
        elif line.startswith("remerge CONFLICT") and current and current not in files:
            files.append(current)
    return any(line.startswith("remerge CONFLICT") for line in out.splitlines()), files


def _base_merges(repo_path, merge):
    """The merges of the base branch into a pull request's branch: commits on its first-parent line with two parents,
    the second an ancestor of the base (M^1). Other merges (a worktree merged back) are not base merges."""
    out = _git(repo_path, "rev-list", "--first-parent", "--parents", merge + "^2", "^" + merge + "^1")[1]
    found = []
    for line in out.splitlines():
        sha, *parents = line.split()
        if len(parents) == 2 and _git(repo_path, "merge-base", "--is-ancestor", parents[1], merge + "^1", ok=(0, 1))[0] == 0:
            conflicted, files = _remerge(repo_path, sha)
            found.append({"sha": sha, "conflicted": conflicted, "files": files})
    return found


def _merges(factory_dir, wanted, cached):
    """({(repo, pr): cache entry}, {(repo, pr): why git could not tell}) for `wanted` {repo: [pr numbers]}.
    A number missing from the repository's cache is computed with git unless `cached`; the cache is written once."""
    found, errors = {}, {}
    for repo, numbers in wanted.items():
        cache = read_merges(factory_dir, repo)
        by_pr = {e["pr"]: e for e in cache.values() if isinstance(e, dict) and isinstance(e.get("pr"), int) and isinstance(e.get("base_merges"), list)}
        missing = [n for n in numbers if n not in by_pr]
        if missing and not cached:
            try:
                merges = _merge_commits(repo)
            except GitError as e:
                errors.update({(repo, n): str(e) for n in missing})
                merges = {}
            added = False
            for n in missing:
                if n in merges:
                    try:
                        cache[merges[n]] = by_pr[n] = {"pr": n, "base_merges": _base_merges(repo, merges[n])}
                        added = True
                    except GitError as e:
                        errors[(repo, n)] = str(e)
            if added:
                _write(factory_dir, "merges", repo, cache)
        found.update({(repo, n): by_pr[n] for n in numbers if n in by_pr})
    return found, errors


# ---- approvals ----------------------------------------------------------------------

def backfill(repo_path, factory_dir, number, merged_at):
    """When pull request `number` was approved at or before `merged_at`, as an ISO str, or None: the newest APPROVED review,
    else the newest `lgtm` comment of a user who is not a bot. GhError when gh cannot answer. Does not check the commenter's permission."""
    def newest(rows, field, keep):
        stamps = [t for r in rows if isinstance(r, dict) and keep(r) for t in [transcript.parse_ts(r.get(field))] if t and t <= merged_at]
        return max(stamps, default=None)

    base = "repos/{owner}/{repo}/"
    found = newest(kaizen._gh_get(repo_path, factory_dir, base + "pulls/%d/reviews?per_page=100" % number),
                   "submitted_at", lambda r: r.get("state") == "APPROVED")
    if found is None:
        found = newest(kaizen._gh_get(repo_path, factory_dir, base + "issues/%d/comments?per_page=100" % number), "created_at",
                       lambda r: isinstance(r.get("body"), str) and r["body"].strip().lower() == "lgtm"
                       and isinstance(r.get("user"), dict) and r["user"].get("type") != "Bot")
    return _iso(found)


def _approvals(factory_dir, wanted, cached):
    """{(repo, pr): approval time (ISO str) or None} for `wanted` [(repo, pr, merged_at)], from the repositories' caches;
    a pull request not in them is looked up through gh (8 at a time) unless `cached`, and the caches are written once.
    A failed lookup is not in the result."""
    known = {repo: read_approvals(factory_dir, repo) for repo, _, _ in wanted}
    missing = [w for w in wanted if str(w[1]) not in known[w[0]]]
    if missing and not cached:
        def look(w):
            try:
                return backfill(w[0], factory_dir, w[1], w[2])
            except GhError:
                return _FAILED  # kaizen._gh_get has logged it

        with ThreadPoolExecutor(GH_WORKERS) as pool:
            for (repo, n, _), found in zip(missing, pool.map(look, missing)):
                if found is not _FAILED:
                    known[repo][str(n)] = found
        for repo in {w[0] for w in missing}:
            _write(factory_dir, "approvals", repo, known[repo])
    return {(repo, n): known[repo][str(n)] for repo, n, _ in wanted if str(n) in known[repo]}


# ---- the figures --------------------------------------------------------------------

def _durations(seconds):
    p50, p90 = (metrics.percentile(seconds, p) for p in (50, 90))
    return {"p50_s": None if p50 is None else int(round(p50)), "p90_s": None if p90 is None else int(round(p90)), "n": len(seconds)}


def _zone(now):
    """(today, tz) of `now`: its own zone when it has one, else None, the machine's zone at each moment (so a clock change is honoured)."""
    if now is not None and now.tzinfo:
        return now.date(), now.tzinfo
    return datetime.now().date(), None


def _midnight(d, tz):
    """The start of day `d` in `tz` (None: the machine's zone at that date), in UTC."""
    local = datetime.combine(d, time.min, tz) if tz else datetime.combine(d, time.min).astimezone()
    return local.astimezone(timezone.utc)


def baseline_start(cutoff, now, merges):
    """When the baseline's first day starts (UTC): the local date of `cutoff`, or without one of the earliest of the pull request
    merge times `merges`, but no later than yesterday."""
    today, tz = _zone(now)
    first = cutoff.astimezone(tz).date() if cutoff else min((m.astimezone(tz).date() for m in merges), default=today)
    return _midnight(min(first, today - timedelta(days=1)), tz)


def sessions_from(cutoff, baseline, started):
    """The time kaizen's transcripts are needed from: None (all) without a cutoff; else the earliest of the cutoff, the start of
    the baseline and the TaskStarted times `started` of the tasks merged since (a session of a task cannot predate its start);
    None too when one of those has no time."""
    if cutoff is None or None in started:
        return None
    return min([cutoff, baseline, *started])


def _spent_by_day(sessions, tasks, prices, dates, tz):
    """{date: spend} of every model call of the task sessions and of the dispatcher and site manager sessions, on `dates`."""
    keys, repos = {(t["repo_path"], t["slug"]) for t in tasks}, {t["repo_path"] for t in tasks}
    out = {d: {"tasks": 0.0, "dispatcher": 0.0, "site_manager": 0.0, "unpriced_tokens": 0} for d in dates}
    since = _midnight(dates[0], tz)
    for s in sessions:
        who = "tasks" if (s.repo_path, s.task) in keys else kaizen._spender(s, repos)
        if who is None:
            continue
        for ts, cost, tokens in kaizen._spend(s, prices, since):
            spent = out.get(ts.astimezone(tz).date())
            if spent:
                spent[who] += cost
                spent["unpriced_tokens"] += tokens
    return out


def _sent_backs(queue, day):
    """[(date, whether the next record for the pull request has the same head)] of the queue's send-backs."""
    out = []
    for i, r in enumerate(queue):
        if r.get("step") == "sent-back":
            nxt = next((x for x in queue[i + 1:] if (x.get("repo"), x.get("pr")) == (r.get("repo"), r.get("pr"))), None)
            out.append((day(_ts(r)), nxt is not None and isinstance(r.get("head"), str) and nxt.get("head") == r["head"]))
    return out


def _verdict(today, baseline, lower_is_better):
    """None when either (value, n) has no value; "few" when either n < PULSE_MIN_N; "same" within 10% of the baseline; else "better" or "worse"."""
    (t, nt), (b, nb) = today, baseline
    if t is None or b is None:
        return None
    if nt < PULSE_MIN_N or nb < PULSE_MIN_N:
        return "few"
    if abs(t - b) <= 0.10 * b:
        return "same"
    return "better" if (t < b) == lower_is_better else "worse"


def _verdicts(today, baseline):
    def lead(d):
        yes, of = (sum(f[k] for f in d["first_pass"].values()) for k in ("yes", "of"))
        return {"cost": (d["cost"]["per_merged"], d["merged"]), "first_pass": (yes / of if of else None, of),
                "approval_to_merge": (d["handover"]["approval_to_merge"]["p50_s"], d["handover"]["approval_to_merge"]["n"]),
                "conflicts": (d["conflicts"]["per_merged"], d["conflicts"]["prs"])}

    t, b = lead(today), lead(baseline)
    return {k: _verdict(t[k], b[k], k != "first_pass") for k in t}


def build(factory_dir, tasks, by_task, sessions, prices, queue, baseline, now=None, cached=False):
    """The `pulse` of the document. `tasks`: every task, with its pull request; `by_task`: its records by store;
    `queue`: `read_queue`'s records. `baseline`: when the baseline's first day starts (`baseline_start`). `cached`: the caches only, no git and no gh."""
    today, tz = _zone(now)
    day = lambda ts: ts.astimezone(tz).date()  # noqa: E731
    first_day = day(baseline)
    baseline_dates = [first_day + timedelta(days=i) for i in range((today - first_day).days)]
    dates = [*baseline_dates, today]
    merged = {d: [] for d in dates}
    for t in tasks:
        at = transcript.parse_ts(t["pr"]["merged"]) if t["pr"] else None
        if at and day(at) in merged:
            merged[day(at)].append((t, at))

    since = _ts(queue[0]) if queue else None
    queued = {(r.get("repo"), r.get("pr")): r.get("approved_at") for r in queue if r.get("step") == "merged"}
    sent_backs = _sent_backs(queue, day)

    first = {id(t): kaizen._handed_over(by_task[(t["repo_path"], t["slug"])]) for rows in merged.values() for t, _ in rows}
    wanted = [(t["repo_path"], t["pr"]["number"], at) for rows in merged.values() for t, at in rows
              if first[id(t)] and first[id(t)] <= at and (t["repo_path"], t["pr"]["number"]) not in queued]
    github = _approvals(factory_dir, wanted, cached)
    pull_requests = {}
    for rows in merged.values():
        for t, _ in rows:
            pull_requests.setdefault(t["repo_path"], []).append(t["pr"]["number"])
    merges, git_errors = _merges(factory_dir, pull_requests, cached)
    spend = _spent_by_day(sessions, tasks, prices, dates, tz)

    def approval(t):
        """(time, from GitHub) of a task's approval, or None."""
        key = (t["repo_path"], t["pr"]["number"])
        a, backfilled = (queued[key], False) if key in queued else (github.get(key), True)
        a = transcript.parse_ts(a)
        return (a, backfilled) if a else None

    def figures(days):
        """The figures of the pull requests merged on `days` (dates, oldest first) and of the spend on them."""
        rows = [row for d in days for row in merged[d]]
        spent = {k: sum(spend[d][k] for d in days) for k in spend[days[0]]}
        total = spent["tasks"] + spent["dispatcher"] + spent["site_manager"]
        first_pass = {k: {"yes": 0, "of": 0} for k in (*SIZES, "unstated")}
        for t, _ in rows:
            if t["first_pass"] is not None:
                f = first_pass[t["size"] if t["size"] in SIZES else "unstated"]
                f["of"] += 1
                f["yes"] += t["first_pass"]
        times = [(t, first[id(t)], at, approval(t)) for t, at in rows if first[id(t)] and first[id(t)] <= at]
        split = [(h, a[0], at, a[1]) for t, h, at, a in times if a and h <= a[0] <= at]
        known = [d for d in days if since is not None and day(since) <= d]  # the days the queue log covers
        covered = [same for d, same in sent_backs if d in known]
        mine = [e for t, _ in rows for e in [merges.get((t["repo_path"], t["pr"]["number"]))] if e]
        conflicted = sum(1 for e in mine for b in e["base_merges"] if isinstance(b, dict) and b.get("conflicted") is True)
        return {
            "date": days[0].isoformat(), "to": days[-1].isoformat(), "days": len(days), "merged": len(rows),
            "cost": {**{k: round(v, 6) if isinstance(v, float) else v for k, v in spent.items()},
                     "per_merged": round(total / len(rows), 6) if rows else None},
            "first_pass": first_pass,
            "handover": {
                "to_merge": _durations([_seconds(at, h) for _, h, at, _ in times]),
                "to_approval": _durations([_seconds(a, h) for h, a, _, _ in split]),
                "approval_to_merge": _durations([_seconds(at, a) for _, a, at, _ in split]),
                "approvals_backfilled": sum(1 for *_, backfilled in split if backfilled),
                "sent_back": len(covered) if known else None, "sent_back_unchanged": sum(covered) if known else None},
            "conflicts": {
                "prs": len(mine), "base_merges": sum(len(e["base_merges"]) for e in mine), "conflicted": conflicted,
                "per_merged": round(conflicted / len(mine), 6) if mine else None,
                "error": next((git_errors[k] for t, _ in rows for k in [(t["repo_path"], t["pr"]["number"])] if k in git_errors), None)}}

    today_figures, baseline_figures = figures([today]), figures(baseline_dates)
    return {"queue_since": _iso(since), "today": today_figures, "baseline": baseline_figures,
            "days": [figures([d]) for d in dates], "verdicts": _verdicts(today_figures, baseline_figures)}
