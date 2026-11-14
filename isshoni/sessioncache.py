"""A disk cache of parsed sessions, so a run does not parse every transcript again.

One pickle per transcript under $ISSHONI_CACHE_DIR (empty: off), else $XDG_CACHE_HOME/isshoni/sessions,
else ~/.cache/isshoni/sessions. An entry holds the code and price fingerprint, the transcript's
signature taken before the parse, and the Session as `transcript.load_session` returned it.
"""
from __future__ import annotations

import hashlib
import os
import pickle
import sys
import tempfile

from . import prices as price_file, transcript

def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _fingerprint(prices):
    """Hash of the code and price file a parsed session depends on."""
    h = hashlib.sha256(repr(sys.version_info[:2]).encode())
    for p in (transcript.__file__, price_file.__file__, prices.path):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"?")
    return h.hexdigest()


def _signature(path):
    """Size and mtime of the transcript and of every entry of its subagents directory."""
    st = os.stat(path)
    subs = []
    try:
        with os.scandir(os.path.splitext(path)[0] + os.sep + "subagents") as it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                    subs.append((e.name, s.st_size, s.st_mtime_ns))
                except OSError:
                    subs.append((e.name,))
    except OSError:
        pass
    return (st.st_size, st.st_mtime_ns), sorted(subs)


def _root():
    root = os.environ.get("ISSHONI_CACHE_DIR")
    if root is None:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        root = os.path.join(base, "isshoni", "sessions")
    return root


class SessionCache:
    def __init__(self, projects_dir, prices):
        self.prices = prices
        root = _root()
        projects = os.path.abspath(os.path.expanduser(projects_dir))
        self.dir = os.path.join(root, _digest(projects)[:16]) if root else None
        self.fingerprint = _fingerprint(prices) if self.dir else None

    def _entry(self, path):
        return os.path.join(self.dir, _digest(os.path.abspath(path))[:32] + ".pickle")

    def load(self, path):
        """The session from the cache, or parsed and stored. Any trouble with the cache is a miss."""
        if not self.dir:
            return transcript.load_session(path, self.prices)
        try:
            sig = _signature(path)
        except OSError:
            return transcript.load_session(path, self.prices)
        fp = self.fingerprint
        entry = self._entry(path)
        try:
            with open(entry, "rb") as f:
                cached = pickle.load(f)
            if cached[0] == fp and cached[1] == sig:
                cached[2].path = path
                return cached[2]
        except Exception:
            pass
        s = transcript.load_session(path, self.prices)
        self._store(entry, (fp, sig, s))
        return s

    def _store(self, entry, data):
        tmp = None
        try:
            os.makedirs(os.path.dirname(self.dir), mode=0o700, exist_ok=True)
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                pickle.dump(data, f, pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, entry)
        except Exception:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def prune(self, paths):
        """Remove every file of this projects dir's cache that is not the entry of one of `paths`."""
        if not self.dir:
            return
        keep = {os.path.basename(self._entry(p)) for p in paths}
        try:
            with os.scandir(self.dir) as it:
                stale = [e.path for e in it if e.name not in keep]
        except OSError:
            return
        for p in stale:
            try:
                os.unlink(p)
            except OSError:
                pass
