"""Simulated HID++ transport: a Bolt receiver with one paired mouse.

Stands in for hidraw. The mouse is described by a feature table plus mutable
state, answers short and long requests, records every write and can emit
unsolicited notifications on demand. All identifiers are invented.
"""

import queue
import struct
import threading

from opl_agent import hidpp

BOLT_RECEIVER_PID = 0xC548          # a receiver PID the agent already knows
MOUSE_WPID = 0x4242                 # invented wireless PID
MOUSE_NAME = "Test Mouse X1"
MOUSE_INDEX = 1

# feature index -> HID++ 2.0 feature id (index 0 is always Root)
FEATURES = [0x0000, 0x0001, 0x0003, 0x0005, 0x1004, 0x2201, 0x2110, 0x2150]


class SimulatedMouse:
    """Feature table and mutable state of one HID++ 2.0 mouse."""

    def __init__(self):
        self.battery_percent = 73
        self.charging = False
        self.battery_caps = (0x0F, 0x03)           # levels crit/low/good/full; rechargeable + SoC
        self.dpi = 1000
        self.default_dpi = 1000
        self.fail_dpi_write = False                 # answer setSensorDpi with an error
        self.smartshift = {"mode": 2, "threshold": 30, "torque": 0}
        self.firmware = (0x12, 0x34, 0x0567)        # major, minor, build
        # 0x2150 thumb wheel: reporting mode 1 native / 2 diverted, direction inverted, smooth
        self.thumb = {"mode": 1, "invert": 0, "smooth": 0}
        self.fail_thumb_write = False               # answer setThumbwheelReporting with an error

    def handle(self, fidx, fn, params):
        """Return reply params for feature index `fidx`, or an int error code."""
        if fidx >= len(FEATURES):
            return 6                                  # INVALID_FEATURE_INDEX
        fid = FEATURES[fidx]
        if fid == 0x0000:
            if fn == 0:
                want = struct.unpack(">H", params[:2])[0]
                if want in FEATURES:
                    return bytes([FEATURES.index(want), 0, 1])
                return bytes([0, 0, 0])
            if fn == 1:
                return bytes([4, 2, params[2]])
        elif fid == 0x0001:
            if fn == 0:
                return bytes([len(FEATURES) - 1])
            if fn == 1:
                return struct.pack(">HB", FEATURES[params[0]], 0)
        elif fid == 0x0003:
            if fn == 0:
                return bytes([1])
            if fn == 1:
                major, minor, build = self.firmware
                return bytes([0]) + b"RBM" + bytes([major, minor]) + struct.pack(">H", build)
        elif fid == 0x0005:
            if fn == 0:
                return bytes([len(MOUSE_NAME)])
            if fn == 1:
                return MOUSE_NAME.encode()[params[0]:params[0] + 16]
            if fn == 2:
                return bytes([3])                     # mouse
        elif fid == 0x1004:
            if fn == 0:
                return bytes(self.battery_caps)
            if fn == 1:
                return bytes([self.battery_percent, 4, 1 if self.charging else 0, 0])
        elif fid == 0x2201:
            if fn == 0:
                return bytes([1])
            if fn == 1:
                return bytes([0]) + struct.pack(">4H", 200, 0xE032, 8000, 0)
            if fn == 2:
                return bytes([0]) + struct.pack(">HH", self.dpi, self.default_dpi)
            if fn == 3:
                if self.fail_dpi_write:
                    return 5                          # ERR_BUSY
                self.dpi = struct.unpack(">H", params[1:3])[0]
                return bytes([0]) + struct.pack(">H", self.dpi)
        elif fid == 0x2110:
            if fn == 0:
                s = self.smartshift
                return bytes([s["mode"], s["threshold"], s["torque"]])
            if fn == 1:
                s = self.smartshift
                if params[0]:
                    s["mode"] = params[0]
                if params[1]:
                    s["threshold"] = params[1]
                return b""
        elif fid == 0x2150:
            t = self.thumb
            if fn == 0:                               # info: native res, diverted res, default dir, caps
                return struct.pack(">HHBB", 120, 14, 0, 0x03)
            if fn == 1:
                return bytes([t["mode"], t["invert"], t["smooth"]])
            if fn == 2:
                if self.fail_thumb_write:
                    return 5                          # ERR_BUSY
                t.update(mode=params[0], invert=params[1] & 1, smooth=params[2] & 1)
                return bytes([t["mode"], t["invert"], t["smooth"]])
        return 7                                      # INVALID_FUNCTION_ID


class _Handle:
    """What the agent holds after opening the receiver's node."""

    def __init__(self, receiver):
        self.rx = receiver

    def write(self, data):
        self.rx._on_write(bytes(data))

    def drain(self):
        try:
            while True:
                self.rx._out.get_nowait()
        except queue.Empty:
            pass

    def read(self, timeout):
        try:
            return self.rx._out.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None

    def close(self):
        pass


class SimulatedBolt:
    """A transport (`list_nodes` + `open`) exposing one Bolt receiver."""

    PATH = "/dev/hidraw-sim0"

    def __init__(self):
        self.mouse = SimulatedMouse()
        self.writes = []                  # every raw report the agent wrote
        self._out = queue.Queue()
        self._lock = threading.Lock()

    # -- transport surface ------------------------------------------------
    def list_nodes(self):
        return [hidpp.HidppNode(path=self.PATH, vid=hidpp.LOGITECH_VID, pid=BOLT_RECEIVER_PID,
                                name="Simulated Bolt Receiver", is_receiver=True, is_bolt=True)]

    def open(self, path):
        assert path == self.PATH, path
        return _Handle(self)

    # -- test surface -----------------------------------------------------
    def emit(self, report):
        """Queue an unsolicited report, as the device would send."""
        self._out.put(bytes(report))

    # -- device side ------------------------------------------------------
    def _reply(self, request, params):
        short = request[0] == hidpp.SHORT and len(params) <= 3
        size = hidpp.SHORT_LEN if short else hidpp.LONG_LEN
        head = bytes([hidpp.SHORT if short else hidpp.LONG, request[1], request[2], request[3]])
        self._out.put((head + params).ljust(size, b"\0"))

    def _error(self, request, version2, code):
        marker = 0xFF if version2 else 0x8F
        self._out.put(bytes([hidpp.SHORT, request[1], marker, request[2], request[3], code, 0]))

    def _on_write(self, data):
        with self._lock:
            self.writes.append(data)
            idx, b2, b3, params = data[1], data[2], data[3], data[4:]
            if idx == 0xFF and b2 == 0x83 and b3 == 0xB5:
                # pairing info for slot params[0] - 0x50: wpid little endian at [2:4]
                if params[0] - 0x50 == MOUSE_INDEX:
                    self._reply(data, bytes([params[0], 0x10]) + struct.pack("<H", MOUSE_WPID) + b"\x01\x02\x03\x04")
                else:
                    self._error(data, False, 2)
                return
            if idx != MOUSE_INDEX:
                self._error(data, False, 8)           # UNKNOWN_DEVICE
                return
            result = self.mouse.handle(b2, b3 >> 4, params)
            if isinstance(result, int):
                self._error(data, True, result)
            else:
                self._reply(data, result)
