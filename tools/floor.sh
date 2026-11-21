#!/bin/sh
# Records assets/tabs/factory.gif with vhs: the real binary over tests/fixtures/floor-root and invented repositories in a temp dir.
# Run from the repository root: sh tools/floor.sh
set -e
export BIN="$PWD/bin/isshoni-tui" ISSHONI_ROOT="$PWD/tests/fixtures/floor-root"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export FLOOR_REPOS="$tmp/repos"
make build
for r in alpha delta-docs; do mkdir -p "$FLOOR_REPOS/$r/.claude/worktrees"; done
vhs tools/floor.tape
