"""isshoni host, against a fake ssh and a fake docker on PATH: the "server" is a directory, the "container" is a
shell with HOME in it and stubs for claude, git, make and pgrep first on PATH. The commands and scripts are the real ones."""
import base64
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

# Records every call (argv, stdin) to $FAKE_LOG, then runs the remote command in a local shell: the "server".
FAKE_SSH = r'''#!/usr/bin/env python3
import base64, json, os, subprocess, sys
a = sys.argv[1:]
down = os.environ.get("FAKE_DOWN")
if a[0] == "-G":
    if os.environ.get("FAKE_IDENTITY"):
        print("identityfile " + os.environ["FAKE_IDENTITY"])
    sys.exit(0)
data = sys.stdin.buffer.read()
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": a, "stdin": base64.b64encode(data).decode()}) + "\n")
if down and os.path.exists(down):
    sys.exit(255)
sys.exit(subprocess.run(["sh", "-c", a[1]], input=data).returncode)
'''

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

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
a, srv = sys.argv[1:], Path(os.environ["FAKE_SRV"])
img, ctr = srv / "image.json", srv / "container.json"
keys = srv / "home/.claude/factory/dashboard/authorized_keys"
def log(kind, **kw):
    with open(srv / "docker.log", "a") as f:
        f.write(json.dumps(dict(kind=kind, **kw)) + "\n")
def serve():
    ok = keys.exists() and keys.read_text().strip() and (srv / "home/code/isshoni/bin/isshoni-tui").exists()
    (srv / "serving").unlink(missing_ok=True)
    if ok:
        (srv / "serving").touch()
if a[0] == "info":
    sys.exit(1 if (srv / "nodocker").exists() else 0)
if a[:2] == ["compose", "version"]:
    sys.exit(0)
if a[0] == "build":
    n = len(list(srv.glob("build-*"))) + 1
    label = a[a.index("--label") + 1].split("=", 1)[1]
    (srv / ("build-%d" % n)).write_bytes(sys.stdin.buffer.read())
    img.write_text(json.dumps({"label": label, "id": "sha256:img%d" % n, "args": a}))
    sys.exit(0)
if a[:2] == ["image", "inspect"]:
    if not img.exists():
        sys.exit(1)
    d = json.loads(img.read_text())
    print(d["label"] if "Labels" in " ".join(a) else d["id"])
    sys.exit(0)
if a[:2] == ["image", "rm"]:
    img.unlink()
    sys.exit(0)
if a[0] == "inspect":
    if not ctr.exists():
        sys.exit(1)
    print("running" if "Status" in a[2] else "true " + json.loads(ctr.read_text())["image"])
    sys.exit(0)
if a[0] == "port":
    sys.exit(0 if ctr.exists() else 1)
if a[0] == "compose" and "up" in a:
    log("up", stdin=sys.stdin.read(), state=os.environ["FACTORY_STATE"], port=os.environ["DASHBOARD_PORT"])
    ctr.write_text(json.dumps({"image": json.loads(img.read_text())["id"]}))
    serve()
    sys.exit(0)
if a[0] == "compose" and "down" in a:
    log("down", state=os.environ["FACTORY_STATE"])
    ctr.unlink(missing_ok=True)
    sys.exit(0)
if a[0] == "restart":
    serve()
    sys.exit(0)
if a[0] == "exec":
    env = dict(os.environ, HOME=str(srv / "home"), PATH=str(srv / "cbin") + os.pathsep + os.environ["PATH"])
    sys.exit(subprocess.run(a[3:], env=env).returncode)
