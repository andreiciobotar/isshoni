"""`factory wizard`, `factory reinstall` and `factory uninstall`: set the factory up on this machine, bring its plugin to the marketplace's main, and take it away."""
import collections
import functools
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

import factory_core as core

PLUGIN = "factory@factory"
SOURCE = "andreiciobotar/isshoni"
Step = collections.namedtuple("Step", "name purpose optional state act recheck", defaults=(False,))


class Skip(Exception):
    """An optional step that was declined or cannot run: one line, and the run goes on."""


def clone():
    """The clone this script runs from, else None: the installed plugin holds only factory/."""
    root = core.FACTORY.parents[2]
    return root if (root / "bin" / "isshoni").is_file() and (root / "go.mod").is_file() else None


def claude_json(*args, cwd=None):
    """The list `claude <args> --json` prints (run in `cwd`: a local-scope entry is enabled only there), or None."""
    rc, out, _ = core.run(["claude", *args, "--json"], cwd=cwd)
    try:
        data = json.loads(out) if rc == 0 else None
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def missing_dependencies():
    todo = []
    for tool, fix in (("git", "install git"), ("gh", "https://cli.github.com, then `gh auth login`"),
                      ("claude", "https://claude.com/claude-code, then run `claude` and log in"),
                      ("openssl", "install openssl")):
        if not shutil.which(tool):
            todo.append(f"{tool} is missing: {fix}")
    if sys.version_info < (3, 11):
        todo.append("Python 3.11 or newer is required: https://www.python.org/downloads/")
    # "/" is no repository, so no App identity replaces the human's login
    if shutil.which("gh") and core.run(["gh", "auth", "status"], cwd="/")[0] != 0:
        todo.append("gh is not logged in: run `gh auth login`")
    if shutil.which("claude"):
        rc, out, _ = core.run(["claude", "auth", "status"])
        try:
            logged_in = rc == 0 and json.loads(out).get("loggedIn") is True
        except (ValueError, AttributeError):
            logged_in = False
        if not logged_in:
            todo.append("claude is not logged in: run `claude` and log in")
    return todo


def dependencies_state():
    todo = missing_dependencies()
    return not todo, "\n    ".join(todo) or "git, gh, claude, openssl, Python 3.11+, both logins"


def dependencies_act(args, detail):
    raise core.Fail(detail)


def dashboard_state():
    root = clone()
    if not root:
        return None, "the dashboard needs a clone of the repository"
    return (root / "bin" / "isshoni-tui").exists(), str(root / "bin" / "isshoni-tui")


def dashboard_act(args, detail):
    if not shutil.which("go"):
        raise Skip("Go 1.27+ is missing: https://go.dev/dl, then run the wizard again")
    if not core.ask("Build the dashboard (make build)?", args.yes):
        raise Skip("declined")
    rc, out, err = core.run(["make", "build"], cwd=clone(), timeout=core.LAND_TEST_TIMEOUT)
    if rc != 0:
        raise core.Fail(f"make build failed: {((err or out).strip().splitlines() or ['no output'])[-1]}")
    return "built the dashboard (make build)"


def plugin_entries(plugins):
    """The entries of PLUGIN at user and local scope. A project-scope one is made by hand: no factory command installs or uninstalls it."""
    return [p for p in plugins if isinstance(p, dict) and p.get("id") == PLUGIN and p.get("scope") in ("user", "local")]


def own_market(markets):
    """The marketplace named factory, or None; a Fail when it is not from SOURCE."""
    market = next((m for m in markets if isinstance(m, dict) and m.get("name") == "factory"), None)
    if market and market.get("repo") != SOURCE:
        raise core.Fail(f"a marketplace named factory exists from {market.get('repo') or market.get('path') or market.get('source')}, "
                        f"not {SOURCE}: remove it (`claude plugin marketplace remove factory`) if it is not yours to keep, "
                        f"then run the wizard again")
    return market


def plugin_command(verb, entry):
    """(argv, cwd) of `claude plugin install|uninstall` for one entry, at its scope and, for a local one, in its repository."""
    argv = ["claude", "plugin", verb, PLUGIN, "--scope", entry["scope"], *(["--keep-data"] if verb == "uninstall" else [])]
    return argv, entry.get("projectPath") if entry["scope"] == "local" else None


