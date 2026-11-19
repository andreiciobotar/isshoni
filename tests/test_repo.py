"""Specs are working papers kept in ~/.claude/factory/specs/: nothing under docs/specs/ is tracked."""
import re
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
    PREFIX = "- Keep git in this worktree"
    DIRS = ("factory/agents", "factory/roles", ".claude/agents")
    FACTORY_MD = "docs/factory.md"

    def role_files(self):
        return [f for d in self.DIRS for f in sorted((ROOT / d).glob("*.md"))]

    def doc_text(self):
        return (ROOT / self.FACTORY_MD).read_text()

    def doc_bullet(self):
        m = re.search(r"^The line, as the eight files carry it[^\n]*\n\n```\n(.*?)\n```", self.doc_text(), re.S | re.M)
        self.assertTrue(m, "docs/factory.md has no fenced bullet under 'The line, as the eight files carry it'")
        return m.group(1)

    def probe_table(self):
        """{"T1": [seven cells]} from the table whose header starts `| # | Call | lead |`, and its heading sentence."""
        lines = self.doc_text().split("\n")
        start = next(i for i, l in enumerate(lines) if l.startswith("| # | Call | lead |"))
        heading = next(l for l in reversed(lines[:start]) if l.startswith("Measured "))
        rows = {}
        for l in lines[start + 2:]:
            if not l.startswith("| T"):
                break
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", l)[1:-1]]
            rows[cells[0]] = cells[2:]
        return heading, rows

    def claims(self):
        lines = self.doc_text().split("\n")
        start = next(i for i, l in enumerate(lines) if l.startswith("| The line says | Rows | Expect |"))
        out = []
        for l in lines[start + 2:]:
            if not l.startswith("|"):
                break
            says, rows, expect = [c.strip() for c in l.strip("|").split("|")]
            out.append((says, [r.strip() for r in rows.split(",")], expect))
        return out

    def test_eight_files_carry_the_bullet_the_docs_quote(self):
        found = {}
        for f in self.role_files():
            lines = [l for l in f.read_text().split("\n") if l.startswith(self.PREFIX)]
            if lines:
                found[f.relative_to(ROOT).as_posix()] = lines
        self.assertEqual(sorted(found), [
            ".claude/agents/coder.md", ".claude/agents/explorer.md", ".claude/agents/reviewer.md",
            "factory/agents/coder.md", "factory/agents/explorer.md", "factory/agents/lander.md",
            "factory/agents/reviewer.md", "factory/roles/lead.md"])
        self.assertEqual({len(v) for v in found.values()}, {1})
        for path, (line,) in found.items():
            self.assertEqual(line, self.doc_bullet(), path)

    def test_every_claim_of_the_bullet_is_in_it_and_holds_in_the_probe_table(self):
        bullet = self.doc_bullet()
        self.assertNotIn("<<", bullet)
        heading, table = self.probe_table()
        self.assertRegex(heading, r"20\d\d-\d\d-\d\d")
        self.assertRegex(heading, r"Claude Code \d+\.\d+\.\d+")
        claims = self.claims()
        self.assertTrue(claims)
        for says, rows, expect in claims:
            self.assertIn(says, bullet)
            for row in rows:
                self.assertIn(row, table, says)
                cells = table[row]
                if expect == "accepted":
                    self.assertTrue(all(c in ("accepted", "no Write tool") for c in cells), (says, row, cells))
                else:
                    self.assertEqual(expect, "refused")
                    self.assertTrue(any(c.startswith("refused") for c in cells), (says, row, cells))

    def test_no_role_file_offers_a_heredoc(self):
        for f in self.role_files():
            text = f.read_text()
            self.assertNotIn("<<'EOF'", text, f.name)
            for l in text.split("\n"):
                if re.search(r"heredoc", l, re.I):
                    self.assertTrue(l.startswith(self.PREFIX), (f.name, l[:80]))

    def test_the_reviewer_copies_with_factory_scratch_and_the_lander_reads_the_exit_code_from_the_result(self):
        reviewer = (ROOT / "factory/agents/reviewer.md").read_text()
        self.assertIn("factory scratch", reviewer)
        self.assertNotIn("git archive -o", reviewer)
        self.assertRegex(reviewer, r"(?m)^tools: .*\bWrite\b")
        self.assertIn("Exit code N", (ROOT / "factory/agents/lander.md").read_text())


class ReviewWordsAreInOnePlace(unittest.TestCase):
    SKILLS = ("factory/skills/coding-taste/SKILL.md", ".claude/skills/coding-taste/SKILL.md")

    def test_the_two_coding_taste_copies_are_the_same_bytes(self):
        a, b = [(ROOT / p).read_bytes() for p in self.SKILLS]
        self.assertEqual(a, b)

    def test_only_the_skill_and_the_dispatch_skill_state_the_verdict_words(self):
        words = re.compile(r"\bAPPROVE\b|CHANGES_NEEDED")
        found = sorted(
            f.relative_to(ROOT).as_posix()
            for d in ("factory/roles", "factory/agents", "factory/skills", ".claude/agents", ".claude/skills")
            for f in (ROOT / d).rglob("*") if f.is_file() and words.search(f.read_text()))
        self.assertEqual(found, [".claude/skills/coding-taste/SKILL.md", "factory/skills/coding-taste/SKILL.md", "factory/skills/dispatch/SKILL.md"])
        self.assertNotRegex((ROOT / "CLAUDE.md").read_text(), words)


if __name__ == "__main__":
    unittest.main()
