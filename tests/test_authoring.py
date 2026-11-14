import unittest

from tests.support import ROOT  # noqa: F401  (puts the repository on sys.path)
from isshoni import authoring

WT = "/r/alpha/.claude/worktrees/t1/"


class KindTests(unittest.TestCase):
    def check(self, want, *paths):
        for p in paths:
            self.assertEqual(authoring.kind(p), want, p)

    def test_scratch_and_spec_outside_a_worktree(self):
        self.check("scratch", "/r/alpha/x.retro.json", "/r/alpha/tmp/a.py", "/home/u/.claude/jobs/1/tmp/q.py", "/home/u/scratchpad/n.md",
                   "/private/var/x/y", "/var/folders/ab/T/z.txt", "/x/T/z", "/tmp/a", "/home/u/.claude/plans/p.md")
        self.check("spec", "/home/u/.claude/factory/specs/ccmon/x.md", "/r/alpha/x.pr.md")

    def test_rules_inside(self):
        self.check("rules", WT + "CLAUDE.md", WT + "a/AGENTS.md", WT + ".claude/agents/coder.md", WT + "x/skills/s/SKILL.md",
                   WT + "factory/roles/lead.md", WT + ".claude/commands/c.md", WT + "skills/a/test_x.py")

    def test_tests_inside(self):
        self.check("tests", WT + "a_test.go", WT + "tests/x.py", WT + "internal/tui/testdata/k.json", WT + "x/test/y.sh",
                   WT + "tests/fixtures/g/a.jsonl", WT + "a/b.golden", WT + "isshoni/test_a.py", WT + "docs/fixtures/a.md")

    def test_docs_inside(self):
        self.check("docs", WT + "README.md", WT + "docs/a.md", WT + "notes.txt")

    def test_product_inside(self):
        self.check("product", *(WT + "a/b" + e for e in (".go", ".py", ".sh", ".toml", ".json", ".yaml", ".yml", ".mod", ".sum",
                                                       ".js", ".ts", ".css", ".html")),
                   WT + "Makefile", WT + "docker/Dockerfile", WT + ".gitignore", WT + "a/.env", WT + "bin/factory", WT + "isshoni/not_test.py")

    def test_other(self):
        self.check("other", "", "/home/u/.claude/projects/x/memory/m.md", "/r/alpha/isshoni/a.py", "/home/u/notes.md",
                   "/r/alpha/CLAUDE.md", "/r/alpha/.claude/hooks-made/t1/a.py", WT)

    def test_the_worktree_is_cut_at_its_name(self):
        self.check("product", "/x/.claude/worktrees/a1/tmp-not/x.py", "/x/.claude/worktrees/a1/b/c.py")
        self.check("tests", "/x/.claude/worktrees/tmp/tests/c.py")  # a worktree named like scratch is still inside
        self.check("product", "/x/.claude/worktrees/a1/T")  # the file is the one word `T`: no extension
        self.check("product", "/x/.claude/worktrees/a1/b/c.xyz", WT + "web/app.tsx", WT + "db/001.sql")  # no list of extensions

    def test_a_path_in_a_worktree_is_never_scratch_or_spec(self):
        self.check("product", WT + "tmpx/a.py")
        self.check("tests", WT + "tmp/tests/a.py")
        self.check("docs", WT + "factory/specs/a.md")
        self.check("docs", WT + "a.pr.md")


if __name__ == "__main__":
    unittest.main()
