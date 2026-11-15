"""Command line interface: argparse, --since parsing, table and JSON rendering."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone

from . import host, metrics, prices as price_file, sessioncache, transcript
from .transcript import SessionError

DEFAULT_PROJECTS = "~/.claude/projects"


# ---- formatting ---------------------------------------------------------------

def human(n):
    if n is None:
        return "-"
    n = float(n)
    for lim, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= lim:
            return "%.1f%s" % (n / lim, suf)
    return "%d" % round(n)


def money(x, currency, unpriced=0):
    """`$` for USD, else the code and a space; a trailing `*` when some tokens had no price."""
    prefix = "$" if currency == "USD" else currency + " "
    if x <= 0:
        s = "0"
    elif x < 0.00005:
        prefix, s = "<" + prefix, "0.0001"
    elif x < 0.00995:
        s = "%.4f" % x
    elif round(x, 2) < 100:
        s = "%.2f" % x
    elif round(x) < 1e4:
        s = "%.0f" % x
    elif round(x / 1e3, 1) < 1e3:
        s = "%.1fk" % (x / 1e3)
    else:
        s = "%.1fM" % (x / 1e6)
    return prefix + s + ("*" if unpriced else "")


def fmt_duration(ms):
    if ms is None:
        return "-"
    s = int(round(ms / 1000.0))
    if s < 60:
        return "%ds" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh%02dm" % (h, m)


def fmt_dt(d, with_date=True):
    if not d:
        return "-"
    d = d.astimezone(timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M:%S")


def fmt_rate(r):
    return "-" if r is None else "%d%%" % round(r * 100)


def table(headers, rows, left=()):
    """Plain aligned text; columns named in `left` are left-aligned, others right."""
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def line(cells):
        out = []
        for i, c in enumerate(cells):
            out.append(c.ljust(widths[i]) if headers[i] in left else c.rjust(widths[i]))
        return "  ".join(out).rstrip()

    return "\n".join([line(headers)] + [line(r) for r in rows])


# ---- --since --------------------------------------------------------------------

def parse_since(text, now=None):
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([mdhw])", text.strip())
    if m:
        n, u = int(m.group(1)), m.group(2)
        return now - timedelta(**{{"m": "minutes", "d": "days", "h": "hours", "w": "weeks"}[u]: n})
    try:
        d = datetime.fromisoformat(text.strip())
    except ValueError:
        raise SessionError("invalid --since value %r (use Nm, Nh, Nd, Nw or an ISO date)" % text)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def footer(prices, models, tokens):
    """The prices line, plus the unpriced line when some tokens had no price."""
    print("prices: %s as of %s (%s)" % (prices.currency, prices.as_of, prices.path))
    if models:
        print("* no price for: %s (%s tokens); cost excludes them" % (", ".join(models), human(tokens)))


FROM_SINCE = object()  # load_all's default cutoff: whatever `--since` says


def load_all(args, prices, cutoff=FROM_SINCE):
    """The sessions of the projects directory; `cutoff` drops those that ended before it (None: none dropped)."""
    paths = transcript.discover_sessions(args.projects_dir)
    cache = sessioncache.SessionCache(args.projects_dir, prices)
    cache.prune(paths)
    if cutoff is FROM_SINCE:
        cutoff = parse_since(args.since) if getattr(args, "since", None) else None
    sessions = []
    for p in paths:
        if cutoff is not None:
            try:
                if os.path.getmtime(p) < cutoff.timestamp():
                    continue
            except OSError:
                continue
        s = cache.load(p)
        if cutoff is not None and s.last_ts is not None and s.last_ts < cutoff:
            continue
        if getattr(args, "repo", None) not in (None, s.repo) or getattr(args, "task", None) not in (None, s.task):
            continue
        metrics.analyze(s, args.agents_dir, args.repeat_threshold)
        sessions.append(s)
    sessions.sort(key=lambda s: s.first_ts or transcript.EPOCH)
    return sessions


# ---- commands -----------------------------------------------------------------

def cmd_sessions(args):
    prices = price_file.load(args.prices)
    sessions = load_all(args, prices)
    sums = [metrics.session_summary(s) for s in sessions]
    repos = metrics.group_repos(sessions)
    malformed = sum(x["malformed"] for x in sums)
    if args.json:
        emit_json({"sessions": sums, "repos": repos, "malformed": malformed, "prices": prices.to_dict()})
        return 0
    headers = ["repo / task / session", "start (UTC)", "cost", "main%", "sub%", "tokens", "disp", "repeat",
               "loops", "recheck", "warn", "malformed"]
    by_id = {x["id"]: x for x in sums}

    def row(label, x):
        t, c = x["tokens"], x["cost"]
        return [label, x["start"] and fmt_dt(datetime.fromisoformat(x["start"])) or "-",
                money(c["total"], prices.currency, x["cost"]["unpriced_tokens"]),
                "%.0f" % c["main_pct"], "%.0f" % c["subagent_pct"], human(t["total"]),
                x["dispatch_count"], x["repeats"], x["review_loops"],
                fmt_rate(x["recheck"]["rate"]), x["warn_count"], x["malformed"]]

    rows = []
    for r in repos:
        rows.append(row(r["repo"], r))
        in_task = {i for t in r["tasks"] for i in t["sessions"]}
        rows.extend(row("  " + i[:8], by_id[i]) for i in r["sessions"] if i not in in_task)
        for t in r["tasks"]:
            rows.append(row("  " + t["task"], t))
            rows.extend(row("    " + i[:8], by_id[i]) for i in t["sessions"])
    print(table(headers, rows, left=("repo / task / session", "start (UTC)")))
    print("\n%d repositories, %d tasks, %d sessions, %d malformed lines" % (
        len(repos), sum(len(r["tasks"]) for r in repos), len(sums), malformed))
    footer(prices, sorted({m for x in sums for m in x["cost"]["unpriced_models"]}), sum(x["cost"]["unpriced_tokens"] for x in sums))
    return 0


def usage_rows(mapping, currency):
    """One row per key; an entry with only unpriced tokens shows `unpriced` as its cost."""
    return [[k, human(v["input"]), human(v["output"]), human(v["cache_write"]), human(v["cache_read"]),
             human(v["total"]),
             "unpriced" if v["unpriced"] and not v["cost"] else money(v["cost"], currency, v["unpriced"])]
            for k, v in mapping.items()]


def cmd_session(args):
    prices = price_file.load(args.prices)
    path = transcript.find_session(args.projects_dir, args.id)
    s = sessioncache.SessionCache(args.projects_dir, prices).load(path)
    metrics.analyze(s, args.agents_dir, args.repeat_threshold)
    x = metrics.session_summary(s, detail=True)
    x["prices"] = prices.to_dict()
    if args.json:
        emit_json(x)
        return 0
    t, c, cur = x["tokens"], x["cost"], prices.currency
    main_un = x["by_agent"]["main"]["unpriced"]
    print("session   %s" % x["id"])
    others = [w for w in x["worktrees"] if w != x["task"]]
    print("repo      %s (%s)" % (x["repo"], x["repo_path"] or x["project_dir"]))
    print("task      %s%s" % (x["task"] or "-", " (also entered: %s)" % ", ".join(others) if others else ""))
    print("span      %s .. %s UTC" % (fmt_dt(s.first_ts), fmt_dt(s.last_ts)))
    print("cost      total %s, main %s (%.1f%%), subagents %s (%.1f%%)" % (
        money(c["total"], cur, x["cost"]["unpriced_tokens"]), money(c["main"], cur, main_un), c["main_pct"],
        money(c["subagents"], cur, x["cost"]["unpriced_tokens"] - main_un), c["subagent_pct"]))
    print("tokens    total %s, main %s (%.1f%%), subagents %s (%.1f%%)" % (
        human(t["total"]), human(t["main"]), t["main_pct"], human(t["subagents"]), t["subagent_pct"]))
    rr = x["recheck"]
    print("signals   dispatches %d, repeats %d, review loops %d, re-check rate %s (%d/%d), malformed lines %d" % (
        x["dispatch_count"], x["repeats"], x["review_loops"], fmt_rate(rr["rate"]),
        rr["rechecked"], rr["explorer_with_citations"], x["malformed"]))
    uh = ["input", "output", "cache-w", "cache-r", "total", "cost"]
    print("\nBy model")
    print(table(["model"] + uh, usage_rows(x["by_model"], cur), left=("model",)))
    print("\nBy agent")
    print(table(["agent"] + uh, usage_rows(x["by_agent"], cur), left=("agent",)))
    if x["warnings"]:
        print("\nWarnings")
        for w in x["warnings"]:
            print("  WARN %s" % w)
    print("\nDispatches")
    if not s.dispatches:
        print("  (none)")
        footer(prices, x["cost"]["unpriced_models"], x["cost"]["unpriced_tokens"])
        return 0
    rows = []
    for d in s.dispatches:
        u = d.usage
        flags = list(d.flags)
        if d.status != "completed":
            flags.append(d.status)
        desc = (d.description.strip().splitlines() or [""])[0]
        rows.append([fmt_dt(d.start, False), ("  " * (d.depth - 1) if d.depth > 1 else "") + d.agent_type,
                     ",".join(m.replace("claude-", "") for m in d.models) or "-",
                     human(u.input), human(u.output), human(u.total), money(u.cost, cur, u.unpriced),
                     fmt_duration(d.duration_ms), ",".join(flags) or "-", desc[:60]])
    print(table(["time", "agent", "model", "in", "out", "total", "cost", "duration", "flags", "description"],
                rows, left=("time", "agent", "model", "flags", "description")))
    footer(prices, x["cost"]["unpriced_models"], x["cost"]["unpriced_tokens"])
    return 0


def cmd_agents(args):
    prices = price_file.load(args.prices)
    sessions = load_all(args, prices)
    agg = metrics.aggregate_agents(sessions)
    agg["prices"] = prices.to_dict()
    if args.json:
        emit_json(agg)
        return 0
    cur = prices.currency
    rows = []
    for a in agg["agents"]:
        u = a["usage"]
        rows.append([a["agent"], a["dispatches"], ",".join(m.replace("claude-", "") for m in a["models"]) or "-",
                     money(u["cost"], cur, u["unpriced"]),
                     *("-" if a[k] is None else money(a[k], cur, u["unpriced"]) for k in ("cost_p50", "cost_p90")),
                     human(u["input"]), human(u["output"]), human(u["cache_write"]), human(u["cache_read"]),
                     human(a["tokens_p50"]), human(a["tokens_p90"]),
                     fmt_duration(a["duration_ms_p50"]), fmt_duration(a["duration_ms_p90"]),
                     a["repeats"], a["warn_count"]])
    print(table(["agent", "disp", "models", "cost", "cost p50", "cost p90", "input", "output", "cache-w", "cache-r",
                 "tok p50", "tok p90", "dur p50", "dur p90", "repeat", "warn"], rows,
                left=("agent", "models")))
    print("\n%d sessions, %d malformed lines" % (agg["sessions"], agg["malformed"]))
    footer(prices, agg["unpriced_models"], sum(a["usage"]["unpriced"] for a in agg["agents"]))
    return 0


def _pulse_lines(pulse, cur):
    """The pulse's heading and four lead figures, one line each: today and the baseline with the pull requests behind each, and the verdict."""
    today, base = pulse["today"], pulse["baseline"]
    first_pass = lambda d: tuple(sum(f[k] for f in d["first_pass"].values()) for k in ("yes", "of"))  # noqa: E731
    median = lambda d: d["handover"]["approval_to_merge"]  # noqa: E731
    figures = (
        ("all-in cost per merged PR", "cost", lambda d: (d["cost"]["per_merged"] is not None and money(d["cost"]["per_merged"], cur, d["cost"]["unpriced_tokens"]), d["merged"])),
        ("first pass", "first_pass", lambda d: (first_pass(d)[1] and "%d%%" % round(100 * first_pass(d)[0] / first_pass(d)[1]), first_pass(d)[1])),
        ("approval to merge median", "approval_to_merge", lambda d: (median(d)["p50_s"] is not None and fmt_duration(median(d)["p50_s"] * 1000), median(d)["n"])),
        ("conflicted base merges per merged PR", "conflicts", lambda d: (d["conflicts"]["per_merged"] is not None and "%.2f" % d["conflicts"]["per_merged"], d["conflicts"]["prs"])))
    against = "yesterday (%s)" % base["date"] if base["days"] == 1 else "the %d days before (%s to %s)" % (base["days"], base["date"], base["to"])
    return ["pulse: today %s against %s" % (today["date"], against)] + [
        "%s: %s: %s" % (label, ", ".join("%s %s (n=%d)" % (name, shown or "-", n) for name, d in (("today", today), ("baseline", base)) for shown, n in [f(d)]),
                        pulse["verdicts"][key] or "-")
        for label, key, f in figures]


