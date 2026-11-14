"""`isshoni host`: onboard a remote Linux Docker host to run this repository's factory container over SSH.

Every remote action is `ssh <target> <command>`. The App's key and the Claude token travel only on the stdin of
`docker exec -i` into the container, never in an argv or an environment."""
from __future__ import annotations

import getpass
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTAINER = IMAGE = PROJECT = "isshoni-factory"
MONITOR = "/state/home/.isshoni/bin/isshoni"
NAME_RE = re.compile(r"[^A-Za-z0-9_-]")
REPO_RE = re.compile(r"(?!\.{1,2}/)[A-Za-z0-9_.][A-Za-z0-9_.-]*/(?!\.{1,2}$)[A-Za-z0-9_.][A-Za-z0-9_.-]*")
HOST_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
ALIAS_RE = re.compile(r"[A-Za-z0-9_.-]+")
SERVER_CHECK = """docker info >/dev/null 2>&1 || echo docker
docker compose version >/dev/null 2>&1 || echo compose
case "$(uname -m)" in x86_64|aarch64) ;; *) echo arch;; esac
if docker port isshoni-factory 2222/tcp 2>/dev/null | grep -q ":$1\\$"; then :
elif command -v ss >/dev/null; then ss -ltn | grep -q ":$1 " && echo port
else echo noss; fi
exit 0
"""
SERVER_PROBLEMS = {
    "docker": "docker does not run for this user without sudo: the ssh user must be able to run docker.",
    "compose": "`docker compose` is missing: install Docker's compose plugin.",
    "arch": "the server's architecture is neither x86_64 nor aarch64.",
    "port": "port %s is in use on the server: pass another --dashboard-port.",
}
KEY_TYPES = ("ssh-", "ecdsa-", "sk-")
DASHBOARD_SERVING = "pgrep -f '[i]sshoni-tui --serve' >/dev/null"
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Fixed scripts that run in the container (`sh -c`), arguments as $1.. never carry a secret.
MERGE_PY = r"""
import json, os, sys
path = os.path.expanduser(sys.argv[1])
def merge(a, b):
    for k, v in b.items():
        a[k] = merge(a[k] if isinstance(a.get(k), dict) else {}, v) if isinstance(v, dict) else v
    return a
try:
    with open(path) as f:
        cur = json.load(f)
except FileNotFoundError:
    cur = {}
os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
with os.fdopen(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    json.dump(merge(cur, json.load(sys.stdin)), f, indent=2)
    f.write("\n")
os.replace(path + ".tmp", path)
"""
MERGE = 'exec python3 -c "$1" "$2"'
LOGIN_PY = r"""
import json, os, sys
env = json.load(open(os.path.expanduser("~/.claude/settings.json"))).get("env", {})
sys.exit(not env.get("CLAUDE_CODE_OAUTH_TOKEN"))
"""
LOGIN_OK = 'python3 -c "$1" && claude -p "reply with ok"'
IDENTITY_SUMS = (
    'cd ~/.claude/factory/identity && python3 -c \'import hashlib; '
    '[print(hashlib.sha256(open(f, "rb").read()).hexdigest(), f) for f in ("key.pem", "app.json")]\''
)
IDENTITY_PUT = (
    'set -eu; umask 077; d=$HOME/.claude/factory/identity; mkdir -p "$d"; chmod 700 "$d"; '
    'tar -x -C "$d" -f -; chmod 600 "$d/key.pem" "$d/app.json"'
)
SOURCE_PUT = """set -eu
d=$HOME/.isshoni
mkdir -p "$HOME/code"
rm -rf "$d.new"; mkdir "$d.new"
tar -x -C "$d.new" -f -
rm -rf "$d"; mv "$d.new" "$d"
make -C "$d" install
claude plugin marketplace list --json | grep -q '"name": *"factory"' && claude plugin marketplace update factory || claude plugin marketplace add "$d"
claude plugin list --json | grep -q '"factory@factory"' && claude plugin update factory@factory || claude plugin install factory@factory
echo "$1" > "$d/.rev"
"""
KEYS_PY = r"""
import os, sys
path = os.path.expanduser("~/.claude/factory/dashboard/authorized_keys")
pub = sys.argv[1]
try:
    with open(path) as f:
        have = [l for l in f.read().splitlines() if l.strip()]
except FileNotFoundError:
    have = []
new = list(have)
for i, line in enumerate(have):
    if pub.split()[1] in line.split():
        if line.split()[0] != "write":
            new[i] = "write " + pub
        break
else:
    new.append("write " + pub)
if new != have:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with os.fdopen(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(new) + "\n")
    os.replace(path + ".tmp", path)
    print("changed")
"""
KEYS = 'exec python3 -c "$1" "$2"'
# `docker/start` serves the dashboard only if the key file and the binary exist when the container starts.
# Here they came later, and a restart would kill the sessions: start it in place.
SERVE = "docker exec -d %s sh -c 'exec \"$HOME/.isshoni/bin/isshoni-tui\" --serve 0.0.0.0:2222'" % CONTAINER
CLONE = """set -eu
mkdir -p "$HOME/code"; cd "$HOME/code"
if [ -e "$2" ]; then echo "~/code/$2 exists and is not a checkout: move it away and run this again." >&2; exit 1; fi
rm -rf "$2~clone"; trap 'rm -rf "$HOME/code/$2~clone"' EXIT
if ! GIT_TERMINAL_PROMPT=0 git clone "https://github.com/$1" "$2~clone"; then
  rm -rf "$2~clone"; git init -q "$2~clone"; cd "$2~clone"
  git remote add origin "https://github.com/$1"
  eval "$("$HOME/.isshoni/factory/bin/factory" env)"
  branch=$(git ls-remote --symref origin HEAD | awk '/^ref:/ {sub("refs/heads/", "", $2); print $2}')
  git fetch origin
  git checkout -B "$branch" "origin/$branch"
  cd ..
fi
mv "$2~clone" "$2"
"""


