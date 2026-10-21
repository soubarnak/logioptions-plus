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
FEATURES = [0x0000, 0x0001, 0x0003, 0x0005, 0x1004, 0x2201, 0x2110, 0x1D4B, 0x0020, 0x2121, 0x2150, 0x1814, 0x1815, 0x1B04]
F_WAKE, F_CONFIG_CHANGE = 0x1D4B, 0x0020
F_REPROG = 0x1B04
F_HIRES_WHEEL, F_THUMB_WHEEL = 0x2121, 0x2150

# 0x1b04 reprogrammable controls: (cid, task id, flags1, position, group, group mask, flags2).
# flags1 bits: 0x01 mouse, 0x10 reprogrammable, 0x20 divertable. Invented table.
CONTROLS = [
    (80, 80, 0x01, 1, 1, 1, 0),                 # left button: fixed
    (81, 81, 0x01, 2, 1, 1, 0),                 # right button: fixed
    (82, 82, 0x31, 3, 2, 2, 0),                 # middle (0x52 on a real mouse)
    (83, 83, 0x31, 4, 2, 2, 0),                 # back (0x53)
    (86, 86, 0x31, 5, 2, 2, 0),                 # forward (0x56)
    (195, 195, 0x31, 6, 3, 4, 0x01),            # gesture button
]
DIVERT = 0x01                                   # setCidReporting flags: divert; the change bit is DIVERT << 1
PERSISTENT = 0x04                               # same byte: divert kept across power cycles; change bit 0x08
RAW_XY = 0x10                                   # same byte: divert raw XY; the change bit is RAW_XY << 1

