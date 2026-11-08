#!/usr/bin/env python3
"""mcp-supervise: restart, stop or start one stdio MCP server without mcp-proxy noticing.

mcp-proxy starts each stdio server once and shares it between all clients; if
one exits, the whole proxy shuts down. This shim sits between mcp-proxy and a
server:

    mcp-proxy <-stdio-> mcp-supervise <-stdio-> server (uvx/npx/...)
                              ^
                              +-- control socket (status / restart / stop / start)

and restarts the server when
  * its memory (RSS of the whole process tree) exceeds --max-rss, or it has run
    longer than --max-age - only while no request is in flight, so nothing is
    interrupted;
  * its memory exceeds --hard-rss - immediately; in-flight requests get a
    JSON-RPC error instead of hanging;
  * it crashes - after a short backoff. More than --max-crashes crashes within
    --crash-window: it is marked "failed" and retried with a longer backoff.
A server that crashes or hangs while starting (no answer within
--start-timeout) never takes mcp-proxy down: the shim answers mcp-proxy's
initialize itself, marks the server "failed" (requests get an error pointing
to its log) and keeps retrying with backoff until it comes up.
With --idle-stop it stops a server nobody used for that long ("sleeping") and
starts it again on the next request.

After every (re)start the shim replays the `initialize` request and the
`notifications/initialized` notification that mcp-proxy originally sent, so the
proxy's session (and every client session behind it) keeps working. State
inside the server (e.g. resource subscriptions) is lost - fine for the usual
stateless tool servers.

With --definition FILE (written by mcp-manager: {"command", "args", "env",
"supervise"}), every (re)start reloads the server's command, args, env and
limits from that file, so an edited server only needs a restart of itself.

Control socket (--control-dir): one JSON line in, one JSON line out:
  {"cmd": "status"} | {"cmd": "log"} | {"cmd": "restart"} | {"cmd": "stop"} | {"cmd": "start"}
A server stopped this way answers requests with an error until it is started.

Usage:
  mcp-supervise [options] -- <command> [args...]
  mcp-supervise wrap-config <servers.json> <output.json>   (used by entrypoint.sh)

Sizes: 512M, 1.5G, 800K, or bytes. Durations: 30s, 15m, 12h, 2d, or seconds.
Logs go to stderr, which mcp-proxy shows in its own log.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import signal
import sys
import time

LINE_LIMIT = 64 * 1024 * 1024  # largest single JSON-RPC message
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
LOG_LINES = 200


# --- helpers -----------------------------------------------------------------


def parse_size(value: str) -> int:
    s = str(value).strip().upper().removesuffix("B").removesuffix("I")
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    mult = units.get(s[-1:], 1)
    if s[-1:] in units:
        s = s[:-1]
    n = float(s)
    if n <= 0:
        raise ValueError(f"size must be > 0: {value!r}")
    return int(n * mult)


def parse_duration(value: str) -> float:
    s = str(value).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    mult = units.get(s[-1:], 1)
    if s[-1:] in units:
        s = s[:-1]
    n = float(s)
    if n <= 0:
        raise ValueError(f"duration must be > 0: {value!r}")
    return n * mult


def fmt_size(n: int) -> str:
    return f"{n / 1024**2:.0f}M"


def fmt_age(sec: float) -> str:
    if sec >= 3600:
        return f"{sec / 3600:.1f}h"
    if sec >= 60:
        return f"{sec / 60:.0f}m"
    return f"{sec:.0f}s"


def session_rss(sid: int) -> int:
    """Sum RSS of every process in session `sid` (the server and its children).

    The server is started with start_new_session=True, so everything it spawns
    (uv -> python, npx -> node, ...) shares its session id, even if reparented.
    """
    total = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                stat = f.read()
        except OSError:
            continue
        # Fields after "(comm)": state ppid pgrp session ... rss is field 24.
        fields = stat[stat.rfind(b")") + 2 :].split()
        try:
            if int(fields[3]) == sid:
                total += int(fields[21]) * PAGE_SIZE
        except (IndexError, ValueError):
            continue
    return total


def socket_path(control_dir: str, name: str) -> str:
    return os.path.join(control_dir, re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".sock")


def id_key(msg_id: object) -> str:
    return json.dumps(msg_id, sort_keys=True)


def kind(msg: object) -> str | None:
    if not isinstance(msg, dict):
        return None
    if "method" in msg:
        return "request" if "id" in msg else "notification"
    if "id" in msg:
        return "response"
    return None


def messages(line: bytes) -> list:
    try:
        parsed = json.loads(line)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


# --- supervisor ----------------------------------------------------------------


class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.cmd: list[str] = args.command
        self.name: str = args.name or os.path.basename(self.cmd[0])
        self.max_rss = args.max_rss
        self.hard_rss = args.hard_rss
        self.max_age = args.max_age
        self.idle_stop = args.idle_stop
        self.check_interval = args.check_interval
        self.log_interval = args.log_interval
        self.max_crashes = args.max_crashes
        self.crash_window = args.crash_window
        self.init_timeout = args.init_timeout
        self.start_timeout = args.start_timeout
        self.failure: str | None = None  # why the server is in the "failed" state
        self.retry_at: float | None = None
        self.grace = args.grace
        self.control_dir = args.control_dir
        self.definition = args.definition
        self.def_env: dict[str, str] = {}
        # Environment for the server: what mcp-proxy gave us. With a definition
        # file, its "env" is replaced on every reload instead of accumulating.
        self.base_env = dict(os.environ)
        if self.definition:
            loaded = self.read_definition()
            if loaded is not None:
                for key in loaded.get("env", {}):
                    self.base_env.pop(key, None)
                self.def_env = {k: str(v) for k, v in loaded.get("env", {}).items()}

        self.child: asyncio.subprocess.Process | None = None
        self.retired: set[int] = set()  # pids we stopped on purpose
        self.started_at = 0.0
        self.generation = 0

        self.init_request: dict | None = None  # mcp-proxy's original initialize
        self.initialized_note: dict | None = None
        self.initialized = False  # first initialize answered: supervision active
        self.pending_init: tuple[str, asyncio.Future] | None = None

        self.inflight_in: dict[str, float] = {}  # proxy -> server requests
        self.inflight_out: set[str] = set()  # server -> proxy requests

        self.ready = asyncio.Event()  # the current child accepts messages
        self.lock = asyncio.Lock()  # child swap and writes to the child
        self.restarting = False
        self.paused: str | None = None  # None | "stopped" (manual) | "sleeping" (idle-stop) | "failed"
        self.crashes: collections.deque[float] = collections.deque()
        self.stopping = False

        # statistics for the control socket
        self.created = time.time()
        self.restarts = 0
        self.crash_total = 0
        self.requests = 0
        self.errors = 0
        self.last_activity = time.monotonic()
        self.last_request_wall: float | None = None
        self.events: collections.deque[dict] = collections.deque(maxlen=20)
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=LOG_LINES)

    # -- output ------------------------------------------------------------

    def log(self, msg: str) -> None:
        line = f"mcp-supervise[{self.name}]: {msg}"
        self.stderr_tail.append(time.strftime("%H:%M:%S ") + line)
        sys.stderr.write(line + "\n")
        sys.stderr.flush()

    def event(self, what: str) -> None:
        self.events.append({"time": time.time(), "event": what})

    def to_proxy(self, data: bytes | dict) -> None:
        if isinstance(data, dict):
            data = json.dumps(data, separators=(",", ":")).encode() + b"\n"
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            self.stopping = True

    def error_to_proxy(self, msg_id: object, message: str) -> None:
        self.errors += 1
        self.to_proxy({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": message}})

    def fail_inflight(self, reason: str) -> None:
        for key in list(self.inflight_in):
            self.error_to_proxy(
                json.loads(key),
                f"MCP server '{self.name}' was restarted or stopped by mcp-supervise ({reason}); please retry",
            )
        self.inflight_in.clear()
        self.inflight_out.clear()

    # -- definition reload -------------------------------------------------

    def read_definition(self) -> dict | None:
        try:
            with open(self.definition, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data.get("command"), str) or not isinstance(data.get("args", []), list):
                raise ValueError("needs a string \"command\" and a list \"args\"")
            return data
        except (OSError, ValueError) as exc:
            self.log(f"definition {self.definition} not used: {exc}")
            return None

    def reload_definition(self) -> None:
        data = self.read_definition() if self.definition else None
        if data is None:
            return
        cmd = [data["command"], *[str(a) for a in data.get("args", [])]]
        env = {k: str(v) for k, v in data.get("env", {}).items()}
        sup = data.get("supervise") or {}
        limits = {}
        try:
            for key, attr in (("maxRss", "max_rss"), ("hardRss", "hard_rss"), ("maxAge", "max_age"), ("idleStop", "idle_stop")):
                value = sup.get(key)
                limits[attr] = (parse_size if key.endswith("Rss") else parse_duration)(str(value)) if value else None
        except ValueError as exc:
            self.log(f"definition limits not used: {exc}")
            limits = {}
        changed = cmd != self.cmd or env != self.def_env
        self.cmd, self.def_env = cmd, env
        for attr, value in limits.items():
            setattr(self, attr, value)
        if changed:
            self.log(f"definition reloaded: {' '.join(cmd)}")

    # -- child process -----------------------------------------------------

    async def spawn(self) -> None:
        self.child = await asyncio.create_subprocess_exec(
            *self.cmd,
            env={**self.base_env, **self.def_env},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,  # copied to our stderr and kept for status
            start_new_session=True,
            limit=LINE_LIMIT,
        )
        self.generation += 1
        self.started_at = time.monotonic()
        self.last_activity = self.started_at
        asyncio.create_task(self.pump_child(self.child))
        asyncio.create_task(self.pump_stderr(self.child))

    async def stop_child(self, child: asyncio.subprocess.Process) -> None:
        """MCP stdio shutdown: close stdin, then SIGTERM, then SIGKILL."""
        self.retired.add(child.pid)
        try:
            if child.stdin:
                child.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    break
            try:
                await asyncio.wait_for(child.wait(), self.grace)
                break
            except asyncio.TimeoutError:
                continue
        try:  # leftovers in the server's process group
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def write_child(self, line: bytes) -> None:
        assert self.child and self.child.stdin
        self.child.stdin.write(line)
        await self.child.stdin.drain()

    async def pump_stderr(self, child: asyncio.subprocess.Process) -> None:
        assert child.stderr
        while True:
            try:
                line = await child.stderr.readline()
            except ValueError:
                continue
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\n")
            self.stderr_tail.append(time.strftime("%H:%M:%S ") + text)
            sys.stderr.write(text + "\n")
            sys.stderr.flush()

    async def pump_child(self, child: asyncio.subprocess.Process) -> None:
        assert child.stdout
        while True:
            try:
                line = await child.stdout.readline()
            except ValueError:  # message larger than LINE_LIMIT
                self.log("message from server exceeds the size limit; dropping it")
                continue
            if not line:
                break
            if child.pid in self.retired or child is not self.child:
                continue  # late output of a server we are replacing
            self.from_child(line)
        rc = await child.wait()
        if child.pid in self.retired or self.stopping:
            return
        if self.pending_init is not None and not self.pending_init[1].done():
            self.pending_init[1].set_result({"error": f"server exited ({rc}) while starting"})
            return  # restart() reports the failure
        asyncio.create_task(self.on_crash(rc))

    def from_child(self, line: bytes) -> None:
        self.last_activity = time.monotonic()
        for msg in messages(line):
            k = kind(msg)
            if k == "response":
                key = id_key(msg["id"])
                if self.pending_init and key == self.pending_init[0]:
                    if not self.pending_init[1].done():
                        self.pending_init[1].set_result(msg)
                    return  # our replayed initialize: not for the proxy
                if key not in self.inflight_in:
                    return  # already failed by a restart: drop
                del self.inflight_in[key]
                if "error" in msg:
                    self.errors += 1
                if self.init_request is not None and key == id_key(self.init_request.get("id")):
                    if not self.initialized:
                        self.initialized = True
                        self.event("started")
                        self.log(f"supervising pid {self.child.pid if self.child else '?'}: {self.limits()}")
            elif k == "request":
                self.inflight_out.add(id_key(msg["id"]))
        self.to_proxy(line)

    # -- messages from mcp-proxy ---------------------------------------------

    async def pump_proxy(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=LINE_LIMIT)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while not self.stopping:
            try:
                line = await reader.readline()
            except ValueError:
                self.log("message from mcp-proxy exceeds the size limit; dropping it")
                continue
            if not line:
                break  # mcp-proxy closed stdin: shut down
            await self.from_proxy(line)

    async def from_proxy(self, line: bytes) -> None:
        msgs = messages(line)
        requests = [m for m in msgs if kind(m) == "request"]
        while True:
            await self.ready.wait()
            if self.paused is not None:
                for m in msgs:
                    if kind(m) == "notification" and m.get("method") == "notifications/initialized":
                        self.initialized_note = m
                if not requests:
                    return  # notifications / stale responses for a stopped server
                if self.paused == "failed":
                    for m in requests:
                        self.requests += 1
                        if m.get("method") == "initialize" and self.init_request is None:
                            self.init_request = m
                            self.answer_initialize(m)
                        else:
                            self.error_to_proxy(m["id"], self.failed_message())
                    return
                if self.paused == "stopped":
                    for m in requests:
                        self.requests += 1
                        self.error_to_proxy(m["id"], f"MCP server '{self.name}' is stopped")
                    return
                if not await self.restart("woken by a request", wake=True):
                    for m in requests:
                        self.error_to_proxy(m["id"], f"MCP server '{self.name}' failed to start")
                    return
                continue
            async with self.lock:
                if not self.ready.is_set() or self.paused is not None:
                    continue  # a restart / stop started meanwhile
                for msg in msgs:
                    k = kind(msg)
                    if k == "request":
                        if msg["method"] == "initialize":
                            self.init_request = msg
                        self.inflight_in[id_key(msg["id"])] = time.monotonic()
                        self.requests += 1
                        self.last_request_wall = time.time()
                    elif k == "notification" and msg["method"] == "notifications/initialized":
                        self.initialized_note = msg
                    elif k == "response":
                        key = id_key(msg["id"])
                        if key not in self.inflight_out:
                            return  # answer for a server we replaced: drop
                        self.inflight_out.discard(key)
                self.last_activity = time.monotonic()
                try:
                    await self.write_child(line)
                except (BrokenPipeError, ConnectionResetError, AssertionError):
                    pass  # the server died; on_crash fails the in-flight requests
                return

    # -- restart / stop / crash ---------------------------------------------------

    def limits(self) -> str:
        parts = []
        if self.max_rss:
            parts.append(f"max-rss {fmt_size(self.max_rss)}")
        if self.hard_rss:
            parts.append(f"hard-rss {fmt_size(self.hard_rss)}")
        if self.max_age:
            parts.append(f"max-age {fmt_age(self.max_age)}")
        if self.idle_stop:
            parts.append(f"idle-stop {fmt_age(self.idle_stop)}")
        parts.append("restart on crash")
        return ", ".join(parts)

    def failed_message(self) -> str:
        retry = ""
        if self.retry_at is not None:
            retry = f"; next retry in {max(0, self.retry_at - time.monotonic()):.0f}s"
        return f"MCP server '{self.name}' failed to start ({self.failure}){retry}. See its log in mcp-manager or docker logs"

    def answer_initialize(self, request: dict) -> None:
        """Answer mcp-proxy's initialize ourselves, so a broken server does not stop the proxy.

        Every capability is announced, so mcp-proxy registers all handlers and
        forwards everything once the real server is up; until then requests
        get an error.
        """
        params = request.get("params") or {}
        self.inflight_in.pop(id_key(request.get("id")), None)
        self.to_proxy({
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": True}, "prompts": {"listChanged": True},
                                 "resources": {"listChanged": True}, "logging": {}},
                "serverInfo": {"name": f"{self.name} (failed to start)", "version": "0"},
            },
        })

    async def enter_failed(self, reason: str) -> None:
        """Mark the server failed and retry it with backoff until it starts."""
        first = self.paused != "failed"
        self.ready.clear()
        try:
            async with self.lock:
                if self.child is not None and self.child.returncode is None:
                    await self.stop_child(self.child)
                self.child = None
                # Answer mcp-proxy's pending initialize first: fail_inflight()
                # would otherwise send it an error, and mcp-proxy would exit.
                if self.init_request is not None and not self.initialized:
                    self.answer_initialize(self.init_request)
                self.fail_inflight(f"failed: {reason}")
                self.initialized = True
                self.paused = "failed"
                self.failure = reason
        finally:
            self.ready.set()
        if first:
            self.event(f"failed: {reason}")
            self.log(f"server failed ({reason}); other servers are not affected, retrying with backoff")
            asyncio.create_task(self.retry_failed())

    async def retry_failed(self) -> None:
        delay = 2.0
        while not self.stopping and self.paused == "failed":
            self.retry_at = time.monotonic() + delay
            await asyncio.sleep(delay)
            if self.stopping or self.paused != "failed":
                break
            if await self.restart(f"retry after failure ({self.failure})"):
                self.log("recovered")
                self.failure = None
                self.crashes.clear()
                break
            delay = min(delay * 2, 300.0)
        self.retry_at = None

    async def start_watchdog(self) -> None:
        """First start: give up waiting for initialize after --start-timeout."""
        await asyncio.sleep(self.start_timeout)
        if not self.initialized and not self.stopping:
            await self.enter_failed(f"no answer to initialize within {self.start_timeout:.0f}s")

    async def restart(self, reason: str, wake: bool = False) -> bool:
        """(Re)start the server and replay initialize. False if the new one fails."""
        self.restarting = True
        self.ready.clear()
        try:
            async with self.lock:
                old = self.child
                had_inflight = len(self.inflight_in)
                self.fail_inflight(reason)
                if old is not None:
                    old_rss = session_rss(old.pid) if old.returncode is None else 0
                    await self.stop_child(old)
                    self.child = None
                    self.log(
                        f"restarting ({reason}); old pid {old.pid}"
                        + (f", rss {fmt_size(old_rss)}" if old_rss else "")
                        + (f", failed {had_inflight} in-flight request(s)" if had_inflight else "")
                    )
                else:
                    self.log(f"starting ({reason})")
                self.reload_definition()
                await self.spawn()
                if self.init_request is not None:
                    key = f"mcp-supervise-init-{self.generation}"
                    fut: asyncio.Future = asyncio.get_running_loop().create_future()
                    self.pending_init = (id_key(key), fut)
                    replay = dict(self.init_request, id=key)
                    await self.write_child(json.dumps(replay).encode() + b"\n")
                    try:
                        resp = await asyncio.wait_for(fut, self.init_timeout)
                    except asyncio.TimeoutError:
                        self.log(f"new server did not answer initialize within {self.init_timeout:.0f}s")
                        return False
                    finally:
                        self.pending_init = None
                    if "error" in resp:
                        self.log(f"new server rejected initialize: {resp['error']}")
                        return False
                    if self.initialized_note is not None:
                        await self.write_child(json.dumps(self.initialized_note).encode() + b"\n")
                self.paused = None
                self.failure = None
                if not wake and old is not None:
                    self.restarts += 1
                self.event(reason)
                self.log(f"{'started' if old is None else 'restarted'}: new pid {self.child.pid if self.child else '?'}")
                return True
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.log(f"start failed: {exc}")
            return False
        finally:
            self.restarting = False
            self.ready.set()

    async def stop_server(self, mode: str, reason: str) -> None:
        """Stop the server but stay alive, so mcp-proxy keeps running."""
        self.ready.clear()
        try:
            async with self.lock:
                old = self.child
                had_inflight = len(self.inflight_in)
                self.fail_inflight(reason)
                if old is not None:
                    rss = session_rss(old.pid) if old.returncode is None else 0
                    await self.stop_child(old)
                    self.child = None
                    self.log(
                        f"stopped ({reason}); pid {old.pid}"
                        + (f", freed rss {fmt_size(rss)}" if rss else "")
                        + (f", failed {had_inflight} in-flight request(s)" if had_inflight else "")
                    )
                self.paused = mode
                self.event(reason)
        finally:
            self.ready.set()

    async def on_crash(self, rc: int | str | None) -> None:
        if not self.initialized:
            await self.enter_failed(f"exited ({rc}) while starting")
            return
        while not self.stopping and self.paused is None:
            now = time.monotonic()
            self.crashes.append(now)
            self.crash_total += 1
            while self.crashes and now - self.crashes[0] > self.crash_window:
                self.crashes.popleft()
            if len(self.crashes) > self.max_crashes:
                await self.enter_failed(f"{len(self.crashes)} crashes within {fmt_age(self.crash_window)}")
                return
            delay = min(30, 2 ** (len(self.crashes) - 1))
            self.log(f"server exited ({rc}); restarting in {delay}s")
            self.event(f"crash, exit {rc}")
            self.ready.clear()
            await asyncio.sleep(delay)
            if await self.restart(f"crash, exit {rc}"):
                return
            if self.paused is not None:
                return
            rc = "start failed"

    async def monitor(self) -> None:
        last_log = time.monotonic()
        announced: str | None = None
        while not self.stopping:
            await asyncio.sleep(self.check_interval)
            child = self.child
            if (
                not self.initialized
                or self.restarting
                or self.paused is not None
                or child is None
                or child.returncode is not None
            ):
                continue
            now = time.monotonic()
            rss = session_rss(child.pid)
            age = now - self.started_at
            busy = bool(self.inflight_in or self.inflight_out)
            if self.log_interval and now - last_log >= self.log_interval:
                self.log(f"pid {child.pid}: rss {fmt_size(rss)}, up {fmt_age(age)}")
                last_log = now
            if self.idle_stop and not busy and now - self.last_activity > self.idle_stop:
                await self.stop_server("sleeping", f"idle {fmt_age(now - self.last_activity)} > idle-stop")
                continue
            reason, force = None, False
            if self.hard_rss and rss > self.hard_rss:
                reason, force = f"rss {fmt_size(rss)} > hard-rss {fmt_size(self.hard_rss)}", True
            elif self.max_rss and rss > self.max_rss:
                reason = f"rss {fmt_size(rss)} > max-rss {fmt_size(self.max_rss)}"
            elif self.max_age and age > self.max_age:
                reason = f"up {fmt_age(age)} > max-age {fmt_age(self.max_age)}"
            if reason is None:
                announced = None
                continue
            if force or not busy:
                announced = None
                if not await self.restart(reason):
                    await self.on_crash("start failed")
            elif announced is None:
                announced = reason
                n = len(self.inflight_in) + len(self.inflight_out)
                self.log(f"restart pending ({reason}); waiting for {n} in-flight request(s)")

    # -- control socket ------------------------------------------------------------

    def status(self) -> dict:
        child = self.child
        running = child is not None and child.returncode is None
        if self.paused == "failed":
            state = "failed"  # also while a retry is in progress
        elif self.restarting:
            state = "restarting"
        elif self.paused:
            state = self.paused
        elif not self.initialized:
            state = "starting"
        elif running:
            state = "running"
        else:
            state = "crashed"
        return {
            "name": self.name,
            "state": state,
            "pid": child.pid if running else None,
            "rss": session_rss(child.pid) if running else 0,
            "uptime": time.monotonic() - self.started_at if running else None,
            "supervisor_since": self.created,
            "limits": {
                "maxRss": self.max_rss,
                "hardRss": self.hard_rss,
                "maxAge": self.max_age,
                "idleStop": self.idle_stop,
            },
            "restarts": self.restarts,
            "crashes": self.crash_total,
            "requests": self.requests,
            "errors": self.errors,
            "inflight": len(self.inflight_in) + len(self.inflight_out),
            "idle": time.monotonic() - self.last_activity,
            "last_request": self.last_request_wall,
            "events": list(self.events),
            "failure": self.failure,
            "retryIn": max(0, self.retry_at - time.monotonic()) if self.retry_at is not None else None,
            "command": self.cmd,
        }

    async def control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads(await reader.readline() or b"{}")
            cmd = req.get("cmd")
            if cmd == "status":
                resp = self.status()
            elif cmd == "log":
                resp = {"lines": list(self.stderr_tail)}
            elif cmd in ("restart", "stop", "start") and not self.initialized and self.paused != "failed":
                resp = {"ok": False, "error": "server has not finished initializing yet"}
            elif cmd == "restart":
                resp = {"ok": await self.restart("manual restart")}
            elif cmd == "stop":
                await self.stop_server("stopped", "manual stop")
                resp = {"ok": True}
            elif cmd == "start":
                resp = {"ok": True} if self.paused is None else {"ok": await self.restart("manual start", wake=True)}
            else:
                resp = {"ok": False, "error": f"unknown command {cmd!r}"}
        except Exception as exc:  # noqa: BLE001
            resp = {"ok": False, "error": str(exc)}
        try:
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
            writer.close()
        except (ConnectionError, OSError):
            pass

    async def serve_control(self) -> None:
        if not self.control_dir:
            return
        try:
            os.makedirs(self.control_dir, mode=0o700, exist_ok=True)
            path = socket_path(self.control_dir, self.name)
            if os.path.exists(path):
                os.unlink(path)
            await asyncio.start_unix_server(self.control, path=path)
            os.chmod(path, 0o600)
        except OSError as exc:
            self.log(f"control socket disabled: {exc}")

    # -- lifecycle -----------------------------------------------------------------

    def shutdown_now(self, code: int) -> None:
        self.stopping = True
        try:
            if self.child is not None and self.child.returncode is None:
                os.killpg(self.child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        sys.stdout.flush()
        os._exit(code)

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, done.set)
        await self.spawn()
        self.ready.set()
        await self.serve_control()
        asyncio.create_task(self.start_watchdog())
        proxy_task = asyncio.create_task(self.pump_proxy())
        monitor_task = asyncio.create_task(self.monitor())
        signal_task = asyncio.create_task(done.wait())
        await asyncio.wait({proxy_task, signal_task}, return_when=asyncio.FIRST_COMPLETED)
        self.stopping = True
        monitor_task.cancel()
        if self.child is not None:
            await self.stop_child(self.child)
        return 0


# --- wrap-config ----------------------------------------------------------------

# servers.json "supervise" key -> (flag, parser)
OPTIONS = {
    "maxRss": ("--max-rss", parse_size),
    "hardRss": ("--hard-rss", parse_size),
    "maxAge": ("--max-age", parse_duration),
    "idleStop": ("--idle-stop", parse_duration),
    "checkInterval": ("--check-interval", parse_duration),
    "maxCrashes": ("--max-crashes", int),
    "logInterval": ("--log-interval", parse_duration),
    "startTimeout": ("--start-timeout", parse_duration),
}
ENV = {
    "maxRss": "MCP_SUPERVISE_MAX_RSS",
    "hardRss": "MCP_SUPERVISE_HARD_RSS",
    "maxAge": "MCP_SUPERVISE_MAX_AGE",
    "idleStop": "MCP_SUPERVISE_IDLE_STOP",
    "checkInterval": "MCP_SUPERVISE_CHECK_INTERVAL",
    "maxCrashes": "MCP_SUPERVISE_MAX_CRASHES",
    "logInterval": "MCP_SUPERVISE_LOG_INTERVAL",
    "startTimeout": "MCP_SUPERVISE_START_TIMEOUT",
}


def supervise_args(name: str, opts: dict, control_dir: str = "", definition: str = "") -> list[str]:
    """mcp-supervise arguments (before "--") for one server; validates `opts`."""
    unknown = set(opts) - set(OPTIONS)
    if unknown:
        raise ValueError(f"server '{name}': unknown supervise option(s): {', '.join(sorted(unknown))}")
    args = ["--name", name]
    if control_dir:
        args += ["--control-dir", control_dir]
    if definition:
        args += ["--definition", definition]
    for key, value in opts.items():
        if value in (None, ""):
            continue
        flag, parser = OPTIONS[key]
        parser(str(value))  # validate now, not at runtime
        args += [flag, str(value)]
    return args


def wrap_config(src: str, dst: str) -> int:
    """Wrap servers in mcp-supervise. 0: wrote dst, 2: nothing to wrap, 1: error.

    MCP_SUPERVISE=true wraps every server with the MCP_SUPERVISE_* defaults.
    Per server, "supervise": {...} overrides them (and opts that server in even
    when MCP_SUPERVISE is off); "supervise": false opts it out.
    The control socket directory comes from MCP_SUPERVISE_DIR and is passed as
    an argument: mcp-proxy does not pass its environment to the servers.
    """
    with open(src, encoding="utf-8") as f:
        cfg = json.load(f)
    enabled = os.environ.get("MCP_SUPERVISE", "").strip().lower() in {"1", "true", "yes", "on"}
    control_dir = os.environ.get("MCP_SUPERVISE_DIR", "").strip()
    defaults = {k: os.environ[v].strip() for k, v in ENV.items() if os.environ.get(v, "").strip()}
    wrapped = 0
    for name, server in cfg.get("mcpServers", {}).items():
        sup = server.pop("supervise", None)
        if sup is False or (sup is None and not enabled) or not server.get("enabled", True):
            continue
        if sup in (True, None):
            sup = {}
        if not isinstance(sup, dict):
            raise ValueError(f"server '{name}': \"supervise\" must be true, false or an object")
        opts = {**defaults, **sup}
        args = supervise_args(name, opts, control_dir)
        summary = [f"{k}={v}" for k, v in opts.items() if v not in (None, "")]
        server["args"] = [*args, "--", server["command"], *server.get("args", [])]
        server["command"] = "mcp-supervise"
        wrapped += 1
        print(f"entrypoint: supervise '{name}': {', '.join(summary) or 'restart on crash only'}", file=sys.stderr)
    if not wrapped:
        return 2
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # env may hold secrets
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return 0


# --- main -------------------------------------------------------------------------


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "wrap-config":
        if len(sys.argv) != 4:
            print("usage: mcp-supervise wrap-config <servers.json> <output.json>", file=sys.stderr)
            return 1
        try:
            return wrap_config(sys.argv[2], sys.argv[3])
        except (OSError, ValueError) as exc:
            print(f"entrypoint: ERROR: {sys.argv[2]}: {exc}", file=sys.stderr)
            return 1

    p = argparse.ArgumentParser(prog="mcp-supervise", description=__doc__.split("\n\n")[0])
    p.add_argument("--name", help="name used in log lines and for the control socket")
    p.add_argument("--max-rss", type=parse_size, help="restart when idle and RSS exceeds this")
    p.add_argument("--hard-rss", type=parse_size, help="restart immediately when RSS exceeds this")
    p.add_argument("--max-age", type=parse_duration, help="restart when idle and older than this")
    p.add_argument("--idle-stop", type=parse_duration, help="stop after this long unused; start on next request")
    p.add_argument("--check-interval", type=parse_duration, default=30.0)
    p.add_argument("--log-interval", type=parse_duration, default=3600.0, help="periodic RSS log line")
    p.add_argument("--max-crashes", type=int, default=5)
    p.add_argument("--crash-window", type=parse_duration, default=600.0)
    p.add_argument("--init-timeout", type=parse_duration, default=60.0, help="initialize timeout on restarts")
    p.add_argument("--start-timeout", type=parse_duration, default=120.0,
                   help="first start: mark failed if initialize is not answered within this")
    p.add_argument("--grace", type=parse_duration, default=5.0, help="wait between stop signals")
    p.add_argument("--control-dir", help="directory for the control socket")
    p.add_argument("--definition", help="JSON file with command/args/env/supervise, reloaded on every restart")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        p.error("missing server command after --")
    return asyncio.run(Supervisor(args).run())


if __name__ == "__main__":
    sys.exit(main())