def hand(command):
    argv, cwd = command
    return f"cd {shlex.quote(cwd)} && {shlex.join(argv)}" if cwd else shlex.join(argv)


def run_commands(commands):
    """Run each (argv, cwd); the first failure is a Fail that names it and lists, one per line, what still has to run."""
    for i, (argv, cwd) in enumerate(commands):
        rc, out, err = core.run(argv, cwd=cwd, timeout=core.NETWORK_TIMEOUT)
        if rc != 0:
            raise core.Fail(f"`{shlex.join(argv)}` failed: {(err or out).strip()}\n    By hand:\n      "
                            + "\n      ".join(hand(c) for c in commands[i:]), 1)


def plugin_purpose(root):
    return ("install the factory plugin into Claude Code for the whole user (for one repository only: `factory wizard --repository <path>`)"
            if root is None else f"install the factory plugin into Claude Code for {root} only (for the whole user: `factory wizard`)")


def plugin_state(root=None):
    plugins = claude_json("plugin", "list", cwd=root)
    if plugins is None:
        return False, "cannot read `claude plugin list --json`"
    entries = plugin_entries(plugins)
    user = next((e for e in entries if e["scope"] == "user"), None)
    local = next((e for e in entries if e["scope"] == "local" and e.get("projectPath") == root), None)
    wanted = user if root is None else local
    if wanted and wanted.get("enabled") is False:
        return False, f"{PLUGIN} is installed but disabled: run `claude plugin enable {PLUGIN} --scope {wanted['scope']}`"
    return bool(wanted) and not (root and user), PLUGIN


def plugin_act(args, detail):
    if "disabled" in detail:
        raise core.Fail(detail)
    root = args.repository
    plugins, markets = claude_json("plugin", "list", cwd=root), claude_json("plugin", "marketplace", "list")
    for name, found in (("plugin list", plugins), ("plugin marketplace list", markets)):
        if found is None:
            raise core.Fail(f"cannot read `claude {name} --json`: nothing was added")
    market, entries = own_market(markets), plugin_entries(plugins)
    if root is None:
        wanted, stale = None, [e for e in entries if e["scope"] == "local"]
        install = ["claude", "plugin", "install", PLUGIN], None
        if stale:
            print(f"  also removes the {len(stale)} repository-scope install(s): the user-scope one covers them")
    else:
        wanted = next((e for e in entries if e["scope"] == "local" and e.get("projectPath") == root), None)
        stale = [e for e in entries if e["scope"] == "user"]
        install = ["claude", "plugin", "install", PLUGIN, "--scope", "local"], root
        if stale:
            print("  also removes the user-scope install: every session would still load it")
    run_commands([*([] if market else [(["claude", "plugin", "marketplace", "add", SOURCE], None)]),
                  *([] if wanted else [install]), *(plugin_command("uninstall", e) for e in stale)])
    return f"installed {PLUGIN}" + (f" for {root}" if root else "")


def settings_state():
    path = core.SETTINGS_PATH.expanduser()
    if not path.exists():
        return False, f"{path} is missing"
    try:
        core.load_settings()
    except core.Fail as e:
        return False, f"{e} ({path})"
    return True, str(path)


def settings_act(args, detail):
    if core.SETTINGS_PATH.expanduser().exists():
        raise core.Fail(detail)
    core.load_settings()  # writes the TOML from a settings.json when only that exists
    if not core.SETTINGS_PATH.expanduser().exists():
        core.write_text(core.SETTINGS_PATH.expanduser(), core.settings_text())
    return f"wrote {core.SETTINGS_PATH.expanduser()}"


def cost_state():
    root = clone()
    if not root:
        return None, "cost alerts and the daily digest need a clone of the repository"
    if "/.claude/worktrees/" in str(root):
        return None, "this clone is a task worktree and its path disappears with it: run the wizard from the main checkout"
    try:
        monitor = core.load_settings()["monitor"]
    except core.Fail as e:
        return False, str(e)
    return bool(monitor and Path(monitor).is_file()), monitor or "monitor is not set"