# Bolt pairing runs on HID++ 1.0 registers and notifications (layouts after Solaar):
# register 0xC0 discovery [timeout, 1 start | 2 cancel]; register 0x2C1 (sub id 0x82, address 0xC1)
# pairing [1 pair | 2 cancel, slot, address(6), authentication, entropy]; notifications 0x53 discovery
# status, 0x4F advertised device, 0x54 pairing status, 0x4D passkey to enter on the device.
N_DISCOVERY_STATUS, N_DEVICE_DISCOVERY, N_PAIRING_STATUS, N_PASSKEY = 0x53, 0x4F, 0x54, 0x4D
BOLT_ERR_TIMEOUT, BOLT_ERR_FAILED = 1, 2


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
        # 0x2150 thumb wheel: reporting mode 0 native / 1 diverted, direction inverted, smooth
        self.thumb = {"mode": 0, "invert": 0, "smooth": 0}
        # getThumbwheelInfo, as the MX Master 3S answers it: 18 native and 120 diverted units per
        # revolution (6.67 diverted units per notch). The default here is 126 = 7 units per notch
        # so a whole number of units makes a notch; tests set `thumb_diverted_resolution` for the real 120.
        self.thumb_native_resolution = 18
        self.thumb_diverted_resolution = 126
        self.thumb_default_direction = 0            # that answer's direction byte: 0 on the real mouse
        self.fail_thumb_write = False               # answer setThumbwheelReporting with an error
        self.thumb_ignores_mode = False             # accept setThumbwheelReporting but keep the reporting mode
        # 0x1814 / 0x1815 Easy-Switch: host slots (name bytes, paired, bus, os) and the current host
        self.hosts = [
            {"name": b"Desk PC", "paired": True, "bus": 5, "os": 1},
            {"name": b"Laptop", "paired": True, "bus": 5, "os": 3},
            {"name": b"", "paired": False, "bus": 0, "os": 0}]
        self.current_host = 0
        self.max_host_name = 32
        self.fail_host_switch = False               # answer setCurrentHost with an error
        self.fail_host_name_write = False           # answer setHostFriendlyName with an error
        self.fail_host_os_read = False              # answer getHostOsVersion (fn7) with an error
        self.host_writes = []                       # (fn, host) of moveHost (5), deleteHost (6), setHostOsVersion (8)
        self.host_os_silent = False                 # getHostOsVersion gets no reply at all
        self.host_os_echo = None                    # when set, getHostOsVersion echoes this host index instead
        self.host_caps = 0x03                       # 0x1815 capability flags: bit0 read names, bit1 write names
        self.name_reads, self.name_writes = [], []  # (host, offset) / (host, offset, name bytes) requests seen
        # 0x1b04: cids the mouse reports to software instead of acting on, and cids held now
        self.diverted = set()
        self.persistent = set()                     # cids diverted persistently (flag 0x04), also reported to software
        self.divert_writes = []                     # cids software asked to divert or undivert, in order
        self.raw_xy = set()                         # controls whose motion is reported as raw XY
        self.held = []
        self.fail_divert_write = False              # answer setCidReporting with an error
        self.ignore_divert = False                  # accept setCidReporting but change nothing
        # 0x1b04 remap: cid -> cid it acts as. Left and right (80, 81) are remappable only when
        # this is set; a setCidReporting that names no target (0) keeps that control's remap (as
        # real mice do), and naming the control itself undoes the remap.
        self.remappable_primary = False
        self.primary_group_mask = 1                 # 0x1b04 group mask of left/right: the groups they may become
        self.remap = {}
        self.fail_remap_write = False               # answer a remapping setCidReporting with an error
        self.fail_remap_write_after = None          # ... but only after this many accepted remaps
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
                return struct.pack(">HHBB", self.thumb_native_resolution, self.thumb_diverted_resolution,
                                   self.thumb_default_direction, 0x0f)
            if fn == 1:
                return bytes([t["mode"], t["invert"], t["smooth"]])
            if fn == 2:
                if self.fail_thumb_write:
                    return 5                          # ERR_BUSY
                t.update(mode=t["mode"] if self.thumb_ignores_mode else params[0],
                         invert=params[1] & 1, smooth=params[2] & 1)
                return bytes([t["mode"], t["invert"], t["smooth"]])
        elif fid == F_REPROG:
            if fn == 0:
                return bytes([len(CONTROLS)])
            if fn == 1:
                if params[0] >= len(CONTROLS):
                    return 2
                cid, task, flags1, *rest = CONTROLS[params[0]]
                if self.remappable_primary and cid in (80, 81):
                    flags1 |= 0x10
                    rest[2] = self.primary_group_mask
                return struct.pack(">HHBBBBB", cid, task, flags1, *rest)
            cid = struct.unpack(">H", params[:2])[0]
            if cid not in [c[0] for c in CONTROLS]:
                return 2
            if fn == 2:                               # getCidReporting: cid, flags, remapped cid, flags2
                flags = ((DIVERT if cid in self.diverted else 0) | (PERSISTENT if cid in self.persistent else 0)
                         | (RAW_XY if cid in self.raw_xy else 0))
                return struct.pack(">HBHB", cid, flags, self.remap.get(cid, cid), 0)
            if fn == 3:                               # setCidReporting: cid, flags, remap cid
                if self.fail_divert_write:
                    return 5
                target = struct.unpack(">H", params[3:5])[0]
                remapping = target not in (0, cid)
                undoing = target == cid
                if remapping and (self.fail_remap_write or (
                        self.fail_remap_write_after is not None
                        and len(self.remap) >= self.fail_remap_write_after)):
                    return 5
                if remapping:
                    if not (self.remappable_primary and cid in (80, 81)
                            or next(c for c in CONTROLS if c[0] == cid)[2] & 0x10):
                        return 2                      # not remappable
                    self.remap[cid] = target
                elif undoing:
                    self.remap.pop(cid, None)          # target 0 keeps whatever mapping there is
                if params[2] & (DIVERT << 1):
                    self.divert_writes.append(cid)
                    if not next(c for c in CONTROLS if c[0] == cid)[2] & 0x20:
                        return 2                      # not divertable
                    if not self.ignore_divert:
                        (self.diverted.add if params[2] & DIVERT else self.diverted.discard)(cid)
                if params[2] & (PERSISTENT << 1):
                    (self.persistent.add if params[2] & PERSISTENT else self.persistent.discard)(cid)
                if params[2] & (RAW_XY << 1):
                    if not next(c for c in CONTROLS if c[0] == cid)[6] & 0x01:
                        return 2                      # no raw XY on this control
                    if not self.ignore_divert:
                        (self.raw_xy.add if params[2] & RAW_XY else self.raw_xy.discard)(cid)
                return bytes(params[:5])
        elif fid == 0x1814:
            if fn == 0:                               # getHostInfo: host count, current host
                return bytes([len(self.hosts), self.current_host])
            if fn == 1:                               # setCurrentHost: a real mouse leaves, so no reply
                if self.fail_host_switch:
                    return 5
                if params[0] >= len(self.hosts):
                    return 2                          # INVALID_ARGUMENT
                self.current_host = params[0]
                return None
        elif fid == 0x1815:
            # Layout after Solaar's HostsInfo: fn0 [capability flags, 0, host count, current host],
            # fn1 getHostInfo(host) [host, status, bus, pages, name length, max name length],
            # fn2 getHostDescriptor, fn3 getHostFriendlyName(host, offset) [host, offset, 14 name bytes],
            # fn4 setHostFriendlyName(host, offset, 14 name bytes).
            if fn == 0:
                return bytes([self.host_caps, 0, len(self.hosts), self.current_host])
            if fn in (1, 2, 3, 4, 5, 6, 7, 8) and params[0] >= len(self.hosts):
                return 2
            if fn == 1:
                h = self.hosts[params[0]]
                return bytes([params[0], 1 if h["paired"] else 0, h["bus"], 1, len(h["name"]),
                              h.get("max_name", self.max_host_name)])
            if fn == 2:
                return bytes([params[0], 0])
            if fn == 3:
                self.name_reads.append((params[0], params[1]))
                return bytes([params[0], params[1]]) + self.hosts[params[0]]["name"][params[1]:params[1] + 14]
            if fn in (5, 6, 8):
                # moveHost, deleteHost, setHostOsVersion: writes the agent must never send
                self.host_writes.append((fn, params[0]))
                return 5
            if fn == 7:
                # getHostOsVersion(host) -> [host, os type, major u8, minor u16 BE, patch u16 BE]
                if self.host_os_silent:
                    return None
                if self.fail_host_os_read:
                    return 5
                echo = params[0] if self.host_os_echo is None else self.host_os_echo
                return bytes([echo, self.hosts[params[0]]["os"], 10, 0, 0, 0, 0])
            if fn == 4:
                if self.fail_host_name_write:
                    return 5
                h = self.hosts[params[0]]
                chunk = bytes(params[2:16])
                self.name_writes.append((params[0], params[1], len(chunk.rstrip(b"\0"))))
                # the device takes the name as ending where the last write ends
                h["name"] = (h["name"][:params[1]] + chunk.rstrip(b"\0"))[:h.get("max_name", self.max_host_name)]
                h["paired"] = True
                return bytes([params[0], params[1]])
        return 7                                      # INVALID_FUNCTION_ID