def _friction_lines(f, cur, subagents_only):
    """The friction figures, one line each; `-` is unknown."""
    m = lambda v, unpriced=0: "-" if v is None else money(v, cur, unpriced)  # noqa: E731
    pct = lambda v, digits=0: "-" if v is None else "%.*f%%" % (digits, 100 * v)  # noqa: E731
    d, r, c, w, q = f["dispatcher"], f["rework"], f["causes"], f["wakeups"], f["queue"]
    stated = sum(v for k, v in c.items() if k != "unstated")
    refused = ", ".join("%s %s (%d/%d, next %d calls %s)" % (x["role"], pct(x["share"], 1), x["guard"] + x["classifier"], x["bash"],
                                                           f["refusals"]["after_calls"], m(x["after_cost"])) for x in f["refusals"]["roles"])
    held, late = f["held"], f["late_conflicts"]
    plural = lambda n, noun: "%d %s%s" % (n, noun, "" if n == 1 else "s")  # noqa: E731
    return [
        "dispatcher: %s per call (n=%d calls), median context %s" % (m(d["per_call"], d["unpriced_tokens"]), d["calls"], human(d["context_median"])),
        "cost after the first review: %s of task cost (%s of %s, median %s per task, n=%d; subagents only %s)" % (
            pct(r["share"]), m(r["cost"]), m(r["task_cost"]), m(r["per_task_median"]), r["n"], pct(subagents_only)),
        "defect causes: %s (n=%d stated, %d unstated)" % (", ".join("%s %d" % (k, v) for k, v in c.items() if k != "unstated"), stated, c["unstated"]),
        "refused bash calls: %s" % (refused or "none recorded"),
        "lead prompts after hand-over: %d in %d tasks, %s after them, %s per prompt" % (w["prompts"], w["tasks"], m(w["cost"]), m(w["per_prompt"])),
        "queue attempts per merged PR: " + ("no queue log yet" if q is None else "%s (n=%d: updates %d, re-runs %d, send-backs %d, stops %d), queue log since %s" % (
            "-" if q["per_merged"] is None else "%.2f" % q["per_merged"], q["merged"], q["updated"], q["rerun"], q["sent_back"], q["stopped"], q["since"])),
        "queue send-backs and pauses: " + ("send-backs %d, pauses %d, paused %d min, in %s" % (
            *(sum(h[k] for h in held) for k in ("sent_back", "paused", "paused_minutes")), plural(len(held), "task")) if held else "none recorded"),
        "late conflicts: %s, %d of %d PRs in git history" % (
            "no queue log yet" if late["log_tasks"] is None else "%s in the queue log" % plural(late["log_tasks"], "task"), late["git_tasks"], late["git_prs"])]


