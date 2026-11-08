#!/usr/bin/env python3
"""End-to-end test of mcp-supervise through the real mcp-proxy (host side, stdlib only).

Starts the image with tests/fake_mcp_server.py supervised (max-rss 120M,
hard-rss 400M, max-age 20s, check every 1s) and checks, over ONE client HTTP
session, that:
  1. max-rss: after leaking 200M the server is restarted once idle,
  2. a request in flight delays a soft restart until it finishes,
  3. hard-rss: restarts even with a request in flight, which gets an error,
  4. crash: the server is restarted and the session keeps working,
  5. max-age: an idle server is restarted after its maximum age,
  6. the proxy itself never restarted.

Usage: tests/supervise-test.py <image> [host-port]
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

image = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 18090
name = f"mcp-supervise-test-{os.getpid()}"
here = os.path.dirname(os.path.abspath(__file__))
base = f"http://127.0.0.1:{port}/servers"
sessions = {}
next_id = 0


def post(msg, timeout=30, server="fake"):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Host": "mcp-test:8000",
    }
    if server in sessions:
        headers["mcp-session-id"] = sessions[server]
    req = urllib.request.Request(f"{base}/{server}/mcp", json.dumps(msg).encode(), headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
        sid = resp.headers.get("mcp-session-id")
    if not body.strip():
        return sid, None
    if body.lstrip().startswith("{"):
        return sid, json.loads(body)
    data = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
    return sid, json.loads(data[-1]) if data else None


def rpc(method, params=None, timeout=30, server="fake"):
    global next_id
    next_id += 1
    _, resp = post({"jsonrpc": "2.0", "id": next_id, "method": method, "params": params or {}}, timeout, server)
    return resp


def tool(name, server="fake", **arguments):
    resp = rpc("tools/call", {"name": name, "arguments": arguments}, timeout=60, server=server)
    if "error" in resp:
        return None, resp["error"]["message"]
    result = resp["result"]
    if result.get("isError"):
        return None, result["content"][0]["text"]
    return result["content"][0]["text"], None


def pid(server="fake"):
    value, err = tool("pid", server=server)
    assert err is None, err
    return int(value)


def logs():
    return subprocess.run(["docker", "logs", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout


def control(server, cmd):
    """Talk to the server's mcp-supervise control socket inside the container."""
    code = ("import json,socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); "
            "s.sendall(json.dumps({'cmd': sys.argv[2]}).encode()+b'\\n'); "
            "print(s.makefile().readline())")
    out = subprocess.run(["docker", "exec", name, "python3", "-c", code, f"/run/mcp-supervise/{server}.sock", cmd],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def wait_for(pred, what, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}")


def wait_restarts(n, timeout=60):
    wait_for(lambda: logs().count(": restarted: new pid") >= n, f"{n} restart(s)", timeout)


def check(label, cond):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        raise SystemExit(1)


cfg = tempfile.mkdtemp()
with open(os.path.join(cfg, "servers.json"), "w") as f:
    json.dump({"mcpServers": {"fake": {
        "command": "python3", "args": ["/tests/fake_mcp_server.py"],
        "supervise": {"maxRss": "120M", "hardRss": "400M", "maxAge": "20s", "checkInterval": "1s"},
    }, "sleepy": {
        "command": "python3", "args": ["/tests/fake_mcp_server.py"],
        "supervise": {"idleStop": "4s", "checkInterval": "1s"},
    }}}, f)
os.chmod(cfg, 0o755)
os.chmod(os.path.join(cfg, "servers.json"), 0o644)

subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8000",
                "-e", "MCP_PROXY_ALLOWED_HOSTS=mcp-test:*",
                "-v", f"{cfg}:/mcp:ro", "-v", f"{here}:/tests:ro", image], check=True, stdout=subprocess.DEVNULL)
