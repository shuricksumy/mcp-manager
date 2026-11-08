#!/usr/bin/env python3
"""End-to-end test of mcp-manager (admin API + mcp-proxy), host side, stdlib only.

Usage: tests/manager-test.py <image> [proxy-port] [manager-port]
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

image = sys.argv[1]
pport = int(sys.argv[2]) if len(sys.argv) > 2 else 18100
mport = int(sys.argv[3]) if len(sys.argv) > 3 else 18101
name = f"mcp-manager-test-{os.getpid()}"
here = os.path.dirname(os.path.abspath(__file__))
AUTH = "Basic " + base64.b64encode(b"admin:test-pw").decode()
FAKE = {"command": "python3", "args": ["/tests/fake_mcp_server.py"]}
sessions, next_id = {}, 0


def check(label, cond):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        raise SystemExit(1)


def http(method, path, body=None, auth=True, header=True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = AUTH
    if header:
        headers["X-MCP-Manager"] = "1"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{mport}{path}", data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw.decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode()


def api(method, path, body=None):
    status, data = http(method, path, body)
    if status >= 400:
        raise AssertionError(f"{method} {path} -> {status} {data}")
    return data


def state():
    return api("GET", "/api/state")


def server_state(srv):
    for s in state()["servers"]:
        if s["name"] == srv:
            return s
    return None


def mcp(server, msg, timeout=60):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Host": "mcp-test:8000"}
    if server in sessions:
        headers["mcp-session-id"] = sessions[server]
    req = urllib.request.Request(f"http://127.0.0.1:{pport}/servers/{server}/mcp", json.dumps(msg).encode(), headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid, body = r.headers.get("mcp-session-id"), r.read().decode()
    if sid:
        sessions[server] = sid
    data = [l[5:] for l in body.splitlines() if l.startswith("data:")]
    return json.loads(data[-1]) if data else (json.loads(body) if body.strip() else None)


def connect(server):
    sessions.pop(server, None)
    r = mcp(server, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}, 10)
    mcp(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return r and "result" in r


def tool(server, tname, **arguments):
    global next_id
    next_id += 1
    r = mcp(server, {"jsonrpc": "2.0", "id": next_id, "method": "tools/call", "params": {"name": tname, "arguments": arguments}})
    if "error" in r:
        return None, r["error"]["message"]
    if r["result"].get("isError"):  # mcp-proxy reports a failed call as an error result
        return None, r["result"]["content"][0]["text"]
    return r["result"]["content"][0]["text"], None


def wait_for(pred, what, timeout=90):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            if pred():
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what} ({last})")


def logs():
    return subprocess.run(["docker", "logs", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout


cfg = tempfile.mkdtemp()
with open(os.path.join(cfg, "servers.json"), "w") as f:
    json.dump({"mcpServers": {
        "fake": {**FAKE, "env": {"PLAIN": "one", "API_TOKEN": "s3cret"}},
        "off": {**FAKE, "enabled": False},
    }}, f)

subprocess.run(["docker", "run", "-d", "--name", name,
                "-p", f"127.0.0.1:{pport}:8000", "-p", f"127.0.0.1:{mport}:8001",
                "-e", "MCP_MANAGER=true", "-e", "MCP_MANAGER_PASSWORD=test-pw",
                "-e", "MCP_PROXY_ALLOWED_HOSTS=mcp-test:*", "-e", "MCP_SUPERVISE_CHECK_INTERVAL=1s",
                "-v", f"{cfg}:/mcp", "-v", f"{here}:/tests:ro", image], check=True, stdout=subprocess.DEVNULL)
ok = False
try:
    wait_for(lambda: http("GET", "/healthz", auth=False)[0] == 204, "manager to come up")
    wait_for(lambda: (server_state("fake") or {}).get("runtime", {}).get("state") == "running", "fake to run")

    # auth / CSRF / page
    check("no credentials -> 401", http("GET", "/api/state", auth=False)[0] == 401)
    check("POST without X-MCP-Manager -> 403", http("POST", "/api/proxy/restart", header=False)[0] == 403)
    status, page = http("GET", "/")
    check("admin page served", status == 200 and "MCP Manager" in page)

    # import: one file per server, secrets separate and masked
    d = os.path.join(cfg, "servers.d")
    def read_in_container(path):  # the secret file is root-owned, mode 600: read it as root
        return json.loads(subprocess.run(["docker", "exec", name, "cat", path], capture_output=True, text=True, check=True).stdout)
    public = read_in_container("/mcp/servers.d/fake.json")
    secret = read_in_container("/mcp/servers.d/fake.secret.json")
    check("imported into servers.d/fake.json (+ .secret.json)", public["env"] == {"PLAIN": "one"} and secret["env"] == {"API_TOKEN": "s3cret"})
    check("secret file is mode 600", oct(os.stat(os.path.join(d, "fake.secret.json")).st_mode & 0o777) == "0o600")
    view = api("GET", "/api/servers/fake")
    check("API masks secrets", {"key": "API_TOKEN", "value": "", "secret": True, "hasValue": True} in view["env"] and "s3cret" not in json.dumps(state()))
    check("disabled server imported, not loaded", server_state("off")["runtime"]["state"] == "disabled")

    # MCP through the proxy
    wait_for(lambda: connect("fake"), "MCP session to fake")
    check("server env: PLAIN + secret", tool("fake", "env", name="PLAIN")[0] == "one" and tool("fake", "env", name="API_TOKEN")[0] == "s3cret")
    proxy_pid = state()["proxy"]["pid"]
    pid1 = int(tool("fake", "pid")[0])

    # edit a running server: only it restarts, session survives, secret kept
    env = [{"key": "PLAIN", "value": "two", "secret": False}, {"key": "API_TOKEN", "value": "", "secret": True}]
    r = api("PUT", "/api/servers/fake", {**FAKE, "env": env, "supervise": {"maxRss": "300M"}})
    check(f"edit applied live: {r}", r["applied"] and r["action"] == "restarted")
    pid2 = int(tool("fake", "pid")[0])
    check(f"edit restarted only that server (pid {pid1} -> {pid2}), same session, proxy pid unchanged",
          pid2 != pid1 and state()["proxy"]["pid"] == proxy_pid)
    check("new env active, secret kept", tool("fake", "env", name="PLAIN")[0] == "two" and tool("fake", "env", name="API_TOKEN")[0] == "s3cret")
    check("new limit active", server_state("fake")["runtime"]["limits"]["maxRss"] == 300 * 1024 ** 2)

    # stop / start
    check("stop", api("POST", "/api/servers/fake/stop")["ok"])
    check("stopped: requests get an error", "is stopped" in (tool("fake", "pid")[1] or ""))
    check("start", api("POST", "/api/servers/fake/start")["ok"] and tool("fake", "pid")[0] is not None)

    # add a server: pending until proxy restart
    r = api("POST", "/api/servers", {"name": "fresh", **FAKE, "env": [{"key": "WHO", "value": "fresh", "secret": False}]})
    check("new server saved, needs proxy restart", r["needsProxyRestart"] and any(p["name"] == "fresh" for p in state()["pending"]))
    api("POST", "/api/proxy/restart")
    wait_for(lambda: state()["proxy"]["pid"] not in (None, proxy_pid) and server_state("fresh")["runtime"]["state"] == "running", "proxy restart")
    wait_for(lambda: connect("fresh"), "MCP session to fresh")
    check("fresh reachable after proxy restart", tool("fresh", "env", name="WHO")[0] == "fresh" and not state()["pending"])

    # disable / enable
    wait_for(lambda: connect("fake"), "reconnect to fake")
    r = api("PUT", "/api/servers/fake", {**FAKE, "enabled": False, "env": env})
    check("disable stops it", r["action"] == "stopped" and server_state("fake")["runtime"]["state"] == "stopped")
    r = api("PUT", "/api/servers/fake", {**FAKE, "enabled": True, "env": env})
    check("enable starts it", r["action"] == "started" and tool("fake", "pid")[0] is not None)

    # validation
    status, err = http("POST", "/api/servers", {"name": "bad name", **FAKE})
    check(f"invalid name rejected ({status})", status == 400)
    status, err = http("PUT", "/api/servers/fresh", {**FAKE, "supervise": {"maxRss": "lots"}})
    check(f"invalid limit rejected ({status})", status == 400)

    # import via API
    r = api("POST", "/api/import", {"config": {"mcpServers": {"pasted": {**FAKE, "env": {"SECRET_KEY": "x"}}}}})
    check("import via API", r["imported"] == ["pasted"] and os.path.exists(os.path.join(d, "pasted.secret.json")))

    # delete
    api("DELETE", "/api/servers/fresh")
    st = state()
    check("deleted: files gone, still routed until restart", not os.path.exists(os.path.join(d, "fresh.json")) and "fresh" in st["removed"])

    # persistence across a container restart
    subprocess.run(["docker", "restart", name], check=True, stdout=subprocess.DEVNULL)
    wait_for(lambda: http("GET", "/healthz", auth=False)[0] == 204, "manager after restart")
    wait_for(lambda: server_state("pasted")["runtime"]["state"] == "running", "pasted after restart")
    names = sorted(s["name"] for s in state()["servers"])
    check(f"persisted across container restart: {names}", names == ["fake", "off", "pasted"])
    wait_for(lambda: connect("fake"), "reconnect after restart")
    check("edits persisted", tool("fake", "env", name="PLAIN")[0] == "two")
    ok = True
finally:
    if not ok:
        print("--- container log ---")
        print(logs()[-6000:])
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "--rm", "-v", f"{cfg}:/c", "alpine", "rm", "-rf", "/c/servers.d", "/c/manager.password"], capture_output=True)
