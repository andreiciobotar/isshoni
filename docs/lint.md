# Lint

`make lint` runs inside `make test`, so CI runs it too. It runs all three checks and fails when any reports a finding.

| check | holds | config |
|---|---|---|
| `python3 tools/ruff.py check` | ruff rules `F` (pyflakes: unused or undefined names) and `B` (bugbear: likely bugs) on every tracked Python file, `bin/isshoni` and `factory/bin/factory` included | `ruff.toml` |
| `go tool staticcheck ./...` | staticcheck's default checks | none |
| `python3 tools/lint.py` | sleeps in tests, calls without a timeout | `tools/lint_allowance.txt` |

Rules left out of ruff, and why:

| rule | why |
|---|---|
| `B023` | each lambda is called inside its own iteration; none escapes |
| `B904`, `B905` | `raise` without `from` and `zip` without `strict=` are the style here |
| `PLW2901`, `E741`, `E731` | not selected: the loop variable is normalised on purpose, and short names and assigned lambdas are fine |

There is no `# noqa` and no `//lint:ignore`: fix the finding, or leave the rule out in `ruff.toml` with its reason.

## How the tools arrive

- staticcheck is a `tool` directive in `go.mod`, pinned in `go.sum`, fetched by `make modules` with the Go modules. To bump it: `go get -tool honnef.co/go/tools/cmd/staticcheck@<version>`.
- ruff is downloaded by `tools/ruff.py` from the release archive for the platform, checked against a SHA-256 written in the script, and cached in `${XDG_CACHE_HOME:-~/.cache}/isshoni/ruff-<version>/ruff`. A failed download ends with a line starting `network:`. To bump it: change `VERSION` and the four checksums in `tools/ruff.py` (each release asset has a `.sha256` file).

## The repository's own checks

`tools/lint.py` counts four things per file and compares each count with `tools/lint_allowance.txt`:

| rule | counts | in |
|---|---|---|
| `sleep-py` | `time.sleep(` | tracked Python under `factory/tests/` and `tests/` |
| `sleep-go` | `time.Sleep(` | tracked `*_test.go` |
| `timeout-py` | `subprocess.run`, `.call`, `.check_call`, `.check_output` with no `timeout=` and no `**` argument | every tracked Python file |
| `timeout-go` | `exec.Command(` | every tracked Go file |

The allowance file has one line per file that keeps findings, `<rule> <path> <count>`; `#` starts a comment. A file not listed has an allowance of 0. A count above the allowance fails with each finding as `path:line: rule: what to do instead`. A count below fails too, and so does a line for a file with no finding or no file: lower or delete the line by hand. An allowance only goes down. Nothing rewrites the file.
