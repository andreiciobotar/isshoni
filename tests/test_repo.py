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
        (bullet,) = found["factory/agents/lander.md"]
        self.assertIn("`$?`", bullet)
        self.assertIn("<<'EOF'", bullet)

    def test_the_reviewer_copies_with_factory_scratch_and_the_lander_reads_the_exit_code_from_the_result(self):
        reviewer = (ROOT / "factory/agents/reviewer.md").read_text()
        self.assertIn("factory scratch", reviewer)
        self.assertNotIn("git archive -o", reviewer)
        self.assertIn("Exit code N", (ROOT / "factory/agents/lander.md").read_text())


class CredentialRuleIsOneSentence(unittest.TestCase):
    SENTENCE = ("Never print a credential (a token, a key, a password) into the transcript, where it stays on disk: the guard hook refuses "
                "the commands it recognises (`gh auth token`, `factory env`, a read of `identity/`, an `echo` of `$GH_TOKEN`), and the rule "
                "holds for the ones it does not. To learn which identity acts, run `gh auth status` (never with `--show-token`) or read the "
                "`acting on GitHub as` line of `factory status`.")

    def test_eight_files_hold_it_once_as_their_own_line_after_the_git_rule(self):
        for f in ("factory/agents/coder.md", "factory/agents/explorer.md", "factory/agents/lander.md", "factory/agents/reviewer.md",
                  "factory/roles/lead.md", ".claude/agents/coder.md", ".claude/agents/explorer.md", ".claude/agents/reviewer.md"):
            with self.subTest(f=f):
                text = (ROOT / f).read_text()
                self.assertEqual(text.count(self.SENTENCE), 1)
                lines = text.split("\n")
                at = next(i for i, l in enumerate(lines) if "This session is isolated" in l)
                self.assertEqual(lines[at + 1], "- " + self.SENTENCE)


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