ok = False
try:
    def up(server):
        try:
            sid, resp = post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}, 5, server)
            sessions[server] = sid
            return resp is not None and "result" in resp
        except (urllib.error.URLError, ConnectionError, OSError):
            return False
    for srv in ("fake", "sleepy"):
        wait_for(lambda: up(srv), f"{srv} to come up")
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, server=srv)
    t0 = time.time()

    # 1. max-rss
    p1 = pid()
    check("leak 200M", tool("leak", mb=200)[0] == "leaked 200M")
    wait_restarts(1)
    p2 = pid()
    check(f"max-rss restart: pid {p1} -> {p2}, same client session", p2 != p1)

    # 2. in-flight request delays a soft restart
    tool("leak", mb=200)
    result = {}
    th = threading.Thread(target=lambda: result.update(r=tool("sleep", seconds=6)))
    th.start()
    time.sleep(3)
    check("restart pending while a request is in flight", "restart pending" in logs() and pid() == p2)
    th.join()
    check("in-flight request completed normally", result["r"] == ("slept", None))
    wait_restarts(2)
    p3 = pid()
    check(f"soft restart after the request finished: pid {p2} -> {p3}", p3 != p2)

    # 3. hard-rss restarts even with a request in flight
    th = threading.Thread(target=lambda: result.update(r=tool("sleep", seconds=30)))
    th.start()
    time.sleep(1)
    tool("leak", mb=450)
    th.join(timeout=40)
    check("hard-rss: in-flight request got an error", result["r"][0] is None and "by mcp-supervise" in result["r"][1])
    wait_restarts(3)
    p4 = pid()
    check(f"hard-rss restart: pid {p3} -> {p4}", p4 != p3)

    # 4. crash
    value, err = tool("crash")
    check("crashing call returned an error", value is None)
    wait_restarts(4)
    p5 = pid()
    check(f"crash restart: pid {p4} -> {p5}", p5 != p4)

    # 5. max-age (20s since the last restart, idle)
    wait_restarts(5, timeout=45)
    p6 = pid()
    check(f"max-age restart: pid {p5} -> {p6}", p6 != p5 and "max-age" in logs())

    # 6. control socket: status, stop, start, restart
    st = control("fake", "status")
    check(f"status: state {st['state']}, rss {st['rss'] // 2**20}M, {st['restarts']} restarts, {st['crashes']} crash",
          st["state"] == "running" and st["pid"] == p6 and st["restarts"] == 5 and st["crashes"] == 1)
    check("stop", control("fake", "stop")["ok"] and control("fake", "status")["state"] == "stopped")
    value, err = tool("pid")
    check("stopped server answers with an error", value is None and "is stopped" in err)
    check("start", control("fake", "start")["ok"])
    p7 = pid()
    check(f"started again: pid {p7}, same client session", p7 != p6)
    check("restart", control("fake", "restart")["ok"])
    p8 = pid()
    check(f"manual restart: pid {p7} -> {p8}", p8 != p7)
    check("log has server lines", any("restarted" in l for l in control("fake", "log")["lines"]))

    # 7. idle-stop: sleeps when unused, wakes on the next request
    s1 = pid("sleepy")
    wait_for(lambda: control("sleepy", "status")["state"] == "sleeping", "sleepy to sleep", 30)
    check("idle server is sleeping, rss 0", control("sleepy", "status")["rss"] == 0)
    s2 = pid("sleepy")
    check(f"woken by a request: pid {s1} -> {s2}", s2 != s1 and control("sleepy", "status")["state"] == "running")

    # 8. the proxy never restarted
    starts = logs().count("entrypoint: exec")
    restarted = subprocess.run(["docker", "inspect", "-f", "{{.RestartCount}}", name], capture_output=True, text=True).stdout.strip()
    check(f"proxy never restarted (exec lines {starts}, restart count {restarted}), {time.time() - t0:.0f}s", starts == 1 and restarted == "0")
    ok = True
    print("--- mcp-supervise log ---")
    print("\n".join(l for l in logs().splitlines() if "mcp-supervise[" in l or "supervise '" in l))
finally:
    if not ok:
        print("--- container log ---")
        print(logs())
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
