"""Focus tracking on wlroots compositors (sway, Hyprland, river, ...).

Speaks just enough of the Wayland wire protocol to bind ``zwlr_foreign_toplevel_manager_v1``
(wlr-foreign-toplevel-management) and follow which toplevel is ``activated``. No Python
dependencies and no libwayland: the protocol is a stream of 32-bit-aligned messages on a
unix socket, and this client needs no file descriptors, shared memory or surfaces. The
trade-off is a hand-written decoder for the six interfaces below instead of a library.

The transport is injectable (``send(bytes)``, ``recv(timeout) -> bytes`` returning b"" when
nothing arrived, raising ConnectionError when the peer is gone, ``close()``), so tests run
against a scripted compositor. The source follows the window source interface of
`focus`: ``active()`` blocks briefly, may raise, and returns a `Window` or None. The
protocol gives the toplevel's app_id (the Wayland counterpart of WM_CLASS) but no process
id, so ``exe`` stays empty.
"""

import logging
import os
import select
import socket
import struct
import threading
import time

from .focus import Window

log = logging.getLogger("opl.focus")

MANAGER = "zwlr_foreign_toplevel_manager_v1"
MANAGER_VERSION = 3                  # the newest version this decoder knows (adds `parent`)
STATE_ACTIVATED = 2                  # zwlr_foreign_toplevel_handle_v1.state

DISPLAY_ID = 1
HANDSHAKE_SECONDS = 2.0


class WaylandError(Exception):
    """The compositor cannot be used for focus tracking (no protocol, error, bad data)."""


class WaylandUnsupported(WaylandError):
    """This compositor will never offer what is needed (it lacks the protocol): not worth retrying."""
    permanent = True


# -- wire format -------------------------------------------------------------------------

def _pad(n):
    return b"\0" * (-n % 4)


def _string(text):
    raw = text.encode() + b"\0"
    return struct.pack("=I", len(raw)) + raw + _pad(len(raw))


def _message(obj, opcode, payload=b""):
    return struct.pack("=II", obj, ((8 + len(payload)) << 16) | opcode) + payload


