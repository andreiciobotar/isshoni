"""Who wrote what: the kind of a file an Edit or Write touched. The one place the path rules live."""
from __future__ import annotations

import re

KINDS = ("product", "tests", "docs", "rules", "spec", "scratch", "other")
CODE = ("product", "tests")  # the kinds the headline is about

_WORKTREE = re.compile(r"/\.claude/worktrees/[^/]+/(.*)", re.S)

# (kind, inside a worktree, pattern); the first match wins. Outside, the pattern sees the absolute path, inside the relative one.
_RULES = tuple((kind, inside, re.compile(pattern)) for kind, inside, pattern in (
    ("scratch", False, r"\.retro\.json$|(^|/)(tmp|scratchpad|T)/|/\.claude/jobs/|^/private/var/|^/var/folders/"),
    ("spec", False, r"/factory/specs/|\.pr\.md$"),
    ("rules", True, r"(^|/)(CLAUDE|AGENTS)\.md$|(^|/)(agents|skills|roles|commands)/"),
    ("tests", True, r"_test\.go$|(^|/)(test|tests|testdata|fixtures)/|\.golden$|(^|/)test_[^/]*\.py$"),
    ("docs", True, r"\.(md|txt)$"),
    ("product", True, r"\.(go|py|sh|toml|json|yaml|yml|mod|sum|js|ts|css|html)$|(^|/)\.[^/]*$|(^|/)[^/.]+$"),
))


def kind(path: str) -> str:
    m = _WORKTREE.search(path)
    inside, rel = (True, m.group(1)) if m else (False, path)
    return next((k for k, i, rx in _RULES if i == inside and rx.search(rel)), "other")
