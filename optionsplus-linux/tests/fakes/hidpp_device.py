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
FEATURES = [0x0000, 0x0001, 0x0003, 0x0005, 0x1004, 0x2201, 0x2110, 0x1D4B, 0x0020, 0x2121, 0x2150]
F_WAKE, F_CONFIG_CHANGE = 0x1D4B, 0x0020

# the receiver's own feature table (device index 0xFF)
RECEIVER_FEATURES = [0x0000, 0x1817]
F_PAIRING = 0x1817
# pairing event function 0 = status [status, error, slot]; function 1 = passkey digits
PAIR_CLOSED, PAIR_OPEN, PAIR_DONE, PAIR_FAILED = 0, 1, 2, 3


class SimulatedMouse:
    """Feature table and mutable state of one HID++ 2.0 mouse."""

    def __init__(self, name=MOUSE_NAME, wpid=MOUSE_WPID):
        self.name = name
        self.battery_percent = 73
        self.charging = False
        self.battery_caps = (0x0F, 0x03)           # levels crit/low/good/full; rechargeable + SoC
        self.dpi = 1000
        self.default_dpi = 1000
        self.fail_dpi_write = False                 # answer setSensorDpi with an error
        self.smartshift = {"mode": 2, "threshold": 30, "torque": 0}
        self.firmware = (0x12, 0x34, 0x0567)        # major, minor, build
        self.features = list(FEATURES)              # a test may drop some to model a simpler mouse
        self.fail_smartshift_write = False
        self.fail_wheel_write = False
        self.wheel_multiplier = 8
        self.wheel_caps = 0x08 | 0x04               # invert supported, ratchet switch
        self.wheel_mode = 0x02                      # as the kernel driver leaves it: HID++ target off,
                                                    # high resolution on, invert off
        # 0x2150 thumb wheel: reporting mode 1 native / 2 diverted, direction inverted, smooth
        self.thumb = {"mode": 1, "invert": 0, "smooth": 0}
        self.fail_thumb_write = False               # answer setThumbwheelReporting with an error
        self.wpid = wpid                            # hardware identity behind the receiver
        self.powered = True

    def power(self, on):
        """Switch the mouse on or off; a power cycle drops its DPI back to the default."""
        if on and not self.powered:
            self.dpi = self.default_dpi
        self.powered = on

    def handle(self, fidx, fn, params):
        """Return reply params for feature index `fidx`, or an int error code."""
        if fidx >= len(self.features):
            return 6                                  # INVALID_FEATURE_INDEX
        fid = self.features[fidx]
        if fid == 0x0000:
            if fn == 0:
                want = struct.unpack(">H", params[:2])[0]
                if want in self.features:
                    return bytes([self.features.index(want), 0, 1])
                return bytes([0, 0, 0])
            if fn == 1:
                return bytes([4, 2, params[2]])
        elif fid == 0x0001:
            if fn == 0:
                return bytes([len(self.features) - 1])
            if fn == 1:
                return struct.pack(">HB", self.features[params[0]], 0)
        elif fid == 0x0003:
            if fn == 0:
                return bytes([1])
            if fn == 1:
                major, minor, build = self.firmware
                return bytes([0]) + b"RBM" + bytes([major, minor]) + struct.pack(">H", build)
        elif fid == 0x0005:
            if fn == 0:
                return bytes([len(self.name)])
            if fn == 1:
                return self.name.encode()[params[0]:params[0] + 16]
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
                if self.fail_smartshift_write:
                    return 5                          # ERR_BUSY
                s = self.smartshift
                if params[0]:
                    s["mode"] = params[0]
                if params[1]:
                    s["threshold"] = params[1]
                return b""
        elif fid == 0x2121:
            if fn == 0:
                return bytes([self.wheel_multiplier, self.wheel_caps])
            if fn == 1:
                return bytes([self.wheel_mode])
            if fn == 2:
                if self.fail_wheel_write:
                    return 5
                self.wheel_mode = params[0]
                return bytes([self.wheel_mode])
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
        self.mice = {MOUSE_INDEX: SimulatedMouse()}   # paired devices by index
        self.path = self.PATH
        self.present = True               # is the receiver's hidraw node there
        self._changed = threading.Event()
        self.receiver_features = list(RECEIVER_FEATURES)   # a test may drop the pairing feature
        self.pairing_open = False         # is the receiver's pairing lock open
        self.fail_pairing_open = False    # answer the open-lock request with an error
        self.fail_pairing_cancel = False
        self.writes = []                  # every raw report the agent wrote
        self._out = queue.Queue()
        self._lock = threading.Lock()

    @property
    def mouse(self):
        return self.mice[MOUSE_INDEX]

    # -- transport surface ------------------------------------------------
    def list_nodes(self):
        if not self.present:
            return []
        return [hidpp.HidppNode(path=self.path, vid=hidpp.LOGITECH_VID, pid=BOLT_RECEIVER_PID,
                                name="Simulated Bolt Receiver", is_receiver=True, is_bolt=True)]

    def open(self, path):
        if not self.present or path != self.path:
            raise FileNotFoundError(path)
        return _Handle(self)

    def wait_change(self, timeout):
        """Block until a node appears or disappears; True if one did."""
        hit = self._changed.wait(timeout)
        self._changed.clear()
        return hit

    # -- test surface -----------------------------------------------------
    def unplug(self):
        """Pull the receiver: its hidraw node goes away."""
        self.present = False
        self._changed.set()

    def plug(self, path=None):
        """Plug the receiver back in, possibly as a different hidraw node."""
        self.path = path or self.path
        self.present = True
        self._changed.set()

    def emit(self, report):
        """Queue an unsolicited report, as the device would send."""
        self._out.put(bytes(report))

    def emit_event(self, feature_id, function=0, params=b"", index=MOUSE_INDEX):
        """Queue an unsolicited feature event (software id 0) from the mouse."""
        head = bytes([hidpp.LONG, index, FEATURES.index(feature_id), function << 4])
        self.emit((head + params).ljust(hidpp.LONG_LEN, b"\0"))

    def emit_pairing_status(self, status, error=0, slot=0):
        """Queue a pairing status event from the receiver."""
        self._emit_receiver_event(0, bytes([status, error, slot]))

    def emit_passkey(self, digits):
        """Queue a passkey event: the digits the user must type on the device."""
        self._emit_receiver_event(1, digits.encode())

    def complete_pairing(self, name="Second Mouse Z2", wpid=0x4343):
        """A device pairs in the open lock: it appears at the next free index."""
        index = max(self.mice) + 1
        self.mice[index] = SimulatedMouse(name, wpid)
        self.pairing_open = False
        self.emit_pairing_status(PAIR_DONE, 0, index)
        return index

    def _emit_receiver_event(self, function, params):
        head = bytes([hidpp.LONG, 0xFF, self.receiver_features.index(F_PAIRING), function << 4])
        self.emit((head + params).ljust(hidpp.LONG_LEN, b"\0"))

    def _receiver_handle(self, data):
        fidx, fn, params = data[2], data[3] >> 4, data[4:]
        if fidx == 0 and fn == 0:
            want = struct.unpack(">H", params[:2])[0]
            if want in self.receiver_features:
                return bytes([self.receiver_features.index(want), 0, 1])
            return bytes([0, 0, 0])
        if fidx < len(self.receiver_features) and self.receiver_features[fidx] == F_PAIRING:
            if fn == 0:                                # open the lock; params[0] = timeout seconds
                if self.fail_pairing_open:
                    return 8                           # BUSY
                self.pairing_open = True
                return bytes([params[0]])
            if fn == 1:                                # close the lock (cancel)
                if self.fail_pairing_cancel:
                    return 8
                self.pairing_open = False
                return b""
        return 7

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
                if params[0] - 0x50 in self.mice:
                    mouse = self.mice[params[0] - 0x50]
                    self._reply(data, bytes([params[0], 0x10]) + struct.pack("<H", mouse.wpid) + b"\x01\x02\x03\x04")
                else:
                    self._error(data, False, 2)
                return
            if idx == 0xFF:
                result = self._receiver_handle(data)
                if isinstance(result, int):
                    self._error(data, True, result)
                else:
                    self._reply(data, result)
                return
            mouse = self.mice.get(idx)
            if mouse is None:
                self._error(data, False, 8)           # UNKNOWN_DEVICE
                return
            if not mouse.powered:
                self._error(data, False, 8)           # nothing answers for a mouse that is off
                return
            result = mouse.handle(b2, b3 >> 4, params)
            if isinstance(result, int):
                self._error(data, True, result)
            else:
                self._reply(data, result)


