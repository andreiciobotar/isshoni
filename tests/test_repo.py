"""Specs are working papers kept in ~/.claude/factory/specs/: nothing under docs/specs/ is tracked."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NoSpecsTracked(unittest.TestCase):
    def test_nothing_under_docs_specs_is_tracked(self):
        try:
            p = subprocess.run(["git", "ls-files", "--", "docs/specs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        except OSError:
            self.skipTest("git is not installed")
        if p.returncode:
            self.skipTest("not a git checkout")
        self.assertEqual(p.stdout, "", "move these to ~/.claude/factory/specs/<repo directory name>/ and git rm them")


class GitRuleIsOneLine(unittest.TestCase):
    def test_eight_files_carry_the_same_keep_git_in_this_worktree_bullet(self):
        prefix = "- Keep git in this worktree"
        found = {}
        for d in ("factory/agents", "factory/roles", ".claude/agents"):
            for f in sorted((ROOT / d).glob("*.md")):
                lines = [l for l in f.read_text().split("\n") if l.startswith(prefix)]
                if lines:
                    found[f.relative_to(ROOT).as_posix()] = lines
        self.assertEqual(sorted(found), [
            ".claude/agents/coder.md", ".claude/agents/explorer.md", ".claude/agents/reviewer.md",
            "factory/agents/coder.md", "factory/agents/explorer.md", "factory/agents/lander.md",
            "factory/agents/reviewer.md", "factory/roles/lead.md"])
        self.assertEqual(len({tuple(v) for v in found.values()}), 1, "one bullet per file, the same in all")
        self.assertEqual({len(v) for v in found.values()}, {1})


if __name__ == "__main__":
    unittest.main()
