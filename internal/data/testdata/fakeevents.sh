#!/bin/sh
# Stand-in for `python3 -m isshoni events [--follow] [--since X] [--projects-dir D]`.
# Logs its arguments to $STUBDIR/calls. A file named fail in $STUBDIR makes it exit 1 with "boom" on stderr, a
# file named hang makes it never return. Otherwise it prints $STUBDIR/events.jsonl; with --follow it then keeps
# running (a child sleep, pid in $STUBDIR/child) until it is killed, unless a file named end exists.
echo "$*" >> "$STUBDIR/calls"
[ -e "$STUBDIR/fail" ] && { echo "noise" >&2; echo "boom" >&2; exit 1; }
[ -e "$STUBDIR/hang" ] && { sleep 30 & echo $! > "$STUBDIR/child"; wait; }
[ -e "$STUBDIR/events.jsonl" ] && cat "$STUBDIR/events.jsonl"
case "$*" in
*--follow*) [ -e "$STUBDIR/end" ] && exit 0; sleep 30 & echo $! > "$STUBDIR/child"; wait ;;
esac
