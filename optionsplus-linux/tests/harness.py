"""Run an in-process Agent on a temp socket and talk to it like the UI does."""

import asyncio
import json
import socket
import struct
import tempfile
import threading

from opl_agent.main import Agent


def short_tmpdir():
    """Unix socket paths top out near 108 bytes; keep them short."""
    d = tempfile.mkdtemp(prefix="opl")
    if len(d) >= 60:
        d = tempfile.mkdtemp(prefix="opl", dir="/tmp")
    return d


class RunningAgent:
    def __init__(self, **kw):
        self.agent = Agent(**kw)
        self.path = self.agent.server.path
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error = None
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.agent.start())
        except BaseException as exc:      # surface startup failures to the test
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    def start(self):
        self._thread.start()
        assert self._ready.wait(15), "agent did not start"
        if self._error:
            raise self._error
        return self

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        asyncio.run_coroutine_threadsafe(self.agent.stop(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    def client(self):
        return Client(self.path)


class Client:
    """Speaks the UI's wire format: length-prefixed frames, snake_case msg_id."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(10)
        self.sock.connect(path)
        self._n = 0
        self.greeting = self.recv_raw()

    def close(self):
        self.sock.close()

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def recv_raw(self):
        self._read(4)                                   # total length: ignored
        proto = self._read(struct.unpack(">I", self._read(4))[0])
        body = self._read(struct.unpack(">I", self._read(4))[0])
        return proto, body

    def recv(self):
        proto, body = self.recv_raw()
        assert proto == b"json", proto
        return json.loads(body)

    @staticmethod
    def frame(body, proto=b"json"):
        total = len(proto) + len(body) + 8
        return (struct.pack("<I", total) + struct.pack(">I", len(proto)) + proto +
                struct.pack(">I", len(body)) + body)

    def send_raw(self, data):
        self.sock.sendall(data)

    def send(self, verb, path, payload=None, **extra):
        self._n += 1
        msg = {"msg_id": str(self._n), "verb": verb, "path": path}
        if payload is not None:
            msg["payload"] = payload
        msg.update(extra)
        self.send_raw(self.frame(json.dumps(msg).encode()))
        return str(self._n)

    def call(self, verb, path, payload=None):
        """Send a request, return the reply that carries its msgId."""
        mid = self.send(verb, path, payload)
        while True:
            m = self.recv()
            if m.get("msgId") == mid and "result" in m:
                return m

    def subscribe(self, path):
        """SUBSCRIBE, return (reply, [initial pushes])."""
        mid = self.send("SUBSCRIBE", path)
        pushes, reply = [], None
        while reply is None:
            m = self.recv()
            if m.get("msgId") == mid and "result" in m:
                reply = m
            else:
                pushes.append(m)
        self.sock.settimeout(0.3)
        try:
            while True:
                pushes.append(self.recv())
        except (socket.timeout, TimeoutError):
            pass
        finally:
            self.sock.settimeout(10)
        return reply, pushes