class _Reader:
    def __init__(self, data):
        self.data, self.at = data, 0

    def u32(self):
        if self.at + 4 > len(self.data):
            raise WaylandError("truncated message")
        (v,) = struct.unpack_from("=I", self.data, self.at)
        self.at += 4
        return v

    def blob(self):
        n = self.u32()
        if self.at + n > len(self.data):
            raise WaylandError("truncated message")
        raw = self.data[self.at:self.at + n]
        self.at += n + (-n % 4)
        return raw

    def string(self):
        raw = self.blob()
        return raw.rstrip(b"\0").decode("utf-8", "replace") if raw else None

    def u32_array(self):
        raw = self.blob()
        return struct.unpack("=%dI" % (len(raw) // 4), raw[:len(raw) // 4 * 4])


class _Toplevel:
    """One toplevel handle: what it showed at its last `done`, and what is pending until the next."""

    def __init__(self):
        self.app_id, self.activated = "", False
        self.pending_app_id, self.pending_activated = "", False

    def commit(self):
        self.app_id, self.activated = self.pending_app_id, self.pending_activated


# -- transport ---------------------------------------------------------------------------

def socket_path(environ):
    """The compositor socket named by WAYLAND_DISPLAY (absolute, or under XDG_RUNTIME_DIR)."""
    name = environ.get("WAYLAND_DISPLAY") or "wayland-0"
    if os.path.isabs(name):
        return name
    runtime = environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise WaylandError("XDG_RUNTIME_DIR is not set")
    return os.path.join(runtime, name)


class SocketTransport:
    def __init__(self, path):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.settimeout(HANDSHAKE_SECONDS)
            self._sock.connect(path)
        except OSError:
            self._sock.close()
            raise

    def send(self, data):
        self._sock.settimeout(HANDSHAKE_SECONDS)
        self._sock.sendall(data)

    def recv(self, timeout=0):
        if not select.select([self._sock], [], [], timeout)[0]:
            return b""
        data = self._sock.recv(65536)
        if not data:
            raise ConnectionError("the compositor closed the connection")
        return data

    def close(self):
        self._sock.close()


def connect_from_environment(environ):
    return SocketTransport(socket_path(environ))


# -- the source --------------------------------------------------------------------------

class WaylandSource:
    """The activated toplevel of a compositor that offers wlr-foreign-toplevel-management.
    `connect()` returns a transport. Connects on `start()` (or the first `active()`), and
    again after a lost connection."""

    def __init__(self, connect, timeout=HANDSHAKE_SECONDS):
        self._connect = connect
        self._timeout = timeout
        self._conn = None
        self._lock = threading.Lock()            # the poll thread and close() at stop
        self._buf = b""
        self._next_id = DISPLAY_ID
        self._kinds = {}
        self._globals = {}
        self._done = set()
        self._toplevels = {}
        self._focused = None
        self._manager = None

    # public
    def start(self):
        with self._lock:
            if self._conn is None:
                self._open()

    def active(self):
        with self._lock:
            if self._conn is None:
                self._open()
            try:
                self._pump(0)
            except (WaylandError, OSError):      # ConnectionError is an OSError
                self._drop()
                raise
            return self._window()

    def close(self):
        with self._lock:
            self._drop()

    # connection
    def _drop(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _open(self):
        conn = self._connect()
        self._conn = conn
        self._buf = b""
        self._next_id = DISPLAY_ID
        self._kinds = {DISPLAY_ID: "display"}
        self._globals = {}                       # interface -> (name, version)
        self._done = set()                       # callbacks that fired
        self._toplevels = {}                     # handle id -> _Toplevel
        self._focused = None
        self._manager = None
        try:
            registry = self._new("registry")
            self._send(DISPLAY_ID, 1, struct.pack("=I", registry))     # get_registry
            self._roundtrip()
            if MANAGER not in self._globals:
                raise WaylandUnsupported("this compositor does not offer the wlr-foreign-toplevel-management "
                                   "protocol")
            name, version = self._globals[MANAGER]
            version = min(version, MANAGER_VERSION)
            self._manager = self._new("manager")
            self._send(registry, 0, struct.pack("=I", name) + _string(MANAGER)
                       + struct.pack("=II", version, self._manager))   # bind
            self._roundtrip()                    # the open toplevels have been described by now
        except BaseException:
            self._drop()
            raise

    def _new(self, kind):
        self._next_id += 1
        self._kinds[self._next_id] = kind
        return self._next_id

    def _send(self, obj, opcode, payload=b""):
        self._conn.send(_message(obj, opcode, payload))

    def _roundtrip(self):
        callback = self._new("callback")
        self._send(DISPLAY_ID, 0, struct.pack("=I", callback))         # sync
        deadline = time.monotonic() + self._timeout
        while callback not in self._done:
            left = deadline - time.monotonic()
            if left <= 0:
                raise WaylandError("the compositor did not answer")
            self._pump(left)

    def _pump(self, timeout):
        """Read what the compositor sent (waiting up to `timeout` for the first bytes only)."""
        while True:
            data = self._conn.recv(timeout)
            if not data:
                return
            self._buf += data
            self._dispatch()
            timeout = 0

    def _dispatch(self):
        while len(self._buf) >= 8:
            obj, word = struct.unpack_from("=II", self._buf)
            size, opcode = word >> 16, word & 0xffff
            if size < 8:
                raise WaylandError("malformed message")
            if len(self._buf) < size:
                return
            payload, self._buf = self._buf[8:size], self._buf[size:]
            self._event(obj, opcode, _Reader(payload))

    # events
    def _event(self, obj, opcode, r):
        kind = self._kinds.get(obj)
        if kind == "display" and opcode == 0:                          # error
            raise WaylandError("compositor error: %s" % (self._error_text(r),))
        elif kind == "registry" and opcode == 0:                       # global
            name, interface, version = r.u32(), r.string(), r.u32()
            self._globals[interface] = (name, version)
        elif kind == "callback" and opcode == 0:                       # done
            self._done.add(obj)
        elif kind == "manager":
            if opcode == 0:                                            # toplevel(new_id)
                handle = r.u32()
                self._kinds[handle] = "toplevel"
                self._toplevels[handle] = _Toplevel()
            elif opcode == 1:                                          # finished
                raise WaylandError("the compositor ended the toplevel manager")
        elif kind == "toplevel" and obj in self._toplevels:
            self._toplevel_event(obj, self._toplevels[obj], opcode, r)

    @staticmethod
    def _error_text(r):
        r.u32(), r.u32()                         # object, code
        return r.string()

    def _toplevel_event(self, handle, tl, opcode, r):
        if opcode == 1:                                                # app_id
            tl.pending_app_id = r.string() or ""
        elif opcode == 4:                                              # state(array)
            tl.pending_activated = STATE_ACTIVATED in r.u32_array()
        elif opcode == 5:                                              # done: apply the pending state
            tl.commit()
            if tl.activated:
                self._focused = handle
            elif self._focused == handle:
                self._focused = None
        elif opcode == 6:                                              # closed
            del self._toplevels[handle]
            del self._kinds[handle]
            if self._focused == handle:
                self._focused = None
            self._send(handle, 7)                                      # destroy

    def _window(self):
        tl = self._toplevels.get(self._focused)
        if tl is None or not tl.app_id:
            return None
        return Window((tl.app_id,))           # the full app_id: matching splits reverse-DNS ids on the profile side
