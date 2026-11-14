# Contributing

- `make test` must pass (it checks formatting first: fix with `gofmt -w <files>`); a change in behaviour comes with a test.
- To see a view as the golden tests do: `make screenshot FIXTURE=grouping VIEW=dashboard` (a PNG under `shots/`, needs Chrome) or `make snapshot FIXTURE=grouping ARGS="--no-color --row 3"`. `FIXTURE` is a directory of `tests/fixtures`, copied with every file at 2026-01-01T10:30:00Z and rendered in UTC. The binary's own clock still applies, so the header clock differs from the golden file; the rows are the same. The golden tests write their frames to a directory when `ISSHONI_SHOT_DIR` is set: `ISSHONI_SHOT_DIR=/tmp/shots go test ./internal/tui`. Regenerate the goldens with `make golden` (`go test ./internal/tui -update`) and read the diff; `make fmt` writes gofmt's changes.
- One pull request, one outcome. The title states the outcome. The body says what changes for the user, where a reviewer should look, what you verified and what you did not.
- Anything a user can hit (a flag, a default, a limit) is documented in `README.md` or `docs/`.

## Licence of contributions

By opening a pull request you license your contribution to the owner, Andrei Ciobotar, under the MIT licence in [LICENSE](LICENSE), without the Commons Clause, so the owner stays free to sell Isshoni or change its licence. You also say it is yours to license (your own work, or your employer lets you).