def cost_act(args, detail):
    if not core.ask("Turn on cost alerts and the daily digest?", args.yes):
        raise Skip("declined")
    old, target = core.load_settings()["monitor"], str(clone() / "bin" / "isshoni")
    core.set_setting("paths.monitor", target)
    return f"set monitor to {target}" + (f" (it pointed at {old}, which is missing)" if old else "")


def stack_state():
    # "/" is no repository, so the human's login is used: this is the human's tool
    rc, out, _ = core.run(["gh", "extension", "list"], cwd="/")
    found = rc == 0 and any("github/gh-stack" in line.split("\t") for line in out.splitlines())
    return found, "github/gh-stack" if found else "not installed; optional, the factory works without it"


def stack_act(args, detail):
    version = re.search(r"gh version ((\d+)\.\d+\S*)", core.run(["gh", "--version"], cwd="/")[1])
    if version and int(version[2]) < 2:
        raise Skip(f"gh {version[1]} is older than 2.0, which extensions need: upgrade gh (https://cli.github.com), then run the wizard again")
    if not core.ask("Install GitHub's stack extension (gh extension install github/gh-stack)? "
                    "Optional: it shows and manages stacks from the terminal; the factory works without it.", args.yes):
        raise Skip("declined")
    rc, out, err = core.run(["gh", "extension", "install", "github/gh-stack"], cwd="/", timeout=core.RUN_TIMEOUT)
    if rc != 0:
        raise core.Fail(f"gh extension install github/gh-stack failed: {((err or out).strip().splitlines() or ['no output'])[-1]}")
    return "installed GitHub's stack extension (gh extension install github/gh-stack)"


def setup_app(args, detail):
    """The GitHub App: create it when there is none, then verify and repair it online. None when nothing changed."""
    identity = core.load_identity()
    if identity and args.name:
        raise core.Fail(f"this machine already has the App {identity['slug']}: the wizard does not rename it; rename it on "
                   f"its GitHub settings page ({identity['html_url']}) and run the wizard again", 1)
    if identity is None:
        if (core.IDENTITY.expanduser() / "key.pem").exists():
            raise core.Fail(f"{core.IDENTITY.expanduser() / 'key.pem'} exists without app.json: remove the directory with "
                       f"`rm -r {core.IDENTITY}`, delete the App it belongs to on your GitHub apps settings page "
                       f"(it holds the name), and run again", 1)
        if not core.ask("Give the factory its own GitHub identity, so its pull requests come from a bot you can approve? "
                   "Two confirmations in your browser.", args.yes):
            raise Skip("declined")
        owner = args.owner
        if not owner:
            rc, out, _ = core.run(["gh", "api", "user", "--jq", ".login"])
            owner = out.strip()
            if rc != 0 or not owner:
                raise core.Fail("cannot read your GitHub login (`gh api user`): pass --owner ACCOUNT", 1)
        identity = core.create_app(owner, args.name)
    elif args.owner and args.owner.lower() != identity["owner"].lower():
        raise core.Fail(f"this machine's identity is the App {identity['slug']} of {identity['owner']}, not {args.owner}: "
                   f"one identity per machine (to replace it, remove it first with `factory uninstall`)", 1)
    return f"the GitHub App {identity['slug']}" if core.verify_app(identity) else None


def app_state():
    try:
        identity = core.load_identity()
    except core.Fail as e:
        return False, str(e)
    if not identity:
        return False, "no identity"
    ready = identity.get("installation_id") and (core.IDENTITY.expanduser() / "key.pem").exists()
    return bool(ready), f"{identity.get('slug')} ({'installed' if ready else 'not installed or key missing'})"


WIZARD_STEPS = (
    Step("dependencies", "git, gh, claude, openssl, Python 3.11+, and both logins", False, dependencies_state, dependencies_act),
    Step("dashboard", "build the terminal dashboard", True, dashboard_state, dashboard_act),
    Step("plugin", "install the factory plugin into Claude Code", False, plugin_state, plugin_act),
    Step("settings", "write the factory's settings file", False, settings_state, settings_act),
    Step("cost criteria", "cost alerts and the daily digest", True, cost_state, cost_act),
    Step("gh stack", "GitHub's stack extension for gh", True, stack_state, stack_act),
    Step("GitHub App", "the factory's own GitHub identity", True, app_state, setup_app, recheck=True),
)