class Stop(Exception):
    """A plain sentence for the user: what is missing and what to do."""


# ---- remote ---------------------------------------------------------------------

def ssh_env():
    """The laptop's environment without the Claude token: no ssh child needs it."""
    return {k: v for k, v in os.environ.items() if k != TOKEN_VAR}


def ssh(target, command, stdin=b""):
    try:
        p = subprocess.run(["ssh", target, command], input=stdin, capture_output=True, env=ssh_env())
    except FileNotFoundError:
        raise Stop("ssh is not installed on this machine; install an OpenSSH client.") from None
    return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")


def box(target, script, *args, stdin=b""):
    """Run `sh -c script` in the container; the stdin is where anything secret goes."""
    return ssh(target, "docker exec -i %s sh -c %s sh %s" % (CONTAINER, shlex.quote(script), shlex.join(args)), stdin)


def tail(text, lines=12):
    return "\n".join(text.strip().splitlines()[-lines:])


def need(result, what):
    rc, out, err = result
    if rc:
        raise Stop("%s failed on the server:\n%s" % (what, tail(err or out)))
    return out


def env_prefix(**values):
    return " ".join("%s=%s" % (k, shlex.quote(str(v))) for k, v in values.items())


# ---- laptop ---------------------------------------------------------------------

def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def store():
    return Path(os.environ.get("FACTORY_HOME") or "~/.claude/factory").expanduser()


def registry_path():
    return store() / "hosts.json"


def load_registry():
    try:
        return json.loads(registry_path().read_text())
    except FileNotFoundError:
        return {}


