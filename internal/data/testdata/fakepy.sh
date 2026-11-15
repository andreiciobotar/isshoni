#!/bin/sh
# Stand-in for `python3 -m isshoni <cmd> [id] --json ...` and for `python3 <root>/factory/bin/factory status --json`
# and `... ops list --json` (prints $STUBDIR/ops.json; a file ops.err is the stderr of a factory with no ops command, exit 2).
# Logs each call to $STUBDIR/calls; a factory or kaizen call without --cached (the pass that may ask GitHub) is also
# logged to $STUBDIR/fetches. A file named fail or hang in $STUBDIR makes every call fail or never return; fetch-fail
# and fetch-hang do so only for calls without --cached. $STUBDIR/sessions.json, status.json and kaizen.json, when
# present, are what the sessions, factory and kaizen calls print; status-fetch.json and kaizen-fetch.json replace
# them for calls without --cached.
factory=; [ "$2" = status ] && factory=1
fetch=1
for a; do [ "$a" = --cached ] && fetch=; done
hang() { # a child that records its pid in $STUBDIR/child, so a test can check the whole group was killed
	sleep 30 & echo $! > "$STUBDIR/child"; wait; exit 1
}
stop() { # $1 is the failure message, $2 its exit code
	[ -e "$STUBDIR/fail" ] && { echo "$1" >&2; exit "${2:-1}"; }
	[ -e "$STUBDIR/hang" ] && hang
	[ -n "$fetch" ] && [ -e "$STUBDIR/fetch-fail" ] && { echo "$1" >&2; exit "${2:-1}"; }
	[ -n "$fetch" ] && [ -e "$STUBDIR/fetch-hang" ] && hang
}
printjson() { # $1 is the file for a call without --cached, $2 the file for any call
	[ -n "$fetch" ] && [ -e "$STUBDIR/$1" ] && exec cat "$STUBDIR/$1"
	[ -e "$STUBDIR/$2" ] && exec cat "$STUBDIR/$2"
}
echo $$ >> "$STUBDIR/pids" # one line per call, so a test can check no call outlived it
if [ -n "$factory" ]; then
	echo "factory $PWD" >> "$STUBDIR/calls"
	[ -n "$fetch" ] && echo "factory $PWD" >> "$STUBDIR/fetches"
	stop "factory: not inside a git repository" 2
	printjson status-fetch.json status.json
	echo '{"rows":[],"cap":5,"open":0,"in_motion":0,"blocked":0,"in_review":0,"merged":0,"settled":0}'
	exit
fi
if [ "$2" = ops ]; then
	echo "ops $PWD" >> "$STUBDIR/calls"
	[ -e "$STUBDIR/ops.err" ] && { cat "$STUBDIR/ops.err" >&2; exit 2; }
	fetch=; stop "factory: boom"
	printjson ops.json ops.json
	echo '{"watches":[]}'
	exit
fi
echo "$3 $4" >> "$STUBDIR/calls"
[ "$3" = kaizen ] && [ -n "$fetch" ] && echo "$3 $4" >> "$STUBDIR/fetches"
if [ "$3" = kaizen ]; then stop boom; else fetch=; stop boom; fi
case "$3" in
session) printf '{"id":"%s","project":"p","tokens":{"total":10},"dispatches":[]}' "$4" ;;
kaizen) printjson kaizen-fetch.json kaizen.json; echo "{}" ;;
agents) echo '{"agents":[],"by_model":{},"sessions":0}' ;;
*) printjson sessions.json sessions.json; echo '{"sessions":[],"malformed":0}' ;;
esac
