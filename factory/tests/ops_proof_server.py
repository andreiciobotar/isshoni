#!/usr/bin/env python3
"""A stdio MCP server for the live proof of the ops watch (factory/tests/live_ops_watch.py), standard library only.

    ops_proof_server.py <state dir>

One read tool, `read_served`, answers {"served": 10*n, "errors": 0} where n counts the calls so far, kept in
`<state dir>/count`. Every call is appended to `<state dir>/calls.log`, which only this server writes."""
import json
import sys
from pathlib import Path

TOOL = {"name": "read_served", "description": "How many requests were served so far, and how many of them failed.",
        "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}


def serve(state):
    count, calls = state / "count", state / "calls.log"
    for line in sys.stdin:
        msg = json.loads(line)
        method, id_ = msg.get("method"), msg.get("id")
        if id_ is None:  # a notification
            continue
        if method == "initialize":
            result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "proof", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            n = (int(count.read_text()) if count.exists() else 0) + 1
            count.write_text(str(n))
            with calls.open("a") as f:
                f.write(f"call {n}\n")
            result = {"content": [{"type": "text", "text": json.dumps({"served": 10 * n, "errors": 0})}]}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}), flush=True)


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