def args_wizard(parser):
    parser.add_argument("--yes", action="store_true", help="accept every offer without asking")
    parser.add_argument("--list", action="store_true", help="show each step and its state; change nothing")
    parser.add_argument("--owner", help="the account or organization to create the App under (default: your own GitHub account)")
    parser.add_argument("--name", help="the new App's name, 34 characters at most (default: isshoni-lander-<owner>)")
    parser.add_argument("--repository", help="install the plugin for this repository only (default: for the whole user)")


@core.command("wizard", "set everything up, step by step; rerun to repair or finish", args_wizard)
def cmd_wizard(args):
    if args.name and len(args.name) > core.APP_NAME_MAX:
        raise core.Fail(f"--name is {len(args.name)} characters: an App name is {core.APP_NAME_MAX} at most", 2)
    args.repository = core.Repo(args.repository).root if args.repository else None
    steps = [step._replace(purpose=plugin_purpose(args.repository), state=functools.partial(plugin_state, args.repository))
             if step.act is plugin_act else step for step in WIZARD_STEPS]
    did = failed = False
    for step in () if args.list else steps:
        done, detail = step.state()
        print(f"{step.name}: {step.purpose}")
        if done is None:
            print(f"  skipped: {detail}")
        elif done and not step.recheck:
            print(f"  done: {detail}")
        else:
            try:
                text = step.act(args, detail)
            except Skip as e:
                print(f"  skipped: {e}")
            except core.Fail as e:
                print(f"  needs you: {e}")
                if not step.optional:
                    print("Then run the wizard again.")
                    return 1
                failed = True
            else:
                print(f"  did: {text}" if text else f"  done: {detail}")
                did = did or bool(text)
    states = [(step, *step.state()) for step in steps]
    print("Summary:")
    for step, done, detail in states:
        print(f"  {step.name}: {'n/a' if done is None else 'done' if done else 'not done'} ({detail})")
    print("site manager: starts with the dispatcher session and stops with it; nothing to install")
    required = all(done for step, done, _ in states if not step.optional)
    if required:
        print("To start working, in the repository you want work done in, run `claude`, then:\n"
              "  /factory:dispatch")
    if not args.list and not did and not failed and all(done is not False for _, done, _ in states):
        print("Nothing to do: everything is set up.")
    if args.list:
        core.print_hosts()
    else:
        print("A server: `make wizard-remote` (docs/container.md)." if clone()
              else "A server: needs a clone of the repository, then `make wizard-remote` there (docs/container.md).")
    return 0 if required and not failed else 1