class SimulatedBluetoothMouse(SimulatedBolt):
    """A mouse paired directly over Bluetooth: no receiver, one node at the fixed
    device index 0xFF, and a transport that carries long reports only. A short
    request is never answered; every reply (errors too) is a long report."""

    PATH = "/dev/hidraw-sim-ble0"
    BLE_PID = MOUSE_WPID              # the catalogue knows the mouse by this PID

    def __init__(self):
        super().__init__()
        self.path = self.PATH

    def list_nodes(self):
        if not self.present:
            return []
        return [hidpp.HidppNode(path=self.path, vid=hidpp.LOGITECH_VID, pid=self.BLE_PID,
                                name=MOUSE_NAME, is_receiver=False, long_only=True)]

    def _error(self, request, version2, code):
        marker = 0xFF if version2 else 0x8F
        self._out.put(bytes([hidpp.LONG, request[1], marker, request[2], request[3], code]).ljust(hidpp.LONG_LEN, b"\0"))

    def _reply(self, request, params):
        head = bytes([hidpp.LONG, request[1], request[2], request[3]])
        self._out.put((head + params).ljust(hidpp.LONG_LEN, b"\0"))

    def _on_write(self, data):
        with self._lock:
            self.writes.append(data)
            if data[0] != hidpp.LONG:
                return                                # no short-report collection: dropped
            idx, b2, b3, params = data[1], data[2], data[3], data[4:]
            if idx != hidpp.DEV_RECEIVER or not self.mouse.powered:
                self._error(data, False, 8)           # UNKNOWN_DEVICE
                return
            result = self.mouse.handle(b2, b3 >> 4, params)
            if isinstance(result, int):
                self._error(data, True, result)
            else:
                self._reply(data, result)
