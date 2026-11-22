"""The repository's own checks: python3 tools/lint.py, from the repository root. docs/lint.md says what they hold.

Each rule counts its findings per file and compares the count with tools/lint_allowance.txt (0 when the file is not
listed). Any difference fails, so an allowance only goes down.
"""
import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ALLOWANCE = "tools/lint_allowance.txt"
SCRIPTS = ("bin/isshoni", "factory/bin/factory")
SUBPROCESS_CALLS = {"run", "call", "check_call", "check_output"}

SLEEP_PY = "sleep-py"
SLEEP_GO = "sleep-go"
TIMEOUT_PY = "timeout-py"
TIMEOUT_GO = "timeout-go"
ADVICE = {
    SLEEP_PY: "wait for the event, not the clock",
    SLEEP_GO: "wait for the event, not the clock",
    TIMEOUT_PY: "pass timeout=",
    TIMEOUT_GO: "use exec.CommandContext with a deadline",
}


def tracked():
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], capture_output=True, text=True, check=True, timeout=60).stdout
    return sorted(f for f in out.splitlines() if not f.startswith("tests/fixtures/") and Path(f).is_file())


def grep_lines(text, pattern):
    return [n for n, line in enumerate(text.splitlines(), 1) if pattern in line]


def calls_without_timeout(path, text):
    """Line numbers of subprocess calls with no timeout= and no ** argument; a file that does not parse is one finding."""
    try:
        tree = ast.parse(text, path)
    except SyntaxError as e:
        return [], [f"{path}:{e.lineno or 1}: cannot parse: {e.msg}"]
    lines = []
    for node in ast.walk(tree):
        f = node.func if isinstance(node, ast.Call) else None
        if (isinstance(f, ast.Attribute) and f.attr in SUBPROCESS_CALLS and isinstance(f.value, ast.Name) and f.value.id == "subprocess"
                and not any(k.arg in (None, "timeout") for k in node.keywords)):
            lines.append(node.lineno)
    return sorted(lines), []


def findings(files):
    """({(rule, path): [line, ...]}, [error line, ...])."""
    found, errors = defaultdict(list), []
    for path in files:
        text = Path(path).read_text(errors="replace")
        is_py = path.endswith(".py") or path in SCRIPTS
        if is_py:
            lines, errs = calls_without_timeout(path, text)
            found[TIMEOUT_PY, path] += lines
            errors += errs
        if path.endswith(".py") and path.startswith(("factory/tests/", "tests/")):
            found[SLEEP_PY, path] += grep_lines(text, "time.sleep(")
        if path.endswith("_test.go"):
            found[SLEEP_GO, path] += grep_lines(text, "time.Sleep(")
        if path.endswith(".go"):
            found[TIMEOUT_GO, path] += grep_lines(text, "exec.Command(")
    return {k: v for k, v in found.items() if v}, errors


def allowances():
    """({(rule, path): count}, [error line, ...])."""
    allowed, errors = {}, []
    p = Path(ALLOWANCE)
    for n, line in enumerate(p.read_text().splitlines() if p.exists() else [], 1):
        line = line.split("#", 1)[0].split()
        if not line:
            continue
        if len(line) != 3 or line[0] not in ADVICE or not line[2].isdigit() or (line[0], line[1]) in allowed:
            errors.append(f'{ALLOWANCE}:{n}: expected "<rule> <path> <count>" with a known rule, once per file')
        else:
            allowed[line[0], line[1]] = int(line[2])
    return allowed, errors


def main():
    found, report = findings(tracked())
    allowed, errors = allowances()
    report += errors
    for rule, path in sorted(found.keys() | allowed.keys(), key=lambda k: (k[1], k[0])):
        lines, allowance = found.get((rule, path), []), allowed.get((rule, path), 0)
        if len(lines) > allowance:
            report += [f"{path}:{n}: {rule}: {ADVICE[rule]}" for n in lines]
        elif len(lines) < allowance:
            report.append(f"{path}: {rule}: {len(lines)} left, allowance {allowance}: lower it in {ALLOWANCE}")
    if report:
        print("\n".join(report))
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main())
