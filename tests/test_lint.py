"""tools/lint.py as a command, in a temporary git repository holding small planted files."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LINT = Path(__file__).resolve().parent.parent / "tools" / "lint.py"
ALLOWANCE = "tools/lint_allowance.txt"
SLEEP = "time" + ".sleep(1)"  # joined so that this file holds no sleep of its own


class LintTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)

    def put(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def lint(self):
        p = subprocess.run([sys.executable, str(LINT)], cwd=self.root, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.splitlines()

    def test_a_clean_tree_passes_without_output(self):
        self.put("factory/tests/test_a.py", "x = 1\n")
        self.assertEqual(self.lint(), (0, []))

    def test_a_python_sleep_in_a_test_is_a_finding(self):
        self.put("tests/test_a.py", "import time\n\n" + SLEEP + "\n")
        self.assertEqual(self.lint(), (1, ["tests/test_a.py:3: sleep-py: wait for the event, not the clock"]))

    def test_a_python_sleep_outside_the_test_directories_is_not(self):
        self.put("isshoni/a.py", "import time\n\n" + SLEEP + "\n")
        self.assertEqual(self.lint(), (0, []))

    def test_a_go_sleep_in_a_test_is_a_finding(self):
        self.put("internal/a_test.go", "package a\n\nfunc f() {\n\ttime.Sleep(1)\n}\n")
        self.assertEqual(self.lint(), (1, ["internal/a_test.go:4: sleep-go: wait for the event, not the clock"]))

    def test_exec_command_in_go_is_a_finding(self):
        self.put("internal/a.go", "package a\n\nvar c = exec.Command(\"x\")\n")
        self.assertEqual(self.lint(), (1, ["internal/a.go:3: timeout-go: use exec.CommandContext with a deadline"]))

    def test_exec_command_context_is_not(self):
        self.put("internal/a.go", "package a\n\nvar c = exec.CommandContext(ctx, \"x\")\n")
        self.assertEqual(self.lint(), (0, []))

    def test_a_subprocess_call_without_timeout_is_a_finding(self):
        self.put("isshoni/a.py", "import subprocess\n\nsubprocess.run(x)\n")
        self.assertEqual(self.lint(), (1, ["isshoni/a.py:3: timeout-py: pass timeout="]))

    def test_calls_with_timeout_or_double_star_are_not(self):
        self.put("isshoni/a.py", "import subprocess\n\nsubprocess.run(x, timeout=5)\nsubprocess.run(x, **kw)\n")
        self.assertEqual(self.lint(), (0, []))

    def test_a_call_over_several_lines_is_reported_at_its_first_line(self):
        self.put("isshoni/a.py", "import subprocess\n\nsubprocess.check_output(\n    x,\n    check=True,\n)\n")
        self.assertEqual(self.lint(), (1, ["isshoni/a.py:3: timeout-py: pass timeout="]))

    def test_a_script_without_extension_is_read(self):
        self.put("bin/isshoni", "import subprocess\nsubprocess.call(x)\n")
        self.assertEqual(self.lint(), (1, ["bin/isshoni:2: timeout-py: pass timeout="]))

    def test_a_file_that_does_not_parse_is_a_finding(self):
        self.put("isshoni/a.py", "def (\n")
        code, lines = self.lint()
        self.assertEqual((code, len(lines), lines[0].startswith("isshoni/a.py:1: cannot parse: ")), (1, 1, True))

    def test_fixtures_are_not_read(self):
        self.put("tests/fixtures/a.py", "import time\n" + SLEEP + "\n")
        self.assertEqual(self.lint(), (0, []))

    def test_an_allowance_equal_to_the_count_passes(self):
        self.put("tests/test_a.py", "import time\n" + SLEEP + "\n")
        self.put(ALLOWANCE, "# why\nsleep-py tests/test_a.py 1  # trailing\n")
        self.assertEqual(self.lint(), (0, []))

    def test_a_count_below_its_allowance_says_to_lower_it(self):
        self.put("tests/test_a.py", "import time\n" + SLEEP + "\n")
        self.put(ALLOWANCE, "sleep-py tests/test_a.py 2\n")
        self.assertEqual(self.lint(), (1, [f"tests/test_a.py: sleep-py: 1 left, allowance 2: lower it in {ALLOWANCE}"]))

    def test_a_count_above_its_allowance_lists_every_finding(self):
        self.put("tests/test_a.py", "import time\n" + SLEEP + "\n" + SLEEP + "\n")
        self.put(ALLOWANCE, "sleep-py tests/test_a.py 1\n")
        self.assertEqual(self.lint()[1], ["tests/test_a.py:2: sleep-py: wait for the event, not the clock",
                                          "tests/test_a.py:3: sleep-py: wait for the event, not the clock"])

    def test_an_allowance_for_a_file_with_no_finding_is_stale(self):
        self.put("tests/test_a.py", "x = 1\n")
        self.put(ALLOWANCE, "sleep-py tests/test_a.py 1\n")
        self.assertEqual(self.lint(), (1, [f"tests/test_a.py: sleep-py: 0 left, allowance 1: lower it in {ALLOWANCE}"]))

    def test_an_allowance_for_a_missing_file_is_stale(self):
        self.put(ALLOWANCE, "timeout-go internal/gone.go 1\n")
        self.assertEqual(self.lint(), (1, [f"internal/gone.go: timeout-go: 0 left, allowance 1: lower it in {ALLOWANCE}"]))

    def test_a_malformed_allowance_line_fails(self):
        self.put(ALLOWANCE, "sleep-py tests/test_a.py\n")
        code, lines = self.lint()
        self.assertEqual((code, len(lines), lines[0].startswith(f"{ALLOWANCE}:1: ")), (1, 1, True))


if __name__ == "__main__":
    unittest.main()
