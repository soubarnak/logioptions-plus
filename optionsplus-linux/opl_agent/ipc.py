"""Agent-side implementation of the Options+ UI <-> agent socket protocol.

Frame layout (observed in the frontend's main.js):

    u32  total length   (the UI writes it little-endian and ignores it on read)
    u32  BE protocol-name length (4)
    ...  protocol name, always b"json"
    u32  BE payload length
    ...  payload: UTF-8 protobuf-JSON of logi.protocol.Message

Message fields: msgId, verb, path, origin, result {code, what}, payload (Any).
"""

import asyncio
import copy
import getpass
import hashlib
import json
import logging
import os
import socket
import struct

from .defaults import fill

log = logging.getLogger("opl.ipc")


def _clip(payload):
    """A payload for the debug log: 400 bytes, or all of it when OPL_IPC_LOG_FULL=1."""
    if not payload:
        return ""
    text = json.dumps(payload)
    return text if os.environ.get("OPL_IPC_LOG_FULL") == "1" else text[:400]

PROTO = b"json"
PROTO_PB = b"protobuf"

# On-connect greeting the real agent sends as the first frame: a protobuf
# logi.protocol.Message{verb=OPTIONS(7), path="/", origin="backend"}. The
# frontend treats this as "backend ready" before it runs its boot sagas.
GREETING = bytes.fromhex("10071a012f2207") + b"backend"


def socket_path(name="logitech_kiros_agent", user=None):
    override = os.environ.get("OPL_SOCKET")
    if override and name == "logitech_kiros_agent":
        return override
    user = user or getpass.getuser()
    return f"/tmp/{name}-{hashlib.md5(user.encode()).hexdigest()}"


def socket_alive(path):
    """True when a process accepts connections on the Unix socket at ``path``."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def encode_frame(obj):
    body = json.dumps(obj, separators=(",", ":")).encode()
    total = len(PROTO) + len(body) + 8
    return struct.pack(">I", total) + struct.pack(">I", len(PROTO)) + PROTO + struct.pack(">I", len(body)) + body


def broadcast_msg(path, payload=None):
    """A BROADCAST frame. It must carry no "result": the UI routes any frame
    with a result to the pending-call table by msgId, and only a result-less
    BROADCAST to the handler subscribed on its path."""
    msg = {"msgId": "0", "verb": "BROADCAST", "path": path, "origin": "backend"}
    if payload is not None:
        msg["payload"] = fill(copy.deepcopy(payload))
    return msg


class Connection:
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.subscriptions = set()
        self.closed = False

    async def run(self):
        try:
            while True:
                head = await self.reader.readexactly(8)
                plen = struct.unpack(">I", head[4:8])[0]
                proto = await self.reader.readexactly(plen)
                blen = struct.unpack(">I", await self.reader.readexactly(4))[0]
                body = await self.reader.readexactly(blen)
                if proto != PROTO:
                    log.warning("unsupported protocol %r", proto)
                    continue
                try:
                    msg = json.loads(body)
                except ValueError:
                    log.warning("bad json: %r", body[:200])
                    continue
                asyncio.ensure_future(self.server.dispatch(self, msg))
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.closed = True
            self.server.connections.discard(self)
            self.writer.close()

    def send(self, obj):
        if self.closed:
            return
        try:
            self.writer.write(encode_frame(obj))
        except (ConnectionResetError, BrokenPipeError):
            self.closed = True

    def send_raw(self, proto, body):
        """Write one frame with an explicit sub-protocol name and raw body bytes."""
        if self.closed:
            return
        total = len(proto) + len(body) + 8
        frame = (struct.pack(">I", total) + struct.pack(">I", len(proto)) + proto +
                 struct.pack(">I", len(body)) + body)
        try:
            self.writer.write(frame)
        except (ConnectionResetError, BrokenPipeError):
            self.closed = True


class Server:
    def __init__(self, router, path=None):
        self.router = router
        self.path = path or socket_path()
        self.connections = set()

    async def start(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        srv = await asyncio.start_unix_server(self._accept, path=self.path)
        os.chmod(self.path, 0o600)
        log.info("listening on %s", self.path)
        return srv

    async def _accept(self, reader, writer):
        conn = Connection(self, reader, writer)
        self.connections.add(conn)
        log.info("client connected")
        conn.send_raw(PROTO_PB, GREETING)   # "backend ready" handshake
        await conn.run()
        log.info("client disconnected")

    async def dispatch(self, conn, msg):
        verb = msg.get("verb")
        path = msg.get("path", "")
        log.debug("<- %s %s %s", verb, path, _clip(msg.get("payload")))
        if verb == "SUBSCRIBE":
            conn.subscriptions.add(path)
            reply = self._reply(msg, "SUCCESS")
            conn.send(reply)
            # Push any initial state the frontend's boot sagas wait on.
            init = getattr(self.router, "initial_state", None)
            if init is not None:
                try:
                    for p, pl in (init(path) or []):
                        conn.send(broadcast_msg(p, pl))
                except Exception:
                    log.exception("initial_state push failed for %s", path)
            return
        elif verb == "UNSUBSCRIBE":
            conn.subscriptions.discard(path)
            reply = self._reply(msg, "SUCCESS")
        else:
            try:
                code, payload, what = await self.router.handle(verb, path, msg.get("payload"))
            except Exception as exc:  # keep the UI alive on handler bugs
                log.exception("handler failed for %s %s", verb, path)
                code, payload, what = "EXCEPTION", None, str(exc)
            reply = self._reply(msg, code, payload, what)
        log.debug("-> %s %s %s %s", reply["verb"], path, reply["result"]["code"],
                  _clip(reply.get("payload")))
        conn.send(reply)

    @staticmethod
    def _reply(msg, code, payload=None, what=""):
        # The UI sends snake_case "msg_id"; the agent answers in proto-JSON
        # camelCase "msgId". The UI matches replies to pending calls by it.
        out = {
            "msgId": str(msg.get("msg_id", msg.get("msgId", ""))),
            "verb": msg.get("verb", "GET"),
            "path": msg.get("path", ""),
            "origin": "backend",
            "result": {"code": code, "what": what},
        }
        if payload is not None:
            out["payload"] = fill(copy.deepcopy(payload))
        return out

    def broadcast(self, path, payload=None):
        """Push an event to every client subscribed to `path`."""
        msg = broadcast_msg(path, payload)
        for c in list(self.connections):
            if path in c.subscriptions:
                log.debug("=> BROADCAST %s %s", path, _clip(payload))
                c.send(msg)
