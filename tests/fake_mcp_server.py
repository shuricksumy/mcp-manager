#!/usr/bin/env python3
"""Minimal stdio MCP server for tests/supervise-test.py (stdlib only).

Tools: pid (returns its pid), leak (allocates `mb` MiB and keeps it),
crash (exits immediately), sleep (answers after `seconds`).
"""
import json
import os
import sys
import threading
import time

# FAKE_FAIL_FIRST=N with FAKE_COUNTER=<file>: exit(1) on the first N starts.
if os.environ.get("FAKE_FAIL_FIRST"):
    counter = os.environ["FAKE_COUNTER"]
    try:
        n = int(open(counter).read())
    except (OSError, ValueError):
        n = 0
    open(counter, "w").write(str(n + 1))
    if n < int(os.environ["FAKE_FAIL_FIRST"]):
        sys.stderr.write(f"fake: failing start {n + 1} on purpose\n")
        sys.exit(1)

hog = []
lock = threading.Lock()


def send(msg):
    with lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def text(t):
    return {"content": [{"type": "text", "text": str(t)}]}


TOOLS = [
    {"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}}
    for n in ("pid", "leak", "crash", "sleep", "env")
]


def call(req):
    name = req["params"]["name"]
    args = req["params"].get("arguments") or {}
    if name == "pid":
        return text(os.getpid())
    if name == "leak":
        mb = int(args.get("mb", 100))
        block = bytearray(mb * 1024 * 1024)
        for i in range(0, len(block), 4096):  # touch pages so they count as RSS
            block[i] = 1
        hog.append(block)
        return text(f"leaked {mb}M")
    if name == "crash":
        os._exit(3)
    if name == "env":
        return text(os.environ.get(str(args.get("name", "")), "<unset>"))
    if name == "sleep":
        time.sleep(float(args.get("seconds", 1)))
        return text("slept")
    raise ValueError(name)


def handle(req):
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        return
    try:
        if method == "initialize":
            result = {
                "protocolVersion": req["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = call(req)
        elif method == "ping":
            result = {}
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})
            return
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    except Exception as exc:  # noqa: BLE001
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(exc)}})


for line in sys.stdin:
    if line.strip():
        req = json.loads(line)
        threading.Thread(target=handle, args=(req,), daemon=True).start()
