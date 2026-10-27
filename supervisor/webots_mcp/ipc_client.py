"""IPC client: connects the (unique) Supervisor to the Rust host.

Transport: TCP on loopback, newline-delimited JSON, JSON-RPC 2.0 shapes.
The Rust host is the server (it outlives Webots); this side connects, and
reconnects whenever Webots restarts the controller or the host restarts.

  supervisor -> host   request  hello      {protocol, token, tools[], state, ...}
                       notify   state      {state, mode, sim_time, ...}
  host -> supervisor   request  tool.call  {name, arguments}   -> {text}
                       request  sim.control {op, ...}           -> clock status
                       request  ping

Webots API calls never happen on these threads: tools go through
``runtime.call_main`` (command queue drained by the main loop) and clock ops
through ``Clock.submit`` (drained by ``Clock.tick``).
"""
import asyncio
import json
import math
import socket
import threading

from . import registry

PROTOCOL = 1


def _clean(obj):
    """JSON-safe copy: NaN/inf -> None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


class IpcClient:
    def __init__(self, addr, token, info, clock, log):
        host, _, port = addr.rpartition(":")
        self.addr = (host or "127.0.0.1", int(port))
        self.token = token or None
        self.info = dict(info)        # static: pid, world_path, ...
        self.clock = clock
        self.log = log

        self._sock = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._hello_id = 1

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="webots-mcp-tools", daemon=True)
        self._thread = threading.Thread(
            target=self._run, name="webots-mcp-ipc", daemon=True)

    # ---------------- lifecycle ----------------

    def start(self):
        self._loop_thread.start()
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    def notify_state(self, snapshot):
        """Called from the main thread when state/mode changed."""
        self._send({"jsonrpc": "2.0", "method": "state", "params": snapshot})

    # ---------------- connection loop ----------------

    def _run(self):
        backoff = 0.2
        announced = False
        while not self._stop.is_set():
            try:
                sock = socket.create_connection(self.addr, timeout=3)
            except OSError as exc:
                if not announced:
                    self.log.info("Waiting for the MCP host at %s:%s (%s)",
                                  self.addr[0], self.addr[1], exc)
                    announced = True
                self._stop.wait(backoff)
                backoff = min(backoff * 1.7, 2.0)
                continue

            backoff, announced = 0.2, False
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock = sock
            self.log.info("Connected to the MCP host at %s:%s", *self.addr)

            try:
                self._send_hello()
                with sock.makefile("rb") as reader:
                    for raw in reader:
                        self._on_line(raw)
            except OSError as exc:
                self.log.info("MCP host connection error: %s", exc)
            finally:
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
            if not self._stop.is_set():
                self.log.info("MCP host disconnected; reconnecting")
                self._stop.wait(0.3)

    def _send_hello(self):
        params = dict(self.info)
        params.update({
            "protocol": PROTOCOL,
            "token": self.token,
            "tools": registry.export_manifest(),
        })
        snap = self.clock.snapshot()
        params.update(state=snap["state"], mode=snap["mode"], sim_time=snap["sim_time"])
        self._send({"jsonrpc": "2.0", "id": self._hello_id, "method": "hello",
                    "params": params})

    # ---------------- sending ----------------

    def _send(self, message):
        sock = self._sock
        if sock is None:
            return False
        data = (json.dumps(_clean(message), ensure_ascii=False, default=str)
                + "\n").encode("utf-8")
        try:
            with self._send_lock:
                sock.sendall(data)
            return True
        except OSError:
            return False

    def _reply(self, msg_id, result):
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _reply_error(self, msg_id, code, message):
        self._send({"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": code, "message": message}})

    # ---------------- receiving ----------------

    def _on_line(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            self.log.warning("Ignoring malformed IPC line: %r", raw[:200])
            return
        if not isinstance(msg, dict):
            return

        method = msg.get("method")
        msg_id = msg.get("id")

        if method is None:  # a response (only to hello)
            if "error" in msg:
                self.log.error("Host rejected hello: %s", msg["error"])
            elif msg_id == self._hello_id:
                self.log.info("Host accepted hello: %s", msg.get("result"))
            return

        params = msg.get("params") or {}

        if method == "ping":
            self._reply(msg_id, {"pong": True})
        elif method == "tool.call":
            asyncio.run_coroutine_threadsafe(self._tool_call(msg_id, params), self._loop)
        elif method == "sim.control":
            fut = self.clock.submit(params.get("op"), params)
            fut.add_done_callback(
                lambda f, i=msg_id: self._loop.call_soon_threadsafe(self._control_done, i, f))
        elif msg_id is not None:
            self._reply_error(msg_id, -32601, f"unknown method: {method}")

    async def _tool_call(self, msg_id, params):
        try:
            text = await registry.call_tool(params.get("name"), params.get("arguments"))
            self._reply(msg_id, {"text": text})
        except registry.ToolError as exc:
            self._reply_error(msg_id, exc.code, str(exc))
        except Exception as exc:  # tools normally catch their own errors
            self.log.exception("Tool crashed: %s", params.get("name"))
            self._reply_error(msg_id, -32000, f"{type(exc).__name__}: {exc}")

    def _control_done(self, msg_id, fut):
        if fut.cancelled():
            self._reply_error(msg_id, -32000, "cancelled")
            return
        exc = fut.exception()
        if exc is None:
            self._reply(msg_id, fut.result())
        elif isinstance(exc, ValueError):
            self._reply_error(msg_id, -32602, str(exc))
        else:
            self._reply_error(msg_id, -32000, f"{type(exc).__name__}: {exc}")
