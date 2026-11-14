"""isshoni host against real Docker: the "server" is the local Docker, reached through a fake ssh that runs the
command locally. Runs when CI is set (the test job has Docker) or ISSHONI_HOST_E2E=1, and Docker works.

Only claude is a stub (the real one needs a login): a wrapper around `docker` puts it first on PATH for `docker exec`."""
import contextlib
import io
import json
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ROOT  # noqa: F401  (puts the repository root on sys.path)
from tests.test_host import CLAUDE_STUB, FAKE_SSH, write_exe
from isshoni import cli, host

TOKEN = "sk-ant-oat01-E2ESECRETTOKEN"
KEY = "-----BEGIN RSA PRIVATE KEY-----\nE2ESECRETKEYMATERIAL\n-----END RSA PRIVATE KEY-----\n"
REPO = "andreiciobotar/isshoni"  # public, cloned anonymously
STUB_PATH = "/state/stub:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def docker_says_why_not():
    if not (os.environ.get("CI") or os.environ.get("ISSHONI_HOST_E2E")):
        return "set ISSHONI_HOST_E2E=1 (or CI) to run against Docker"
    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode:
        return "docker does not work here"
    if subprocess.run(["docker", "ps", "-aq", "--filter", "name=^isshoni-factory$"], capture_output=True, text=True).stdout.strip():
        return "a container named isshoni-factory already exists"
    if subprocess.run(["docker", "image", "inspect", "isshoni-factory"], capture_output=True).returncode == 0:
        return "an image named isshoni-factory already exists"
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(docker_says_why_not(), docker_says_why_not() or "")
class HostEndToEnd(unittest.TestCase):
    def run_host(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO("")):
            rc = cli.main(["host", *argv])
        return rc, out.getvalue(), err.getvalue()

    def docker(self, *argv):
        return subprocess.run(["docker", *argv], capture_output=True, text=True)

    def cleanup(self):
        self.docker("rm", "-f", host.CONTAINER)
        self.docker("image", "rm", "-f", host.IMAGE)
        for root, dirs, _ in os.walk(self.state):  # Go's module cache is read-only
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_whole_life_of_a_host(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.path.expanduser("~")))).resolve()
        self.addCleanup(self.cleanup)
        self.state = self.tmp / "state"
        (self.state / "stub").mkdir(parents=True)
        write_exe(self.state / "stub/claude", CLAUDE_STUB)
        fh, bin_dir, laptop = self.tmp / "fh", self.tmp / "bin", self.tmp / "laptop"
        for d in (fh / "identity", bin_dir, laptop):
            d.mkdir(parents=True)
        (fh / "identity/key.pem").write_text(KEY)
        (fh / "identity/app.json").write_text(json.dumps({"bot_login": "e2e[bot]", "owner": "octocat"}))
        real_docker, real_ssh = shutil.which("docker"), shutil.which("ssh")
        write_exe(bin_dir / "ssh", FAKE_SSH)
        # The "server" is Docker's Linux machine, which on a Mac is not this one.
        write_exe(bin_dir / "uname", "#!/bin/sh\nexec %s info -f '{{.Architecture}}'\n" % real_docker)
        write_exe(bin_dir / "docker", '#!/bin/sh\nif [ "$1" = exec ]; then shift; exec %s exec -e PATH=%s "$@"; fi\nexec %s "$@"\n'
                  % (real_docker, STUB_PATH, real_docker))
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(laptop / "id")], check=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(laptop / "stranger")], check=True)
        port = free_port()
        env = {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "HOME": str(laptop), "FACTORY_HOME": str(fh),
               "CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG") or os.path.expanduser("~/.docker")}
        self.enterContext(mock.patch.dict(os.environ, env))
        add = ["add", "localhost", "--state", str(self.state), "--repo", REPO, "--key", str(laptop / "id.pub"), "--dashboard-port", str(port)]

        rc, out, err = self.run_host(*add)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ssh -J localhost -p %d -o HostKeyAlias=isshoni-localhost 127.0.0.1" % port, out)

        info = json.loads(self.docker("inspect", host.CONTAINER).stdout)[0]
        limits = info["HostConfig"]
        self.assertEqual((info["State"]["Running"], limits["NanoCpus"], limits["Memory"], limits["MemorySwap"], limits["PidsLimit"], limits["ReadonlyRootfs"],
                          limits["CapDrop"], limits["SecurityOpt"]),
                         (True, 2 * 10**9, 4 * 2**30, 4 * 2**30, 1024, True, ["ALL"], ["no-new-privileges:true"]))
        self.assertEqual(limits["PortBindings"]["2222/tcp"], [{"HostIp": "127.0.0.1", "HostPort": str(port)}])

        home = self.state / "home/.claude"
        mode = lambda p: stat.S_IMODE(p.stat().st_mode)
        self.assertEqual((home / "factory/identity/key.pem").read_text(), KEY)
        self.assertEqual([mode(home / "factory/identity"), mode(home / "factory/identity/key.pem"), mode(home / "factory/identity/app.json"), mode(home / "settings.json")],
                         [0o700, 0o600, 0o600, 0o600])
        self.assertEqual(json.loads((home / "settings.json").read_text())["env"], {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})
        self.assertEqual(json.loads((home / "factory/settings.json").read_text()),
                         {"monitor": "/state/home/.isshoni/bin/isshoni", "dispatchers": ["dispatcher-isshoni"]})
        pub = (laptop / "id.pub").read_text().strip()
        self.assertEqual((home / "factory/dashboard/authorized_keys").read_text(), "write " + pub + "\n")
        self.assertTrue((self.state / "home/code/isshoni/.git").is_dir())
        stub = [json.loads(l) for l in (self.state / "home/stub-claude.log").read_text().splitlines()]
        self.assertTrue([c for c in stub if c["argv"][:3] == ["--bg", "-n", "dispatcher-isshoni"] and c["cwd"] == "/state/home/code/isshoni"], stub)

        ssh_args = ["-p", str(port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes",
                    "-o", "BatchMode=yes", "-tt", "127.0.0.1"]
        def dashboard(key):
            """What the dashboard over ssh shows to `key`: read until its title or a refusal, at most 20 s."""
            p = subprocess.Popen([real_ssh, "-i", str(laptop / key), *ssh_args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out, end = b"", time.monotonic() + 20
            try:
                while time.monotonic() < end and b"Isshoni" not in out and b"Permission denied" not in out:
                    if select.select([p.stdout], [], [], 0.5)[0]:
                        chunk = os.read(p.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        out += chunk
            finally:
                p.kill()
                p.wait()
                p.stdout.close()
                p.stdin.close()
            return out.decode(errors="replace")
        self.assertIn("Isshoni", dashboard("id"))
        self.assertIn("Permission denied", dashboard("stranger"))

        logs = self.docker("logs", host.CONTAINER)
        for secret in (TOKEN, "E2ESECRETKEYMATERIAL"):
            self.assertNotIn(secret, logs.stdout + logs.stderr)

        rc, out, err = self.run_host(*add)
        self.assertEqual((rc, err, [l.rsplit(": ", 1)[1] for l in out.splitlines() if l.startswith("[")]), (0, "", ["already"] * 12), out)
        self.assertEqual(self.run_host("status")[0], 0)

        rc, out, _ = self.run_host("remove", "localhost")
        self.assertEqual(rc, 0)
        self.assertTrue(self.state.is_dir())
        self.assertNotEqual(self.docker("inspect", host.CONTAINER).returncode, 0)
        rc, out, err = self.run_host(*add)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.run_host("remove", "localhost", "--purge-state")[0], 0)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
