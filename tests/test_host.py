"""isshoni host, against a fake ssh and a fake docker on PATH: the "server" is a directory, the "container" is a
shell with HOME in it and stubs for claude, git, make and pgrep first on PATH. The commands and scripts are the real ones."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ROOT  # noqa: F401  (puts the repository root on sys.path)
from isshoni import cli, host

SECRET_KEY = "-----BEGIN RSA PRIVATE KEY-----\nSECRETKEYMATERIAL\n-----END RSA PRIVATE KEY-----\n"
TOKEN = "sk-ant-oat01-SECRETTOKEN"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItestkey laptop"

# Records every call to the directory $FAKE_LOG (N.target, N.cmd, N.in), then runs the remote command in a local
# shell: the "server". Plain sh, because the tests make hundreds of calls.
FAKE_SSH = r"""#!/bin/sh
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN+set}" ] && [ -n "$FAKE_LOG" ]; then echo leaked >> "$FAKE_LOG/leak"; fi
if [ "$1" = -G ]; then [ -z "$FAKE_IDENTITY" ] || echo "identityfile $FAKE_IDENTITY"; exit 0; fi
if [ -n "$FAKE_LOG" ]; then
  n=$(( $(cat "$FAKE_LOG/n" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_LOG/n"
  printf %s "$1" > "$FAKE_LOG/$n.target"; printf %s "$2" > "$FAKE_LOG/$n.cmd"; cat > "$FAKE_LOG/$n.in"
  [ -e "$FAKE_DOWN" ] && exit 255
  exec sh -c "$2" < "$FAKE_LOG/$n.in"
fi
exec sh -c "$2"
"""

# Stands in for claude in the container: records its argv, keeps agents and plugins in $HOME/stub-state.json.
CLAUDE_STUB = r'''#!/usr/bin/env python3
import json, os, sys
home, a = os.environ["HOME"], sys.argv[1:]
with open(home + "/stub-claude.log", "a") as f:
    f.write(json.dumps({"argv": a, "cwd": os.getcwd()}) + "\n")
path = home + "/stub-state.json"
s = json.load(open(path)) if os.path.exists(path) else {"agents": [], "plugins": [], "marketplaces": []}
if a[:1] == ["--version"]:
    print("2.1.325 (Claude Code)")
elif a[:1] == ["-p"]:
    try:
        ok = json.load(open(home + "/.claude/settings.json"))["env"]["CLAUDE_CODE_OAUTH_TOKEN"]
    except (OSError, KeyError, ValueError):
        ok = None
    print("ok" if ok else "Not logged in")
    sys.exit(0 if ok else 1)
elif a[:2] == ["agents", "--json"] and os.path.exists(home + "/agents-broken"):
    print("supervisor unreachable")
    sys.exit(1)
elif a[:2] == ["agents", "--json"]:
    cwds = s.get("cwds", {})
    print(json.dumps([{"name": n, "kind": "background", "status": "busy", "pid": 1, **({"cwd": cwds[n]} if n in cwds else {})}
                      for n in s["agents"]]))
elif "--bg" in a:
    s["agents"].append(a[a.index("-n") + 1])
    s.setdefault("cwds", {})[a[a.index("-n") + 1]] = os.getcwd()
elif a[:2] == ["plugin", "list"]:
    print(json.dumps([{"id": p} for p in s["plugins"]]))
elif a[:3] == ["plugin", "marketplace", "list"]:
    print(json.dumps([{"name": m} for m in s["marketplaces"]]))
elif a[:3] == ["plugin", "marketplace", "add"]:
    s["marketplaces"].append("factory")
elif a[:2] == ["plugin", "install"]:
    s["plugins"].append(a[2])
json.dump(s, open(path, "w"))
'''

# Stands in for the factory script in the container: `dispatcher` answers from the agents of the claude stub;
# `settings set` is the real script's, on the server's own home (not the laptop's FACTORY_HOME).
FACTORY_STUB = r'''#!/usr/bin/env python3
import json, os, sys
home = os.environ["HOME"]
if sys.argv[1:2] == ["settings"]:
    os.execve(sys.executable, [sys.executable, "@REAL@", *sys.argv[1:]], {**os.environ, "FACTORY_HOME": home + "/.claude/factory"})
if sys.argv[1:] == ["dispatcher"]:
    if os.path.exists(home + "/agents-broken"):
        print("supervisor unreachable")
        sys.exit(2)
    agents = json.load(open(home + "/stub-state.json"))["agents"]
    if os.path.exists(home + "/stub-crash"):
        sys.exit(1)
    if "dispatcher-" + os.path.basename(os.getcwd()) in agents:
        sys.exit(0)
    print("factory: no dispatcher session", file=sys.stderr)
    sys.exit(1)
'''.replace("@REAL@", os.path.join(ROOT, "factory", "bin", "factory"))

FAKE_DOCKER = r"""#!/bin/sh
S=$FAKE_SRV
serve() {
  rm -f "$S/serving"
  [ -s "$S/home/.claude/factory/dashboard/authorized_keys" ] && [ -e "$S/home/.isshoni/bin/isshoni-tui" ] && touch "$S/serving"
  return 0
}
case "$1" in
info) [ ! -e "$S/nodocker" ];;
build)
  n=$(( $(ls "$S" | grep -c '^build-') + 1 )); cat > "$S/build-$n"
  for a; do case "$a" in isshoni.docker=*) label=${a#*=};; esac; done
  printf '%s\n%s\n' "$label" "sha256:img$n" > "$S/image"; echo "$*" > "$S/image.args";;
image)
  case "$2" in
  inspect) [ -e "$S/image" ] || exit 1; case "$*" in *Labels*) sed -n 1p "$S/image";; *) sed -n 2p "$S/image";; esac;;
  rm) rm "$S/image";;
  esac;;