def print_modes(rows, cur):
    """The landed tasks from before the team (single) and since (team) by stated size (summary.by_mode) as plain text."""
    if not rows:
        print("no landed task carries a mode yet")
        return 0
    landed = lambda mode: sum(r["n"] for r in rows if r["mode"] == mode)  # noqa: E731
    print("landed tasks: single %d, team %d" % (landed("single"), landed("team")))
    cell = lambda v, fmt="%g", scale=1: "-" if v is None else fmt % (v * scale)  # noqa: E731
    out = []
    for r in rows:
        d = r["defects"]
        defects = None if d is None else ", ".join("%s %d" % kv for kv in d.items() if kv[1]) or "none"
        out.append([r["size"] or "none", r["mode"], r["n"], cell(r["cost_median"], "%.2f"),
                    cell(r["lead_share_median"], "%.0f%%", 100), cell(r["lead_calls_median"]),
                    cell(r["handover_s_median"], "%.0f", 1 / 60), "%d/%d" % (r["first_pass"], r["n"]),
                    cell(r["review_rounds_median"]), cell(defects, "%s"), r["lead_wakeups"], cell(r["build_runs_median"])])
    print(table(["size", "mode", "n", "cost " + cur, "lead share", "lead calls", "hand-over min", "first pass", "rounds",
                 "defects", "wakeups", "builds"], out, left=("size", "mode", "defects")))
    return 0


