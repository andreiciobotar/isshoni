"""Stand-in for `python3 -m isshoni <sessions|session|agents> --json`: no agents, and one fresh session in each repository under $FLOOR_REPOS."""
import json
import os
import sys
from datetime import datetime, timezone

now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
base = os.environ["FLOOR_REPOS"]
sessions = [
    {"id": "floor-" + name, "project": name, "project_dir": name, "repo": name, "repo_path": os.path.join(base, name),
     "task": None, "cwd": os.path.join(base, name), "path": "", "start": now, "end": now}
    for name in sorted(os.listdir(base)) if os.path.isdir(os.path.join(base, name))]
out = {
    "agents": {"agents": [], "by_model": {}, "sessions": 0},
    "sessions": {"malformed": 0, "sessions": sessions},
    "session": sessions[0],
}
print(json.dumps(out[sys.argv[1]]))
