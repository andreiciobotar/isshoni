"""Run a command, then report and kill every process it started that is still alive.

    python3 tools/noleft.py <command...>

Prints `left behind: N` and one line per survivor (pid, command), kills them (they are the command's own),
and exits 1 when N > 0, else with the command's exit code.

A process is the command's when it descends from it at some poll (every POLL seconds, one `ps` call each; a
process whose whole parent chain ends within POLL of its start is missed). It is not found by an environment
token: macOS hides the environment of system binaries such as /bin/sleep and /bin/sh from `ps -E`.
A survivor is a (pid, start time) pair, so a reused pid is never taken for it.
"""
import os
import signal
import subprocess
import sys
import threading
import time

POLL = 0.1
GRACE = 1.0  # a process just told to stop gets this long to be gone


SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class Stopped(Exception):
    """The wrapper itself was told to stop."""


def table():
    """{(pid, start time): (ppid, command)} of every process."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,command="], capture_output=True, text=True, timeout=10,
                         env={**os.environ, "LC_ALL": "C"}).stdout  # the C locale keeps lstart at 5 tokens
    rows = (line.split(None, 7) for line in out.splitlines())
    return {(int(f[0]), " ".join(f[2:7])): (int(f[1]), f[7]) for f in rows if len(f) == 8}


def scan(mine, rows):
    """Add to `mine` ({key: command}) every live process whose parent is a live member of it."""
    grew = True
    while grew:
        live = {pid for pid, start in mine if (pid, start) in rows}
        new = {k: cmd for k, (ppid, cmd) in rows.items() if ppid in live and k not in mine}
        mine.update(new)
        grew = bool(new)


def track(root, mine, stop):
    while not stop.is_set():
        rows = table()
        if not mine:  # the root's key, taken once: a pid reused after the root is gone is not the root
            mine.update({k: cmd for k, (_, cmd) in rows.items() if k[0] == root})
        scan(mine, rows)
        stop.wait(POLL)


def on_signal(signum, frame):
    for sig in SIGNALS:  # a second signal must not cut the wait and the sweep short
        signal.signal(sig, signal.SIG_IGN)
    raise Stopped(signum)


def main(command):
    for sig in SIGNALS:
        signal.signal(sig, on_signal)
    p = subprocess.Popen(command)
    mine, stop = {}, threading.Event()
    tracker = threading.Thread(target=track, args=(p.pid, mine, stop))
    tracker.start()
    try:
        rc = p.wait()
    except Stopped as s:
        rc = 128 + s.args[0]
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    finally:
        stop.set()
    tracker.join()
    deadline = time.monotonic() + GRACE
    while True:
        rows = table()
        scan(mine, rows)
        left = {k: cmd for k, cmd in mine.items() if k in rows}
        if not left or time.monotonic() > deadline:
            break
        time.sleep(POLL)
    print("left behind: %d" % len(left), flush=True)
    for (pid, _), cmd in sorted(left.items()):
        print("  %d %s" % (pid, cmd[:120]), flush=True)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return 1 if left else rc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: noleft.py <command...>")
    sys.exit(main(sys.argv[1:]))
