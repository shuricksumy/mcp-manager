#!/usr/bin/env python3
"""mcp-manager: admin page + API that manages the MCP servers behind mcp-proxy.

    mcp-manager (admin UI / API, :8001)
      └─ mcp-proxy (:8000)                     restarted by the manager if it dies
           ├─ mcp-supervise ⇄ server A         per-server restart / stop / limits
           └─ mcp-supervise ⇄ server B

Each server is one file in the servers directory (default /mcp/servers.d):
  <name>.json         {"description", "enabled", "command", "args", "env", "supervise"}
  <name>.secret.json  {"env": {...}}   secret env values only, mode 600, never sent to the UI
On first start, an existing servers.json is imported (secret-looking env keys
such as *TOKEN*, *SECRET*, *PASSWORD*, *KEY* go to the secret file); the
original file is left untouched.

Applying changes:
  * edit a running server  -> only that server restarts (mcp-supervise reloads
    its definition file); no client session is dropped;
  * stop / start / delete  -> immediate, through the server's control socket;
  * add / enable a server  -> needs an mcp-proxy restart (it only reads its
    server list at startup). The UI shows a banner; you decide when.

The admin page uses HTTP basic auth (user "admin"). The password is
MCP_MANAGER_PASSWORD, or generated on first start and stored in
<servers dir>/../manager.password (mode 600).

Usage (entrypoint.sh does this when MCP_MANAGER=true):
  mcp-manager [options] -- mcp-proxy <flags...> --named-server-config <generated config>
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import hmac
import importlib.machinery
import importlib.util
import json
import os
import re
import secrets
import signal
import sys
import time
import urllib.parse


def _load_supervise():
    here = os.path.dirname(os.path.realpath(__file__))
    for candidate in ("mcp-supervise", "mcp-supervise.py"):
        path = os.path.join(here, candidate)
        if os.path.exists(path):
            loader = importlib.machinery.SourceFileLoader("mcp_supervise", path)
            spec = importlib.util.spec_from_loader("mcp_supervise", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
    raise SystemExit("mcp-manager: mcp-supervise not found next to mcp-manager")


sup = _load_supervise()

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SECRET_RE = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|(^|_)KEY$|CREDENTIAL|AUTHORIZATION|COOKIE|PRIVATE", re.I)
SUPERVISE_KEYS = ("maxRss", "hardRss", "maxAge", "idleStop", "logInterval")
HISTORY_POINTS = 720  # 1 h at the 5 s sampling interval
SAMPLE_INTERVAL = 5.0
MAX_BODY = 1024 * 1024


def log(msg: str) -> None:
    sys.stderr.write(f"mcp-manager: {msg}\n")
    sys.stderr.flush()


def write_json(path: str, data: object, mode: int = 0o644) -> None:
    """Atomic write, so a crash never leaves half a file."""
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def proc_rss(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read()
        return int(stat[stat.rfind(b")") + 2 :].split()[21]) * sup.PAGE_SIZE
    except (OSError, IndexError, ValueError):
        return 0


def read_int(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as f:
            value = f.read().strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


# --- server definitions ---------------------------------------------------------


class ValidationError(ValueError):
    pass


class _Respond(Exception):
    """Abort request parsing and send the response prepared so far."""


def validate(name: str, d: dict) -> dict:
    """Return a clean public definition or raise ValidationError."""
    if not NAME_RE.match(name or ""):
        raise ValidationError("name: letters, digits, '.', '_' or '-', up to 64 characters")
    command = d.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValidationError("command is required")
    args = d.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
        raise ValidationError("args must be a list of strings")
    env = d.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float)) for k, v in env.items()):
        raise ValidationError("env must map names to string values")
    for key in env:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ValidationError(f"env: invalid variable name {key!r}")
    supervise = d.get("supervise", {})
    if supervise is not False:
        if not isinstance(supervise, dict):
            raise ValidationError("supervise must be an object or false")
        supervise = {k: str(v).strip() for k, v in supervise.items() if str(v).strip()}
        try:
            sup.supervise_args(name, supervise)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    return {
        "description": str(d.get("description", "")),
        "enabled": bool(d.get("enabled", True)),
        "command": command.strip(),
        "args": [str(a) for a in args],
        "env": {k: str(v) for k, v in env.items()},
        "supervise": supervise,
    }


class Store:
    def __init__(self, directory: str) -> None:
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, name: str, secret: bool = False) -> str:
        return os.path.join(self.dir, f"{name}{'.secret' if secret else ''}.json")

    def names(self) -> list[str]:
        out = []
        for entry in os.listdir(self.dir):
            if entry.endswith(".json") and not entry.endswith(".secret.json"):
                name = entry[: -len(".json")]
                if NAME_RE.match(name):
                    out.append(name)
        return sorted(out, key=str.lower)

    def exists(self, name: str) -> bool:
        return os.path.exists(self._path(name))

    def load(self, name: str) -> tuple[dict, dict]:
        with open(self._path(name), encoding="utf-8") as f:
            public = validate(name, json.load(f))
        secret_env = {}
        if os.path.exists(self._path(name, True)):
            with open(self._path(name, True), encoding="utf-8") as f:
                secret_env = {k: str(v) for k, v in json.load(f).get("env", {}).items()}
        return public, secret_env

    def save(self, name: str, public: dict, secret_env: dict) -> None:
        write_json(self._path(name), public)
        if secret_env:
            write_json(self._path(name, True), {"env": secret_env}, 0o600)
        elif os.path.exists(self._path(name, True)):
            os.unlink(self._path(name, True))

    def delete(self, name: str) -> None:
        for secret in (False, True):
            try:
                os.unlink(self._path(name, secret))
            except FileNotFoundError:
                pass

    def import_servers_json(self, path: str, overwrite: bool = False) -> list[str]:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return self.import_mcp_servers(data, overwrite)

    def import_mcp_servers(self, data: dict, overwrite: bool = False) -> list[str]:
        servers = data.get("mcpServers", data) if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            raise ValidationError('expected {"mcpServers": {"name": {...}}}')
        imported = []
        for name, raw in servers.items():
            if not isinstance(raw, dict):
                raise ValidationError(f"{name}: server entry must be an object")
            if self.exists(name) and not overwrite:
                continue
            env = {k: str(v) for k, v in (raw.get("env") or {}).items()}
            public_env = {k: v for k, v in env.items() if not SECRET_RE.search(k)}
            secret_env = {k: v for k, v in env.items() if SECRET_RE.search(k)}
            public = validate(name, {**raw, "env": public_env})
            self.save(name, public, secret_env)
            imported.append(name)
        return imported


# --- manager ----------------------------------------------------------------------


class Manager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.store = Store(args.servers_dir)
        self.runtime = args.runtime_dir
        self.defs_dir = os.path.join(self.runtime, "defs")
        self.control_dir = args.control_dir
        self.proxy_config = args.proxy_config
        self.proxy_argv = args.proxy
        self.port = args.port
        self.public_proxy_port = os.environ.get("MCP_PROXY_PUBLISHED_PORT", "").strip()
        self.defaults = {
            k: os.environ[v].strip() for k, v in sup.ENV.items() if os.environ.get(v, "").strip()
        }
        self.password = self._password(args.servers_dir)
        self.proxy: asyncio.subprocess.Process | None = None
        self.proxy_started = 0.0
        self.proxy_restarts = 0
        self.proxy_expected_exit = False
        self.proxy_state = "starting"
        self.loaded: dict[str, dict] = {}  # what the running mcp-proxy was started with
        self.status: dict[str, dict] = {}
        self.history: dict[str, collections.deque] = {}
        self.versions = ""
        self.lock = asyncio.Lock()
        self.tree_pids: set[int] = set()  # mcp-proxy and everything it started
        self.tree_sids: set[int] = set()
        try:
            with open("/etc/mcp-proxy.versions", encoding="utf-8") as f:
                self.versions = f.read().strip()
        except OSError:
            pass

    def _password(self, servers_dir: str) -> str:
        env = os.environ.get("MCP_MANAGER_PASSWORD", "")
        if env:
            return env
        path = os.path.join(os.path.dirname(os.path.abspath(servers_dir)), "manager.password")
        try:
            with open(path, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except OSError:
            pass
        value = secrets.token_urlsafe(18)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(value + "\n")
            log(f"generated admin password (user 'admin'): {value}  - stored in {path}")
        except OSError as exc:
            log(f"generated admin password (user 'admin'): {value}  - could not store it: {exc}")
        return value

    # -- config generation -----------------------------------------------------

    def effective(self, name: str) -> dict:
        public, secret_env = self.store.load(name)
        supervise = public["supervise"]
        opts = False if supervise is False else {**self.defaults, **supervise}
        return {**public, "env": {**public["env"], **secret_env}, "supervise_effective": opts}

    def write_definition(self, name: str, eff: dict) -> str:
        os.makedirs(self.defs_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.defs_dir, f"{name}.json")
        write_json(
            path,
            {"command": eff["command"], "args": eff["args"], "env": eff["env"], "supervise": eff["supervise_effective"] or {}},
            0o600,
        )
        return path

    def generate(self) -> dict[str, dict]:
        """Write mcp-proxy's config from the store; return what it contains."""
        servers, loaded = {}, {}
        for name in self.store.names():
            try:
                eff = self.effective(name)
            except (OSError, ValueError) as exc:
                log(f"server '{name}' skipped: {exc}")
                continue
            if not eff["enabled"]:
                continue
            if eff["supervise_effective"] is False:
                servers[name] = {"command": eff["command"], "args": eff["args"], "env": eff["env"]}
            else:
                definition = self.write_definition(name, eff)
                args = sup.supervise_args(name, eff["supervise_effective"], self.control_dir, definition)
                servers[name] = {
                    "command": "mcp-supervise",
                    "args": [*args, "--", eff["command"], *eff["args"]],
                    "env": eff["env"],
                }
            loaded[name] = eff
        os.makedirs(os.path.dirname(self.proxy_config), mode=0o700, exist_ok=True)
        write_json(self.proxy_config, {"mcpServers": servers}, 0o600)
        return loaded

    def pending(self) -> list[dict]:
        """Changes that only take effect after an mcp-proxy restart."""
        out = []
        for name in self.store.names():
            try:
                eff = self.effective(name)
            except (OSError, ValueError):
                continue
            if not eff["enabled"]:
                continue
            old = self.loaded.get(name)
            if old is None:
                out.append({"name": name, "why": "new or enabled"})
            elif (old["supervise_effective"] is False) != (eff["supervise_effective"] is False):
                out.append({"name": name, "why": "supervision switched on/off"})
            elif eff["supervise_effective"] is False and (
                old["command"], old["args"], old["env"]) != (eff["command"], eff["args"], eff["env"]):
                out.append({"name": name, "why": "changed (not supervised, cannot reload)"})
        return out

    # -- mcp-proxy process --------------------------------------------------------

    async def start_proxy(self) -> None:
        self.loaded = self.generate()
        self.status = {}
        log(f"starting mcp-proxy with {len(self.loaded)} server(s): {', '.join(self.loaded) or '-'}")
        self.proxy_expected_exit = False
        self.proxy = await asyncio.create_subprocess_exec(*self.proxy_argv)
        self.proxy_started = time.monotonic()
        self.proxy_state = "running"

    async def stop_proxy(self) -> None:
        proc = self.proxy
        if proc is None or proc.returncode is not None:
            return
        self.proxy_expected_exit = True
        self.proxy_state = "restarting"
        self.track_tree()
        proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 20)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        self.kill_strays()

    @staticmethod
    def _procs() -> dict[int, tuple[int, int]]:
        """pid -> (ppid, session id) for every process."""
        out = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as f:
                    stat = f.read()
                fields = stat[stat.rfind(b")") + 2 :].split()
                out[int(entry)] = (int(fields[1]), int(fields[3]))
            except (OSError, IndexError, ValueError):
                continue
        return out

    def track_tree(self) -> None:
        """Remember mcp-proxy's process tree; servers run in their own sessions."""
        proc = self.proxy
        if proc is None or proc.returncode is not None:
            return
        procs = self._procs()
        tree, frontier = {proc.pid}, [proc.pid]
        while frontier:
            parent = frontier.pop()
            for pid, (ppid, _) in procs.items():
                if ppid == parent and pid not in tree:
                    tree.add(pid)
                    frontier.append(pid)
        self.tree_pids = tree
        self.tree_sids = {procs[p][1] for p in tree if p in procs} - {procs.get(os.getpid(), (0, 0))[1]}

    def kill_strays(self) -> None:
        """Kill what is left of the old mcp-proxy's tree (servers that ignored shutdown).

        Only processes recorded by track_tree() or in one of its server
        sessions - never e.g. a `docker exec` shell.
        """
        me = os.getpid()
        for pid, (_, sid) in self._procs().items():
            if pid != me and (pid in self.tree_pids or sid in self.tree_sids):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.tree_pids, self.tree_sids = set(), set()

    async def restart_proxy(self) -> None:
        async with self.lock:
            await self.stop_proxy()
            self.proxy_restarts += 1
            await self.start_proxy()

    async def watch_proxy(self) -> None:
        backoff = 1
        while True:
            proc = self.proxy
            if proc is None:
                await asyncio.sleep(1)
                continue
            rc = await proc.wait()
            if proc is not self.proxy or self.proxy_expected_exit:
                await asyncio.sleep(0.2)
                continue
            self.proxy_state = "crashed"
            uptime = time.monotonic() - self.proxy_started
            backoff = 1 if uptime > 60 else min(backoff * 2, 30)
            log(f"mcp-proxy exited ({rc}) after {sup.fmt_age(uptime)}; restarting in {backoff}s")
            await asyncio.sleep(backoff)
            async with self.lock:
                if proc is self.proxy:
                    self.kill_strays()
                    self.proxy_restarts += 1
                    await self.start_proxy()

    # -- control sockets ----------------------------------------------------------

    async def ctl(self, name: str, cmd: str, timeout: float = 5) -> dict:
        path = sup.socket_path(self.control_dir, name)
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=16 * 1024 * 1024), 2)
        try:
            writer.write(json.dumps({"cmd": cmd}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
            return json.loads(line)
        finally:
            writer.close()

    def supervised_loaded(self, name: str) -> bool:
        eff = self.loaded.get(name)
        return eff is not None and eff["supervise_effective"] is not False

    async def sample(self) -> None:
        while True:
            self.track_tree()
            for name in list(self.loaded):
                if not self.supervised_loaded(name):
                    continue
                try:
                    st = await self.ctl(name, "status", 3)
                except (OSError, asyncio.TimeoutError, ValueError):
                    st = {"state": "starting" if time.monotonic() - self.proxy_started < 120 else "unreachable"}
                self.status[name] = st
                if st.get("state") in ("running", "sleeping", "stopped"):  # skip start-up / unknown
                    hist = self.history.setdefault(name, collections.deque(maxlen=HISTORY_POINTS))
                    hist.append([round(time.time()), st.get("rss", 0)])
            await asyncio.sleep(SAMPLE_INTERVAL)

    # -- API ----------------------------------------------------------------------

    def public_view(self, name: str) -> dict:
        public, secret_env = self.store.load(name)
        env = [{"key": k, "value": v, "secret": False} for k, v in public["env"].items()]
        env += [{"key": k, "value": "", "secret": True, "hasValue": bool(v)} for k, v in secret_env.items()]
        return {**public, "name": name, "env": env}

    def state(self) -> dict:
        servers = []
        names = self.store.names()
        for name in names:
            try:
                view = self.public_view(name)
            except (OSError, ValueError) as exc:
                servers.append({"name": name, "error": str(exc), "runtime": {"state": "invalid"}})
                continue
            if name not in self.loaded:
                runtime = {"state": "disabled" if not view["enabled"] else "not loaded"}
            elif not self.supervised_loaded(name):
                runtime = {"state": "unsupervised"}
            else:
                runtime = self.status.get(name, {"state": "starting"})
            if name in self.loaded and not view["enabled"] and runtime.get("state") not in ("stopped",):
                runtime = {**runtime, "note": "disabled; route removed on next proxy restart"}
            view["supervise_effective"] = (
                False if view["supervise"] is False else {**self.defaults, **view["supervise"]}
            )
            servers.append({**view, "runtime": runtime, "history": list(self.history.get(name, []))})
        removed = [n for n in self.loaded if n not in names]
        proxy = self.proxy
        running = proxy is not None and proxy.returncode is None
        return {
            "proxy": {
                "state": self.proxy_state if running or self.proxy_state != "running" else "stopped",
                "pid": proxy.pid if running else None,
                "rss": proc_rss(proxy.pid) if running else 0,
                "uptime": time.monotonic() - self.proxy_started if running else None,
                "restarts": self.proxy_restarts,
                "versions": self.versions,
                "publicPort": self.public_proxy_port,
            },
            "container": {
                "memCurrent": read_int("/sys/fs/cgroup/memory.current"),
                "memMax": read_int("/sys/fs/cgroup/memory.max"),
            },
            "defaults": self.defaults,
            "pending": self.pending(),
            "removed": removed,
            "servers": servers,
            "now": time.time(),
        }

    def merge_env(self, name: str, entries: list) -> tuple[dict, dict]:
        """UI env rows -> (public env, secret env). Empty secret value keeps the stored one."""
        if not isinstance(entries, list):
            raise ValidationError("env must be a list of {key, value, secret}")
        old_secret = {}
        if self.store.exists(name):
            _, old_secret = self.store.load(name)
        public, secret = {}, {}
        for row in entries:
            key = str(row.get("key", "")).strip()
            if not key:
                continue
            value = str(row.get("value", ""))
            if row.get("secret"):
                if value == "" and key in old_secret:
                    value = old_secret[key]
                secret[key] = value
            else:
                public[key] = value
        dup = set(public) & set(secret)
        if dup:
            raise ValidationError(f"env: duplicate variable(s): {', '.join(sorted(dup))}")
        return public, secret

    async def save_server(self, name: str, body: dict, create: bool) -> dict:
        if create and self.store.exists(name):
            raise ValidationError(f"server '{name}' already exists")
        if not create and not self.store.exists(name):
            raise LookupError(name)
        public_env, secret_env = self.merge_env(name, body.get("env", []))
        public = validate(name, {**body, "env": public_env})
        self.store.save(name, public, secret_env)
        return await self.apply(name)

    async def apply(self, name: str) -> dict:
        """Make a saved change live where possible without restarting mcp-proxy."""
        eff = self.effective(name)
        if not self.supervised_loaded(name) or eff["supervise_effective"] is False:
            return {"applied": False, "needsProxyRestart": any(p["name"] == name for p in self.pending())}
        was_enabled = self.loaded[name]["enabled"]
        self.write_definition(name, eff)
        self.loaded[name] = eff
        if eff["enabled"] and not was_enabled:
            result = await self.ctl(name, "start", 120)
            await self.refresh_status(name)
            return {"applied": bool(result.get("ok")), "action": "started", "error": result.get("error")}
        if not eff["enabled"]:
            await self.ctl(name, "stop", 30)
            await self.refresh_status(name)
            return {"applied": True, "action": "stopped"}
        state = self.status.get(name, {}).get("state")
        if state in ("stopped", "sleeping"):
            return {"applied": True, "action": "saved; used at next start"}
        result = await self.ctl(name, "restart", 120)
        await self.refresh_status(name)
        return {"applied": bool(result.get("ok")), "action": "restarted", "error": result.get("error")}

    async def refresh_status(self, name: str) -> None:
        try:
            self.status[name] = await self.ctl(name, "status", 3)
        except (OSError, asyncio.TimeoutError, ValueError):
            pass

    async def server_action(self, name: str, action: str) -> dict:
        if not self.supervised_loaded(name):
            raise ValidationError(f"'{name}' is not loaded in mcp-proxy or not supervised; restart the proxy first")
        timeout = 120 if action in ("restart", "start") else 30
        result = await self.ctl(name, action, timeout)
        await self.refresh_status(name)
        return result

    async def delete_server(self, name: str) -> dict:
        if not self.store.exists(name):
            raise LookupError(name)
        stopped = False
        if self.supervised_loaded(name):
            try:
                await self.ctl(name, "stop", 30)
                await self.refresh_status(name)
                stopped = True
            except (OSError, asyncio.TimeoutError, ValueError):
                pass
        self.store.delete(name)
        self.history.pop(name, None)
        return {"deleted": True, "stopped": stopped}

    # -- HTTP ---------------------------------------------------------------------

    def authorized(self, headers: dict) -> bool:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return False
        try:
            user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user.encode(), b"admin") & hmac.compare_digest(
            password.encode(), self.password.encode()
        )

    async def route(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, object]:
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/") if p]
        if method in ("POST", "PUT", "DELETE") and headers.get("x-mcp-manager") != "1":
            return 403, {"error": "missing X-MCP-Manager header"}
        data = {}
        if body:
            try:
                data = json.loads(body)
            except ValueError:
                return 400, {"error": "invalid JSON"}
        if parts == ["api", "state"] and method == "GET":
            return 200, self.state()
        if parts == ["api", "proxy", "restart"] and method == "POST":
            asyncio.create_task(self.restart_proxy())
            return 202, {"ok": True}
        if parts == ["api", "import"] and method == "POST":
            imported = self.store.import_mcp_servers(data.get("config", {}), bool(data.get("overwrite")))
            for name in imported:
                if self.supervised_loaded(name):
                    await self.apply(name)
            return 200, {"imported": imported}
        if parts == ["api", "servers"] and method == "POST":
            name = str(data.get("name", "")).strip()
            return 201, await self.save_server(name, data, create=True)
        if len(parts) >= 3 and parts[:2] == ["api", "servers"]:
            name = parts[2]
            if not NAME_RE.match(name):
                return 404, {"error": "no such server"}
            if len(parts) == 3:
                if method == "GET":
                    if not self.store.exists(name):
                        return 404, {"error": "no such server"}
                    return 200, self.public_view(name)
                if method == "PUT":
                    return 200, await self.save_server(name, data, create=False)
                if method == "DELETE":
                    return 200, await self.delete_server(name)
            if len(parts) == 4:
                if parts[3] == "log" and method == "GET":
                    if not self.supervised_loaded(name):
                        return 200, {"lines": ["(no log: server is not supervised or not loaded)"]}
                    return 200, await self.ctl(name, "log", 5)
                if parts[3] in ("start", "stop", "restart") and method == "POST":
                    return 200, await self.server_action(name, parts[3])
        return 404, {"error": "not found"}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, ctype, payload = 500, "application/json", b""
        try:
            request_line = await asyncio.wait_for(reader.readline(), 30)
            method, target, _ = request_line.decode("latin-1").split(" ", 2)
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), 30)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            if length > MAX_BODY:
                status, payload = 413, b'{"error": "body too large"}'
                raise _Respond
            body = await reader.readexactly(length) if length else b""
            path = urllib.parse.urlsplit(target).path
            if path == "/healthz":
                status, payload = 204, b""
            elif not self.authorized(headers):
                await asyncio.sleep(1)  # slow down password guessing
                status, ctype, payload = 401, "text/plain", b"authentication required\n"
            elif path in ("/", "/index.html") and method == "GET":
                status, ctype, payload = 200, "text/html; charset=utf-8", PAGE.encode()
            else:
                try:
                    status, result = await self.route(method, path, headers, body)
                except ValidationError as exc:
                    status, result = 400, {"error": str(exc)}
                except LookupError:
                    status, result = 404, {"error": "no such server"}
                except (OSError, asyncio.TimeoutError) as exc:
                    status, result = 502, {"error": f"server control failed: {exc or type(exc).__name__}"}
                payload = json.dumps(result).encode()
        except _Respond:
            pass
        except (ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            status, payload = 400, b'{"error": "bad request"}'
        except Exception as exc:  # noqa: BLE001
            log(f"request failed: {exc!r}")
            status, payload = 500, b'{"error": "internal error"}'
        reason = {200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 400: "Bad Request",
                  401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large",
                  500: "Internal Server Error", 502: "Bad Gateway"}.get(status, "OK")
        head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {ctype}", f"Content-Length: {len(payload)}",
                "Connection: close", "Cache-Control: no-store", "X-Content-Type-Options: nosniff",
                "X-Frame-Options: DENY", "Referrer-Policy: no-referrer"]
        if status == 401:
            head.append('WWW-Authenticate: Basic realm="mcp-manager", charset="UTF-8"')
        try:
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
            await writer.drain()
            writer.close()
        except (ConnectionError, OSError):
            pass

    # -- main -----------------------------------------------------------------------

    async def run(self, import_path: str | None) -> int:
        if import_path and not self.store.names() and os.path.exists(import_path):
            try:
                imported = self.store.import_servers_json(import_path)
                log(f"imported {len(imported)} server(s) from {import_path} into {self.store.dir}: {', '.join(imported)}")
            except (OSError, ValueError) as exc:
                log(f"could not import {import_path}: {exc}")
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, done.set)
        await self.start_proxy()
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.port)
        log(f"admin page on port {self.port} (user 'admin')")
        tasks = [asyncio.create_task(self.watch_proxy()), asyncio.create_task(self.sample())]
        await done.wait()
        log("shutting down")
        server.close()
        for t in tasks:
            t.cancel()
        await self.stop_proxy()
        return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="mcp-manager", description=__doc__.split("\n\n")[0])
    p.add_argument("--servers-dir", default="/mcp/servers.d")
    p.add_argument("--import", dest="import_path", default="/mcp/servers.json",
                   help="servers.json imported once when the servers directory is empty")
    p.add_argument("--runtime-dir", default="/tmp/mcp-manager")
    p.add_argument("--control-dir", default="/run/mcp-supervise")
    p.add_argument("--proxy-config", default="/tmp/mcp-manager/servers.json",
                   help="generated config; must match mcp-proxy's --named-server-config")
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_MANAGER_PORT", "8001")))
    p.add_argument("proxy", nargs=argparse.REMAINDER, help="-- mcp-proxy command line")
    args = p.parse_args()
    if args.proxy and args.proxy[0] == "--":
        args.proxy = args.proxy[1:]
    if not args.proxy:
        p.error("missing mcp-proxy command after --")
    return asyncio.run(Manager(args).run(args.import_path))