sys.exit("fake docker: unexpected " + " ".join(a))
'''

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
               "FAKE_SRV": str(self.srv), "FAKE_LOG": str(self.tmp / "ssh.log"), "FAKE_DOWN": str(self.srv / "down"),
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
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", stdin):
            try:
                rc = cli.main(["host", *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def add(self, *extra, target="nas"):
        return self.run_host("add", target, "--state", self.state, *extra)

    def calls(self):
        path = self.tmp / "ssh.log"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def ssh_config(self):
        return (self.laptop / ".ssh/config").read_text()

    def steps(self, out):
        return [l.rsplit(": ", 1)[1] for l in out.splitlines() if l.startswith("[")]

    def test_first_run_does_everything_and_second_does_nothing(self):
        rc, out, err = self.add("--repo", "acme/widgets")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(self.steps(out)[1:], ["done"] * 10 + ["already"], out)  # connect adds nothing without an alias
        self.assertIn("ssh -J nas -p 2222 -o HostKeyAlias=isshoni-nas 127.0.0.1", out)
        home = self.srv / "home"
        self.assertTrue((home / "code/widgets/.git").is_dir())
        self.assertEqual((home / "code/isshoni/.rev").read_text().strip(), subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip())
        self.assertIn("dispatcher-widgets", (home / "stub-state.json").read_text())
        builds, ups = len(list(self.srv.glob("build-*"))), (self.srv / "docker.log").read_text().count('"up"')
        rc, out, err = self.add("--repo", "acme/widgets")
        self.assertEqual((rc, err, self.steps(out)), (0, "", ["already"] * 12), out)
        self.assertEqual((len(list(self.srv.glob("build-*"))), (self.srv / "docker.log").read_text().count('"up"')), (builds, ups))

    def test_image_args_and_compose_environment(self):
        self.add()
        args = json.loads((self.srv / "image.json").read_text())["args"]
        self.assertEqual(args[-1], "-")
        self.assertIn("UID=%d" % os.getuid(), args)
        up = json.loads((self.srv / "docker.log").read_text().splitlines()[0])
        self.assertEqual((up["state"], up["port"], up["stdin"]), (self.state, "2222", "name: isshoni-factory\n"))

    def test_new_commit_reships_source_and_updates_the_plugin(self):
        self.add("--repo", "acme/widgets")
        (self.repo / "Makefile").write_text("install:\n\techo new\n")
        self.commit("second")
        rc, out, _ = self.add()
        self.assertEqual(self.steps(out)[1:], ["already"] * 3 + ["done"] + ["already"] * 7, out)
        argvs = [json.loads(l)["argv"] for l in (self.srv / "home/stub-claude.log").read_text().splitlines()]
        self.assertIn(["plugin", "update", "factory@factory"], argvs)
        self.assertIn(["plugin", "marketplace", "update", "factory"], argvs)

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
        self.assertEqual([c["argv"] for c in self.calls() if "docker" in c["argv"][1] and ("build" in c["argv"][1] or "up" in c["argv"][1])], [])

    def test_unreachable_server_is_a_missing_prerequisite(self):
        (self.srv / "down").touch()
        rc, _, err = self.add()
        self.assertEqual(rc, 1)
        self.assertIn("ssh nas true", err)

    def test_secrets_only_on_stdin_and_never_in_output(self):
        rc, out, err = self.add("--repo", "acme/widgets")
        self.assertEqual(rc, 0)
        calls = self.calls()
        for secret in (TOKEN, "SECRETKEYMATERIAL"):
            self.assertFalse([c for c in calls if secret in json.dumps(c["argv"])], secret)
            self.assertNotIn(secret, out + err)
        stdins = [base64.b64decode(c["stdin"]) for c in calls]
        self.assertTrue([s for s in stdins if TOKEN.encode() in s])
        self.assertTrue([s for s in stdins if b"SECRETKEYMATERIAL" in s])
        self.assertIn("private key", out)
        home = self.srv / "home/.claude"
        mode = lambda p: stat.S_IMODE(p.stat().st_mode)
        self.assertEqual([mode(home / "factory/identity"), mode(home / "factory/identity/key.pem"), mode(home / "factory/identity/app.json"), mode(home / "settings.json")],
                         [0o700, 0o600, 0o600, 0o600])
        self.assertEqual((home / "factory/identity/key.pem").read_text(), SECRET_KEY)

    def test_token_pasted_with_getpass_when_the_environment_has_none(self):
        del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        write_exe(self.srv / "bin/claude", "#!/bin/sh\nexit 0\n")
        with mock.patch.object(host.getpass, "getpass", return_value=TOKEN + "\n"):
            rc, out, err = self.add()
        self.assertEqual(rc, 0, err)
        self.assertNotIn(TOKEN, out + err)
        self.assertEqual(json.loads((self.srv / "home/.claude/settings.json").read_text())["env"]["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)

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

    def test_settings_merge_keeps_other_keys(self):
        claude, factory = self.srv / "home/.claude", self.srv / "home/.claude/factory"
        factory.mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({"theme": "dark", "env": {"A": "b"}}))
        (factory / "settings.json").write_text(json.dumps({"worktree_cap": 3, "monitor": "old"}))
        self.assertEqual(self.add("--repo", "acme/widgets", "--repo", "acme/gadgets")[0], 0)
        self.assertEqual(json.loads((claude / "settings.json").read_text()),
                         {"theme": "dark", "env": {"A": "b", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}})
        self.assertEqual(json.loads((factory / "settings.json").read_text()),
                         {"worktree_cap": 3, "monitor": "/state/home/code/isshoni/bin/isshoni", "dispatchers": ["dispatcher-widgets", "dispatcher-gadgets"]})

    def test_authorised_adds_keeps_and_upgrades(self):
        other = "ssh-ed25519 AAAAother bob"
        self.assertEqual(host.authorised([other], PUB), [other, "write " + PUB])
        self.assertEqual(host.authorised([other, "write " + PUB], PUB), [other, "write " + PUB])
        self.assertEqual(host.authorised([PUB, other], PUB), ["write " + PUB, other])

    def test_dashboard_key_file_and_restart_before_the_dispatchers(self):
        keys = self.srv / "home/.claude/factory/dashboard/authorized_keys"
        keys.parent.mkdir(parents=True)
        keys.write_text(PUB + "\n")
        rc, out, _ = self.add("--repo", "acme/widgets")
        self.assertEqual(rc, 0)
        self.assertEqual(keys.read_text(), "write " + PUB + "\n")
        self.assertTrue((self.srv / "serving").exists())
        restarts = [i for i, c in enumerate(self.calls()) if "docker restart" in c["argv"][1]]
        starts = [i for i, c in enumerate(self.calls()) if "--bg" in c["argv"][1]]
        self.assertTrue(restarts and starts and max(restarts) < min(starts))

    def test_private_repository_falls_back_to_fetching_as_the_app(self):
        rc, out, err = self.add("--repo", "acme/private-thing")
        self.assertEqual(rc, 0, err)
        log = (self.srv / "git.log").read_text()
        self.assertIn("remote add origin https://github.com/acme/private-thing", log)
        self.assertIn("checkout -B main origin/main", log)

    def test_alias_block_added_once_refused_elsewhere_and_removed(self):
        config = self.laptop / ".ssh/config"
        config.write_text("Host other\n  User me\n")
        self.add("--alias", "factory")
        self.add("--alias", "factory")
        self.assertEqual(self.ssh_config().count("Host factory\n"), 1)
        self.assertIn("ProxyJump nas", self.ssh_config())
        self.assertEqual(self.run_host("remove", "nas")[0], 0)
        self.assertEqual(self.ssh_config(), "Host other\n  User me\n")
        config.write_text("Host factory\n  HostName example.invalid\n")
        before = self.calls()
        rc, _, err = self.add("--alias", "factory", target="nas2")
        self.assertEqual(rc, 1)
        self.assertIn("Host factory", err)
        self.assertEqual(config.read_text(), "Host factory\n  HostName example.invalid\n")
        self.assertEqual(len(self.calls()), len(before))

    def test_alias_config_is_created_0600(self):
        self.add("--alias", "factory")
        self.assertEqual(stat.S_IMODE((self.laptop / ".ssh/config").stat().st_mode), 0o600)

    def test_alias_is_asked_only_on_a_terminal(self):
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertEqual(self.add()[0], 0)
        self.assertFalse((self.laptop / ".ssh/config").exists())
        with mock.patch("builtins.input", return_value="box"):
            rc, out, _ = self.run_host("add", "nas", "--name", "nas-tty", "--state", self.state, tty=True)
        self.assertIn("ssh box", out)
        self.assertIn("Host box", self.ssh_config())

    def test_registry_is_0600_and_accumulates_repos(self):
        self.add("--repo", "acme/widgets")
        self.add("--repo", "acme/gadgets", "--repo", "acme/widgets")
        path = self.fh / "hosts.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        e = json.loads(path.read_text())["nas"]
        self.assertEqual((e["target"], e["state"], e["dashboard_port"], e["repos"], e["alias"]), ("nas", self.state, 2222, ["acme/widgets", "acme/gadgets"], None))
        self.assertEqual(len(e["rev"]), 40)

    def test_state_cannot_change_and_repo_names_are_unique(self):
        self.add()
        rc, _, err = self.run_host("add", "nas", "--state", "/elsewhere")
        self.assertEqual(rc, 1)
        self.assertIn("cannot move", err)
        self.assertEqual(self.add("--repo", "a/x", "--repo", "b/x")[0], 1)
        self.assertEqual(self.add("--repo", "acme/isshoni")[0], 1)
        self.assertEqual(self.run_host("add", "other")[0], 1)  # --state is required the first time

    def test_list_needs_no_network(self):
        self.add("--repo", "acme/widgets", "--alias", "factory")
        n = len(self.calls())
        rc, out, _ = self.run_host("list")
        self.assertEqual((rc, out), (0, "nas  nas  %s  acme/widgets  factory\n" % self.state))
        self.assertEqual(len(self.calls()), n)

    def test_status_exit_code_follows_what_is_running(self):
        self.add("--repo", "acme/widgets")
        rc, out, _ = self.run_host("status")
        self.assertEqual(rc, 0, out)
        self.assertIn("dispatcher-widgets running", out)
        (self.srv / "home/stub-state.json").write_text(json.dumps({"agents": [], "plugins": [], "marketplaces": []}))
        rc, out, _ = self.run_host("status", "nas")
        self.assertEqual(rc, 1)
        self.assertIn("dispatcher-widgets missing", out)
        (self.srv / "down").touch()
        self.assertEqual(self.run_host("status")[0], 1)
        self.assertEqual(self.run_host("status", "nope")[0], 1)

    def test_remove_keeps_the_state_directory_unless_purged(self):
        self.add("--repo", "acme/widgets")
        n = len(self.calls())
        rc, out, _ = self.run_host("remove", "nas")
        self.assertEqual(rc, 0)
        self.assertIn(self.state, out)
        removal = [c["argv"][1] for c in self.calls()[n:]]
        self.assertFalse([c for c in removal if "rm -rf" in c or self.state in c.replace("FACTORY_STATE=" + self.state, "")])
        self.assertTrue(Path(self.state).is_dir())
        self.assertFalse((self.srv / "image.json").exists() or (self.srv / "container.json").exists())
        self.assertEqual(json.loads((self.fh / "hosts.json").read_text()), {})
        self.add("--repo", "acme/widgets")
        self.assertEqual(self.run_host("remove", "nas", "--purge-state")[0], 0)
        self.assertTrue([c for c in self.calls() if "rm -rf %s" % self.state in c["argv"][1]])
        self.assertFalse(Path(self.state).exists())

    def test_remove_of_an_unreachable_server_removes_nothing_local(self):
        self.add("--alias", "factory")
        (self.srv / "down").touch()
        rc, _, err = self.run_host("remove", "nas")
        self.assertEqual(rc, 1)
        self.assertIn("nothing was removed", err)
        self.assertIn("nas", json.loads((self.fh / "hosts.json").read_text()))
        self.assertIn("Host factory", self.ssh_config())


if __name__ == "__main__":
    unittest.main()
