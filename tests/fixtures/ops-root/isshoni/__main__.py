"""Stand-in for `python3 -m isshoni <sessions|agents> --json`: no sessions, no agents."""
import sys

print('{"agents": [], "by_model": {}, "sessions": 0}' if sys.argv[1] == "agents" else '{"sessions": [], "malformed": 0}')