# --- admin page -------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP Manager</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #667085; --line: #e3e6eb;
  --accent: #2f6fde; --accent-text: #ffffff; --ok: #1a8a4a; --warn: #b26b00; --bad: #c4302b;
  --idle: #6b7280; --chip: #eef1f5; --code: #f1f3f6; --shadow: 0 1px 2px rgba(16,24,40,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1218; --panel: #171b23; --text: #e6e9ef; --muted: #9aa3b2; --line: #2a303b;
    --accent: #5b8ff0; --accent-text: #0b1220; --ok: #3fbf74; --warn: #e3a33a; --bad: #f06a62;
    --idle: #8b93a1; --chip: #222833; --code: #1e232d; --shadow: none;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { position: sticky; top: 0; z-index: 5; background: var(--panel); border-bottom: 1px solid var(--line); }
.bar { max-width: 1280px; margin: 0 auto; padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; }
h1 { font-size: 17px; margin: 0; font-weight: 650; letter-spacing: -.01em; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--muted); font-size: 12.5px; align-items: center; flex: 1; min-width: 0; }
.meta b { color: var(--text); font-weight: 600; }
.meta > span { white-space: nowrap; }
@media (max-width: 640px) { .meta { order: 3; flex-basis: 100%; } .actions { margin-left: auto; } }
input[readonly] { opacity: .65; cursor: not-allowed; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
main { max-width: 1280px; margin: 0 auto; padding: 16px; }
button { font: inherit; border: 1px solid var(--line); background: var(--panel); color: var(--text);
  border-radius: 8px; padding: 6px 12px; cursor: pointer; }
button:hover { border-color: var(--muted); }
button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); font-weight: 600; }
button.danger { color: var(--bad); }
button:disabled { opacity: .45; cursor: default; }
.banner { border: 1px solid var(--warn); background: color-mix(in srgb, var(--warn) 10%, var(--panel));
  border-radius: 10px; padding: 10px 14px; margin-bottom: 14px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.banner p { margin: 0; flex: 1; min-width: 220px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
@media (max-width: 420px) { .grid { grid-template-columns: 1fr; } }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.head { display: flex; gap: 8px; align-items: center; }
.head h2 { font-size: 15px; margin: 0; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.desc { color: var(--muted); font-size: 12.5px; margin-top: -6px; }
.badge { margin-left: auto; font-size: 11.5px; font-weight: 600; padding: 2px 9px; border-radius: 99px; background: var(--chip); white-space: nowrap; }
.badge::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; background: currentColor; vertical-align: 1px; }
.s-running { color: var(--ok); } .s-sleeping, .s-stopped, .s-disabled { color: var(--idle); }
.s-restarting, .s-starting, .s-not-loaded { color: var(--warn); } .s-crashed, .s-unreachable, .s-invalid { color: var(--bad); }
.s-unsupervised { color: var(--muted); }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.stat { min-width: 0; } .stat span { display: block; color: var(--muted); font-size: 11.5px; }
.stat b { font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.spark { width: 100%; height: 46px; display: block; }
.spark .line { fill: none; stroke: var(--accent); stroke-width: 1.5; }
.spark .area { fill: var(--accent); opacity: .12; }
.spark .soft { stroke: var(--warn); stroke-dasharray: 4 3; stroke-width: 1; }
.spark .hard { stroke: var(--bad); stroke-dasharray: 2 3; stroke-width: 1; }
.mem { display: flex; justify-content: space-between; color: var(--muted); font-size: 11.5px; }
code, .cmd { font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
.cmd { background: var(--code); border-radius: 6px; padding: 6px 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.endpoint { display: flex; gap: 6px; align-items: center; font-size: 12px; color: var(--muted); min-width: 0; }
.endpoint code { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.endpoint button { padding: 1px 8px; font-size: 11.5px; }
.event { font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.foot { display: flex; gap: 6px; flex-wrap: wrap; margin-top: auto; }
.foot .right { margin-left: auto; }
.empty { text-align: center; color: var(--muted); padding: 60px 16px; }
dialog { border: 1px solid var(--line); border-radius: 14px; padding: 0; background: var(--panel); color: var(--text);
  width: min(720px, calc(100vw - 32px)); max-height: calc(100vh - 32px); }
dialog::backdrop { background: rgba(0,0,0,.4); }
.dlg { display: flex; flex-direction: column; max-height: calc(100vh - 34px); }
.dlg h3 { margin: 0; padding: 14px 18px; border-bottom: 1px solid var(--line); font-size: 15px; }
.dlg .body { padding: 14px 18px; overflow: auto; display: flex; flex-direction: column; gap: 12px; }
.dlg .btns { padding: 12px 18px; border-top: 1px solid var(--line); display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; color: var(--muted); }
label.inline { flex-direction: row; align-items: center; gap: 8px; color: var(--text); }
input, textarea, select { font: inherit; color: var(--text); background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; width: 100%; }
textarea { font: 12.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; resize: vertical; }
input[type=checkbox] { width: auto; }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.row4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
@media (max-width: 560px) { .row2, .row4 { grid-template-columns: 1fr 1fr; } }
fieldset { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin: 0; display: flex; flex-direction: column; gap: 10px; }
legend { font-size: 12.5px; color: var(--muted); padding: 0 4px; }
.envrow { display: grid; grid-template-columns: 1.1fr 1.6fr auto auto; gap: 6px; align-items: center; }
.envrow label.inline { font-size: 12px; }
.hint { color: var(--muted); font-size: 12px; margin: 0; }
.err { color: var(--bad); font-size: 12.5px; min-height: 1em; margin: 0; }
pre.log { margin: 0; background: var(--code); border-radius: 8px; padding: 10px; font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: pre-wrap; word-break: break-all; min-height: 320px; max-height: 60vh; overflow: auto; }
.toast { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg);
  padding: 9px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; max-width: calc(100vw - 32px); z-index: 20; }
.toast.show { opacity: 1; }
.membar { width: 120px; height: 6px; background: var(--chip); border-radius: 99px; overflow: hidden; display: inline-block; vertical-align: middle; }
.membar i { display: block; height: 100%; background: var(--accent); }
</style>
</head>
<body>
<header><div class="bar">
  <h1>MCP Manager</h1>
  <div class="meta" id="meta">loading…</div>
  <div class="actions">
    <button id="btnImport">Import JSON</button>
    <button id="btnProxy">Restart proxy</button>
    <button class="primary" id="btnAdd">+ Add server</button>
  </div>
</div></header>
<main>
  <div id="banner"></div>
  <div class="grid" id="grid"></div>
</main>

<dialog id="dlgEdit"><form class="dlg" method="dialog" id="formEdit">
  <h3 id="editTitle">Add server</h3>
  <div class="body">
    <div class="row2">
      <label>Name<input name="name" required pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}" placeholder="music-assistant"></label>
      <label>Description<input name="description" placeholder="optional"></label>
    </div>
    <div class="row2">
      <label>Command<input name="command" required list="commands" placeholder="uvx"></label>
      <label class="inline" style="align-self:end;padding-bottom:8px"><input type="checkbox" name="enabled" checked> Enabled</label>
    </div>
    <datalist id="commands"><option value="uvx"><option value="npx"><option value="python3"><option value="node"></datalist>
    <label>Arguments <span class="hint">one per line</span>
      <textarea name="args" rows="5" placeholder="--from&#10;git+https://github.com/owner/repo&#10;server-name"></textarea></label>
    <fieldset><legend>Environment</legend>
      <div id="envRows"></div>
      <div><button type="button" id="btnEnvAdd">+ Variable</button></div>
      <p class="hint">Secret values are stored in a separate file (mode 600) and never shown again. Leave a secret empty to keep its stored value.</p>
    </fieldset>
    <fieldset><legend>Supervision</legend>
      <label class="inline"><input type="checkbox" name="supervised" checked> Supervise (per-server restart, stop/start, limits, stats)</label>
      <div class="row4" id="supFields">
        <label>Max RSS<input name="maxRss" placeholder="800M"></label>
        <label>Hard RSS<input name="hardRss" placeholder="2G"></label>
        <label>Max age<input name="maxAge" placeholder="24h"></label>
        <label>Idle stop<input name="idleStop" placeholder="2h"></label>
      </div>
      <p class="hint" id="supHint">Empty = container default. Max RSS / age restart when idle; Hard RSS restarts immediately; Idle stop frees RAM and wakes on the next request.</p>
    </fieldset>
    <p class="err" id="editErr"></p>
  </div>
  <div class="btns">
    <button type="button" value="cancel" id="btnEditCancel">Cancel</button>
    <button class="primary" type="submit" id="btnEditSave">Save</button>
  </div>
</form></dialog>

<dialog id="dlgImport"><form class="dlg" method="dialog" id="formImport">
  <h3>Import servers</h3>
  <div class="body">
    <p class="hint">Paste an <code>mcpServers</code> JSON block, e.g. from a server's README or your old servers.json. Secret-looking variables (TOKEN, KEY, SECRET, PASSWORD…) are stored as secrets.</p>
    <textarea name="json" rows="14" placeholder='{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}'></textarea>
    <label class="inline"><input type="checkbox" name="overwrite"> Overwrite servers with the same name</label>
    <p class="err" id="importErr"></p>
  </div>
  <div class="btns">
    <button type="button" id="btnImportCancel">Cancel</button>
    <button class="primary" type="submit">Import</button>
  </div>
</form></dialog>

<dialog id="dlgLog"><div class="dlg">
  <h3 id="logTitle">Log</h3>
  <div class="body"><pre class="log" id="logBody"></pre></div>
  <div class="btns"><button type="button" id="btnLogClose">Close</button></div>
</div></dialog>

<div class="toast" id="toast"></div>

<script>
"use strict";
const $ = (s, el = document) => el.querySelector(s);
let state = null, editing = null, logTimer = null;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const mb = n => n == null ? "–" : n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + "G" : Math.round(n / 1024 ** 2) + "M";
const age = s => s == null ? "–" : s < 60 ? Math.round(s) + "s" : s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? (s / 3600).toFixed(1) + "h" : (s / 86400).toFixed(1) + "d";
const ago = t => t ? age(Date.now() / 1000 - t) + " ago" : "never";
function parseSize(v) {
  if (!v) return null; const m = String(v).trim().toUpperCase().match(/^([\d.]+)\s*([KMGT]?)/); if (!m) return null;
  return parseFloat(m[1]) * ({"": 1, K: 1024, M: 1024 ** 2, G: 1024 ** 3, T: 1024 ** 4}[m[2]]);
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 3500); }

async function api(method, path, body) {
  const opt = {method, headers: {"X-MCP-Manager": "1"}};
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.status + " " + r.statusText);
  return data;
}

function spark(hist, limits) {
  const w = 300, h = 46;
  const vals = hist.map(p => p[1]);
  const soft = parseSize(limits && limits.maxRss), hard = parseSize(limits && limits.hardRss);
  const top = Math.max(1, ...vals, soft || 0, hard || 0) * 1.08;
  if (vals.length < 2) return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"></svg>`;
  const x = i => (i / (vals.length - 1)) * w, y = v => h - 2 - (v / top) * (h - 4);
  const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const line = (v, cls) => v ? `<line class="${cls}" x1="0" x2="${w}" y1="${y(v)}" y2="${y(v)}"/>` : "";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="memory, last hour">
    <polygon class="area" points="0,${h} ${pts} ${w},${h}"/><polyline class="line" points="${pts}"/>${line(soft, "soft")}${line(hard, "hard")}</svg>`;
}

function render() {
  const s = state, p = s.proxy, c = s.container;
  const memPct = c.memMax ? Math.min(100, 100 * c.memCurrent / c.memMax) : null;
  $("#meta").innerHTML = [
    `proxy <b class="s-${esc(p.state)}">${esc(p.state)}</b>${p.pid ? ` · up ${age(p.uptime)} · ${mb(p.rss)}` : ""}`,
    p.versions ? esc(p.versions) : "",
    c.memCurrent != null ? `container <b>${mb(c.memCurrent)}</b>${c.memMax ? ` / ${mb(c.memMax)} <span class="membar"><i style="width:${memPct}%"></i></span>` : ""}` : "",
    `${s.servers.length} servers`,
  ].filter(Boolean).map(x => `<span>${x}</span>`).join("");

  const banner = [];
  if (s.pending.length) banner.push(`<div class="banner"><p><b>${s.pending.length} change(s) need a proxy restart:</b> ${s.pending.map(x => `${esc(x.name)} (${esc(x.why)})`).join(", ")}. All clients reconnect after a few seconds.</p><button class="primary" data-act="proxy">Restart proxy now</button></div>`);
  if (s.removed.length) banner.push(`<div class="banner"><p>Deleted but still routed until the next proxy restart: ${s.removed.map(esc).join(", ")} (stopped, requests get an error).</p></div>`);
  $("#banner").innerHTML = banner.join("");

  if (!s.servers.length) { $("#grid").innerHTML = `<div class="empty">No servers yet. Use <b>+ Add server</b> or <b>Import JSON</b>.</div>`; return; }
  const host = location.hostname, port = p.publicPort || "8005";
  $("#grid").innerHTML = s.servers.map(srv => {
    const r = srv.runtime || {}, st = r.state || "unknown", ctl = srv.supervise !== false && !["not loaded", "disabled", "unsupervised", "invalid"].includes(st);
    const lim = srv.supervise_effective || {};
    const url = `http://${host}:${port}/servers/${encodeURIComponent(srv.name)}/mcp`;
    const ev = (r.events || []).slice(-1)[0];
    const running = ["running", "restarting", "starting", "crashed"].includes(st);  // crashed = auto-restart pending
    return `<article class="card" data-name="${esc(srv.name)}">
      <div class="head"><h2 title="${esc(srv.name)}">${esc(srv.name)}</h2><span class="badge s-${esc(st.replace(" ", "-"))}">${esc(st)}</span></div>
      ${srv.description ? `<div class="desc">${esc(srv.description)}</div>` : ""}
      ${srv.error ? `<div class="err">${esc(srv.error)}</div>` : ""}
      <div class="endpoint"><code title="${esc(url)}">${esc(url)}</code><button data-act="copy" data-url="${esc(url)}">copy</button></div>
      ${ctl ? `<div><div class="mem"><span>RAM ${mb(r.rss)}</span><span>${[lim.maxRss && "soft " + lim.maxRss, lim.hardRss && "hard " + lim.hardRss].filter(Boolean).join(" · ") || "no limit"}</span></div>${spark(srv.history || [], lim)}</div>
      <div class="stats">
        <div class="stat"><span>Uptime</span><b>${age(r.uptime)}</b></div>
        <div class="stat"><span>Requests</span><b>${r.requests ?? "–"}${r.errors ? ` <span class="s-crashed">(${r.errors} err)</span>` : ""}</b></div>
        <div class="stat"><span>Restarts</span><b>${r.restarts ?? "–"}${r.crashes ? ` <span class="s-crashed">(${r.crashes} crash)</span>` : ""}</b></div>
        <div class="stat"><span>Last used</span><b>${r.last_request ? ago(r.last_request) : "never"}</b></div>
      </div>
      <div class="event">${ev ? `${ago(ev.time)}: ${esc(ev.event)}` : "&nbsp;"}${r.note ? " · " + esc(r.note) : ""}</div>` :
      `<p class="hint">${st === "not loaded" ? "Saved, but mcp-proxy has not loaded it yet: restart the proxy." : st === "disabled" ? "Disabled: not started." : st === "unsupervised" ? "Running without supervision: no stats or controls." : ""}</p>`}
      <div class="cmd" title="${esc([srv.command, ...(srv.args || [])].join(" "))}">${esc([srv.command, ...(srv.args || [])].join(" "))}</div>
      <div class="foot">
        ${ctl ? (running ? `<button data-act="stop">Stop</button>` : `<button data-act="start">Start</button>`) : ""}
        ${ctl ? `<button data-act="restart" ${running ? "" : "disabled"}>Restart</button>` : ""}
        ${ctl ? `<button data-act="log">Logs</button>` : ""}
        <button data-act="edit">Edit</button>
        <button class="danger right" data-act="delete">Delete</button>
      </div></article>`;
  }).join("");
}

async function refresh() {
  try { state = await api("GET", "/api/state"); render(); }
  catch (e) { $("#meta").textContent = "cannot reach manager: " + e.message; }
}

document.addEventListener("click", async ev => {
  const b = ev.target.closest("button[data-act]"); if (!b) return;
  const act = b.dataset.act, card = b.closest(".card"), name = card && card.dataset.name;
  try {
    if (act === "copy") { await navigator.clipboard.writeText(b.dataset.url); toast("Endpoint copied"); return; }
    if (act === "proxy") return restartProxy();
    if (act === "edit") return openEdit(name);
    if (act === "log") return openLog(name);
    if (act === "delete") {
      if (!confirm(`Delete "${name}"? Its definition and secrets are removed and the server is stopped.`)) return;
      await api("DELETE", `/api/servers/${encodeURIComponent(name)}`); toast(`${name} deleted`);
    } else {
      if (act === "stop" && !confirm(`Stop "${name}"? Requests get an error until you start it again.`)) return;
      b.disabled = true; toast(`${act} ${name}…`);
      const r = await api("POST", `/api/servers/${encodeURIComponent(name)}/${act}`);
      toast(r.ok === false ? `${name}: ${r.error || "failed"}` : `${name}: ${act} done`);
    }
  } catch (e) { toast(e.message); }
  refresh();
});

async function restartProxy() {
  if (!confirm("Restart mcp-proxy? All servers restart and every client reconnects.")) return;
  await api("POST", "/api/proxy/restart"); toast("Restarting proxy…"); setTimeout(refresh, 1500);
}
$("#btnProxy").onclick = () => restartProxy().catch(e => toast(e.message));

function envRow(row = {key: "", value: "", secret: false}) {
  const d = document.createElement("div"); d.className = "envrow";
  d.innerHTML = `<input placeholder="NAME" value="${esc(row.key)}" aria-label="variable name">
    <input placeholder="${row.secret && row.hasValue ? "•••••• (stored; leave empty to keep)" : "value"}" type="${row.secret ? "password" : "text"}" value="${esc(row.value)}" aria-label="value">
    <label class="inline"><input type="checkbox" ${row.secret ? "checked" : ""}> secret</label>
    <button type="button" aria-label="remove">✕</button>`;
  const [, val] = d.querySelectorAll("input"), chk = d.querySelector("input[type=checkbox]");
  chk.onchange = () => { val.type = chk.checked ? "password" : "text"; };
  d.querySelector("button").onclick = () => d.remove();
  $("#envRows").append(d);
}
$("#btnEnvAdd").onclick = () => envRow();

function syncSup() { $("#supFields").style.opacity = $("#formEdit").supervised.checked ? 1 : .4; }
$("#formEdit").supervised.onchange = syncSup;

async function openEdit(name) {
  const f = $("#formEdit"); f.reset(); $("#envRows").innerHTML = ""; $("#editErr").textContent = "";
  editing = name || null;
  $("#editTitle").textContent = name ? `Edit ${name}` : "Add server";
  f.name.readOnly = !!name;
  const d = state.defaults || {};
  for (const k of ["maxRss", "hardRss", "maxAge", "idleStop"]) f[k].placeholder = d[k] ? `default ${d[k]}` : "none";
  if (name) {
    const s = await api("GET", `/api/servers/${encodeURIComponent(name)}`);
    f.name.value = s.name; f.description.value = s.description; f.command.value = s.command;
    f.args.value = (s.args || []).join("\n"); f.enabled.checked = s.enabled;
    f.supervised.checked = s.supervise !== false;
    for (const k of ["maxRss", "hardRss", "maxAge", "idleStop"]) f[k].value = (s.supervise || {})[k] || "";
    (s.env || []).forEach(envRow);
  }
  syncSup(); $("#dlgEdit").showModal();
}
$("#btnAdd").onclick = () => openEdit(null);
$("#btnEditCancel").onclick = () => $("#dlgEdit").close();
$("#formEdit").onsubmit = async ev => {
  ev.preventDefault();
  const f = ev.target, btn = $("#btnEditSave");
  const body = {
    name: f.name.value.trim(), description: f.description.value.trim(), enabled: f.enabled.checked,
    command: f.command.value.trim(), args: f.args.value.split("\n").map(s => s.trim()).filter(Boolean),
    env: [...$("#envRows").children].map(r => { const i = r.querySelectorAll("input"); return {key: i[0].value.trim(), value: i[1].value, secret: i[2].checked}; }),
    supervise: f.supervised.checked ? Object.fromEntries(["maxRss", "hardRss", "maxAge", "idleStop"].map(k => [k, f[k].value.trim()]).filter(([, v]) => v)) : false,
  };
  btn.disabled = true; $("#editErr").textContent = "";
  try {
    const r = editing ? await api("PUT", `/api/servers/${encodeURIComponent(editing)}`, body) : await api("POST", "/api/servers", body);
    $("#dlgEdit").close();
    toast(r.applied ? `${body.name}: saved, ${r.action}` : r.needsProxyRestart ? `${body.name}: saved — restart the proxy to load it` : `${body.name}: saved`);
  } catch (e) { $("#editErr").textContent = e.message; }
  btn.disabled = false; refresh();
};

$("#btnImport").onclick = () => { $("#formImport").reset(); $("#importErr").textContent = ""; $("#dlgImport").showModal(); };
$("#btnImportCancel").onclick = () => $("#dlgImport").close();
$("#formImport").onsubmit = async ev => {
  ev.preventDefault(); const f = ev.target;
  let config; try { config = JSON.parse(f.json.value); } catch (e) { $("#importErr").textContent = "Not valid JSON: " + e.message; return; }
  try {
    const r = await api("POST", "/api/import", {config, overwrite: f.overwrite.checked});
    $("#dlgImport").close(); toast(r.imported.length ? `Imported: ${r.imported.join(", ")}` : "Nothing imported (names already exist)");
  } catch (e) { $("#importErr").textContent = e.message; }
  refresh();
};

async function loadLog(name) {
  const r = await api("GET", `/api/servers/${encodeURIComponent(name)}/log`);
  const pre = $("#logBody"), atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.textContent = (r.lines || []).join("\n") || "(empty)";
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}
async function openLog(name) {
  $("#logTitle").textContent = `${name} — last 200 lines`; $("#logBody").textContent = "loading…";
  $("#dlgLog").showModal(); await loadLog(name); $("#logBody").scrollTop = 1e9;
  clearInterval(logTimer); logTimer = setInterval(() => loadLog(name).catch(() => {}), 2000);
}
$("#btnLogClose").onclick = () => $("#dlgLog").close();
$("#dlgLog").addEventListener("close", () => clearInterval(logTimer));

refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 3000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
