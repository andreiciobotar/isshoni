#!/usr/bin/env python3
"""A stdio MCP server for the live proof of MCP servers per role (factory/tests/live_mcp_roles.py), standard library only.

    mcp_role_server.py <call log> <read tool count>

Serves `read_01` .. `read_NN` and one tool, `write_record`, that the proof never grants. Each carries a description of about
forty words and three input properties. Every call is appended to `<call log>`, which only this server writes."""
import json
import sys
from pathlib import Path

DESCRIPTION = ("Look up one record of the service by its identifier and return its fields, its current state, the time of its last "
               "change and the name of whoever made it. The result is a short JSON object. The call changes nothing and may be "
               "repeated as often as needed, and it is the right tool when only the current state is wanted.")
SCHEMA = {"type": "object", "properties": {"id": {"type": "string", "description": "identifier of the record"},
                                           "fields": {"type": "array", "items": {"type": "string"}, "description": "fields to return"},
                                           "verbose": {"type": "boolean", "description": "return every field"}}, "required": ["id"]}


def tools(count):
    return [{"name": f"read_{i:02d}", "description": DESCRIPTION, "inputSchema": SCHEMA} for i in range(1, count + 1)] + [
        {"name": "write_record", "description": DESCRIPTION.replace("The call changes nothing", "The call overwrites the record"),
         "inputSchema": SCHEMA}]


def serve(log, count):
    listed = tools(count)
    for line in sys.stdin:
        msg = json.loads(line)
        method, id_ = msg.get("method"), msg.get("id")
        if id_ is None:  # a notification
            continue
        if method == "initialize":
            result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "roleproof", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": listed}
        elif method == "tools/call":
            with log.open("a") as f:
                f.write(f"{msg['params']['name']}\n")
            result = {"content": [{"type": "text", "text": json.dumps({"id": "7", "state": "open"})}]}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}), flush=True)


if __name__ == "__main__":
    serve(Path(sys.argv[1]), int(sys.argv[2]))