inspect) [ -e "$S/container" ] || exit 1; case "$*" in *Status*) echo running;; *) echo "true $(cat "$S/container")";; esac;;
port) [ -e "$S/container" ];;
compose)
  case "$*" in
  *" up "*) n=$(( $(ls "$S" | grep -c '^up-') + 1 )); cat > "$S/up-$n"; echo "$FACTORY_STATE $DASHBOARD_PORT" >> "$S/up.log"
    sed -n 2p "$S/image" > "$S/container"; serve;;
  *" down"*) rm -f "$S/container";;
  esac;;
restart) serve;;
exec) if [ "$2" = -d ]; then serve; exit 0; fi; shift 3; HOME=$S/home PATH=$S/cbin:$PATH exec "$@";;
*) echo "fake docker: unexpected $*" >&2; exit 1;;
esac
"""

STUBS = {
    "claude": CLAUDE_STUB,
    "make": '#!/bin/sh\nmkdir -p "$2/bin" && touch "$2/bin/isshoni-tui" && chmod +x "$2/bin/isshoni-tui"\n',
    "pgrep": '#!/bin/sh\n[ -e "$FAKE_SRV/serving" ]\n',
    "git": '''#!/bin/sh
echo "$@" >> "$FAKE_SRV/git.log"
case "$1" in
-C) [ -e "$2/.git/HEAD" ];;
clone) case "$2" in *private*) exit 128;; esac; mkdir -p "$3/.git"; touch "$3/.git/HEAD";;
init) mkdir -p "$3/.git";;
fetch) [ ! -e "$FAKE_SRV/fetchfail" ];;
checkout) touch .git/HEAD;;
ls-remote) printf 'ref: refs/heads/main\\tHEAD\\n';;
esac
''',
}


def write_exe(path, text):
    Path(path).write_text(text)
    Path(path).chmod(0o755)


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


class HostTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.tmp, self.srv, self.laptop, self.fh, self.repo = tmp, tmp / "srv", tmp / "laptop", tmp / "fh", tmp / "clone"
        (self.tmp / "calls").mkdir()
        for d in (self.srv / "cbin", self.srv / "home", self.srv / "bin", self.laptop / ".ssh", self.fh / "identity", self.repo / "docker", self.repo / "factory/bin"):
            d.mkdir(parents=True)
        write_exe(self.srv / "bin/ssh", FAKE_SSH)
        write_exe(self.srv / "bin/docker", FAKE_DOCKER)
        write_exe(self.srv / "bin/uname", "#!/bin/sh\necho x86_64\n")
        write_exe(self.srv / "bin/ss", '#!/bin/sh\n[ -e "$FAKE_SRV/ssbusy" ] && echo "LISTEN 0 4096 127.0.0.1:2222 0.0.0.0:*"\nexit 0\n')
        for name, text in STUBS.items():
            write_exe(self.srv / "cbin" / name, text)
        (self.laptop / ".ssh/id_test.pub").write_text(PUB + "\n")
        (self.fh / "identity/key.pem").write_text(SECRET_KEY)
        (self.fh / "identity/app.json").write_text(json.dumps({"bot_login": "bot[bot]", "owner": "acme"}))
        for name, text in (("docker/Dockerfile", "FROM scratch\n"), ("docker/compose.yaml", "name: isshoni-factory\n"),
                           ("docker/start", "#!/bin/sh\n"), ("factory/bin/factory", FACTORY_STUB), ("Makefile", "install:\n")):
            (self.repo / name).write_text(text)
        (self.repo / "factory/bin/factory").chmod(0o755)
        sh(self.repo, "git", "init", "-q")
        self.commit("first")
        self.state = str(self.srv / "state")
        env = {"PATH": str(self.srv / "bin") + os.pathsep + os.environ["PATH"], "HOME": str(self.laptop), "FACTORY_HOME": str(self.fh),
               "FAKE_SRV": str(self.srv), "FAKE_LOG": str(self.tmp / "calls"), "FAKE_DOWN": str(self.srv / "down"),
               "FAKE_IDENTITY": "~/.ssh/id_test", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
        self.enterContext(mock.patch.dict(os.environ, env))
        self.enterContext(mock.patch.object(host, "ROOT", self.repo))
        self.enterContext(mock.patch.object(host.time, "sleep"))

    def commit(self, message):
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message)

    def run_host(self, *argv, tty=False):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO("")
        stdin.isatty = lambda: tty
        ask = mock.patch("builtins.input", side_effect=AssertionError("asked without a terminal")) if not tty else contextlib.nullcontext()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", stdin), ask:
            try:
                rc = cli.main(["host", *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def add(self, *extra, target="nas"):
        return self.run_host("add", target, "--state", self.state, *extra)

    def calls(self):
        d = self.tmp / "calls"
        n = int((d / "n").read_text()) if (d / "n").exists() else 0
        return [{"argv": [(d / ("%d.target" % i)).read_text(), (d / ("%d.cmd" % i)).read_text()], "stdin": (d / ("%d.in" % i)).read_bytes()}
                for i in range(1, n + 1)]

    def ssh_config(self):
        return (self.laptop / ".ssh/config").read_text()

    def steps(self, out):
        return [l.rsplit(": ", 1)[1] for l in out.splitlines() if l.startswith("[")]

    def test_the_life_of_a_host(self):
        """One host through add, a rerun, a new commit, a second repository, status and remove: the cases that
        only read what an earlier add left share it."""
        config = self.laptop / ".ssh/config"
        config.write_text("Host other\n  User me\n")
        with self.subTest("first run"):
            rc, out, err = self.add("--repo", "acme/widgets", "--alias", "factory")
            self.assertEqual((rc, err), (0, ""), out)
            self.assertEqual(self.steps(out)[1:], ["done"] * 11, out)
            self.assertIn("Connect: ssh factory", out)
            home = self.srv / "home"
            self.assertTrue((home / "code/widgets/.git").is_dir())
            head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual((home / ".isshoni/.rev").read_text().strip(), head)
            self.assertIn("dispatcher-widgets", (home / "stub-state.json").read_text())
            settings = tomllib.loads((home / ".claude/factory/settings.toml").read_text())
            self.assertEqual((settings["paths"]["monitor"], settings["sessions"]["dispatchers"]),
                             ("/state/home/.isshoni/bin/isshoni", ["dispatcher-widgets"]))
            self.assertFalse((home / ".claude/factory/settings.json").exists())
        with self.subTest("image and compose"):
            args = (self.srv / "image.args").read_text().split()
            self.assertEqual(args[-1], "-")
            self.assertIn("UID=%d" % os.getuid(), args)
            self.assertEqual(((self.srv / "up.log").read_text(), (self.srv / "up-1").read_text()), ("%s 2222\n" % self.state, "name: isshoni-factory\n"))
        with self.subTest("secrets only on stdin, never in output"):
            calls = self.calls()
            for secret in (TOKEN, "SECRETKEYMATERIAL"):
                self.assertFalse([c for c in calls if secret in json.dumps(c["argv"])], secret)
                self.assertNotIn(secret, out + err)
            stdins = [c["stdin"] for c in calls]
            self.assertTrue([x for x in stdins if TOKEN.encode() in x])
            self.assertTrue([x for x in stdins if b"SECRETKEYMATERIAL" in x])
            self.assertIn("private key", out)
            claude = home / ".claude"
            mode = lambda p: stat.S_IMODE(p.stat().st_mode)
            self.assertEqual([mode(claude / "factory/identity"), mode(claude / "factory/identity/key.pem"), mode(claude / "factory/identity/app.json"), mode(claude / "settings.json")],
                             [0o700, 0o600, 0o600, 0o600])
            self.assertEqual((claude / "factory/identity/key.pem").read_text(), SECRET_KEY)
        with self.subTest("registry"):
            path = self.fh / "hosts.json"
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            e = json.loads(path.read_text())["nas"]
            self.assertEqual((e["target"], e["state"], e["dashboard_port"], e["repos"], e["alias"]), ("nas", self.state, 2222, ["acme/widgets"], "factory"))
            self.assertEqual(len(e["rev"]), 40)
        with self.subTest("second run does nothing"):
            builds, ups = len(list(self.srv.glob("build-*"))), len(list(self.srv.glob("up-*")))
            rc, out, err = self.add("--repo", "acme/widgets", "--alias", "factory")
            self.assertEqual((rc, err, self.steps(out)), (0, "", ["already"] * 12), out)
            self.assertEqual((len(list(self.srv.glob("build-*"))), len(list(self.srv.glob("up-*")))), (builds, ups))
            self.assertEqual(self.ssh_config().count("Host factory\n"), 1)
            self.assertIn("ProxyJump nas", self.ssh_config())
            self.assertFalse((self.tmp / "calls/leak").exists(), "the Claude token reached an ssh child")
        with self.subTest("when claude cannot list the dispatchers nothing is started"):
            (self.srv / "home/agents-broken").touch()
            started = len([c for c in self.calls() if "--bg" in c["argv"][1]])
            rc, out, err = self.add("--repo", "acme/widgets", "--alias", "factory")
            self.assertEqual(rc, 1)
            self.assertIn("cannot tell which dispatchers run", err)
            self.assertIn("supervisor unreachable", err)
            self.assertEqual(len([c for c in self.calls() if "--bg" in c["argv"][1]]), started)
            rc, out, _ = self.run_host("status")
            self.assertEqual(rc, 1)
            self.assertIn("dispatcher-widgets unknown", out)
            self.assertNotIn("missing", out)
            (self.srv / "home/agents-broken").unlink()
            (self.srv / "home/stub-crash").touch()
            rc, out, _ = self.run_host("status")
            self.assertIn("dispatcher-widgets unknown", out)
            (self.srv / "home/stub-crash").unlink()
        with self.subTest("list needs no network, status follows what runs"):
            n = len(self.calls())
            self.assertEqual(self.run_host("list"), (0, "nas  nas  %s  acme/widgets  factory\n" % self.state, ""))
            self.assertEqual(len(self.calls()), n)
            rc, out, _ = self.run_host("status")
            self.assertEqual(rc, 0, out)
            self.assertIn("dispatcher-widgets running", out)
        with self.subTest("a new commit and a second repository"):
            (self.repo / "Makefile").write_text("install:\n\techo new\n")
            self.commit("second")
            rc, out, err = self.add("--repo", "acme/gadgets", "--alias", "factory")
            self.assertEqual((rc, err), (0, ""), out)
            self.assertEqual(self.steps(out)[1:], ["already"] * 3 + ["done"] + ["already"] * 2 + ["done"] * 2 + ["already", "done", "already"], out)
            argvs = [json.loads(l)["argv"] for l in (self.srv / "home/stub-claude.log").read_text().splitlines()]
            self.assertIn(["plugin", "update", "factory@factory"], argvs)
            self.assertIn(["plugin", "marketplace", "update", "factory"], argvs)
            self.assertEqual(json.loads((self.fh / "hosts.json").read_text())["nas"]["repos"], ["acme/widgets", "acme/gadgets"])
        with self.subTest("state cannot change, names must be unique, state is required"):
            rc, _, err = self.run_host("add", "nas", "--state", "/elsewhere")
            self.assertEqual(rc, 1)
            self.assertIn("cannot move", err)
            self.assertEqual(self.add("--repo", "a/x", "--repo", "b/x")[0], 1)
            self.assertEqual(self.run_host("add", "other")[0], 1)
            self.assertEqual(self.run_host("add", "nas", "--state", "/" + self.state + "/sub/../")[0], 0)  # the same directory, written another way
        with self.subTest("status and remove when something does not answer"):
            (self.srv / "home/stub-state.json").write_text(json.dumps({"agents": [], "plugins": [], "marketplaces": []}))
            rc, out, _ = self.run_host("status", "nas")
            self.assertEqual(rc, 1)
            self.assertIn("dispatcher-widgets missing", out)
            self.assertEqual(self.run_host("status", "nope")[0], 1)
            (self.srv / "down").touch()
            self.assertEqual(self.run_host("status")[0], 1)
            rc, _, err = self.run_host("remove", "nas")
            self.assertEqual(rc, 1)
            self.assertIn("nothing was removed", err)
            self.assertIn("nas", json.loads((self.fh / "hosts.json").read_text()))
            self.assertIn("Host factory", self.ssh_config())
            (self.srv / "down").unlink()
        with self.subTest("remove keeps the state directory unless purged"):
            n = len(self.calls())
            rc, out, _ = self.run_host("remove", "nas")
            self.assertEqual(rc, 0)
            self.assertIn(self.state, out)
            removal = [c["argv"][1] for c in self.calls()[n:]]
            self.assertFalse([c for c in removal if "rm -rf" in c or self.state in c.replace("FACTORY_STATE=" + self.state, "")])
            self.assertTrue(Path(self.state).is_dir())
            self.assertFalse((self.srv / "image").exists() or (self.srv / "container").exists())
            self.assertEqual(json.loads((self.fh / "hosts.json").read_text()), {})
            self.assertEqual(self.ssh_config(), "Host other\n  User me\n")
        with self.subTest("purge of a state directory that is already gone succeeds"):
            host.save_registry({"ghost": {"target": "nas", "state": str(self.tmp / "gone"), "dashboard_port": 2222, "repos": [], "alias": None, "rev": None}})
            self.assertEqual(self.run_host("remove", "ghost", "--purge-state")[0], 0)
            self.assertEqual(json.loads((self.fh / "hosts.json").read_text()), {})
        with self.subTest("purge deletes it"):
            self.add("--repo", "acme/widgets")
            self.assertEqual(self.run_host("remove", "nas", "--purge-state")[0], 0)
            self.assertTrue([c for c in self.calls() if "rm -rf %s" % self.state in c["argv"][1]])
            self.assertFalse(Path(self.state).exists())

    def test_missing_prerequisites_are_all_reported_and_nothing_changes(self):
        (self.fh / "identity/key.pem").unlink()
        (self.srv / "nodocker").touch()
        (self.laptop / ".ssh/id_test.pub").unlink()
        (self.srv / "ssbusy").touch()
        rc, out, err = self.add()
        self.assertEqual((rc, out), (1, ""))
        for text in ("make wizard", "without sudo", "--key", "port 2222 is in use"):
            self.assertIn(text, err)
        self.assertFalse((self.fh / "hosts.json").exists())
        self.assertFalse(Path(self.state).exists())
        self.assertEqual([c for c in self.calls() if "build" in c["argv"][1] or " up " in c["argv"][1]], [])

    def test_unreachable_server_is_a_missing_prerequisite(self):
        (self.srv / "down").touch()
        rc, _, err = self.add()
        self.assertEqual(rc, 1)
        self.assertIn("ssh nas true", err)

    def test_bad_arguments_are_refused_before_anything_is_done(self):
        for argv in (["--repo", "a/.."], ["--repo", "./x"], ["--repo=-a/b"], ["--repo=a/-b"], ["--repo", "a/."],
                     ["--name", "bad name"], ["--name", "a.b"], ["--state", "relative/dir"], ["--state", "/srv/.."], ["--state", "//"]):
            with self.subTest(argv):
                self.assertEqual(self.run_host("add", "nas", *([] if "--state" in argv else ["--state", self.state]), *argv)[0], 1)
        self.assertEqual(self.run_host("add", "a b", "--state", self.state)[0], 1)
        self.assertEqual(self.calls(), [])

    def test_a_failed_private_fetch_is_not_taken_for_a_clone(self):
        (self.srv / "fetchfail").touch()
        rc, _, err = self.add("--repo", "acme/private-thing")
        self.assertEqual(rc, 1)
        self.assertEqual(list((self.srv / "home/code").glob("private-thing*")), [])
        (self.srv / "fetchfail").unlink()
        rc, out, err = self.add("--repo", "acme/private-thing")
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.srv / "home/code/private-thing/.git/HEAD").exists())

    def test_scratch_clone_never_touches_another_repository_and_empty_ones_are_kept(self):
        code = self.srv / "home/code"
        (code / "x.new/.git").mkdir(parents=True)
        (code / "x.new/.git/HEAD").touch()
        (code / "x.new/work").write_text("mine")
        (code / "empty/.git").mkdir(parents=True)  # a repository with no commit yet
        self.assertEqual(self.add("--repo", "acme/x", "--repo", "acme/empty")[0], 0)
        self.assertEqual((code / "x.new/work").read_text(), "mine")
        self.assertTrue((code / "x/.git/HEAD").exists())
        self.assertEqual(self.steps(self.add("--repo", "acme/x", "--repo", "acme/empty")[1]), ["already"] * 12)

    def test_a_leftover_directory_that_is_not_a_checkout_is_refused(self):
        (self.srv / "home/code/widgets").mkdir(parents=True)
        (self.srv / "home/code/widgets/notes").write_text("mine")
        rc, _, err = self.add("--repo", "acme/widgets")
        self.assertEqual(rc, 1)
        self.assertIn("not a checkout", err)
        self.assertEqual((self.srv / "home/code/widgets/notes").read_text(), "mine")

    def test_server_without_ss_says_the_port_was_not_checked(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        for tool in ("docker", "uname"):
            (bare / tool).symlink_to(self.srv / "bin" / tool)
        (bare / "grep").symlink_to(shutil.which("grep"))
        out = subprocess.run(["/bin/sh", "-c", host.SERVER_CHECK, "sh", "2222"], env={**os.environ, "PATH": str(bare)}, capture_output=True, text=True).stdout
        self.assertEqual(out.split(), ["noss"])
        with mock.patch.object(host, "SERVER_CHECK", "echo noss"):
            rc, out, err = self.add()
        self.assertEqual(rc, 0, err)
        self.assertIn("port 2222 could not be checked", out)

    def test_alias_refused_elsewhere_and_created_0600(self):
        config = self.laptop / ".ssh/config"
        config.write_text("Host factory\n  HostName example.invalid\n")
        rc, _, err = self.add("--alias", "factory")
        self.assertEqual(rc, 1)
        self.assertIn("Host factory", err)
        self.assertEqual(config.read_text(), "Host factory\n  HostName example.invalid\n")
        self.assertEqual(self.calls(), [])
        config.unlink()
        self.add("--alias", "factory")
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_token_pasted_with_getpass_and_alias_asked_on_a_terminal(self):
        del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        write_exe(self.srv / "bin/claude", "#!/bin/sh\nexit 0\n")
        with mock.patch.object(host.getpass, "getpass", return_value=TOKEN + "\n"), mock.patch("builtins.input", return_value="box"):
            rc, out, err = self.run_host("add", "nas", "--state", self.state, tty=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(TOKEN, out + err)
        self.assertEqual(json.loads((self.srv / "home/.claude/settings.json").read_text())["env"]["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
        self.assertIn("ssh box", out)
        self.assertIn("Host box", self.ssh_config())

    def wizard(self, verb, *answers):
        """`host <verb>` with its arguments left out, on a terminal that gives `answers`."""
        with mock.patch("builtins.input", side_effect=list(answers)) as asked:
            rc, out, err = self.run_host(verb, tty=True)
        self.assertEqual(asked.call_count, len(answers), "asked a different number of questions than answered")
        return rc, out, err

    def registry(self):
        return json.loads((self.fh / "hosts.json").read_text())

    def test_add_without_arguments_asks_for_them_and_runs_the_same_steps(self):
        rc, out, err = self.wizard("add", "nas", self.state, "acme/widgets", "factory", "")  # the alias is asked once
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("bin/isshoni host add nas --state %s --repo acme/widgets --alias factory" % self.state, out)
        self.assertEqual(self.steps(out)[1:], ["done"] * 11, out)
        e = self.registry()["nas"]
        self.assertEqual((e["target"], e["state"], e["dashboard_port"], e["repos"], e["alias"]), ("nas", self.state, 2222, ["acme/widgets"], "factory"))
        self.assertIn("Host factory", self.ssh_config())
        with self.subTest("a registered name is all it asks, and the rerun does nothing"):
            rc, out, err = self.wizard("add", "nas", "")
            self.assertEqual((rc, err, self.steps(out)), (0, "", ["already"] * 12), out)
            self.assertIn("  nas  nas\n", out)
            self.assertIn("bin/isshoni host add nas\n", out)
            self.assertEqual(self.registry()["nas"]["alias"], "factory")

    def test_an_empty_alias_answer_is_final_and_a_declined_run_does_nothing(self):
        rc, out, err = self.wizard("add", "nas", self.state, "", "", "")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("--state %s --alias ''\n" % self.state, out)
        self.assertIn("Connect: ssh -J nas", out)
        self.assertIsNone(self.registry()["nas"]["alias"])
        self.assertEqual(self.registry()["nas"]["repos"], [])
        (self.fh / "hosts.json").unlink()
        n = len(self.calls())
        rc, out, err = self.wizard("add", "other", self.state, "acme/widgets", "", "n")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("Nothing was done.", out)
        self.assertEqual(len(self.calls()), n)
        self.assertFalse((self.fh / "hosts.json").exists())

    def test_the_printed_command_carries_the_name_of_a_host_registered_with_one(self):
        self.assertEqual(self.add("--name", "box")[0], 0)
        rc, out, err = self.wizard("add", "box", "")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("bin/isshoni host add nas --name box --alias ''\n", out)

    def test_an_empty_alias_removes_the_block_and_asks_nothing(self):
        self.assertEqual(self.add("--alias", "factory")[0], 0)
        self.assertIn("Host factory", self.ssh_config())
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            rc, out, err = self.run_host("add", "nas", "--state", self.state, "--alias", "", tty=True)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertNotIn("Host factory", self.ssh_config())
        self.assertIsNone(self.registry()["nas"]["alias"])

    def test_add_without_a_target_and_without_a_terminal_stops(self):
        rc, out, err = self.run_host("add")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("TARGET", err)
        self.assertIn("make wizard-remote", err)
        self.assertEqual(self.calls(), [])

    def test_remove_without_a_name_asks_which_and_whether_to_purge(self):
        self.assertEqual(self.add()[0], 0)
        rc, out, err = self.wizard("remove", "nas", "", "")  # the default answers are no
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("bin/isshoni host remove nas\n", out)
        self.assertIn("Nothing was done.", out)
        self.assertIn("nas", self.registry())
        rc, out, err = self.wizard("remove", "nas", "y", "n")
        self.assertIn("Nothing was done.", out)
        self.assertIn("nas", self.registry())
        rc, out, err = self.wizard("remove", "nas", "y", "y")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("bin/isshoni host remove nas --purge-state", out)
        self.assertEqual(self.registry(), {})
        self.assertFalse(Path(self.state).exists())
        self.assertIn("make uninstall", out)  # the last host, identity here, no install here

    def test_remove_without_a_name_when_none_is_given_or_registered(self):
        self.assertEqual(self.run_host("remove"), (0, "No host is registered.\n", ""))
        self.assertEqual(self.add()[0], 0)
        rc, out, err = self.run_host("remove")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("make uninstall-remote", err)
        rc, out, err = self.wizard("remove", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("no host named nope", err)

    def test_the_last_remove_points_to_make_uninstall_only_when_nothing_else_uses_the_app(self):
        line = "No install uses the GitHub App any more"
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.add(target="nas2")[0], 0)
        self.assertNotIn(line, self.run_host("remove", "nas")[1])  # another host is left
        (self.fh / "settings.json").write_text("{}")
        self.assertNotIn(line, self.run_host("remove", "nas2")[1])  # an install here
        (self.fh / "settings.json").unlink()
        self.assertEqual(self.add()[0], 0)
        app = self.fh / "identity/app.json"
        kept = app.read_text()
        app.unlink()
        self.assertNotIn(line, self.run_host("remove", "nas")[1])  # no identity here
        app.write_text(kept)
        self.assertEqual(self.add()[0], 0)
        self.assertIn(line, self.run_host("remove", "nas")[1])

    def test_the_server_gets_a_copy_of_the_identity_marked_as_one(self):
        self.assertEqual(self.add()[0], 0)
        copied = json.loads((self.srv / "home/.claude/factory/identity/app.json").read_text())
        self.assertEqual(copied, {"bot_login": "bot[bot]", "owner": "acme", "copy": True})
        self.assertNotIn("copy", json.loads((self.fh / "identity/app.json").read_text()))
        self.assertEqual(self.steps(self.add()[1]), ["already"] * 12)

    def test_wrong_token_is_reported_without_echoing_it(self):
        (self.srv / "cbin/claude").rename(self.srv / "cbin/claude-real")
        write_exe(self.srv / "cbin/claude", """#!/bin/sh
