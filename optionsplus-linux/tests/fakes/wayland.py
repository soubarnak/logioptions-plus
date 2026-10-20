"""A scripted Wayland compositor speaking the wire protocol, for the wlroots focus source.

It stands in for the transport (`send`, `recv`, `close`) so no socket or compositor is
needed. It encodes and decodes the wire format on its own (struct, no code from the
agent) so a mistake in the agent's encoding is caught, not mirrored. It serves wl_display,
wl_registry, wl_callback and, unless `protocol=False`, zwlr_foreign_toplevel_manager_v1
with its handles. `chunk` limits how many bytes one `recv` returns, to split frames."""

import struct

MANAGER = "zwlr_foreign_toplevel_manager_v1"
STATE_ACTIVATED = 2
SERVER_ID = 0xff000000


def _pad(n):
    return b"\0" * (-n % 4)


def string(s):
    raw = s.encode() + b"\0"
    return struct.pack("=I", len(raw)) + raw + _pad(len(raw))


def u32(v):
    return struct.pack("=I", v)


def array(values):
    raw = b"".join(struct.pack("=I", v) for v in values)
    return struct.pack("=I", len(raw)) + raw + _pad(len(raw))


def message(obj, opcode, payload=b""):
    return struct.pack("=II", obj, ((8 + len(payload)) << 16) | opcode) + payload


class FakeCompositor:
    def __init__(self, protocol=True, version=3, chunk=None, answers=True):
        self.protocol = protocol
        self.version = version
        self.chunk = chunk
        self.answers = answers                   # False: never replies (a hung compositor)
        self.closed = False
        self.requests = []                       # (object id, opcode, payload) the client sent
        self.bound = None                        # (interface, version, id) of the manager bind
        self.windows = {}                        # handle id -> {"app_id", "states"}
        self._in = b""
        self._out = b""
        self._next = SERVER_ID
        self._manager = None
        self._registry = None

    # -- transport ---------------------------------------------------------------------
    def send(self, data):
        if self.closed:
            raise BrokenPipeError("compositor gone")
        self._in += data
        while len(self._in) >= 8:
            obj, word = struct.unpack("=II", self._in[:8])
            size, opcode = word >> 16, word & 0xffff
            if len(self._in) < size:
                return
            payload, self._in = self._in[8:size], self._in[size:]
            self.requests.append((obj, opcode, payload))
            if self.answers:
                self._handle(obj, opcode, payload)

    def recv(self, timeout=0):
        if self.closed and not self._out:
            raise ConnectionError("compositor closed the connection")
        n = self.chunk or len(self._out)
        data, self._out = self._out[:n], self._out[n:]
        return data

    def close(self):
        pass

    # -- the scripted desktop ----------------------------------------------------------
    def open_window(self, app_id, active=False):
        self._next += 1
        handle = self._next
        self.windows[handle] = {"app_id": app_id, "states": [STATE_ACTIVATED] if active else []}
        if self._manager is None:                # announced when the client binds
            return handle
        self._announce(handle)
        return handle

    def set_states(self, handle, states, commit=True):
        self.windows[handle]["states"] = list(states)
        self._out += message(handle, 4, array(states))
        if commit:
            self._out += message(handle, 5)

    def activate(self, handle):
        for other, w in self.windows.items():
            if other != handle and STATE_ACTIVATED in w["states"]:
                self.set_states(other, [])
        self.set_states(handle, [STATE_ACTIVATED])

    def rename(self, handle, app_id):
        self.windows[handle]["app_id"] = app_id
        self._out += message(handle, 1, string(app_id))
        self._out += message(handle, 5)

    def close_window(self, handle):
        del self.windows[handle]
        self._out += message(handle, 6)

    def finish(self):
        self._out += message(self._manager, 1)

    def protocol_error(self, text):
        self._out += message(1, 0, u32(self._manager or 1) + u32(1) + string(text))

    def hang_up(self):
        self.closed = True

    # -- internals ---------------------------------------------------------------------
    def _announce(self, handle):
        w = self.windows[handle]
        self._out += message(self._manager, 0, u32(handle))
        self._out += message(handle, 0, string("title of " + w["app_id"]))
        self._out += message(handle, 1, string(w["app_id"]))
        self._out += message(handle, 4, array(w["states"]))
        self._out += message(handle, 5)

    def _handle(self, obj, opcode, payload):
        if obj == 1 and opcode == 1:             # wl_display.get_registry(new_id)
            self._registry = struct.unpack("=I", payload)[0]
            self._out += message(self._registry, 0, u32(1) + string("wl_compositor") + u32(4))
            if self.protocol:
                self._out += message(self._registry, 0, u32(2) + string(MANAGER) + u32(self.version))
        elif obj == 1 and opcode == 0:           # wl_display.sync(new_id)
            callback = struct.unpack("=I", payload)[0]
            self._out += message(callback, 0, u32(0))
        elif obj == self._registry and opcode == 0:   # wl_registry.bind(name, interface, version, id)
            name, ilen = struct.unpack("=II", payload[:8])
            iface = payload[8:8 + ilen - 1].decode()
            at = 8 + ilen + len(_pad(ilen))
            version, new_id = struct.unpack("=II", payload[at:at + 8])
            assert name == 2 and iface == MANAGER, "bound something else"
            assert version <= self.version
            self.bound = (iface, version, new_id)
            self._manager = new_id
            for handle in list(self.windows):    # already open windows are announced at bind
                self._announce(handle)