class _Handle:
    """What the agent holds after opening the receiver's node."""

    def __init__(self, receiver):
        self.rx = receiver
        self._pending = False       # a report was handed out and its routing is not known to be done

    def write(self, data):
        self.rx._on_write(bytes(data))

    def read(self, timeout):
        # asking again means the previous report was routed: `_out.unfinished_tasks` is what is
        # still queued or being routed, which is what `RunningAgent.settle` waits on
        self._done()
        try:
            item = self.rx._out.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None
        self._pending = True
        return item

    def _done(self):
        if self._pending:
            self._pending = False
            self.rx._out.task_done()

    def close(self):
        self._done()


class SimulatedBolt:
    """A transport (`list_nodes` + `open`) exposing one Bolt receiver."""

    PATH = "/dev/hidraw-sim0"

    def __init__(self, path=None, unique_id=b"\x0b\x01\x0b\x01", mouse=None):
        self.mice = {MOUSE_INDEX: mouse or SimulatedMouse()}   # paired devices by index
        self.path = path or self.PATH
        self.unique_id = unique_id        # register 0x2FB
        self.present = True               # is the receiver's hidraw node there
        self._changed = threading.Event()
        self.pairing_open = False         # is the receiver's pairing lock open
        self.discovering = False          # is device discovery running
        self.paired_address = None        # the address the agent asked to pair
        self.passkey = None               # digits to announce when a pairing opens (a device that needs one)
        self.fail_discovery = False       # answer the start-discovery write with an error
        self.fail_pairing_open = False    # answer the pair request with an error
        self.fail_pairing_cancel = False
        self._counter = 0
        self.writes = []                  # every raw report the agent wrote
        self.drop_name_reads = 0          # leave this many device-name requests unanswered
        self.drop_pings = 0               # answer this many root pings of a present device late
        self._late_pings = []             # those pings, answered once the next ping is written
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

    def node_exists(self, path):
        return self.present and path == self.path

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

    def emit_connection(self, link=True, index=MOUSE_INDEX, kind=0x02):
        """Queue a HID++ 1.0 device-connection notification (sub id 0x41): a mouse that
        woke or switched on reports its link as established; flags bit 0x40 set means lost."""
        flags = 0x00 if link else 0x40
        self.emit(bytes([hidpp.SHORT, index, 0x41, kind, flags | 0x04, 0x42, 0x42]))

    def turn_wheel(self, delta, hires=True, index=MOUSE_INDEX):
        """The scroll wheel turns by `delta` (signed; 1/multiplier notches when hi-res, else
        notches). Only a wheel the agent diverted reports to software: the native HID path is
        not modelled, so a native wheel emits nothing here. Returns whether a report went out."""
        mouse = self.mice[index]
        if not mouse.wheel_mode & 0x01:
            return False
        flags = 0x10 if hires else 0x00
        self.emit_event(F_HIRES_WHEEL, 0, bytes([flags]) + struct.pack(">h", delta), index=index)
        return True

    def turn_thumb(self, rotation, index=MOUSE_INDEX):
        """The user turns the thumb wheel to the right by `rotation` units (negative: left);
        reported only while diverted. A mouse whose default direction byte is 0 (the real one)
        reports a turn to the right as a negative rotation."""
        mouse = self.mice[index]
        if mouse.thumb["mode"] != 1:
            return False
        raw = rotation if mouse.thumb_default_direction else -rotation
        self.emit_event(F_THUMB_WHEEL, 0, struct.pack(">hHBB", raw, 0, 2, 0), index=index)
        return True

    def press_button(self, cid, index=MOUSE_INDEX):
        """The user presses a button; a diverted one is reported to software."""
        mouse = self.mice[index]
        mouse.held.append(cid)
        self._button_event(mouse, index)

    def move_mouse(self, dx, dy, index=MOUSE_INDEX):
        """The user moves the mouse; while a raw-XY control is held the motion goes to software."""
        mouse = self.mice[index]
        if any(c in mouse.raw_xy for c in mouse.held):
            self.emit_event(F_REPROG, 1, struct.pack(">hh", dx, dy), index=index)

    def rotate_thumb(self, amount, index=MOUSE_INDEX):
        """The user turns the thumb wheel to the right by `amount` (negative: left); reported to
        software only while it is diverted, with the sign the mouse's default direction gives."""
        mouse = self.mice[index]
        if mouse.thumb["mode"] == 1:
            raw = amount if mouse.thumb_default_direction else -amount
            self.emit_event(0x2150, 0, struct.pack(">hHBB", raw, 0, 2, 0), index=index)

    def release_button(self, cid, index=MOUSE_INDEX):
        mouse = self.mice[index]
        mouse.held.remove(cid)
        self._button_event(mouse, index)

    def _button_event(self, mouse, index):
        reported = mouse.diverted | mouse.persistent
        if any(c in reported for c in mouse.held) or not mouse.held:
            held = [c for c in mouse.held if c in reported]
            self.emit_event(F_REPROG, 0, b"".join(struct.pack(">H", c) for c in held), index=index)

    def emit_notification(self, sub_id, address=0, data=b""):
        """Queue a HID++ 1.0 notification from the receiver."""
        self.emit(bytes([hidpp.LONG, 0xFF, sub_id, address]) + bytes(data).ljust(hidpp.LONG_LEN - 4, b"\0"))

    def emit_discovery_status(self, open_, error=0):
        """0x53: address 0 = discovery running, else closed; data[0] = error."""
        self.emit_notification(N_DISCOVERY_STATUS, 0 if open_ else 1, bytes([error]))

    def advertise(self, name="New Mouse", address=b"\xaa\xbb\xcc\xdd\xee\x01", kind=2, authentication=0):
        """0x4F twice (part 0: kind, address, authentication; part 1: name): a device in pairing
        mode advertises itself to the receiver."""
        self._counter += 1
        lo, hi = self._counter & 0xFF, self._counter >> 8
        first = bytearray(16)
        first[0], first[1], first[3] = hi, 0, kind
        first[6:12], first[14] = address, authentication
        self.emit_notification(N_DEVICE_DISCOVERY, lo, bytes(first))
        second = bytearray(16)
        second[0], second[1], second[2] = hi, 1, len(name)
        second[3:3 + len(name)] = name.encode()
        self.emit_notification(N_DEVICE_DISCOVERY, lo, bytes(second))

    def emit_pairing_status(self, address=0, error=0, slot=0):
        """0x54: address 0 = pairing open, 1 = closed, 2 = paired in `slot`; data[0] = error."""
        data = bytearray(16)
        data[0], data[7] = error, slot
        self.emit_notification(N_PAIRING_STATUS, address, bytes(data))

    def emit_passkey(self, digits):
        """0x4D: the digits the user must enter on the device."""
        self.emit_notification(N_PASSKEY, 0, digits.encode())

    def add_device(self, name="Second Mouse Z2", wpid=0x4343):
        """A device joins the receiver at the next free index, without any notification."""
        index = max(self.mice) + 1
        self.mice[index] = SimulatedMouse(name, wpid)
        return index

    def complete_pairing(self, name="Second Mouse Z2", wpid=0x4343):
        """The device in the open lock pairs: it takes the next index and the receiver says so."""
        index = self.add_device(name, wpid)
        self.pairing_open = False
        self.emit_pairing_status(2, 0, index)
        return index

    def _receiver_handle(self, data):
        return 1                                       # INVALID_SUBID: no HID++ 2.0 root on the receiver

    def _receiver_register(self, data):
        """HID++ 1.0 register access to the receiver; True when handled (lock held)."""
        sub, address, params = data[2], data[3], data[4:]
        if sub == 0x83 and address == 0xFB:             # register 0x2FB: unique id
            self._reply(data, self.unique_id)
            return True
        if sub == 0x80 and address == 0xC0:             # discovery: [timeout, action]
            if self.fail_discovery:
                self._error(data, False, 7)
                return True
            self.discovering = params[1] == 1
            self._reply(data, bytes(params[:3]))
            self.emit_discovery_status(self.discovering)
            return True
        if sub == 0x82 and address == 0xC1:             # pairing: [action, slot, address(6), auth, entropy]
            if params[0] == 1:
                if self.fail_pairing_open:
                    self._error(data, False, 7)
                    return True
                self.discovering = False
                self.pairing_open = True
                self.paired_address = bytes(params[2:8])
                self._reply(data, bytes(params[:3]))
                self.emit_pairing_status(0)
                if self.passkey:
                    self.emit_passkey(self.passkey)
            else:
                if self.fail_pairing_cancel:
                    self._error(data, False, 7)
                    return True
                self.pairing_open = False
                self._reply(data, bytes(params[:3]))
                self.emit_pairing_status(1)
            return True
        if data[2] == 0x83 and data[3] == 0xB5:
            params = data[4:]
            # pairing info for slot params[0] - 0x50: wpid little endian at [2:4]
            if params[0] - 0x50 in self.mice:
                mouse = self.mice[params[0] - 0x50]
                self._reply(data, bytes([params[0], 0x10]) + struct.pack("<H", mouse.wpid) + b"\x01\x02\x03\x04")
            else:
                self._error(data, False, 2)
            return True
        return False

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
            if idx == 0xFF and self._receiver_register(data):
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
            if b2 == 0 and b3 >> 4 == 1:
                for late in self._late_pings:         # the answer to the earlier ping, after its wait ended
                    self._reply(late, mouse.handle(0, 1, late[4:]))
                self._late_pings = []
                if self.drop_pings:
                    self.drop_pings -= 1
                    self._late_pings.append(data)
                    return
            if self.drop_name_reads and b2 < len(mouse.features) and mouse.features[b2] == 0x0005:
                self.drop_name_reads -= 1
                return                                # a busy reader: this request goes unanswered
            result = mouse.handle(b2, b3 >> 4, params)
            if result is None:
                return                                # a request this device never answers
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
            if result is None:
                return
            if isinstance(result, int):
                self._error(data, True, result)
            else:
                self._reply(data, result)