def cmd_kaizen(args):
    from . import kaizen  # kaizen imports this module's formatters

    prices = price_file.load(args.prices)
    cutoff = parse_since(args.since) if args.since else None
    doc = kaizen.build(args.factory_dir, lambda since: load_all(args, prices, since), prices, cutoff, cached=args.cached)
    if args.json:
        emit_json(doc)
        return 0
    s, cur = doc["summary"], prices.currency
    if args.modes:
        return print_modes(s["by_mode"], cur)
    if not doc["tasks"]:
        print("no factory tasks in %s" % doc["factory_dir"])
        return 0
    med = lambda m, f: "-" if s[m]["median"] is None else f(s[m]["median"])  # noqa: E731
    print("tasks %d, landed %d, in flight %d" % (s["tasks"], s["landed"], s["in_flight"]))
    print("median over landed: cost %s, brief to PR %s, PR to merge %s, review rounds %s; first pass %d/%d" % (
        med("cost", lambda v: money(v, cur)), med("brief_to_pr_s", lambda v: fmt_duration(v * 1000)),
        med("pr_to_merge_s", lambda v: fmt_duration(v * 1000)), med("review_rounds", lambda v: "%g" % v),
        s["first_pass"]["yes"], s["first_pass"]["of"]))
    for line in (*_pulse_lines(doc["pulse"], cur), *_friction_lines(doc["friction"], cur, s["rework_share"])):
        print(line)
    for r in doc["repos"]:
        if r["prs"] != "ok":
            print("pull requests unavailable for %s: %s" % (r["repo"], r["prs"]))
        elif r["prs_error"]:
            age = datetime.now(timezone.utc) - datetime.strptime(r["prs_as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            print("pull requests for %s as of %s ago: %s" % (r["repo"], fmt_duration(age.total_seconds() * 1000), r["prs_error"]))
    dur = lambda v: "-" if v is None else fmt_duration(v * 1000)  # noqa: E731
    rows = [[t["slug"], t["version"] or "-", "landed" if t["landed"] else t["state"] or "-", dur(t["brief_to_pr_s"]),
             dur(t["pr_to_merge_s"]), "-" if t["cost"] is None else money(t["cost"]["total"], cur, t["cost"]["unpriced_tokens"]),
             "-" if t["review_rounds"] is None else t["review_rounds"], t["stops"]] for t in doc["tasks"]]
    print()
    print(table(["task", "version", "state", "build", "merge", "cost", "rounds", "stops"], rows, left=("task", "version", "state")))
    return 0


def cmd_anatomy(args):
    from . import anatomy  # anatomy imports kaizen, which imports this module

    prices = price_file.load(args.prices)
    doc = anatomy.build(load_all(args, prices), prices, args.factory_dir)
    if args.json:
        emit_json(doc)
        return 0
    if not doc["tasks"]:
        print("no tasks")
        return 0
    share = lambda c, *ks: "-" if not c["total"] else "%d%%" % round(100 * sum(c[k] for k in ks) / c["total"])  # noqa: E731
    rows = []
    for t in doc["tasks"]:
        for name, r in t["roles"].items():
            c, x = r["cost"], r["context"]
            written = " ".join("%s %d/%d" % (k, w["calls"], w["lines"]) for k, w in r["written"].items() if w["calls"])
            rows.append([t["task"], t["version"] or "-", name, r["runs"], r["calls"], human(x["first"]), human(x["median"]),
                         human(x["max"]), money(c["total"], prices.currency), share(c, "input"), share(c, "output"),
                         share(c, "cache_write_5m", "cache_write_1h"), share(c, "cache_read"), sum(r["tools"].values()),
                         r["dispatches"], written or "-"])
    print(table(["task", "version", "role", "runs", "calls", "ctx first", "ctx median", "ctx max", "cost", "in%", "out%",
                 "cw%", "cr%", "tools", "disp", "written (calls/lines)"], rows, left=("task", "version", "role", "written (calls/lines)")))
    print("%d written calls matched no kind (--json lists them as unmatched)" % sum(u["calls"] for u in doc["unmatched"]))
    unpriced = sum(t["unpriced_tokens"] for t in doc["tasks"])
    models = {m for t in doc["tasks"] for r in t["roles"].values() for m in r["models"] if prices.parts(m, {}) is None}
    footer(prices, sorted(models) if unpriced else [], unpriced)
    return 0


def cmd_events(args):
    from . import events  # events imports kaizen, which imports this module

    cutoff = parse_since(args.since or ("2m" if args.follow else "24h"))
    feed = events.Feed(args.projects_dir, args.factory_dir, cutoff, None if args.no_gh else events.gh_prs, args.follow)
    emit = lambda batch: events.write(sys.stdout, batch)  # noqa: E731
    stop = threading.Event()
    try:
        if args.follow:
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            events.follow(feed, emit, stop)
        else:
            emit(feed.history())
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # the exit flush must not fail too
    return 0


def emit_json(obj):
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


# ---- entry point ------------------------------------------------------------------

def _common(suppress):
    p = argparse.ArgumentParser(add_help=False)
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--projects-dir", default=d or DEFAULT_PROJECTS, help="transcripts root (default %s)" % DEFAULT_PROJECTS)
    p.add_argument("--agents-dir", default=d, help="agent definitions dir (overrides cwd/.claude/agents and ~/.claude/agents)")
    p.add_argument("--repeat-threshold", type=float, default=d or 0.5, help="Jaccard threshold for repeat flag (default 0.5)")
    p.add_argument("--prices", default=d, help="price file (default isshoni/prices.toml)")
    p.add_argument("--json", action="store_true", default=d or False, help="emit one JSON document")
    return p


def build_parser():
    p = argparse.ArgumentParser(prog="isshoni", description="Report how a Claude Code main session delegates to subagents.",
                                parents=[_common(False)])
    sub = p.add_subparsers(dest="command", required=True)
    since = argparse.ArgumentParser(add_help=False)
    since.add_argument("--since", help="Nm, Nh, Nd, Nw or an ISO date")
    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument("--repo", help="only this repository (name)")
    scope.add_argument("--task", help="only this task (worktree name)")
    s = sub.add_parser("sessions", parents=[_common(True), since, scope], help="sessions grouped by repository and task")
    s.set_defaults(func=cmd_sessions)
    s = sub.add_parser("session", parents=[_common(True)], help="one session in detail")
    s.add_argument("id", help="session id or unique prefix")
    s.set_defaults(func=cmd_session)
    s = sub.add_parser("agents", parents=[_common(True), since, scope], help="per agent type aggregates")
    s.set_defaults(func=cmd_agents)
    s = sub.add_parser("kaizen", parents=[_common(True), since], help="improvement measures of the factory")
    s.add_argument("--factory-dir", default="~/.claude/factory", help="the factory's stores (default ~/.claude/factory)")
    s.add_argument("--cached", action="store_true", help="pull requests from the disk cache only; never run gh")
    s.add_argument("--modes", action="store_true", help="landed tasks from before the team (single) and since (team) by stated size, as a table")
    s.set_defaults(func=cmd_kaizen)
    s = sub.add_parser("anatomy", parents=[_common(True), since, scope], help="cost anatomy of each task per role")
    s.add_argument("--factory-dir", default="~/.claude/factory", help="the factory's stores (default ~/.claude/factory)")
    s.set_defaults(func=cmd_anatomy)
    s = sub.add_parser("events", parents=[_common(True), since], help="the factory's events as JSON lines, for the visualiser")
    s.add_argument("--follow", action="store_true", help="after the history, keep printing what happens (default history 2m)")
    s.add_argument("--factory-dir", default="~/.claude/factory", help="the factory's stores (default ~/.claude/factory)")
    s.add_argument("--no-gh", action="store_true", help="do not ask gh for pull requests")
    s.set_defaults(func=cmd_events)
    host.register(sub)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SessionError as e:
        print("isshoni: %s" % e, file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
