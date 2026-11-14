import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from tests.support import AGENTS, ROOT  # noqa: F401  (puts the repository on sys.path)
from isshoni import anatomy, cli, metrics, prices as price_file, transcript

REPO = "/r/alpha"
RATES = {"input": 1, "output": 2, "cache_write_5m": 3, "cache_write_1h": 4, "cache_read": 5}
PRICES = price_file.Prices("p.toml", {"currency": "USD", "as_of": "2026-01-01", "source": "test",
                                      "models": {"claude-fable-5-2": RATES, "claude-haiku-5-5": RATES}})


def usage(i=0, o=0, w5=0, w1=0, r=0, think=0):
    return {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": w5 + w1, "cache_read_input_tokens": r,
            "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": w5},
            "output_tokens_details": {"thinking_tokens": think}}


def call(mid, u, content=None, model="claude-fable-5-2"):
    return {"type": "assistant", "timestamp": "2026-11-10T10:00:00.000Z", "message": {
        "id": mid, "model": model, "usage": u, "content": content or [{"type": "text", "text": "x"}]}}


def tool(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid, content):
    return {"type": "user", "timestamp": "2026-11-10T10:00:00.000Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def text(s):
    return {"type": "user", "timestamp": "2026-11-10T10:00:00.000Z", "message": {"content": s}}


def attachment(kind, *contents):
    return {"type": "attachment", "attachment": {"type": kind}, "rendered": [{"content": c} for c in contents]}


class Anatomy(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.factory = os.path.join(tmp.name, "factory")
        self.n = 0

    def write(self, path, lines):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))

    def session(self, task, lines, subs=(), repo=REPO):
        """A transcript in worktree `task`; subs are (agent type, lines) of its subagent runs."""
        self.n += 1
        wt = "%s/.claude/worktrees/%s" % (repo, task) if task else repo
        sid = "s%d" % self.n
        path = os.path.join(self.tmp, "projects", "p%d" % self.n, sid + ".jsonl")
        head = [{"type": "worktree-state", "worktreeSession": {"worktreePath": wt, "worktreeName": task, "originalCwd": wt}}] if task else []
        disp = [tool("t%d-%d" % (self.n, i), "Agent", subagent_type=a, description="d", prompt="p") for i, (a, _) in enumerate(subs)]
        if disp:
            lines = lines or [call("d%d" % self.n, usage())]
            lines[0]["message"]["content"] = lines[0]["message"]["content"] + disp
        self.write(path, head + lines)
        for i, (a, sub) in enumerate(subs):
            base = os.path.join(self.tmp, "projects", "p%d" % self.n, sid, "subagents", "agent-a%d" % i)
            self.write(base + ".jsonl", sub)
            self.write(base + ".meta.json", [{"agentType": a, "toolUseId": "t%d-%d" % (self.n, i), "spawnDepth": 1}])
        s = transcript.load_session(path, PRICES)
        metrics.analyze(s, AGENTS, 0.5)
        return s

    def build(self, *sessions):
        return anatomy.build(list(sessions), PRICES, self.factory, now=datetime(2026, 11, 14, 12, tzinfo=timezone.utc))

    def role(self, doc, role="lead", i=0):
        return doc["tasks"][i]["roles"][role]

    def test_costs_sum_to_the_monitors(self):
        a = self.session("t1", [call("m1", usage(i=10, o=20, w1=30, r=40)), call("m2", usage(i=1, w5=2, r=3))],
                         subs=[("factory:coder", [call("s1", usage(o=7, w5=9), model="claude-haiku-5-5"),
                                                  call("s2", usage(o=1), model="claude-unpriced")])])
        b = self.session("t1", [call("m3", usage(i=5, o=5))])
        doc = self.build(a, b)
        want = metrics.group_repos([a, b])[0]["tasks"][0]["cost"]
        self.assertEqual(len(doc["tasks"]), 1)
        t = doc["tasks"][0]
        self.assertAlmostEqual(t["cost"], want["total"], places=6)
        self.assertAlmostEqual(t["cost"], sum(r["cost"]["total"] for r in t["roles"].values()), places=6)
        self.assertEqual(t["unpriced_tokens"], want["unpriced_tokens"])
        self.assertEqual(sorted(t["roles"]), ["coder", "lead"])

    def test_tokens_tiers_and_thinking(self):
        doc = self.build(self.session("t1", [call("m1", usage(i=1, o=100, w5=20, w1=30, r=400, think=60))]))
        r = self.role(doc)
        self.assertEqual(r["tokens"], {"input": 1, "output": 100, "thinking": 60, "cache_write_5m": 20,
                                       "cache_write_1h": 30, "cache_read": 400})
        self.assertEqual(r["cost"], {"total": round((1 + 200 + 60 + 120 + 2000) / 1e6, 6), "input": 1e-6, "output": 200e-6,
                                     "cache_write_5m": 60e-6, "cache_write_1h": 120e-6, "cache_read": 2000e-6})
        self.assertEqual(r["models"], ["claude-fable-5-2"])

    def test_a_message_on_several_lines_counts_once(self):
        c = call("m1", usage(o=5), [tool("x1", "Read", file_path="/a")])
        doc = self.build(self.session("t1", [c, dict(c), call("m2", usage(o=5), [tool("x1", "Read", file_path="/a")])]))
        r = self.role(doc)
        self.assertEqual((r["calls"], r["tokens"]["output"], r["tools"]), (2, 10, {"Read": 1}))

    def test_context_over_two_runs(self):
        lead = [call("l1", usage(i=100)), call("l2", usage(i=300)), call("l3", usage(i=200, r=100))]
        subs = [("coder", [call("a1", usage(i=10)), call("a2", usage(i=50))]),
                ("coder", [call("b1", usage(i=30)), call("b2", usage(i=70)), call("b3", usage(i=80))])]
        doc = self.build(self.session("t1", lead, subs=subs))
        self.assertEqual(self.role(doc)["context"], {"first": 100, "median": 300, "peak": 300, "max": 300})
        c = self.role(doc, "coder")
        self.assertEqual((c["runs"], c["calls"]), (2, 5))
        self.assertEqual(c["context"], {"first": 20, "median": 50, "peak": 65, "max": 80})

    def test_a_role_without_calls_has_null_context(self):
        doc = self.build(self.session("t1", [], subs=[("coder", [])]))
        r = self.role(doc, "coder")
        self.assertEqual((r["runs"], r["calls"], r["context"]), (1, 0, {"first": None, "median": None, "peak": None, "max": None}))

    def test_tool_kinds_and_dispatches(self):
        lines = [call("m1", usage(), [tool("x1", "Bash", command="cd /r/x && git status"), tool("x2", "Bash", command="/usr/bin/make test"),
                                      tool("x3", "Bash", command="  "), tool("x4", "Bash", command="cd /r/x && ls | wc"), tool("y1", "Bash", command="abcd x && ls"),
                                      tool("x5", "Read", file_path="/a"), tool("x6", "Agent", subagent_type="coder")]),
                 call("m2", usage(), [tool("x5", "Read", file_path="/a"), tool("x7", "SendMessage", to="a0"),
                                      tool("x8", "Bash", command="git log"), tool("x9", "Agent", subagent_type="coder")])]
        doc = self.build(self.session("t1", lines))
        r = self.role(doc)
        self.assertEqual(r["tools"], {"Bash:git": 2, "Bash": 1, "Bash:ls": 1, "Bash:abcd": 1, "Bash:make": 1, "Read": 1, "SendMessage": 1})
        self.assertEqual(r["dispatches"], 2)

    def fill(self, *lines):
        return self.role(self.build(self.session("t1", list(lines))))["fill"]

    def test_fill_one_source_is_exact(self):
        f = self.fill(call("m1", usage(i=100, o=30), [tool("x1", "Read", file_path="/a")]), result("x1", "abc"),
                      call("m2", usage(i=300)))
        self.assertEqual(f, {"base": 100, "own_output": 30, "inbound": 170, "compactions": 0, "inbound_estimate": {"result:Read": 170}})

    def test_fill_two_sources_split_by_characters(self):
        f = self.fill(call("m1", usage(i=100), [tool("x1", "Bash", command="ls")]), result("x1", [{"type": "text", "text": "a" * 30}]),
                      text("b" * 10), call("m2", usage(i=200)))
        self.assertEqual(f["inbound_estimate"], {"result:Bash": 75, "message": 25})
        self.assertEqual(f["inbound"], 100)

    def test_fill_with_nothing_recorded_is_unaccounted(self):
        f = self.fill(call("m1", usage(i=100, o=10)), call("m2", usage(i=150)))
        self.assertEqual((f["own_output"], f["inbound"], f["inbound_estimate"]), (10, 40, {"unaccounted": 40}))

    def test_fill_own_output_is_capped_at_the_growth(self):
        f = self.fill(call("m1", usage(i=100, o=90)), text("hi"), call("m2", usage(i=130)))
        self.assertEqual((f["own_output"], f["inbound"], f["inbound_estimate"]), (30, 0, {"message": 0}))

    def test_fill_survives_a_shrinking_context(self):
        f = self.fill(call("m1", usage(i=500), [tool("x1", "Read", file_path="/a")]), result("x1", "x" * 9), text("x"),
                      call("m2", usage(i=100)), text("yy"), call("m3", usage(i=160, o=0)))
        self.assertEqual((f["base"], f["compactions"], f["own_output"], f["inbound"], f["inbound_estimate"]),
                         (500, 1, 0, 60, {"message": 60}))

    def test_what_arrives_before_the_first_call_is_base(self):
        f = self.fill(text("x" * 50), call("m1", usage(i=100)), call("m2", usage(i=120)))
        self.assertEqual((f["base"], f["inbound_estimate"]), (100, {"unaccounted": 20}))

    def test_sources(self):
        note = "<task-notification><tool-use-id>t9</tool-use-id></task-notification>"
        f = self.fill(call("m1", usage(i=100), [tool("x1", "Agent", subagent_type="c"), tool("x2", "Glob")]),
                      result("x1", "r" * 10), result("x2", "g" * 10), result("nope", "q" * 10),
                      text("  " + note), {"type": "user", "message": {"content": [{"type": "text", "text": note}, {"type": "text", "text": "hey"}]}},
                      {"type": "attachment", "attachment": {"type": "queued_command", "prompt": note}},
                      attachment("deferred_tools_delta", "d" * 10, "e" * 5),
                      {"type": "attachment", "attachment": {"type": "other"}},
                      call("m2", usage(i=500)))
        e, n = f["inbound_estimate"], len(note)
        chars = {"handback": 10 + 3 * n + 2, "message": 3, "attachment:deferred_tools_delta": 15, "result:Glob": 10, "result:?": 10}
        self.assertEqual(e, {k: round(400 * v / sum(chars.values())) for k, v in chars.items()})

    def test_a_forks_copy_of_its_parents_message_is_not_its_own(self):
        spawn = call("m1", usage(i=10), [tool("t1", "Agent", subagent_type="fork"), tool("r1", "Read", file_path="/a")])
        doc = self.build(self.session("t1", [spawn], subs=[("fork", [dict(spawn), call("f1", usage(i=20))])]))
        self.assertEqual((self.role(doc)["dispatches"], self.role(doc)["tools"]), (2, {"Read": 1}))
        fork = self.role(doc, "fork")
        self.assertEqual((fork["calls"], fork["dispatches"], fork["tools"]), (1, 0, {}))

    def test_a_plugin_agent_is_its_name(self):
        doc = self.build(self.session("t1", [], subs=[("factory:lander", [call("a1", usage(i=1))]), ("Explore", [call("b1", usage(i=1))])]))
        self.assertEqual(sorted(doc["tasks"][0]["roles"]), ["Explore", "lander", "lead"])

    def test_sessions_without_a_task_are_left_out_and_nothing_is_empty(self):
        for doc in (self.build(self.session(None, [call("m1", usage(i=1))])), self.build()):
            self.assertEqual((doc["tasks"], doc["versions"]), ([], []))

    def store(self, name, *recs):
        self.write(os.path.join(self.factory, name + ".jsonl"), recs)

    def test_version_comes_from_the_earliest_record_and_rolls_up(self):
        self.store("events", {"ts": "2026-11-10T09:00:00Z", "repo": REPO, "slug": "t1", "version": "0.3.0"},
                   {"ts": "2026-11-10T08:00:00Z", "repo": REPO, "slug": "t1", "version": "0.2.0"},
                   {"ts": "2026-11-10T07:00:00Z", "repo": "/r/other", "slug": "t1", "version": "0.1.0"},
                   {"ts": "2026-11-10T06:00:00Z", "repo": REPO, "slug": "t1"})
        self.store("tasks", {"ts": "2026-11-11T08:00:00Z", "repo": REPO, "slug": "t2", "version": "0.2.0"})
        s = [self.session("t1", [call("m1", usage(o=10))]), self.session("t2", [call("m2", usage(o=20))]),
             self.session("t3", [call("m3", usage(o=40))])]
        doc = self.build(*s)
        self.assertEqual([t["version"] for t in doc["tasks"]], ["0.2.0", "0.2.0", None])
        v = doc["versions"]
        self.assertEqual([(x["version"], x["tasks"], x["roles"]["lead"]["runs"], x["roles"]["lead"]["tokens"]["output"]) for x in v],
                         [("0.2.0", 2, 2, 30), (None, 1, 1, 40)])
        self.assertAlmostEqual(v[0]["cost"], sum(t["cost"] for t in doc["tasks"][:2]), places=6)

    def test_tasks_are_sorted_by_start_then_name(self):
        def run(ts):
            return [dict(call("m" + ts, usage(i=1)), timestamp="2026-11-%sT10:00:00.000Z" % ts)]
        doc = self.build(self.session("b", run("12")), self.session("a", run("12")), self.session("c", run("10")))
        self.assertEqual([t["task"] for t in doc["tasks"]], ["c", "a", "b"])

    def test_a_null_start_sorts_first(self):
        s = self.session("b", [dict(call("m1", usage(i=1)), timestamp="2026-11-10T10:00:00.000Z")])
        n = self.session("a", [{"type": "assistant", "message": {"id": "m2", "model": "claude-fable-5-2", "usage": usage(i=1), "content": []}}])
        self.assertEqual([t["task"] for t in self.build(s, n)["tasks"]], ["a", "b"])

    def test_versions_sort_by_number(self):
        self.store("tasks", *({"ts": "2026-11-10T09:00:00Z", "repo": REPO, "slug": t, "version": v}
                              for t, v in (("a", "0.10.0"), ("b", "0.9.0"), ("c", "0.2.0"))))
        doc = self.build(*(self.session(t, [call("m" + t, usage(i=1))]) for t in "abcd"))
        self.assertEqual([v["version"] for v in doc["versions"]], ["0.2.0", "0.9.0", "0.10.0", None])

    def cli(self, *argv):
        self.write(os.path.join(self.tmp, "projects", "p0", "x.jsonl"), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["anatomy", "--projects-dir", os.path.join(self.tmp, "projects"), "--factory-dir", self.factory,
                           "--prices", self.prices_path, *argv])
        return rc, out.getvalue()

    def test_cli_text_and_json(self):
        self.prices_path = os.path.join(self.tmp, "prices.toml")
        with open(self.prices_path, "w") as f:
            f.write('currency = "USD"\nas_of = "2026-01-01"\nsource = "test"\n[models.claude-fable-5-2]\n' +
                    "".join("%s = %s\n" % kv for kv in RATES.items()))
        rc, out = self.cli()
        self.assertEqual((rc, out.splitlines()[0]), (0, "no tasks"))
        self.session("t1", [call("m1", usage(i=100, o=50, w1=100, r=750), [tool("x1", "Read", file_path="/a")]), result("x1", "abc"),
                            call("m2", usage(i=2000))])
        rc, out = self.cli()
        lines = out.splitlines()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0].split(), ["task", "version", "role", "runs", "calls", "ctx", "first", "ctx", "median", "ctx", "max",
                                            "cost", "in%", "out%", "cw%", "cr%", "tools", "disp"])
        row = lines[1].split()
        self.assertEqual(row[:8] + row[9:], ["t1", "-", "lead", "1", "2", "950", "1.5k", "2.0k", "33%", "2%", "6%", "59%", "1", "0"])
        self.assertTrue(lines[-1].startswith("prices: USD as of 2026-01-01"))
        rc, js = self.cli("--json")
        doc = json.loads(js)
        self.assertEqual((rc, [t["task"] for t in doc["tasks"]], doc["tasks"][0]["roles"]["lead"]["calls"]), (0, ["t1"], 2))


if __name__ == "__main__":
    unittest.main()