UNIFYING_RECEIVER_PID = 0xC52B
# HID++ 1.0 error codes
ERR_INVALID_SUBID, ERR_INVALID_ADDRESS, ERR_BUSY = 1, 2, 7
# Unifying lock errors in the 0x4A notification (Solaar's PairingError)
LOCK_DEVICE_TIMEOUT, LOCK_NOT_SUPPORTED, LOCK_TOO_MANY_DEVICES, LOCK_SEQUENCE_TIMEOUT = 1, 2, 3, 6


class SimulatedUnifying(SimulatedBolt):
    """A Unifying receiver with one paired mouse. It speaks HID++ 1.0 only: no feature
    table on the receiver, pairing is register 0xB2 and progress arrives as 0x4A
    lock-status notifications (address bit 0 = lock open, data[0] = error code)."""

    PATH = "/dev/hidraw-sim-unifying0"

    def __init__(self):
        super().__init__()
        self.path = self.PATH
        self.ignore_pairing_writes = False        # never answer a write to 0xB2 (receiver hung)
        self.notify_before_reply = False          # close notification goes out before the reply to the close

    def list_nodes(self):
        if not self.present:
            return []
        return [hidpp.HidppNode(path=self.path, vid=hidpp.LOGITECH_VID, pid=UNIFYING_RECEIVER_PID,
                                name="Simulated Unifying Receiver", is_receiver=True, is_bolt=False)]

    def emit_lock_status(self, open_, error=0):
        """Queue a 0x4A lock-status notification from the receiver."""
        self.emit(bytes([hidpp.SHORT, 0xFF, 0x4A, 0x01 if open_ else 0x00, error, 0, 0]))

    def complete_pairing(self, name="Second Mouse Z2", wpid=0x4343):
        """A device pairs in the open lock: it takes the next index and the lock closes."""
        index = self.add_device(name, wpid)
        self.pairing_open = False
        self.emit_connection(True, index=index)       # the new device announces itself, then the lock closes
        self.emit_lock_status(open_=False)
        return index

    def _receiver_register(self, data):
        sub, address, params = data[2], data[3], data[4:]
        if sub == 0x83 and address == 0xB5:
            # pairing info for slot params[0] - 0x20 + 1: wpid big endian at [3:5]
            slot = params[0] - 0x20 + 1
            if slot in self.mice:
                self._reply(data, bytes([params[0], 0x10, 0x00]) + struct.pack(">H", self.mice[slot].wpid) + b"\x01\x02")
            else:
                self._error(data, False, ERR_INVALID_ADDRESS)
            return True
        if sub == 0x80 and address == 0xB2:
            if self.ignore_pairing_writes:
                return True
            action = params[0]
            if action == 0x01:                    # open the lock: [1, device (0 = any), timeout]
                if self.fail_pairing_open:
                    self._error(data, False, ERR_BUSY)
                    return True
                self.pairing_open = True
                self._reply(data, bytes(params[:3]))
                self.emit_lock_status(open_=True)
            elif action == 0x02:                  # close the lock
                if self.fail_pairing_cancel:
                    self._error(data, False, ERR_BUSY)
                    return True
                self.pairing_open = False
                if self.notify_before_reply:
                    self.emit_lock_status(open_=False)
                    self._reply(data, bytes(params[:3]))
                    return True
                self._reply(data, bytes(params[:3]))
                self.emit_lock_status(open_=False)
            else:
                self._error(data, False, ERR_INVALID_ADDRESS)
            return True
        return False
