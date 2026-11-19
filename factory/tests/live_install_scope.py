#!/usr/bin/env python3
"""Live proof of install scope and `factory reinstall`. Run by hand, never by unittest; it needs the `claude` CLI but no login and costs nothing.

    python3 factory/tests/live_install_scope.py [--keep]

Everything lives in a scratch directory under the system temp directory: HOME, CLAUDE_CONFIG_DIR and FACTORY_HOME of every child
process point into it, and the script proves first and last that the real `installed_plugins.json`, `known_marketplaces.json`,
`settings.json` and `plugins/cache` listing did not change (and aborts when they did). The marketplace is a scratch clone of this
repository with the working tree's `factory/` committed on top, added as a directory source (`claude plugin marketplace add` takes
no `file://` URL), so the plugin is a copy in the cache. `factory` runs through `seam.py` (written into the scratch), which imports
the code from a given `lib` directory and, in that process only, points `SOURCE` at the scratch marketplace, shows a directory
marketplace as `repo` (production reads `repo` of a github one), and keeps the wizard, the repository uninstall and the full uninstall
to the plugin and record steps: no App, no dashboard build, no launchd job, no removal of the home or the clone's binary.
What a session loaded is read from the `init` event of `claude -p hi --output-format stream-json --verbose` (it ends `Not logged in`).

Each result is a line `ok: ...` or `FAIL: ...`; facts A, B, C, D, E (a project-scope entry and `marketplace remove`) and F (the cache directory after the last uninstall) print their finding.
The scratch directory is deleted at the end unless `--keep`; its size is printed."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import real_store

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
args = ap.parse_args()

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[1]
REAL_CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
TMP = Path(tempfile.mkdtemp(prefix="install-scope-live-")).resolve()
HOME, CFG, STORE, MKT = TMP / "home", TMP / "cfg", TMP / "home" / ".claude" / "factory", TMP / "mkt"
real_store.refuse(STORE)
for d in (HOME, CFG, STORE):
    d.mkdir(parents=True, exist_ok=True)
ENV = {**os.environ, "HOME": str(HOME), "CLAUDE_CONFIG_DIR": str(CFG), "FACTORY_HOME": str(STORE)}
for name in ("CLAUDECODE", "FACTORY_ROLE", "XDG_CONFIG_HOME"):
    ENV.pop(name, None)
CACHE = CFG / "plugins" / "cache" / "factory" / "factory" / "0.6.0"
failures = []

SEAM = '''import sys
lib, source, *argv = sys.argv[1:]
sys.path.insert(0, lib)
import factory_core as core
import factory_setup as setup

setup.SOURCE = source
real = setup.claude_json


def claude_json(*a, cwd=None):
    out = real(*a, cwd=cwd)
    return [{**m, "repo": m.get("path")} for m in out] if a[:3] == ("plugin", "marketplace", "list") and out else out


setup.claude_json = claude_json
setup.WIZARD_STEPS = [s for s in setup.WIZARD_STEPS if s.act is setup.plugin_act]
setup.UNINSTALL_STEPS = tuple(s for s in setup.UNINSTALL_STEPS if s[0] == "plugin")
setup.REPOSITORY_UNINSTALL_STEPS = tuple(s for s in setup.REPOSITORY_UNINSTALL_STEPS if s[0] != "schedule")
sys.exit(core.main(argv))
'''


def say(text):
    print(text, flush=True)


def check(ok, what):
    say(f"{'ok' if ok else 'FAIL'}: {what}")
    if not ok:
        failures.append(what)


def sh(argv, cwd=None, timeout=300):
    p = subprocess.run([str(a) for a in argv], cwd=cwd, env=ENV, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def show(label, code, out, err=""):
    say(f"  $ {label} -> exit {code}")
    for line in (out + err).strip().splitlines():
        say(f"    {line}")


def claude(*argv, cwd=None):
    code, out, err = sh(["claude", *argv], cwd=cwd)
    show("claude " + " ".join(argv) + (f"  (in {cwd.name})" if cwd else ""), code, out, err)
    return code, out


def factory(lib, *argv):
    """`factory <argv>` run from the code in `lib` through the seam."""
    argv = [str(a) for a in argv]
    code, out, err = sh([sys.executable, TMP / "seam.py", lib, MKT, *argv], cwd=TMP)
    argv = [str(a) for a in argv]
    show("factory " + " ".join(argv) + ("  (installed copy)" if lib != WORKTREE / "factory" / "lib" else ""), code, out, err)
    return code, out + err


def git(repo, *argv):
    code, out, err = sh(["git", "-c", "user.name=proof", "-c", "user.email=proof@example.invalid", *argv], cwd=repo)
    if code:
        sys.exit(f"git {' '.join(argv)} failed: {err}")
    return out.strip()


def fingerprint():
    """What the real Claude Code config holds: checksums of three files and the cache listing with mtimes."""
    files = {f: (hashlib.sha256(p.read_bytes()).hexdigest() if (p := REAL_CLAUDE / f).is_file() else "absent")
             for f in ("plugins/installed_plugins.json", "plugins/known_marketplaces.json", "settings.json")}
    cache = REAL_CLAUDE / "plugins" / "cache"
    return files, sorted((str(p.relative_to(cache)), p.stat().st_mtime_ns) for p in cache.rglob("*")) if cache.is_dir() else []


def isolated():
    before = fingerprint()
    code, out, _ = sh(["claude", "plugin", "marketplace", "list", "--json"])
    return code == 0 and fingerprint() == before and not str(REAL_CLAUDE).startswith(str(TMP))


def new_repo(name):
    repo = TMP / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text(name + "\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "first")
    return repo


def files_of(repo):
    return {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(repo).parts}


def commit_marker(n):
    """A commit in the scratch marketplace that touches factory/; returns its sha."""
    (MKT / "factory" / "PROOF_MARKER").write_text(f"{n}\n")
    git(MKT, "add", "factory/PROOF_MARKER")
    git(MKT, "commit", "-q", "-m", f"marker {n}")
    return git(MKT, "rev-parse", "HEAD")


def marker():
    path = CACHE / "PROOF_MARKER"
    return path.read_text().strip() if path.is_file() else None


def recorded():
    """{(scope, repository name or None): commit} from the scratch installed_plugins.json."""
    path = CFG / "plugins" / "installed_plugins.json"
    doc = json.loads(path.read_text())["plugins"].get("factory@factory", []) if path.is_file() else []
    return {(e["scope"], Path(e["projectPath"]).name if e.get("projectPath") else None): e.get("gitCommitSha") for e in doc}


def scopes():
    return sorted(k for k in recorded() if k[0] in ("user", "local"))


def agents(repo):
    """The factory: agents a headless session started in `repo` loaded, from its init event."""
    code, out, err = sh(["claude", "-p", "hi", "--output-format", "stream-json", "--verbose"], cwd=repo, timeout=180)
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            return sorted(a for a in event.get("agents", []) if a.startswith("factory:"))
    sys.exit(f"no init event from claude -p in {repo}: {(out + err)[-500:]}")


FACTORY_AGENTS = ["factory:coder", "factory:explorer", "factory:lander", "factory:ops-explorer", "factory:reviewer"]
try:
    say(f"claude {sh(['claude', '--version'])[1].strip()}; scratch {TMP.name}")
    if not isolated():
        sys.exit("isolation check failed at the start: the real Claude Code config changed or the scratch is not used; nothing ran")
    say("ok: isolation holds at the start (a scratch `claude plugin marketplace list` left the real config unchanged)")

    git(WORKTREE, "rev-parse", "HEAD")
    git(TMP, "clone", "-q", "--depth", "1", f"file://{WORKTREE}", MKT)
    shutil.copytree(WORKTREE / "factory", MKT / "factory", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    git(MKT, "add", "-A")
    git(MKT, "commit", "-q", "--allow-empty", "-m", "working tree")
    (TMP / "seam.py").write_text(SEAM)
    LIB = WORKTREE / "factory" / "lib"
    A, B, C = new_repo("A"), new_repo("B"), new_repo("C")
    STORE.mkdir(parents=True, exist_ok=True)
    os.environ["FACTORY_HOME"] = str(STORE)  # before the import: the store path is read there
    sys.path.insert(0, str(LIB))
    import factory_core as core
    assert core.STORE.expanduser() == STORE, core.STORE
    records = {r: core.repo_state(str(r), "note", "json") for r in (A, B)}
    other = [STORE / "settings.toml", STORE / "identity" / "key.pem"]
    (STORE / "identity").mkdir()
    for path in [*records.values(), *other]:
        path.write_text("x\n")
    say(f"the marketplace: {len(git(MKT, 'log', '--oneline').splitlines())} commits, HEAD {git(MKT, 'rev-parse', '--short=12', 'HEAD')}\n")

    say("== 1. user scope: the wizard's plugin step")
    code, out = factory(LIB, "wizard", "--yes")
    check(code == 0 and scopes() == [("user", None)], f"`factory wizard` installed at user scope only: {scopes()}")
    check(agents(A) == FACTORY_AGENTS and agents(B) == FACTORY_AGENTS, f"a session in A and a session in B both list {len(FACTORY_AGENTS)} factory: agents")
    check("--repository <path>" in out, "the plugin step names `--repository <path>` as the alternative")

    say("\n== 2. repository scope: wizard --repository A")
    before = files_of(A)
    code, out = factory(LIB, "wizard", "--yes", "--repository", A)
    check(code == 0 and scopes() == [("local", "A")], f"the user entry is gone and A has a local entry: {scopes()}")
    check(agents(A) == FACTORY_AGENTS and agents(B) == [], f"A's session lists the factory: agents, B's lists {len(agents(B))}")
    after = files_of(A)
    changed = sorted(k for k in {*before, *after} if before.get(k) != after.get(k))
    check(changed == [".claude/settings.local.json"], f"the only file changed in A is {changed}")
    check(not git(A, "status", "--porcelain"), "`git status --porcelain` in A is empty")
    say(f"  A/.claude/settings.local.json = {(A / '.claude' / 'settings.local.json').read_text().strip()}")
    code, out = factory(LIB, "wizard", "--yes", "--repository", A)
    check("did:" not in out, "a second `wizard --repository A` changes nothing")
    code, out = factory(LIB, "wizard", "--yes", "--repository", TMP)
    check(code != 0 and not (TMP / ".claude").exists(), "a path outside a repository fails and installs nothing")

    say("\n== repository scope for B too, then FACT A: two local entries, one shared cache directory")
    factory(LIB, "wizard", "--yes", "--repository", B)
    check(scopes() == [("local", "A"), ("local", "B")], f"A and B both have local entries: {scopes()}")
    c0 = recorded()[("local", "A")]
    c1 = commit_marker(1)
    claude("plugin", "marketplace", "update", "factory")
    say(f"  cache copy marker after the update, before any reinstall: {marker()!r}")
    claude("plugin", "uninstall", "factory@factory", "--scope", "local", "--keep-data", cwd=A)
    say(f"  cache directory after uninstalling A only: {'present' if CACHE.is_dir() else 'gone'}")
    claude("plugin", "install", "factory@factory", "--scope", "local", cwd=A)
    got = recorded()
    say(f"FACT A: marketplace HEAD {c1[:12]}; after uninstall+install in A only: A {got[('local', 'A')][:12]}, B {got[('local', 'B')][:12]}; "
        f"cache copy marker {marker()!r} ({'refreshed' if marker() == '1' else 'not refreshed'}); entries at start {c0[:12]}")
    check(True, "fact A printed")

    say("\n== 4. reinstall from the worktree's code")
    c2 = commit_marker(2)
    code, out = factory(LIB, "reinstall")
    lines = out.splitlines()
    pre = {l.split()[1] for l in lines if l.startswith("before: ")}
    post = [l.split() for l in lines if l.startswith("after: ")]
    check(code == 0 and len(pre) == 2 and c2[:12] not in pre, f"reinstall printed two different before commits {sorted(pre)}")
    check(len(post) == 2 and {p[1] for p in post} == {c2[:12]}, f"reinstall printed after: {c2[:12]} for both entries")
    check(set(recorded().values()) == {c2} and marker() == "2", f"both records hold the source's HEAD and the cache copy holds its file (marker {marker()!r})")
    check(any(l.startswith("Running sessions keep the copy") for l in lines), "the running-sessions line is printed")
    code, out = factory(LIB, "reinstall")
    check(code == 0 and f"already at {c2[:12]}" in out, "a second reinstall says `already at` the same commit")

    say("\n== FACT D: reinstall run from the installed copy it deletes and re-creates")
    c3 = commit_marker(3)
    check((CACHE / "lib" / "factory_setup.py").is_file(), "the installed copy holds cmd_reinstall's module")
    code, out = factory(CACHE / "lib", "reinstall")
    ok = code == 0 and set(recorded().values()) == {c3} and marker() == "3" and (CACHE / "bin" / "factory").is_file()
    say(f"FACT D: from the installed copy: exit {code}, records {sorted({v[:12] for v in recorded().values()})} (source {c3[:12]}), "
        f"cache copy marker {marker()!r}, copy present {(CACHE / 'bin' / 'factory').is_file()}")
    check(ok, "fact D: reinstall from the installed copy completes and the copy is replaced")

    say("\n== 5. uninstall --repository A with B installed, and a hand-made project-scope entry in C")
    claude("plugin", "install", "factory@factory", "--scope", "project", cwd=C)
    code, out = factory(LIB, "uninstall", "--yes", "--repository", A)
    check(code == 0 and scopes() == [("local", "B")], f"only A's local entry went: {scopes()}")
    check(agents(A) == [] and agents(B) == FACTORY_AGENTS, "A's session lists no factory: agent, B's still does")
    check(not records[A].exists() and records[B].exists() and all(p.exists() for p in other), "A's store record went; B's record, settings.toml and identity stay")
    check("no installation is left" not in out, "the `no installation is left` line is absent while B remains")
    ignore = HOME / ".config" / "git" / "ignore"
    say(f"FACT C: after `claude plugin uninstall --scope local` in A (through factory uninstall --repository A): "
        f"A/.claude/settings.local.json = {(A / '.claude' / 'settings.local.json').read_text().strip() if (A / '.claude' / 'settings.local.json').is_file() else 'absent'}; "
        f"global git ignore {ignore.relative_to(HOME)} = {ignore.read_text().strip().splitlines() if ignore.is_file() else 'absent'}")
    check(True, "fact C printed")
    code, out = factory(LIB, "uninstall", "--yes", "--list", "--repository", B)
    check(scopes() == [("local", "B")], "`uninstall --repository B --list` removes nothing")

    say("\n== FACT B: a session in a git worktree of D (a repository with a local install, no --plugin-dir)")
    D = new_repo("D")
    factory(LIB, "wizard", "--yes", "--repository", D)
    wt = D / ".claude" / "worktrees" / "x"
    git(D, "worktree", "add", "-q", wt, "-b", "worktree-x")
    check(scopes() == [("local", "B"), ("local", "D")], f"before the session in D's worktree: {scopes()}")
    seen = agents(wt)
    say(f"FACT B: a session started in <D>/.claude/worktrees/x lists {len(seen)} factory: agents ({'loads' if seen else 'does not load'} the local-scope plugin); "
        f"entries now {scopes()}")
    check(seen == FACTORY_AGENTS and ("local", "x") in scopes(), "fact B: the worktree session loads the plugin and Claude Code records a local entry for the worktree")
    code, out = factory(LIB, "uninstall", "--yes", "--repository", D)
    say(f"FACT B (uninstall --repository D): entries left {scopes()}; a session in the worktree lists {len(agents(wt))} factory: agents")
    check(True, "fact B side effect printed")

    say("\n== full uninstall")
    code, out = factory(LIB, "uninstall", "--yes")
    check(code == 0 and scopes() == [], f"no user or local entry is left: {scopes()}")
    code, markets = sh(["claude", "plugin", "marketplace", "list", "--json"])[:2]
    check(json.loads(markets) == [], "no marketplace is left")
    say(f"FACT F: the cache directory once the last entry is uninstalled and the marketplace removed: {'present' if CACHE.is_dir() else 'gone'}")
    check(True, "fact F printed")
    project = [k for k in recorded() if k[0] == "project"]
    in_c = (C / ".claude" / "settings.json").read_text().strip() if (C / ".claude" / "settings.json").is_file() else "absent"
    say(f"FACT E: after `marketplace remove factory`, the hand-made project-scope entry in C is "
        f"{'still recorded' if project else 'removed from installed_plugins.json'}; C/.claude/settings.json = {in_c}")
    check(True, "fact E printed")
    check(f"{C}/.claude/settings.json" in out and "enabledPlugins" in in_c, "the full uninstall names the project-scope entry's settings file under `left behind`, and the file still holds the entry")

    check(all(p.exists() for p in other), "the scratch store's other files are still there")
finally:
    total = sum(1 for p in TMP.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in TMP.rglob("*") if p.is_file())
    final = isolated() if TMP.exists() else False
    check(final, "isolation holds at the end: the real Claude Code config is unchanged")
    if not args.keep:
        shutil.rmtree(TMP, ignore_errors=True)
    say(f"scratch {TMP.name}: {total} files, {size} bytes on disk {'kept' if args.keep else 'deleted'}")
say("FAILED: " + "; ".join(failures) if failures else "all checks passed")
sys.exit(1 if failures else 0)