if [ "$1" = -p ]; then
  [ -e "$HOME/.claude/settings.json" ] && echo "rejected $(cat "$HOME/.claude/settings.json")"; exit 1
fi
exec "$(dirname "$0")/claude-real" "$@"
""")
        rc, out, err = self.add()
        self.assertEqual(rc, 1)
        self.assertIn("does not accept the token", err)
        self.assertNotIn(TOKEN, out + err)

    def test_existing_settings_and_keys_are_kept_and_a_private_repository_is_fetched_as_the_app(self):
        claude, factory = self.srv / "home/.claude", self.srv / "home/.claude/factory"
        keys = factory / "dashboard/authorized_keys"
        keys.parent.mkdir(parents=True)
        keys.write_text("ssh-ed25519 AAAAother bob\n" + PUB + "\n")
        (claude / "settings.json").write_text(json.dumps({"theme": "dark", "env": {"A": "b"}}))
        (factory / "settings.json").write_text(json.dumps({"worktree_cap": 3, "monitor": "old"}))  # an install from before settings.toml
        keys.rename(keys.with_name("keys-aside"))
        keys.mkdir()  # unreadable as a file: not the same as missing, and no key may be dropped over it
        rc, out, err = self.add("--repo", "acme/widgets", "--repo", "acme/private-thing")
        self.assertEqual(rc, 1)
        self.assertIn("authorising the key", err)
        keys.rmdir()
        keys.with_name("keys-aside").rename(keys)
        rc, out, err = self.add("--repo", "acme/widgets", "--repo", "acme/private-thing")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads((claude / "settings.json").read_text()),
                         {"theme": "dark", "env": {"A": "b", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}})
        settings = tomllib.loads((factory / "settings.toml").read_text())
        self.assertEqual((settings["limits"]["worktree_cap"], settings["paths"]["monitor"], settings["sessions"]["dispatchers"]),
                         (3, "/state/home/.isshoni/bin/isshoni", ["dispatcher-widgets", "dispatcher-private-thing"]))
        self.assertEqual(json.loads((factory / "settings.json").read_text()), {"worktree_cap": 3, "monitor": "old"})  # left as it was
        self.assertEqual(keys.read_text(), "ssh-ed25519 AAAAother bob\nwrite " + PUB + "\n")
        self.assertTrue((self.srv / "serving").exists())
        calls = [c["argv"][1] for c in self.calls()]
        self.assertFalse([c for c in calls if "docker restart" in c], "the container must never be restarted")
        serve = [i for i, c in enumerate(calls) if "docker exec -d isshoni-factory sh -c" in c]
        starts = [i for i, c in enumerate(calls) if "--bg" in c]
        self.assertTrue(serve and starts and max(serve) < min(starts))
        log = (self.srv / "git.log").read_text()
        self.assertIn("remote add origin https://github.com/acme/private-thing", log)
        self.assertIn("checkout -B main origin/main", log)


if __name__ == "__main__":
    unittest.main()
