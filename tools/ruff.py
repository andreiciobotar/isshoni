"""Run ruff at the version pinned here: python3 tools/ruff.py <ruff arguments>.

The release archive for this platform is fetched once into ${XDG_CACHE_HOME:-~/.cache}/isshoni/ruff-<version>/ and
checked against the SHA-256 below. Bump VERSION and SHA256 together (the values are in the release's .sha256 files).
"""
import hashlib
import io
import os
import platform
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

VERSION = "0.15.0"
SHA256 = {
    "x86_64-unknown-linux-musl": "f2bd69f091517ebc49405319a6e7818b1037e250b8d539336aa0b91c44ffa4aa",
    "aarch64-unknown-linux-musl": "b9860c394ea814a9e9560e80bc798c53b8a3a5120ce961a2555fb563cee9f73b",
    "x86_64-apple-darwin": "09fa6fe0d4172e1bb84cc6d937a0e1f42ff84c90b61163fff4f31c51a9c14879",
    "aarch64-apple-darwin": "093d355ac33c6b8e91e80b8497d5581c61b028c0405e265cf38fd88f9a291c5c",
}
URL = "https://github.com/astral-sh/ruff/releases/download/" + VERSION + "/ruff-{}.tar.gz"
ATTEMPTS = 3
TIMEOUT = 60


def triple():
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(platform.machine().lower())
    system = {"linux": "unknown-linux-musl", "darwin": "apple-darwin"}.get(sys.platform)
    if not (arch and system and f"{arch}-{system}" in SHA256):
        sys.exit(f"isshoni: no ruff {VERSION} release is pinned for {sys.platform} {platform.machine()}.")
    return f"{arch}-{system}"


def download(name):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(URL.format(name), timeout=TIMEOUT) as r:
                return r.read()
        except OSError as e:  # URLError and timeouts
            last = e
            if attempt < ATTEMPTS:
                time.sleep(2)
    sys.exit(f"network: fetching ruff {VERSION} failed {ATTEMPTS} times ({last}); a network failure, not caused by the code")


def fetch(name, dest):
    data = download(name)
    got = hashlib.sha256(data).hexdigest()
    if got != SHA256[name]:
        sys.exit(f"isshoni: ruff-{name}.tar.gz has SHA-256 {got}, expected {SHA256[name]}; not installed.")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        binary = tar.extractfile(f"ruff-{name}/ruff").read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".ruff-")
    with os.fdopen(fd, "wb") as f:
        f.write(binary)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)


def main():
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    ruff = cache / "isshoni" / f"ruff-{VERSION}" / "ruff"
    if not ruff.exists():
        fetch(triple(), ruff)
    os.execv(ruff, [str(ruff), *sys.argv[1:]])


if __name__ == "__main__":
    main()