def recorded():
    """{(scope, projectPath): commit} of PLUGIN from Claude Code's installed_plugins.json: `plugin list --json` carries no commit."""
    path = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "plugins" / "installed_plugins.json"
    try:
        return {(e["scope"], e.get("projectPath")): e.get("gitCommitSha") for e in json.loads(path.read_text())["plugins"][PLUGIN]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def short(entry, commits):
    return (commits.get((entry["scope"], entry.get("projectPath"))) or "unknown")[:12]


def where(entry):
    return "user" if entry["scope"] == "user" else f"repository {entry.get('projectPath')}"


@core.command("reinstall", "bring every installation of the plugin to the marketplace's current main, at the scopes it has")
def cmd_reinstall(args):
    plugins, markets = claude_json("plugin", "list"), claude_json("plugin", "marketplace", "list")
    if plugins is None or markets is None:
        raise core.Fail("cannot read `claude plugin list --json` or `claude plugin marketplace list --json`: nothing changed")
    if own_market(markets) is None:
        raise core.Fail("the marketplace factory is not added: run `factory wizard`")
    entries = plugin_entries(plugins)
    if not entries:
        raise core.Fail(f"{PLUGIN} is not installed: run `factory wizard`")
    commits = recorded()
    before = sorted({short(e, commits) for e in entries})
    for sha in before:
        print(f"before: {sha}")
    gone = [e for e in entries if e["scope"] == "local" and not Path(e.get("projectPath") or "").is_dir()]
    for e in gone:
        argv, cwd = plugin_command("uninstall", e)
        removed = core.run(argv, cwd=cwd, timeout=core.NETWORK_TIMEOUT)[0] == 0
        print(f"{e.get('projectPath')} no longer exists: " + ("its entry is uninstalled" if removed else "its entry is skipped"))
    kept = [e for e in entries if e not in gone]
    run_commands([(["claude", "plugin", "marketplace", "update", "factory"], None),
                  *(plugin_command("uninstall", e) for e in kept), *(plugin_command("install", e) for e in kept)])
    after, commits = plugin_entries(claude_json("plugin", "list") or []), recorded()
    for e in after:
        print(f"after: {short(e, commits)} {where(e)}")
    if len(before) == 1 and before == sorted({short(e, commits) for e in after}):
        print(f"already at {before[0]}")
    print("Running sessions keep the copy they loaded: restart them, or run /reload-plugins in each.")
    return 0


def uninstall_step(args, left, found, lost, kept):
    """Say what was found and what is lost, then ask; True when the caller should act. --list only says."""
    print(f"  found: {found}\n  lost: {lost}")
    if args.list:
        return False
    if core.ask("  Remove it?", args.yes):
        return True
    left.append(f"{kept}: you declined")
    return False


def uninstall_schedules(args, left):
    try:
        backend = core.scheduler()
    except core.Fail as e:
        return print(f"  {e}: nothing to remove")
    labels = backend.owned()
    if not labels:
        return print("  nothing to remove")
    if uninstall_step(args, left, f"{len(labels)} watch job(s) of this home: {', '.join(labels)}",
                      "the half-hourly watch of those repositories", "the watch jobs"):
        for label in labels:
            backend.remove(label, None)
        print(f"  removed {len(labels)} job(s)")


def uninstall_app(args, left):
    identity = core.load_identity()
    if not identity:
        return print("  nothing to remove")
    if identity.get("copy"):
        return print("  this identity is a copy: the App belongs to the machine that ran `host add`, and only the copy "
                     "here goes, with the home step")
    hosts = core.load_hosts()
    if hosts:
        names = ", ".join(hosts)
        print(f"  the App {identity['slug']} and its installation are kept: {len(hosts)} host(s) use it ({names}).\n"
              "  Run `make uninstall-remote` for each (`bin/isshoni host remove NAME`), then `make uninstall` again.")
        return left.append(f"the App {identity['slug']} and its installation: {len(hosts)} host(s) use it ({names})")
    problem = None
    if identity.get("installation_id") and uninstall_step(
            args, left, f"the App {identity['slug']}, installed", "its access to your repositories",
            f"the App {identity['slug']}'s installation"):
        try:
            status, body = core.github_api("DELETE", f"/app/installations/{identity['installation_id']}", core.app_jwt(identity))
            if status in (204, 404):
                print("  uninstalled" if status == 204 else "  already uninstalled")
            else:
                problem = f"GitHub DELETE the installation failed: {core.reason(status, body)}"
        except core.Fail as e:
            problem = str(e)
    url = core.delete_url(identity)
    print(f"  Delete the App there (Advanced, Delete GitHub App): only you can: {url}")
    left.append(f"the App {identity['slug']} on GitHub, until you delete it at {url}")
    if not args.list:
        core.open_browser(url)
    if problem:
        raise core.Fail(problem)


def uninstall_plugin(args, left):
    if not shutil.which("claude"):
        return print("  claude is not on PATH: nothing to remove")
    plugins, markets = claude_json("plugin", "list"), claude_json("plugin", "marketplace", "list")
    if plugins is None or markets is None:
        raise core.Fail("cannot read `claude plugin list --json` or `claude plugin marketplace list --json`: nothing removed")
    entries = plugin_entries(plugins)
    installed = bool(entries)
    market = next((m for m in markets if isinstance(m, dict) and m.get("name") == "factory"), None)
    if market and market.get("repo") != SOURCE:
        print(f"  a marketplace named factory from {market.get('repo') or market.get('path') or market.get('source')}, "
              f"not {SOURCE}: kept, with the plugin")
        return left.append("the marketplace named factory and the plugin: not from " + SOURCE)
    if not market:
        if installed:
            print(f"  {PLUGIN} is installed but its marketplace is not {SOURCE}: kept")
            left.append(f"the plugin {PLUGIN}: not positively the factory's")
        else:
            print("  nothing to remove")
        return
    for p in plugins:
        if isinstance(p, dict) and p.get("id") == PLUGIN and p.get("scope") == "project":
            left.append(f"{p.get('projectPath')}/.claude/settings.json: the project-scope entry of {PLUGIN}, made by hand; removing the marketplace "
                        "drops its install record, its enabledPlugins line stays for you to remove")
    if uninstall_step(args, left, f"the marketplace {SOURCE}" + (f" and {PLUGIN} ({', '.join(where(e) for e in entries)})" if installed else ""),
                      "the factory's commands and hooks in Claude Code", "the plugin and its marketplace"):
        run_commands([*(plugin_command("uninstall", e) for e in entries if e["scope"] == "local"),
                      *([(["claude", "plugin", "uninstall", PLUGIN], None)] if any(e["scope"] == "user" for e in entries) else []),
                      (["claude", "plugin", "marketplace", "remove", "factory"], None)])
        print("  removed")


def uninstall_repository_schedule(args, left):
    repo = args.repository
    try:
        backend = core.scheduler()
    except core.Fail as e:
        return print(f"  {e}: nothing to remove")
    label = core.job_label(repo.root)
    if not backend.status(label)[0]:
        return print("  nothing to remove")
    if uninstall_step(args, left, f"the watch job {label}", "the half-hourly watch of this repository", "the watch job of the repository"):
        core.schedule_remove(repo, backend)
        print("  removed")


def uninstall_repository_plugin(args, left):
    root = args.repository.root
    if not shutil.which("claude"):
        return print("  claude is not on PATH: nothing to remove")
    plugins = claude_json("plugin", "list", cwd=root)
    if plugins is None:
        raise core.Fail("cannot read `claude plugin list --json`: nothing removed")
    entries = plugin_entries(plugins)
    # a session in a worktree of the repository makes Claude Code add an entry for the worktree's path
    mine = [e for e in entries if e["scope"] == "local" and e.get("projectPath") in (root, *args.repository.worktree_paths)]
    for e in [e for e in mine if not Path(e["projectPath"]).is_dir()]:
        mine.remove(e)
        print(f"  {e['projectPath']} no longer exists: its entry is skipped")
        left.append(f"{e['projectPath']}: the worktree is gone, its entry of {PLUGIN} stays")
    if mine:
        if uninstall_step(args, left, f"{PLUGIN} installed for {', '.join(e['projectPath'] for e in mine)}",
                          "the factory's commands and hooks in Claude Code for this repository", "the plugin of the repository"):
            run_commands([plugin_command("uninstall", e) for e in mine])
            print("  removed")
    elif any(e["scope"] == "user" for e in entries):
        print(f"  {PLUGIN} is installed for the whole user: it is not removed for one repository (`factory uninstall` removes it)")
    else:
        print("  nothing to remove")


def uninstall_repository_records(args, left):
    home = core.STORE.expanduser()
    tag = core.repo_state(args.repository.root, "", "").name  # what every record of the repository has in its name
    mine = sorted(p for p in home.iterdir() if p.is_file() and tag in p.name) if home.is_dir() else []
    if not mine:
        return print("  nothing to remove")
    if uninstall_step(args, left, "in " + str(home) + "\n    " + "\n    ".join(p.name for p in mine), "this repository's records, for good",
                      "the records of the repository"):
        for p in mine:
            p.unlink()
        print(f"  removed {len(mine)} file(s)")


def count_files(path):
    return sum(1 for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def uninstall_home(args, left):
    home = core.STORE.expanduser()
    entries = sorted(home.iterdir()) if home.is_dir() else []
    kept = {"identity", "hosts.json"} if core.load_hosts() else set()
    mine = [p for p in entries if core.HOME_FILE_RE.fullmatch(p.name) and p.name not in kept]
    strangers = [p for p in entries if not core.HOME_FILE_RE.fullmatch(p.name)]
    if strangers and core.other_home():
        print(f"  {home} does not look like a factory home: {', '.join(p.name for p in strangers)}; nothing removed")
        return left.append(f"{home}: does not look like a factory home")
    for p in strangers:
        print(f"  not the factory's, kept: {p}")
        left.append(f"{p}: not the factory's")
    for p in entries:
        if p.name in kept:
            print(f"  kept, hosts still use it: {p}")
            left.append(f"{p}: hosts still use the App (`make uninstall-remote`, then `make uninstall` again)")
    if not mine:
        return print("  nothing to remove")
    found = "\n    ".join(f"{p} ({count_files(p)} files)" if p.is_dir() else str(p) for p in mine)
    if uninstall_step(args, left, "in " + str(home) + "\n    " + found, "all of it, for good, the identity's private key included"
                      if any(p.name == "identity" for p in mine) else "all of it, for good", f"the home {home}"):
        for p in mine:
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        try:
            home.rmdir()
        except OSError:
            return print("  removed what is the factory's; the directory stays")
        print(f"  removed {home}")


def uninstall_dashboard(args, left):
    binary = clone() and clone() / "bin" / "isshoni-tui"
    if not (binary and binary.exists()):
        return print("  nothing to remove")
    if uninstall_step(args, left, str(binary), "the built dashboard (make build makes it again)", "the dashboard binary"):
        binary.unlink()
        print("  removed")


UNINSTALL_STEPS = (("schedules", uninstall_schedules), ("GitHub App", uninstall_app), ("plugin", uninstall_plugin),
                   ("home", uninstall_home), ("dashboard binary", uninstall_dashboard))


REPOSITORY_UNINSTALL_STEPS = (("schedule", uninstall_repository_schedule), ("plugin", uninstall_repository_plugin),
                              ("records", uninstall_repository_records))


def args_uninstall(parser):
    parser.add_argument("--yes", action="store_true", help="confirm every removal without asking")
    parser.add_argument("--list", action="store_true", help="show what each step would remove; change nothing")
    parser.add_argument("--repository", help="take the factory off this repository only (default: remove everything)")


@core.command("uninstall", "remove what the wizard and the factory put on this machine", args_uninstall)
def cmd_uninstall(args):
    args.repository = core.Repo(args.repository) if args.repository else None
    left, failed = [], False
    for name, step in REPOSITORY_UNINSTALL_STEPS if args.repository else UNINSTALL_STEPS:
        print(f"{name}:")
        try:
            step(args, left)
        except (core.Fail, OSError) as e:
            print(f"  failed: {e}")
            failed = True
    print("left behind:")
    behind = ((f"the task worktrees of {args.repository.root} (.claude/worktrees/) and its worktree-* branches: "
               "`factory clean` or `git worktree remove` removes them",
               f"{args.repository.root}/.claude/settings.local.json, with an empty enabledPlugins, and its line in the global git ignore file: "
               "Claude Code's, the factory does not edit them")
              if args.repository else
              ("Claude Code's own transcripts and settings (~/.claude/projects)",
               "your repositories, their task worktrees (.claude/worktrees/) and worktree-* branches: "
               "`factory clean` or `git worktree remove` removes them, before the plugin is uninstalled",
               *([f"this clone ({clone()})"] if clone() else []), "git, gh and its stack extension (gh extension remove stack), Go, Claude Code"))
    for line in (*behind, *left):
        print(f"  {line}")
    if args.repository:
        plugins = claude_json("plugin", "list")
        if plugins is not None and not any(isinstance(p, dict) and p.get("id") == PLUGIN for p in plugins):
            print("no installation is left; factory uninstall removes the rest (the store, the App's installation, the marketplace)")
    elif args.list:
        try:
            core.print_hosts()
        except core.Fail as e:
            print(f"hosts: failed: {e}")
            failed = True
    return 1 if failed else 0