def save_registry(reg):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(reg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def ssh_config():
    return Path("~/.ssh/config").expanduser()


def block_re(name):
    return re.compile(r"\n?# isshoni host %s \(added by .*?\n.*?# end isshoni host %s\n?" % (re.escape(name), re.escape(name)), re.S)


def alias_block(name, alias, target, port):
    return ("# isshoni host %s (added by `isshoni host add`)\nHost %s\n  HostName 127.0.0.1\n  Port %s\n  ProxyJump %s\n"
            "  HostKeyAlias isshoni-%s\n# end isshoni host %s\n" % (name, alias, port, target, name, name))


def read_ssh_config():
    try:
        return ssh_config().read_text()
    except FileNotFoundError:
        return ""


def alias_conflict(name, alias):
    """A `Host <alias>` outside our own block for `name` is not ours to touch."""
    for line in block_re(name).sub("", read_ssh_config()).splitlines():
        m = re.match(r"\s*host(?:\s+|\s*=\s*)(.+)", line, re.I)
        if m and alias in m.group(1).split():
            raise Stop("~/.ssh/config already has a `Host %s` of its own; choose another --alias." % alias)


def write_ssh_config(text):
    path = ssh_config()
    if not path.exists():
        path.parent.mkdir(mode=0o700, exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    path.write_text(text)


def public_key(target, given):
    """(line, why-not): the key to authorise: --key, else the first `<identityfile>.pub` ssh would offer for the target."""
    if given:
        paths = [Path(given).expanduser()]
    else:
        out = subprocess.run(["ssh", "-G", target], capture_output=True, text=True, env=ssh_env()).stdout
        paths = [Path(l.split(None, 1)[1] + ".pub").expanduser() for l in out.splitlines() if l.lower().startswith("identityfile ")]
    for p in paths:
        if p.is_file():
            line = next((l.strip() for l in p.read_text().splitlines() if l.strip()), "")
            if line.startswith(KEY_TYPES) and len(line.split()) >= 2:
                return line, None
            if given:
                break
    return None, ("%s is not a public key file (one line: ssh-ed25519 AAAA... comment)." % given if given
                  else "no public key was found for this target: pass --key <file.pub>.")


# ---- the steps of add -----------------------------------------------------------

@dataclass
class Ctx:
    target: str
    name: str
    state: str
    port: int
    repos: list
    key_file: str | None
    alias: str | None
    head: str = ""
    docker_tree: str = ""
    pub: str = ""
    uid: str = "1000"
    gid: str = "1000"
    entry: dict = field(default_factory=dict)

    def env(self):
        return env_prefix(FACTORY_STATE=self.state, FACTORY_UID=self.uid, FACTORY_GID=self.gid, DASHBOARD_PORT=self.port)

    def save(self, rev):
        reg = load_registry()
        reg[self.name] = {"target": self.target, "state": self.state, "dashboard_port": self.port,
                          "repos": self.repos, "alias": self.alias, "rev": rev}
        save_registry(reg)


def prerequisites(c):
    missing = []
    try:
        c.head = git("rev-parse", "HEAD").decode().strip()
        c.docker_tree = git("rev-parse", "HEAD:docker").decode().strip()
        if git("status", "--porcelain").strip():
            print("note: the working tree has uncommitted changes; what is shipped is HEAD (%s)." % c.head[:7])
    except (OSError, subprocess.CalledProcessError):
        missing.append("this is not a git clone with a commit; run isshoni from a clone of the repository.")
    ident = store() / "identity"
    try:
        has_bot = bool(json.loads((ident / "app.json").read_text()).get("bot_login")) and (ident / "key.pem").is_file()
    except (OSError, ValueError, AttributeError):
        has_bot = False
    if not has_bot:
        missing.append("the GitHub App's identity (identity/key.pem and app.json with bot_login) is not in %s: run `make wizard` first." % store())
    c.pub, why = public_key(c.target, c.key_file)
    if why:
        missing.append(why)
    if ssh(c.target, "true")[0]:
        missing.append("`ssh %s true` fails: check that you can ssh to it without extra steps." % c.target)
    else:
        found = need(ssh(c.target, "sh -c %s sh %s" % (shlex.quote(SERVER_CHECK), c.port)), "checking the server").split()
        missing += [text % c.port if "%s" in text else text for word, text in SERVER_PROBLEMS.items() if word in found]
        if "noss" in found:
            print("note: port %s could not be checked, `ss` is missing on the server." % c.port)
    if missing:
        raise Stop("cannot start, missing:\n  - " + "\n  - ".join(missing))
    return False


def state_directory(c):
    made = ssh(c.target, "test -d %s" % shlex.quote(c.state))[0] != 0
    if made:
        need(ssh(c.target, "mkdir -p %s" % shlex.quote(c.state)), "creating the state directory")
    ids = need(ssh(c.target, "id -u; id -g"), "reading the user and group id").split()
    c.uid, c.gid = ids
    c.save(c.entry.get("rev"))
    return made


def image(c):
    label = ssh(c.target, "docker image inspect -f '{{index .Config.Labels \"isshoni.docker\"}}' %s" % IMAGE)
    if label[0] == 0 and label[1].strip() == c.docker_tree:
        return False
    context = git("archive", "HEAD:docker")
    need(ssh(c.target, "docker build -t %s --build-arg UID=%s --build-arg GID=%s --label isshoni.docker=%s -"
             % (IMAGE, c.uid, c.gid, c.docker_tree), context), "building the image")
    return True


def container(c):
    running = ssh(c.target, "docker inspect -f '{{.State.Running}} {{.Image}}' %s" % CONTAINER)
    built = ssh(c.target, "docker image inspect -f '{{.Id}}' %s" % IMAGE)
    if running[0] == 0 and built[0] == 0 and running[1].split() == ["true", built[1].strip()]:
        return False
    compose = git("show", "HEAD:docker/compose.yaml")
    need(ssh(c.target, "%s docker compose -p %s -f - up -d --no-build" % (c.env(), PROJECT), compose), "starting the container")
    return True


def source(c):
    if box(c.target, "cat ~/.isshoni/.rev")[1].strip() == c.head:
        return False
    need(box(c.target, SOURCE_PUT, c.head, stdin=git("archive", "HEAD")), "installing the source and the plugin")
    return True


def tar_of(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def identity(c):
    files = {n: (store() / "identity" / n).read_bytes() for n in ("key.pem", "app.json")}
    want = "\n".join("%s %s" % (hashlib.sha256(d).hexdigest(), n) for n, d in files.items())
    if box(c.target, IDENTITY_SUMS)[1].strip() == want:
        return False
    print("Copying the GitHub App's private key to the server (into the container's ~/.claude/factory/identity).")
    need(box(c.target, IDENTITY_PUT, stdin=tar_of(files)), "copying the identity")
    return True


def logged_in(c):
    return box(c.target, LOGIN_OK, LOGIN_PY)


def login(c):
    if logged_in(c)[0] == 0:
        return False
    token = os.environ.get(TOKEN_VAR)
    if token:
        print("Using %s from this machine's environment." % TOKEN_VAR)
    else:
        print("A one-year Claude token for this account is created now: you approve it once in the browser.\n"
              "It is stored on the server, inside the container, and can only make model requests.\n"
              "`claude setup-token` runs next and prints it; paste it when asked.")
        try:
            subprocess.run(["claude", "setup-token"], check=True)
        except (OSError, subprocess.CalledProcessError):
            raise Stop("`claude setup-token` did not finish; run it yourself and set %s, then run this again." % TOKEN_VAR) from None
        token = getpass.getpass("Paste the token printed above: ")
    token = token.strip()
    if not token:
        raise Stop("no token was given; run this again.")
    patch = json.dumps({"env": {TOKEN_VAR: token}}).encode()
    need(box(c.target, MERGE, MERGE_PY, "~/.claude/settings.json", stdin=patch), "storing the token")
    rc, out, err = logged_in(c)
    if rc:
        raise Stop("Claude Code on the server does not accept the token; it printed:\n%s"
                   % tail((err or out).replace(token, "***")))
    return True


def merge_settings(c):
    want = {"paths.monitor": MONITOR, "sessions.dispatchers": json.dumps(["dispatcher-" + r.split("/")[1] for r in c.repos])}
    changed = False
    for key, value in want.items():
        out = need(box(c.target, '"$HOME/.isshoni/factory/bin/factory" settings set "$1" "$2"', key, value), "writing the settings")
        changed |= out.strip() != "unchanged"
    return changed


def repositories(c):
    done = False
    for repo in c.repos:
        name = repo.split("/")[1]
        if box(c.target, 'test -d "$HOME/code/$1/.git"', name)[0] == 0:
            continue
        need(box(c.target, CLONE, repo, name), "cloning %s" % repo)
        done = True
    return done


def dashboard_key(c):
    changed = need(box(c.target, KEYS, KEYS_PY, c.pub), "authorising the key").strip() == "changed"
    if box(c.target, DASHBOARD_SERVING)[0] == 0:
        return changed
    need(ssh(c.target, SERVE), "starting the dashboard")
    for _ in range(30):
        if box(c.target, DASHBOARD_SERVING)[0] == 0:
            return True
        time.sleep(1)
    raise Stop("the dashboard is not serving; run it in the foreground to see why: `ssh -t %s \"docker exec -it %s sh -c 'exec ~/.isshoni/bin/isshoni-tui --serve 0.0.0.0:2222'\"`." % (c.target, CONTAINER))


def running_dispatchers(target):
    """The names of the sessions `claude agents --json` lists in the container; Stop when it cannot say."""
    rc, out, err = box(target, "claude agents --json")
    try:
        if rc == 0:
            return {a["name"] for a in json.loads(out) if "name" in a}
    except (ValueError, TypeError):
        pass
    raise Stop("cannot tell which dispatchers run: `claude agents --json` printed:\n%s" % tail(out + err))


def dispatchers(c):
    if not c.repos:
        print("No dispatcher was started: no repository is recorded. Add one with `isshoni host add %s --repo owner/name`." % c.target)
        return False
    started = False
    for repo in c.repos:
        name = "dispatcher-" + repo.split("/")[1]
        if name in running_dispatchers(c.target):
            continue
        need(box(c.target, 'cd "$HOME/code/$1" && claude --bg -n "$2" --permission-mode auto "/factory:dispatch"',
                 repo.split("/")[1], name), "starting %s" % name)
        if name not in running_dispatchers(c.target):
            raise Stop("%s was started but `claude agents --json` does not list it; see `ssh %s docker exec %s claude agents`."
                       % (name, c.target, CONTAINER))
        started = True
    return started


def connect(c):
    changed = False
    if c.alias is None and sys.stdin.isatty():
        answer = input("Add an ssh alias for this host to ~/.ssh/config? Name (empty for no): ").strip()
        if answer and not ALIAS_RE.fullmatch(answer):
            raise Stop("%r is not a usable alias." % answer)
        c.alias = answer or None
        if c.alias:
            alias_conflict(c.name, c.alias)
    if c.alias:
        text = read_ssh_config()
        new = block_re(c.name).sub("", text)
        new = new + ("\n" if new else "") + alias_block(c.name, c.alias, c.target, c.port)
        if new != text:
            write_ssh_config(new)
            changed = True
    print("Connect: " + ("ssh %s" % c.alias if c.alias else "ssh -J %s -p %s -o HostKeyAlias=isshoni-%s 127.0.0.1" % (c.target, c.port, c.name)))
    return changed


STEPS = (("prerequisites", prerequisites), ("state directory", state_directory), ("image", image), ("container", container),
         ("source and plugin", source), ("GitHub identity", identity), ("Claude login", login), ("settings", merge_settings),
         ("repositories", repositories), ("dashboard key", dashboard_key), ("dispatchers", dispatchers), ("connect", connect))


# ---- the verbs ------------------------------------------------------------------

def cmd_add(args):
    reg = load_registry()
    name = args.name or NAME_RE.sub("-", args.target)
    entry = reg.get(name, {})
    if args.target.startswith("-") or re.search(r"\s", args.target):
        raise Stop("%r is not an ssh target." % args.target)
    if not HOST_NAME_RE.fullmatch(name):
        raise Stop("%r is not a usable name: use letters, digits, _ and -." % name)
    if args.state and not args.state.startswith("/"):
        raise Stop("--state must be an absolute directory on the server, e.g. /srv/factory.")
    given_state = "/" + posixpath.normpath(args.state).lstrip("/") if args.state else None
    if entry and entry["target"] != args.target:
        raise Stop("host %s is recorded for target %s, not %s; use --name for another host." % (name, entry["target"], args.target))
    values = {}
    for key, given in (("state", given_state), ("dashboard_port", args.dashboard_port)):
        if entry and given is not None and given != entry[key]:
            raise Stop("host %s has %s %s and it cannot move: `isshoni host remove %s`, then add it again." % (name, key.replace("_", " "), entry[key], name))
        values[key] = entry.get(key) if entry else given
    state, port = values["state"], values["dashboard_port"] or 2222
    if not state:
        raise Stop("--state DIR is required the first time: the directory on the server that holds the factory's state, e.g. --state /srv/factory.")
    if state == "/":
        raise Stop("--state must not be the server's root: name a directory, e.g. /srv/factory.")
    repos = list(dict.fromkeys(entry.get("repos", []) + args.repo))
    names = [r.split("/")[-1] for r in repos]
    for r in repos:
        if not REPO_RE.fullmatch(r):
            raise Stop("%r is not a repository (owner/name)." % r)
    if len(set(names)) != len(names):
        raise Stop("two repositories have the same name; they would share ~/code/<name>.")
    alias = args.alias or entry.get("alias")
    if alias:
        if not ALIAS_RE.fullmatch(alias):
            raise Stop("%r is not a usable alias." % alias)
        alias_conflict(name, alias)
    c = Ctx(args.target, name, state, port, repos, args.key, alias, entry=entry)
    for k, (title, step) in enumerate(STEPS, 1):
        print("[%d/%d] %s: %s" % (k, len(STEPS), title, "done" if step(c) else "already"))
    c.save(c.head)
    return 0


def cmd_list(args):
    for name, e in load_registry().items():
        print("%s  %s  %s  %s  %s" % (name, e["target"], e["state"], ",".join(e["repos"]) or "-", e["alias"] or "-"))
    return 0


def host_status(name, e, head):
    """(lines, healthy)"""
    target = e["target"]
    if ssh(target, "true")[0]:
        return ["%s: unreachable over ssh" % name], False
    state = ssh(target, "docker inspect -f '{{.State.Status}}' %s" % CONTAINER)[1].strip() or "missing"
    rev = box(target, "cat ~/.isshoni/.rev")[1].strip()
    serving = box(target, DASHBOARD_SERVING)[0] == 0
    try:
        listed, why = running_dispatchers(target), None
    except Stop as stop:
        listed, why = set(), str(stop)
    lines = ["%s: container %s" % (name, state),
             "  rev %s (laptop HEAD %s)%s" % (rev[:7] or "none", head[:7], "" if rev == head else ": not current, run `isshoni host add %s`" % target),
             "  dashboard %s" % ("serving" if serving else "not serving")]
    missing = [r for r in e["repos"] if "dispatcher-" + r.split("/")[1] not in listed]
    for r in e["repos"]:
        lines.append("  dispatcher-%s %s" % (r.split("/")[1], "unknown" if why else "missing" if r in missing else "running"))
    if why:
        lines.append("  " + why.replace("\n", "\n  "))
    return lines, state == "running" and serving and not missing and not why


def cmd_status(args):
    reg = load_registry()
    if args.name and args.name not in reg:
        raise Stop("no host named %s; `isshoni host list` shows them." % args.name)
    head = git("rev-parse", "HEAD").decode().strip()
    healthy = True
    for name in [args.name] if args.name else reg:
        lines, ok = host_status(name, reg[name], head)
        print("\n".join(lines))
        healthy &= ok
    return 0 if healthy else 1


def cmd_remove(args):
    reg = load_registry()
    e = reg.get(args.name)
    if not e:
        raise Stop("no host named %s; `isshoni host list` shows them." % args.name)
    target, state = e["target"], e["state"]
    if ssh(target, "true")[0]:
        raise Stop("%s does not answer over ssh; nothing was removed. Bring it back, or edit %s by hand." % (target, registry_path()))
    env = env_prefix(FACTORY_STATE=state, DASHBOARD_PORT=e["dashboard_port"])
    need(ssh(target, "%s docker compose -p %s -f - down" % (env, PROJECT), git("show", "HEAD:docker/compose.yaml")), "removing the container")
    need(ssh(target, "if docker image inspect %s >/dev/null 2>&1; then docker image rm %s; fi" % (IMAGE, IMAGE)), "removing the image")
    if args.purge_state:
        need(ssh(target, "test ! -e %s || { chmod -R u+w %s && rm -rf %s; }" % ((shlex.quote(state),) * 3)), "deleting the state directory")
    text = read_ssh_config()
    if block_re(args.name).search(text):
        write_ssh_config(block_re(args.name).sub("", text))
    del reg[args.name]
    save_registry(reg)
    print("Removed host %s: container and image are gone. %s" % (args.name, "The state directory was deleted." if args.purge_state
          else "The state directory %s on the server is kept." % state))
    return 0


def run(args):
    try:
        return args.host_func(args)
    except Stop as e:
        print("isshoni: %s" % e, file=sys.stderr)
        return 1


def register(sub):
    p = sub.add_parser("host", help="onboard a remote Linux Docker host to run the factory container over SSH")
    p.set_defaults(func=run)
    verbs = p.add_subparsers(dest="verb", required=True)
    s = verbs.add_parser("add", help="bring the host to a running factory; rerun to do only what is missing")
    s.add_argument("target", help="what ssh takes: an alias, user@host or ssh://user@host:port")
    s.add_argument("--state", help="directory on the server for the factory's state (first time only)")
    s.add_argument("--name", help="the host's name here (default: the target)")
    s.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME", help="a repository to clone and start a dispatcher for (repeatable)")
    s.add_argument("--key", metavar="PUBKEY_FILE", help="the public key to authorise for the dashboard (default: the one ssh offers for the target)")
    s.add_argument("--dashboard-port", type=int, help="the server's loopback port for the dashboard (default 2222)")
    s.add_argument("--alias", help="write a Host block with this name to ~/.ssh/config")
    s.set_defaults(host_func=cmd_add)
    s = verbs.add_parser("list", help="the recorded hosts, without network")
    s.set_defaults(host_func=cmd_list)
    s = verbs.add_parser("status", help="container, revision, dashboard and dispatchers over ssh; exit 1 when one is not running")
    s.add_argument("name", nargs="?")
    s.set_defaults(host_func=cmd_status)
    s = verbs.add_parser("remove", help="remove the container, the image, the alias and the record; the state directory stays")
    s.add_argument("name")
    s.add_argument("--purge-state", action="store_true", help="also delete the state directory on the server")
    s.set_defaults(host_func=cmd_remove)
