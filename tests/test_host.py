"""isshoni host, against a fake ssh and a fake docker on PATH: the "server" is a directory, the "container" is a
shell with HOME in it and stubs for claude, git, make and pgrep first on PATH. The commands and scripts are the real ones."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
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
elif a[:2] == ["agents", "--json"]:
    print(json.dumps([{"name": n, "kind": "background"} for n in s["agents"]]))
elif "--bg" in a:
    s["agents"].append(a[a.index("-n") + 1])
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
exec) shift 3; HOME=$S/home PATH=$S/cbin:$PATH exec "$@";;
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
clone) case "$2" in *private*) exit 128;; esac; mkdir -p "$3/.git";;
init) mkdir -p "$3/.git";;
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
        for name, text in STUBS.items():
            write_exe(self.srv / "cbin" / name, text)
        (self.laptop / ".ssh/id_test.pub").write_text(PUB + "\n")
        (self.fh / "identity/key.pem").write_text(SECRET_KEY)
        (self.fh / "identity/app.json").write_text(json.dumps({"bot_login": "bot[bot]", "owner": "acme"}))
        for name, text in (("docker/Dockerfile", "FROM scratch\n"), ("docker/compose.yaml", "name: isshoni-factory\n"),
                           ("docker/start", "#!/bin/sh\n"), ("factory/bin/factory", "#!/bin/sh\n"), ("Makefile", "install:\n")):
            (self.repo / name).write_text(text)
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
        with self.subTest("purge deletes it"):
            self.add("--repo", "acme/widgets")
            self.assertEqual(self.run_host("remove", "nas", "--purge-state")[0], 0)
            self.assertTrue([c for c in self.calls() if "rm -rf %s" % self.state in c["argv"][1]])
            self.assertFalse(Path(self.state).exists())

    def test_missing_prerequisites_are_all_reported_and_nothing_changes(self):
        (self.fh / "identity/key.pem").unlink()
        (self.srv / "nodocker").touch()
        (self.laptop / ".ssh/id_test.pub").unlink()
        rc, out, err = self.add()
        self.assertEqual((rc, out), (1, ""))
        for text in ("make wizard", "without sudo", "--key"):
            self.assertIn(text, err)
        self.assertFalse((self.fh / "hosts.json").exists())
        self.assertFalse(Path(self.state).exists())
        self.assertEqual([c for c in self.calls() if "build" in c["argv"][1] or " up " in c["argv"][1]], [])

    def test_unreachable_server_is_a_missing_prerequisite(self):
        (self.srv / "down").touch()
        rc, _, err = self.add()
        self.assertEqual(rc, 1)
        self.assertIn("ssh nas true", err)

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
        keys.write_text(PUB + "\n")
        (claude / "settings.json").write_text(json.dumps({"theme": "dark", "env": {"A": "b"}}))
        (factory / "settings.json").write_text(json.dumps({"worktree_cap": 3, "monitor": "old"}))
        rc, out, err = self.add("--repo", "acme/widgets", "--repo", "acme/private-thing")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads((claude / "settings.json").read_text()),
                         {"theme": "dark", "env": {"A": "b", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}})
        self.assertEqual(json.loads((factory / "settings.json").read_text()),
                         {"worktree_cap": 3, "monitor": "/state/home/.isshoni/bin/isshoni", "dispatchers": ["dispatcher-widgets", "dispatcher-private-thing"]})
        self.assertEqual(keys.read_text(), "write " + PUB + "\n")
        self.assertTrue((self.srv / "serving").exists())
        restarts = [i for i, c in enumerate(self.calls()) if "docker restart" in c["argv"][1]]
        starts = [i for i, c in enumerate(self.calls()) if "--bg" in c["argv"][1]]
        self.assertTrue(restarts and starts and max(restarts) < min(starts))
        log = (self.srv / "git.log").read_text()
        self.assertIn("remote add origin https://github.com/acme/private-thing", log)
        self.assertIn("checkout -B main origin/main", log)

    def test_authorised_adds_keeps_and_upgrades(self):
        other = "ssh-ed25519 AAAAother bob"
        self.assertEqual(host.authorised([other], PUB), [other, "write " + PUB])
        self.assertEqual(host.authorised([other, "write " + PUB], PUB), [other, "write " + PUB])
        self.assertEqual(host.authorised([PUB, other], PUB), ["write " + PUB, other])


if __name__ == "__main__":
    unittest.main()
