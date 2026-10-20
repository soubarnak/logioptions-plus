"""HID++ 2.0 client over Linux hidraw (stdlib only, blocking).

Report layout (Logitech HID++ 2.0):

    byte0  report id: 0x10 short (7 bytes), 0x11 long (20 bytes), 0x12 very long
    byte1  device index: 0xFF receiver / corded device, 0x01..0x06 paired
           wireless device behind a receiver
    byte2  feature index (index into the device's feature table; 0 = Root)
    byte3  (function << 4) | softwareId
    byte4+ parameters, zero padded

Errors: HID++ 2.0 -> byte2 == 0xFF, byte3 = feature idx, byte4 = fn|swid,
byte5 = code.  HID++ 1.0 -> byte2 == 0x8F, byte3 = sub id, byte4 = address,
byte5 = code.  Notifications (unsolicited) carry software id 0, never ours:
the Reader sends them to listeners and replies to the waiting request.

All functions block; call them from a thread executor.
"""

import errno
import logging
import os
import queue
import select
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

log = logging.getLogger("opl.hidpp")

LOGITECH_VID = 0x046D
SHORT, LONG = 0x10, 0x11
SHORT_LEN, LONG_LEN = 7, 20
DEV_RECEIVER = 0xFF  # receiver itself / corded device
SW_ID = 0x5
DEFAULT_TIMEOUT = 2.0
PING_MARKER = 0xA5

# Known receiver USB PIDs (Unifying, Nano, Lightspeed, Bolt).
RECEIVER_PIDS = {
    0xC52B, 0xC532, 0xC534, 0xC539, 0xC53A, 0xC53D, 0xC53F, 0xC545, 0xC547,
    0xC548,
}
BOLT_PIDS = {0xC548}

# HID++ 2.0 feature ids
F_ROOT = 0x0000
F_FEATURE_SET = 0x0001
F_DEVICE_FW_INFO = 0x0003
F_DEVICE_NAME = 0x0005
F_BATTERY_STATUS = 0x1000
F_UNIFIED_BATTERY = 0x1004
F_ADJUSTABLE_DPI = 0x2201
F_SMART_SHIFT = 0x2110
F_SMART_SHIFT_ENH = 0x2111
F_HIRES_WHEEL = 0x2121
F_THUMB_WHEEL = 0x2150
REG_RECEIVER_PAIRING = 0xB2     # Unifying receiver pairing lock (HID++ 1.0 register)
REG_BOLT_DISCOVERY = 0xC0       # Bolt device discovery (HID++ 1.0 register, short)
REG_BOLT_PAIRING = 0x2C1        # Bolt pairing (HID++ 1.0 register 0x2xx: sub id 0x82/0x83, address 0xC1)
REG_BOLT_UNIQUE_ID = 0x2FB      # Bolt receiver unique id (read)
# HID++ 1.0 notifications: sub id 0x40..0x7F in the feature slot of a report.
N_DEVICE_DISCONNECT, N_DEVICE_CONNECT = 0x40, 0x41
N_PAIRING_LOCK = 0x4A           # Unifying lock status (address bit0 open, data[0] error)
N_PASSKEY_REQUEST = 0x4D        # Bolt: data[0:6] the digits to enter on the device
N_PASSKEY_PRESSED = 0x4E        # Bolt: a key was pressed on the device
N_DEVICE_DISCOVERY = 0x4F       # Bolt: a device advertises itself
N_DISCOVERY_STATUS = 0x53       # Bolt: address 0 discovery running, else closed; data[0] error
N_PAIRING_STATUS = 0x54         # Bolt: address 0 lock open, 2 paired (data[7] slot); data[0] error
F_REPROG_CONTROLS = 0x1B04     # reprogrammable controls v4: divert buttons to software
F_CHANGE_HOST = 0x1814
F_HOSTS_INFO = 0x1815

DEVICE_TYPES = [
    "keyboard", "remote control", "numpad", "mouse", "trackpad", "trackball",
    "presenter", "receiver", "headset", "webcam", "steering wheel", "joystick",
    "gamepad", "dock", "speaker", "microphone", "illumination light",
    "programmable controller", "car sim pedals", "adapter",
]

ERR_HIDPP2 = {
    0: "NO_ERROR", 1: "UNKNOWN", 2: "INVALID_ARGUMENT", 3: "OUT_OF_RANGE",
    4: "HW_ERROR", 5: "LOGITECH_INTERNAL", 6: "INVALID_FEATURE_INDEX",
    7: "INVALID_FUNCTION_ID", 8: "BUSY", 9: "UNSUPPORTED", 10: "UNKNOWN_DEVICE",
}
ERR_HIDPP1 = {
    1: "INVALID_SUBID", 2: "INVALID_ADDRESS", 3: "INVALID_VALUE",
    4: "CONNECT_FAIL", 5: "TOO_MANY_DEVICES", 6: "ALREADY_EXISTS",
    7: "BUSY", 8: "UNKNOWN_DEVICE", 9: "RESOURCE_ERROR",
    10: "REQUEST_UNAVAILABLE", 11: "INVALID_PARAM_VALUE", 12: "WRONG_PIN_CODE",
}


def is_hidpp10_notification(sub_id):
    """A report whose feature slot holds a HID++ 1.0 notification sub id (0x40..0x7F)."""
    return 0x40 <= sub_id < 0x80


class HidppError(Exception):
    """Device returned a HID++ error report."""

    def __init__(self, feature, function, code, version=2):
        table = ERR_HIDPP2 if version == 2 else ERR_HIDPP1
        self.feature = feature
        self.function = function
        self.code = code
        self.version = version
        self.name = table.get(code, "0x%02x" % code)
        super().__init__("HID++%d error %s (feature=0x%02x function=%s)" % (
            1 if version == 1 else 2, self.name, feature, function))


class HidppTimeout(HidppError, TimeoutError):
    def __init__(self, feature=0, function=0):
        Exception.__init__(self, "HID++ timeout (feature=0x%02x function=%s)" % (feature, function))
        self.feature, self.function, self.code, self.version, self.name = feature, function, -1, 2, "TIMEOUT"


class FeatureNotSupported(HidppError):
    def __init__(self, feature_id):
        Exception.__init__(self, "HID++ feature 0x%04x not supported" % feature_id)
        self.feature, self.function, self.code, self.version, self.name = feature_id, None, -2, 2, "NOT_SUPPORTED"


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

@dataclass
class HidppNode:
    path: str          # /dev/hidrawN
    vid: int
    pid: int           # USB PID of the transport (receiver PID for receivers)
    name: str
    is_receiver: bool
    is_bolt: bool = False
    long_only: bool = False   # no short-report collection (Bluetooth direct): use long reports


def _parse_descriptor(desc):
    """Return set of (usage_page, report_id) pairs from a HID report descriptor."""
    out, page, rid, i = set(), 0, 0, 0
    while i < len(desc):
        b = desc[i]
        if b == 0xFE:  # long item
            i += 3 + (desc[i + 1] if i + 1 < len(desc) else 0)
            continue
        size = (0, 1, 2, 4)[b & 3]
        typ, tag = (b >> 2) & 3, b >> 4
        val = int.from_bytes(desc[i + 1:i + 1 + size], "little")
        if typ == 1 and tag == 0:      # global: usage page
            page = val
        elif typ == 1 and tag == 8:    # global: report id
            rid = val
            out.add((page, rid))
        i += 1 + size
    return out


def _read_uevent(path):
    d = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                d[k] = v
    except OSError:
        pass
    return d


def list_receivers_and_devices(base="/sys/class/hidraw"):
    """Scan /sys/class/hidraw for Logitech nodes exposing the HID++ collection.

    A node qualifies when its report descriptor declares report id 0x10 and/or
    0x11 under vendor usage page 0xFF00 or 0xFF43. A node with the long report
    only (a Bluetooth-direct device) is marked `long_only`.
    """
    nodes = []
    try:
        names = sorted(os.listdir(base), key=lambda n: int(n[6:]) if n[6:].isdigit() else 0)
    except OSError:
        return nodes
    for n in names:
        dev = os.path.join(base, n, "device")
        ue = _read_uevent(os.path.join(dev, "uevent"))
        hid_id = ue.get("HID_ID", "")  # bus:vid:pid, all hex
        try:
            _bus, vid, pid = (int(x, 16) for x in hid_id.split(":"))
        except ValueError:
            continue
        if vid != LOGITECH_VID:
            continue
        try:
            with open(os.path.join(dev, "report_descriptor"), "rb") as f:
                pairs = _parse_descriptor(f.read())
        except OSError:
            continue
        if not any(p in (0xFF00, 0xFF43) and r in (SHORT, LONG) for p, r in pairs):
            continue
        has_short = any(p in (0xFF00, 0xFF43) and r == SHORT for p, r in pairs)
        nodes.append(HidppNode(
            path="/dev/" + n, vid=vid, pid=pid, name=ue.get("HID_NAME", ""),
            is_receiver=pid in RECEIVER_PIDS, is_bolt=pid in BOLT_PIDS,
            long_only=not has_short))
    return nodes


# --------------------------------------------------------------------------
# raw I/O
# --------------------------------------------------------------------------

_feat_cache = {}


class HidrawHandle:
    """One open /dev/hidrawN node. The I/O surface hidpp needs from any
    transport handle: ``write``, ``read`` and ``close``."""

    def __init__(self, fd):
        self.fd = fd

    def write(self, data):
        os.write(self.fd, data)

    def read(self, timeout):
        """One report, b"" for a spurious wakeup, None when `timeout` expires."""
        if not select.select([self.fd], [], [], timeout)[0]:
            return None
        try:
            return os.read(self.fd, 64)
        except BlockingIOError:
            return b""
        except OSError as e:
            if e.errno in (errno.EINTR, errno.EAGAIN):
                return b""
            raise

    def close(self):
        os.close(self.fd)


IN_CREATE, IN_DELETE = 0x100, 0x200


def _inotify_fd(path):
    """A non-blocking inotify fd watching `path` for create/delete, or None."""
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            return None
        if libc.inotify_add_watch(fd, os.fsencode(path), IN_CREATE | IN_DELETE) < 0:
            os.close(fd)
            return None
        return fd
    except (OSError, AttributeError):
        return None


class NodeWatcher:
    """Wakes when a hidraw node appears or disappears.

    inotify on /dev when available; otherwise (or when `use_inotify` is off)
    polls the /sys/class/hidraw listing every `poll_seconds`."""

    def __init__(self, dev_dir="/dev", sysfs_dir="/sys/class/hidraw", use_inotify=True,
                 poll_seconds=0.5):
        self.sysfs_dir = sysfs_dir
        self.poll_seconds = poll_seconds
        self._fd = _inotify_fd(dev_dir) if use_inotify else None
        self._seen = self._snapshot()

    def _snapshot(self):
        try:
            return frozenset(n for n in os.listdir(self.sysfs_dir) if n.startswith("hidraw"))
        except OSError:
            return frozenset()

    def _inotify_hit(self, timeout):
        if not select.select([self._fd], [], [], timeout)[0]:
            return False
        try:
            data = os.read(self._fd, 4096)
        except BlockingIOError:
            return False
        off = 0
        hit = False
        while off + 16 <= len(data):
            _wd, _mask, _cookie, nlen = struct.unpack_from("iIII", data, off)
            name = data[off + 16:off + 16 + nlen].rstrip(b"\0")
            hit = hit or name.startswith(b"hidraw")
            off += 16 + nlen
        return hit

    def wait(self, timeout):
        """Block up to `timeout`; True if a hidraw node was added or removed."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            if self._fd is not None:
                if self._inotify_hit(left):
                    self._seen = self._snapshot()
                    return True
                continue
            time.sleep(min(self.poll_seconds, left))
            now = self._snapshot()
            if now != self._seen:
                self._seen = now
                return True

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class Reader:
    """The single reader of one HID++ node.

    A thread owns every read from the transport handle. A report that answers
    a waiting request (same device index, feature index and function/software
    id, or an error report naming them) goes to that request. Any other report
    with software id 0 is an event and goes to the listeners; the rest is
    dropped. Listeners run on their own thread, so they may send requests.
    Concurrent requests are safe: each waits on its own reply.
    """

    POLL = 0.1

    def __init__(self, handle, long_only=False):
        self.handle = handle
        self.long_only = long_only      # transport has no short-report collection
        self.dead = None                # the OSError that stopped the read loop, once it did
        self._swid = 0
        self._lock = threading.Lock()
        self._waiting = []              # [(key, slot)] in request order
        self._listeners = []
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._read_loop, daemon=True, name="hidpp-reader"),
                         threading.Thread(target=self._dispatch_loop, daemon=True, name="hidpp-events")]
        for t in self._threads:
            t.start()

    def next_swid(self):
        """The software id for the next HID++ 2.0 request: 1..15 in turn, so two requests
        for the same feature and function never share a reply key for long, and a reply
        that arrives after its request timed out does not answer a later one."""
        with self._lock:
            self._swid = self._swid % 15 + 1
            return self._swid

    # ---- events ----------------------------------------------------------
    def add_listener(self, fn):
        """fn(device_index, feature_index, function, params) for each event. A HID++ 1.0
        notification (see is_hidpp10_notification) arrives as (index, sub id, address, data)."""
        self._listeners.append(fn)

    def _dispatch_loop(self):
        while True:
            ev = self._events.get()
            if ev is None:
                return
            for fn in list(self._listeners):
                try:
                    fn(*ev)
                except Exception:
                    log.exception("HID++ event listener failed")
            self._events.task_done()        # tests wait on unfinished_tasks to know dispatch is idle

    # ---- reading ---------------------------------------------------------
    def _read_loop(self):
        while not self._stop.is_set():
            try:
                r = self.handle.read(self.POLL)
            except OSError as exc:
                if not self._stop.is_set():
                    log.warning("HID++ read failed, reader stopped: %s", exc)
                    self.dead = exc
                    self._fail_all(exc)
                return
            if r:
                self._route(r)

    def _fail_all(self, exc):
        with self._lock:
            waiting, self._waiting = self._waiting, []
        for _key, slot in waiting:
            slot["error"] = exc
            slot["done"].set()

    def _route(self, r):
        log.debug("rx %s", r.hex())
        if len(r) < 4 or r[0] not in (SHORT, LONG, 0x12):
            return
        idx = r[1]
        if r[2] in (0xFF, 0x8F):
            if len(r) < 6:
                return
            key = (idx, r[3], r[4])
            version = 2 if r[2] == 0xFF else 1
        else:
            key = (idx, r[2], r[3])
            version = None
        with self._lock:
            for i, (k, slot) in enumerate(self._waiting):
                if k == key:
                    del self._waiting[i]
                    break
            else:
                slot = None
        if slot is not None:
            slot["reply"], slot["version"] = r, version
            slot["done"].set()
        elif version is None and is_hidpp10_notification(r[2]):
            # HID++ 1.0 notification: sub id in the feature slot, the whole address byte
            # (not a function nibble) in the function slot
            self._events.put((idx, r[2], r[3], bytes(r[4:])))
        elif version is None and r[3] & 0x0F == 0:
            self._events.put((idx, r[2], r[3] >> 4, bytes(r[4:])))

    # ---- requests --------------------------------------------------------
    def request(self, msg, timeout):
        """Write one report; return the reply report or raise HidppError."""
        idx, b2, b3 = msg[1], msg[2], msg[3]
        fn = b3 >> 4
        key = (idx, b2, b3)
        slot = {"done": threading.Event()}
        with self._lock:
            if self.dead is not None:
                raise self.dead
            self._waiting.append((key, slot))
        try:
            log.debug("tx %s", msg.hex())
            self.handle.write(msg)
            if not slot["done"].wait(timeout):
                raise HidppTimeout(b2, fn)
        finally:
            with self._lock:
                try:
                    self._waiting.remove((key, slot))
                except ValueError:
                    pass
        if "error" in slot:
            raise slot["error"]
        r = slot["reply"]
        if slot["version"] == 2:
            raise HidppError(b2, fn, r[5], 2)
        if slot["version"] == 1:
            raise HidppError(b2, None, r[5], 1)
        return r

    def close(self):
        self._stop.set()
        self._events.put(None)
        if self.dead is None:
            self.dead = OSError(errno.EBADF, "HID++ reader closed")
        self._fail_all(self.dead)
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(2)
        self.handle.close()


class HidrawTransport:
    """The production transport: discovery through sysfs, I/O through hidraw."""

    def __init__(self):
        self._watcher = None

    def list_nodes(self):
        return list_receivers_and_devices()

    def wait_change(self, timeout):
        """Block until a hidraw node is added or removed; True if one was."""
        if self._watcher is None:
            self._watcher = NodeWatcher()
        return self._watcher.wait(timeout)

    def open(self, path):
        return HidrawHandle(os.open(path, os.O_RDWR | os.O_NONBLOCK))


HIDRAW = HidrawTransport()


def open_node(path, long_only=False):
    return Reader(HIDRAW.open(path), long_only=long_only)


def close_node(fd):
    for k in [k for k in _feat_cache if k[0] is fd]:
        del _feat_cache[k]
    fd.close()


@contextmanager
def open_device(path):
    fd = open_node(path)
    try:
        yield fd
    finally:
        close_node(fd)


def _transact(fd, idx, b2, b3, params=b"", long=None, timeout=DEFAULT_TIMEOUT):
    """Write one report, return the matching reply report (full bytes)."""
    params = bytes(params)
    if getattr(fd, "long_only", False):
        long = True
    elif long is None:
        long = len(params) > SHORT_LEN - 4
    size = LONG_LEN if long else SHORT_LEN
    if len(params) > size - 4:
        raise ValueError("too many parameters")
    msg = bytes([LONG if long else SHORT, idx, b2, b3]) + params
    return fd.request(msg.ljust(size, b"\0"), timeout)


def call(fd, dev_index, feature_index, funcid, params=b"", long=None, timeout=DEFAULT_TIMEOUT):
    """HID++ 2.0 request. Returns reply params (bytes after the 4-byte header)."""
    swid = fd.next_swid() if hasattr(fd, "next_swid") else SW_ID
    r = _transact(fd, dev_index, feature_index, ((funcid & 0xF) << 4) | swid,
                  params, long, timeout)
    return bytes(r[4:])


# --------------------------------------------------------------------------
# Root (0x0000)
# --------------------------------------------------------------------------

def root_get_feature(fd, dev_index, feature_id, timeout=DEFAULT_TIMEOUT):
    """Root fn0 GetFeature(featureId) -> (index, type, version), or 0 if unsupported."""
    p = call(fd, dev_index, 0, 0, struct.pack(">H", feature_id), timeout=timeout)
    if p[0] == 0 and feature_id != F_ROOT:
        return 0
    return p[0], p[1], p[2]


def root_ping(fd, dev_index, timeout=DEFAULT_TIMEOUT):
    """Root fn1 Ping: echoes marker, replies with protocol (major, minor).

    HID++ 1.0 devices answer with a 1.0 INVALID_SUBID error; reported as (1, 0).
    """
    try:
        p = call(fd, dev_index, 0, 1, bytes([0, 0, PING_MARKER]), timeout=timeout)
    except HidppError as e:
        if e.version == 1 and e.code == 1:
            return (1, 0)
        raise
    if p[2] != PING_MARKER:
        raise HidppError(0, 1, -3)
    return (p[0], p[1])


def feature_index(fd, dev_index, feature_id):
    """Cached feature index; raises FeatureNotSupported."""
    key = (fd, dev_index, feature_id)
    if key not in _feat_cache:
        r = root_get_feature(fd, dev_index, feature_id)
        if not r:
            raise FeatureNotSupported(feature_id)
        _feat_cache[key] = r[0]
    return _feat_cache[key]


def has_feature(fd, dev_index, feature_id):
    try:
        feature_index(fd, dev_index, feature_id)
        return True
    except FeatureNotSupported:
        return False


def _fcall(fd, idx, feature_id, fn, params=b"", **kw):
    return call(fd, idx, feature_index(fd, idx, feature_id), fn, params, **kw)


# --------------------------------------------------------------------------
# FeatureSet (0x0001), DeviceFwInfo (0x0003), DeviceNameAndType (0x0005)
# --------------------------------------------------------------------------

def enumerate_features(fd, idx):
    """FeatureSet fn0 getCount, fn1 getFeatureID(i). -> {feature_index: feature_id}.

    Count excludes Root, which is always index 0.
    """
    count = _fcall(fd, idx, F_FEATURE_SET, 0)[0]
    out = {0: F_ROOT}
    for i in range(1, count + 1):
        p = _fcall(fd, idx, F_FEATURE_SET, 1, bytes([i]))
        out[i] = struct.unpack(">H", p[:2])[0]
    return out


def get_fw_info(fd, idx):
    """DeviceFwInfo fn0 getEntityCount, fn1 getFwInfo(entity)
    -> list of {type, name, version, build}."""
    p = _fcall(fd, idx, F_DEVICE_FW_INFO, 0)
    out = []
    for e in range(p[0]):
        r = _fcall(fd, idx, F_DEVICE_FW_INFO, 1, bytes([e]))
        out.append({
            "type": r[0] & 0x0F,
            "name": r[1:4].decode("ascii", "replace").strip("\0"),
            "version": "%02x.%02x" % (r[4], r[5]),
            "build": struct.unpack(">H", r[6:8])[0],
        })
    return out


def get_device_name(fd, idx):
    """DeviceNameAndType fn0 getDeviceNameCount, fn1 getDeviceName(charIndex)."""
    n = _fcall(fd, idx, F_DEVICE_NAME, 0)[0]
    buf = b""
    while len(buf) < n:
        chunk = _fcall(fd, idx, F_DEVICE_NAME, 1, bytes([len(buf)]))
        chunk = chunk[:n - len(buf)].split(b"\0")[0]
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", "replace")


def get_device_type(fd, idx):
    """DeviceNameAndType fn2 getDeviceType -> name string (e.g. 'mouse')."""
    t = _fcall(fd, idx, F_DEVICE_NAME, 2)[0]
    return DEVICE_TYPES[t] if t < len(DEVICE_TYPES) else "unknown(%d)" % t


# --------------------------------------------------------------------------
# Battery
# --------------------------------------------------------------------------

_UB_LEVEL_PCT = {1: 5, 2: 20, 4: 50, 8: 90}   # critical/low/good/full approximations
_UB_STATUS = {0: "discharging", 1: "charging", 2: "charging slow",
              3: "charge complete", 4: "error"}
_B1000_STATUS = {0: "discharging", 1: "recharging", 2: "charge in final stage",
                 3: "charge complete", 4: "recharging below optimal speed",
                 5: "invalid battery", 6: "thermal error", 7: "other charging error"}


_UB_LEVEL_NAMES = ((1, "critical"), (2, "low"), (4, "good"), (8, "full"))


def get_battery_caps(fd, idx):
    """0x1004 fn0 -> {levels, rechargeable, soc}; None when the device lacks 0x1004.

    levels: names of the supported level bits; rechargeable: flags bit0;
    soc: flags bit1 (device reports a state-of-charge percentage).
    """
    if not has_feature(fd, idx, F_UNIFIED_BATTERY):
        return None
    caps = _fcall(fd, idx, F_UNIFIED_BATTERY, 0)
    return {"levels": [n for b, n in _UB_LEVEL_NAMES if caps[0] & b],
            "rechargeable": bool(caps[1] & 0x01), "soc": bool(caps[1] & 0x02)}


def get_battery(fd, idx):
    """-> {percent, charging, next_level, status}.

    0x1004 UnifiedBattery: fn0 getCapabilities (byte0 supported-levels flags,
    byte1 flags: bit1 = state-of-charge %), fn1 getStatus (byte0 SoC %, byte1
    level bitmask crit/low/good/full, byte2 charging status, byte3 ext power).
    0x1000 BatteryLevelStatus: fn0 -> (discharge level %, next level %, status).
    """
    if has_feature(fd, idx, F_UNIFIED_BATTERY):
        caps = _fcall(fd, idx, F_UNIFIED_BATTERY, 0)
        st = _fcall(fd, idx, F_UNIFIED_BATTERY, 1)
        soc, level, chg = st[0], st[1], st[2]
        if caps[1] & 0x02:
            pct = soc
        else:
            pct = next((v for b, v in sorted(_UB_LEVEL_PCT.items(), reverse=True) if level & b), None)
        return {"percent": pct, "charging": chg in (1, 2), "next_level": None,
                "status": _UB_STATUS.get(chg, "unknown(%d)" % chg)}
    if has_feature(fd, idx, F_BATTERY_STATUS):
        p = _fcall(fd, idx, F_BATTERY_STATUS, 0)
        return {"percent": p[0], "charging": p[2] in (1, 2, 4), "next_level": p[1],
                "status": _B1000_STATUS.get(p[2], "unknown(%d)" % p[2])}
    raise FeatureNotSupported(F_UNIFIED_BATTERY)


# --------------------------------------------------------------------------
# AdjustableDPI (0x2201)
# --------------------------------------------------------------------------

def get_dpi_list(fd, idx, sensor=0):
    """fn1 getSensorDpiList: words after the sensor byte, 0-terminated; a word
    >= 0xE000 is a step (low 13 bits) for the range prev..next.
    -> {values: [...], min, max, step}."""
    p = _fcall(fd, idx, F_ADJUSTABLE_DPI, 1, bytes([sensor]))
    words = [w for w in struct.unpack(">7H", p[1:15])]
    words = words[:words.index(0)] if 0 in words else words
    for i, w in enumerate(words):
        if w >= 0xE000 and 0 < i < len(words) - 1:
            lo, hi, step = words[i - 1], words[i + 1], w & 0x1FFF
            return {"values": list(range(lo, hi + 1, step)), "min": lo, "max": hi, "step": step}
    return {"values": words, "min": min(words, default=0), "max": max(words, default=0), "step": None}


def get_dpi(fd, idx, sensor=0):
    """fn0 getSensorCount, fn2 getSensorDpi -> {dpi, default, sensor, sensor_count}."""
    count = _fcall(fd, idx, F_ADJUSTABLE_DPI, 0)[0]
    p = _fcall(fd, idx, F_ADJUSTABLE_DPI, 2, bytes([sensor]))
    cur, dflt = struct.unpack(">HH", p[1:5])
    return {"dpi": cur, "default": dflt, "sensor": p[0], "sensor_count": count}


def set_dpi(fd, idx, dpi, sensor=0):
    """fn3 setSensorDpi(sensor, dpi) -> dpi actually applied."""
    p = _fcall(fd, idx, F_ADJUSTABLE_DPI, 3, bytes([sensor]) + struct.pack(">H", dpi))
    return struct.unpack(">H", p[1:3])[0]


# --------------------------------------------------------------------------
# SmartShift (0x2110) / enhanced (0x2111)
# --------------------------------------------------------------------------

def get_smartshift(fd, idx):
    """0x2110 fn0 / 0x2111 fn1 getRatchetControlMode
    -> {mode: 'freespin'|'ratchet', threshold, torque, enhanced}.

    threshold = autoDisengage (1..254 scroll speed, 255 = always ratchet,
    0 = never auto-disengage).  torque only on 0x2111.
    """
    if has_feature(fd, idx, F_SMART_SHIFT_ENH):
        p = _fcall(fd, idx, F_SMART_SHIFT_ENH, 1)
        enh, torque = True, p[2]
    else:
        p = _fcall(fd, idx, F_SMART_SHIFT, 0)
        enh, torque = False, None
    return {"mode": "freespin" if p[0] == 1 else "ratchet", "threshold": p[1],
            "torque": torque, "enhanced": enh}


def set_smartshift(fd, idx, mode=None, threshold=None, torque=None):
    """0x2110 fn1 / 0x2111 fn2 setRatchetControlMode; 0 bytes mean 'unchanged'."""
    m = {None: 0, "freespin": 1, "ratchet": 2}[mode]
    params = bytes([m, threshold or 0, torque or 0])
    if has_feature(fd, idx, F_SMART_SHIFT_ENH):
        _fcall(fd, idx, F_SMART_SHIFT_ENH, 2, params)
    else:
        _fcall(fd, idx, F_SMART_SHIFT, 1, params)
    return get_smartshift(fd, idx)


# --------------------------------------------------------------------------
# High resolution wheel (0x2121)
# --------------------------------------------------------------------------

WHEEL_MODE_TARGET = 0x01        # 1: wheel events go to HID++ (diverted), 0: HID
WHEEL_MODE_HIRES = 0x02
WHEEL_MODE_INVERT = 0x04
WHEEL_CAP_INVERT = 0x08


def get_wheel(fd, idx):
    """fn0 getWheelCapability, fn1 getWheelMode
    -> {multiplier, can_invert, target, hires, invert, mode}."""
    cap = _fcall(fd, idx, F_HIRES_WHEEL, 0)
    mode = _fcall(fd, idx, F_HIRES_WHEEL, 1)[0]
    return {"multiplier": cap[0], "can_invert": bool(cap[1] & WHEEL_CAP_INVERT), "mode": mode,
            "target": bool(mode & WHEEL_MODE_TARGET), "hires": bool(mode & WHEEL_MODE_HIRES),
            "invert": bool(mode & WHEEL_MODE_INVERT)}


def set_wheel(fd, idx, hires=None, invert=None, target=None):
    """fn2 setWheelMode. Starts from the mode the device reports, so bits the
    kernel driver set (including the diversion target) survive unless asked
    for. `target` diverts the wheel to software (True) or hands it back.
    Returns the state read back."""
    mode = get_wheel(fd, idx)["mode"]
    for flag, want in ((WHEEL_MODE_TARGET, target), (WHEEL_MODE_HIRES, hires), (WHEEL_MODE_INVERT, invert)):
        if want is not None:
            mode = (mode | flag) if want else (mode & ~flag)
    _fcall(fd, idx, F_HIRES_WHEEL, 2, bytes([mode]))
    return get_wheel(fd, idx)


def parse_wheel_event(params):
    """0x2121 event 0 (wheel movement) -> (hires, delta). The delta is signed, positive scrolling
    up; in 1/multiplier notches when hires, else in whole notches. None when too short."""
    if len(params) < 3:
        return None
    return bool(params[0] & 0x10), struct.unpack(">h", bytes(params[1:3]))[0]


# ThumbWheel (0x2150)
# --------------------------------------------------------------------------

THUMB_NATIVE, THUMB_DIVERTED = 0, 1     # reporting mode byte: HID native / sent to software


def get_thumbwheel_info(fd, idx):
    """fn0 getThumbwheelInfo -> {native, diverted}: rotation units per notch-equivalent in each mode.
    The bytes after the two resolutions (the default direction among them) are not read: a positive
    rotation is taken as "right" whatever the wheel's own default direction is; unconfirmed on hardware."""
    p = _fcall(fd, idx, F_THUMB_WHEEL, 0)
    native, diverted = struct.unpack(">HH", bytes(p[:4]))
    return {"native": native, "diverted": diverted}


def get_thumbwheel(fd, idx):
    """fn1 getThumbwheelStatus -> {mode, invert, smooth}.

    mode is 0 (native HID) or 1 (diverted), invert is the direction flag.
    The smooth byte follows invert; not confirmed on hardware, see
    feature-parity.md section 4."""
    p = _fcall(fd, idx, F_THUMB_WHEEL, 1)
    return {"mode": p[0], "invert": bool(p[1] & 1), "smooth": bool(p[2] & 1)}


def set_thumbwheel(fd, idx, invert, smooth, mode=THUMB_NATIVE):
    """fn2 setThumbwheelReporting(mode, invert, smooth) -> status read back."""
    _fcall(fd, idx, F_THUMB_WHEEL, 2, bytes([mode, 1 if invert else 0, 1 if smooth else 0]))
    return get_thumbwheel(fd, idx)


def parse_thumb_event(params):
    """0x2150 event 0 (thumb wheel movement) -> the signed rotation, positive to the right;
    None when too short."""
    if len(params) < 2:
        return None
    return struct.unpack(">h", bytes(params[:2]))[0]


# --------------------------------------------------------------------------
# Reprogrammable controls v4 (0x1b04)
# --------------------------------------------------------------------------

CTRL_REPROGRAMMABLE, CTRL_DIVERTABLE = 0x10, 0x20      # control info flags1
CTRL_RAW_XY = 0x01              # control info flags2 (byte 8): can report raw XY while held
REPORT_DIVERT = 0x01            # setCidReporting flags: the divert value; the change bit is value << 1
REPORT_PERSISTENT = 0x04        # same byte: divert that survives a power cycle; its change bit is 0x08
REPORT_RAW_XY = 0x10            # same byte: divert raw XY motion; the change bit is value << 1


def get_controls(fd, idx):
    """fn0 getControlCount, fn1 getControlInfo(i) -> {cid: {"reprogrammable", "divertable", "raw_xy",
    "group", "group_mask"}}. The group mask says which control groups the control may be remapped to."""
    count = _fcall(fd, idx, F_REPROG_CONTROLS, 0)[0]
    out = {}
    for i in range(count):
        p = _fcall(fd, idx, F_REPROG_CONTROLS, 1, bytes([i]))
        cid, flags = struct.unpack(">H", p[:2])[0], p[4]
        out[cid] = {"reprogrammable": bool(flags & CTRL_REPROGRAMMABLE),
                    "divertable": bool(flags & CTRL_DIVERTABLE),
                    "raw_xy": bool(len(p) > 8 and p[8] & CTRL_RAW_XY),
                    "group": p[6] if len(p) > 6 else 0, "group_mask": p[7] if len(p) > 7 else 0}
    return out


def can_remap(controls, source, target):
    """True when control `source` may be remapped to control `target`: it is reprogrammable
    and `target`'s group is one its group mask allows."""
    src, dst = controls.get(source), controls.get(target)
    if not src or not dst or not src["reprogrammable"] or not dst["group"]:
        return False
    return bool(src["group_mask"] & (1 << (dst["group"] - 1)))


def _cid_reporting(fd, idx, cid):
    """fn2 getCidReporting(cid) -> (flags byte, the control id it acts as). The reply names the control
    it describes in bytes 0-1; one for another control (a reply that crossed a concurrent request)
    raises HidppError rather than being taken for this control's."""
    p = _fcall(fd, idx, F_REPROG_CONTROLS, 2, struct.pack(">H", cid))
    if struct.unpack(">H", bytes(p[:2]))[0] != cid:
        raise HidppError(F_REPROG_CONTROLS, 2, 4)       # HW_ERROR: not the answer to this request
    return p[2], (struct.unpack(">H", p[3:5])[0] or cid)


def get_cid_reporting(fd, idx, cid):
    """fn2 getCidReporting(cid) -> (diverted to software, raw XY diverted, the control id it acts as).
    A control counts as diverted when either the temporary (0x01) or the persistent (0x04) bit is set."""
    flags, remap = _cid_reporting(fd, idx, cid)
    return bool(flags & (REPORT_DIVERT | REPORT_PERSISTENT)), bool(flags & REPORT_RAW_XY), remap


def get_cid_remap(fd, idx, cid):
    """The control id `cid` acts as: itself unless remapped."""
    return get_cid_reporting(fd, idx, cid)[2]


def set_cid_remap(fd, idx, cid, target):
    """fn3 setCidReporting(cid, 0, target): make `cid` act as `target` (itself undoes it). The
    divert flags carry no change bits, so the diversion is left alone. The device is read
    back; raises HidppError when it did not take."""
    _fcall(fd, idx, F_REPROG_CONTROLS, 3, struct.pack(">HBH", cid, 0, target))
    if get_cid_remap(fd, idx, cid) != target:
        raise HidppError(F_REPROG_CONTROLS, 3, 4)       # HW_ERROR: the device ignored the request


def set_cid_diverted(fd, idx, cid, divert, raw_xy=None):
    """fn3 setCidReporting(cid, flags, remap): divert the control or hand it back; `raw_xy` True or
    False also diverts or releases its raw motion, None leaves that alone. The device state is read
    back; raises HidppError when it did not take."""
    current, _ = _cid_reporting(fd, idx, cid)
    flags = (REPORT_DIVERT << 1) | (REPORT_DIVERT if divert else 0)
    if not divert and current & REPORT_PERSISTENT:
        flags |= REPORT_PERSISTENT << 1         # a stale persistent divert: clear it too (value bit 0)
    if raw_xy is not None:
        flags |= (REPORT_RAW_XY << 1) | (REPORT_RAW_XY if raw_xy else 0)
    # target 0 means "keep the current mapping" (a swap survives a divert change)
    _fcall(fd, idx, F_REPROG_CONTROLS, 3, struct.pack(">HBH", cid, flags, 0))
    got_divert, got_raw, _ = get_cid_reporting(fd, idx, cid)
    if got_divert != bool(divert) or (raw_xy is not None and got_raw != bool(raw_xy)):
        raise HidppError(F_REPROG_CONTROLS, 3, 4)       # HW_ERROR: the device ignored the request


def parse_raw_xy(params):
    """Event 1 params (divertedRawXYEvent) -> (dx, dy), signed big-endian words."""
    return struct.unpack(">hh", bytes(params[:4]).ljust(4, b"\0"))


def parse_diverted_buttons(params):
    """Event 0 params -> the control ids held down (up to four, big-endian words, 0 ends the list)."""
    out = []
    for i in range(0, min(len(params), 8) - 1, 2):
        cid = (params[i] << 8) | params[i + 1]
        if not cid:
            break
        out.append(cid)
    return out


# --------------------------------------------------------------------------
# Easy-Switch: ChangeHost (0x1814) and HostsInfo (0x1815)
# --------------------------------------------------------------------------

NAME_CHUNK = 14             # name bytes in one getHostFriendlyName reply / setHostFriendlyName request
HOST_NAME_CAN_GET, HOST_NAME_CAN_SET = 0x01, 0x02      # 0x1815 fn0 capability flags
HOST_OS_TIMEOUT = 0.5       # getHostOsVersion: a mouse without it should answer with an error, not stall
HOST_SWITCH_TIMEOUT = 0.5   # a mouse that switches away does not answer; wait this long for an error


def get_current_host(fd, idx):
    """0x1814 fn0 getHostInfo -> (host count, current host index, 0-based)."""
    p = _fcall(fd, idx, F_CHANGE_HOST, 0)
    return p[0], p[1]


def set_current_host(fd, idx, host):
    """0x1814 fn1 setCurrentHost. The mouse moves to the other computer and usually sends no
    reply, so a timeout is the normal outcome; only a HID++ error reply raises."""
    try:
        _fcall(fd, idx, F_CHANGE_HOST, 1, bytes([host]), timeout=HOST_SWITCH_TIMEOUT)
    except HidppTimeout:
        pass


def get_host_name(fd, idx, host, length):
    """0x1815 fn3 getHostFriendlyName(host, offset) -> reply [host, offset, 14 name bytes];
    reads `length` bytes (the name length fn1 reports) in chunks. -> name."""
    raw = b""
    while len(raw) < length:
        p = _fcall(fd, idx, F_HOSTS_INFO, 3, bytes([host, len(raw)]))
        chunk = bytes(p[2:2 + NAME_CHUNK])[:length - len(raw)]
        end = chunk.find(b"\0")
        raw += chunk if end < 0 else chunk[:end]
        if end >= 0 or not chunk:
            break
    return raw.decode("utf-8", "replace")


def get_host_os(fd, idx, host):
    """0x1815 fn7 getHostOsVersion(host) -> reply [host, os type, major, minor, patch];
    -> the OS type, 0 (unknown) on an error reply or a reply for another host, None on a
    timeout (the caller stops asking: every further host would wait as long)."""
    try:
        p = _fcall(fd, idx, F_HOSTS_INFO, 7, bytes([host]), timeout=HOST_OS_TIMEOUT)
    except HidppTimeout:
        return None
    except HidppError:
        return 0
    return p[1] if len(p) >= 2 and p[0] == host else 0


def get_hosts(fd, idx):
    """0x1814 + 0x1815 -> {"current", "max_name", "can_get_name", "can_set_name",
    "hosts": [{index, paired, bus, os, name, max_name}]}.

    0x1815 fn0 [flags, _, host count, current host], fn1 getHostInfo(host)
    [host, status, bus type, pages, name length, max name length], fn3 getHostFriendlyName,
    fn7 getHostOsVersion(host) [host, os type, major, minor, patch], read for paired hosts only.
    The other 0x1815 functions are fn2 getHostDescriptor, fn4 setHostFriendlyName, fn5 moveHost,
    fn6 deleteHost and fn8 setHostOsVersion; this reader never sends fn5, fn6 or fn8 (writes).
    Sources: the Logitech agent log string Feature1815HostsInfos::GetHostOSVersion and the
    OpenLogi function table (AprilNEA/OpenLogi PR 1824). A host whose OS cannot be read, or whose
    reply names another host, gets type 0, unknown."""
    count, current = get_current_host(fd, idx)
    flags = _fcall(fd, idx, F_HOSTS_INFO, 0)[0]
    can_get, can_set = bool(flags & HOST_NAME_CAN_GET), bool(flags & HOST_NAME_CAN_SET)
    hosts = []
    read_os = True
    for i in range(count):
        d = _fcall(fd, idx, F_HOSTS_INFO, 1, bytes([i]))
        paired = bool(d[1])
        name = get_host_name(fd, idx, i, d[4]) if paired and can_get and d[4] else ""
        # fn7 is not gated on the feature version: a mouse without it answers with an error,
        # which reads as unknown, and a silent one costs one timeout (read_os then goes off)
        os_type = 0
        if paired and read_os:
            os_type = get_host_os(fd, idx, i)
            if os_type is None:
                read_os, os_type = False, 0
        hosts.append({"index": i, "paired": paired, "bus": d[2], "os": os_type, "name": name,
                      "max_name": d[5] or 32})
    return {"current": current, "max_name": max((h["max_name"] for h in hosts), default=32),
            "can_get_name": can_get, "can_set_name": can_set, "hosts": hosts}


def set_host_name(fd, idx, host, name):
    """0x1815 fn4 setHostFriendlyName(host, offset, up to 14 name bytes), in chunks. No
    terminator is written; the caller keeps the name within the slot's maximum length."""
    raw = name.encode("utf-8")
    for pos in range(0, len(raw), NAME_CHUNK):
        _fcall(fd, idx, F_HOSTS_INFO, 4, bytes([host, pos]) + raw[pos:pos + NAME_CHUNK], long=True)


# --------------------------------------------------------------------------
# Receiver / paired devices
# --------------------------------------------------------------------------

def read_register(fd, idx, address, params=b"", timeout=DEFAULT_TIMEOUT):
    """HID++ 1.0 GET_LONG_REGISTER (sub id 0x83) -> reply data after header."""
    return bytes(_transact(fd, idx, 0x83, address, params, False, timeout)[4:])


def _pairing_wpid(fd, n, bolt):
    """Wireless PID via receiver register 0xB5 (pairing info): param 0x20+n-1
    on Unifying, 0x50+n on Bolt. Best effort - see module notes."""
    try:
        d = read_register(fd, DEV_RECEIVER, 0xB5, bytes([(0x50 + n) if bolt else (0x20 + n - 1)]))
    except HidppError:
        return None
    if len(d) < 5:
        return None
    # Unifying: big-endian at [3:5]. Bolt: [0] echo, [1] kind, [2:4] wpid
    # little-endian, [4:8] unit id (MX Master 3S: 51 42 34b0 43ccd316 -> b034)
    return (d[3] << 8 | d[2]) if bolt else struct.unpack(">H", d[3:5])[0]


def write_register(fd, idx, address, params=b"", timeout=DEFAULT_TIMEOUT):
    """HID++ 1.0 SET_REGISTER (sub id 0x80); raises HidppError if the receiver rejects it."""
    return bytes(_transact(fd, idx, 0x80, address, params, False, timeout)[4:])


def unifying_lock(fd, open_, timeout=60):
    """Unifying receiver register 0xB2: [1 open | 2 close, device (0 = any), timeout seconds].
    Raises HidppError/HidppTimeout when the receiver does not accept the write."""
    if open_ and not 0 < timeout <= 255:
        raise ValueError("pairing timeout must be 1..255 seconds")
    write_register(fd, DEV_RECEIVER, REG_RECEIVER_PAIRING,
                   bytes([1, 0, timeout]) if open_ else bytes([2, 0, 0]))


def write_long_register(fd, idx, address, params=b"", timeout=DEFAULT_TIMEOUT):
    """HID++ 1.0 SET_LONG_REGISTER (sub id 0x82), sent as a long report. Registers
    0x2xx (Solaar's numbering) have sub id 0x82/0x83 and the low byte as the address."""
    return bytes(_transact(fd, idx, 0x82, address, params, True, timeout)[4:])


BOLT_DISCOVER_START, BOLT_DISCOVER_CANCEL = 1, 2
BOLT_PAIR, BOLT_PAIR_CANCEL, BOLT_UNPAIR = 1, 2, 3


def bolt_discover(fd, timeout=30, cancel=False):
    """Bolt register 0xC0 (short): [timeout seconds, 1 start | 2 cancel] device discovery.
    Layout from Solaar's BoltReceiver.discover; the receiver then sends 0x53 and 0x4F."""
    if not 0 < timeout <= 255:
        raise ValueError("discovery timeout must be 1..255 seconds")
    write_register(fd, DEV_RECEIVER, REG_BOLT_DISCOVERY & 0xFF,
                   bytes([timeout, BOLT_DISCOVER_CANCEL if cancel else BOLT_DISCOVER_START]))


def bolt_pair(fd, address, authentication=0, entropy=20, slot=0):
    """Bolt register 0x2C1 (long): [1 pair, slot, device address (6), authentication,
    entropy]. Layout from Solaar's BoltReceiver.pair_device; progress arrives as 0x54/0x4D."""
    address = bytes(address)
    if len(address) != 6:
        raise ValueError("a Bolt device address has 6 bytes")
    write_long_register(fd, DEV_RECEIVER, REG_BOLT_PAIRING & 0xFF,
                        bytes([BOLT_PAIR, slot]) + address + bytes([authentication, entropy]))


def bolt_pair_cancel(fd):
    """Bolt register 0x2C1: action 2 stops a pairing in progress (Solaar's pair=None)."""
    write_long_register(fd, DEV_RECEIVER, REG_BOLT_PAIRING & 0xFF, bytes([BOLT_PAIR_CANCEL]))


def bolt_unique_id(fd):
    """Bolt register 0x2FB (read, long): the receiver's unique id, or b"" when it has none."""
    try:
        return read_register(fd, DEV_RECEIVER, REG_BOLT_UNIQUE_ID & 0xFF)[:4]
    except HidppError:
        return b""


BOLT_ERROR_TIMEOUT, BOLT_ERROR_FAILED = 1, 2


def parse_device_discovery(address, data):
    """0x4F notification -> (counter, part, fields): part 0 {kind, address, authentication},
    part 1 {name}; None when the report is too short. Layout from Solaar."""
    data = bytes(data).ljust(16, b"\0")
    counter, part = address + data[0] * 256, data[1]
    if part == 0:
        return counter, 0, {"kind": data[3], "address": data[6:12], "authentication": data[14]}
    if part == 1:
        return counter, 1, {"name": data[3:3 + min(data[2], 13)].decode("utf-8", "replace")}
    return None


def _device_gone(err):
    """True when `err` says nothing answers at the device index any more."""
    return isinstance(err, HidppTimeout) or (err.version == 1 and err.name == "UNKNOWN_DEVICE")


def _once_more_on_timeout(fn, *args):
    """fn(*args); one HidppTimeout is retried once, since a busy reader makes a live device
    miss a single answer."""
    try:
        return fn(*args)
    except HidppTimeout:
        return fn(*args)


def get_paired_devices(fd, bolt=None, max_devices=6, ping_timeout=1.0):
    """Ping device indices 1..max_devices; return those that answer.

    -> [{index, protocol: (maj, min), wpid, name, type}]  (wpid/name/type may be None)
    """
    out = []
    for i in range(1, max_devices + 1):
        try:
            proto = root_ping(fd, i, timeout=ping_timeout)
        except HidppError:
            continue
        d = {"index": i, "protocol": proto, "wpid": None, "name": None, "type": None}
        if bolt is not None:
            d["wpid"] = _pairing_wpid(fd, i, bolt)
        gone = False
        if proto[0] >= 2:
            for key, fn in (("name", get_device_name), ("type", get_device_type)):
                try:
                    d[key] = _once_more_on_timeout(fn, fd, i)
                except HidppError as e:
                    log.debug("%s idx %d: %s", key, i, e)
                    if _device_gone(e):
                        gone = True
                        break
        if gone:
            continue        # it answered the ping, then switched off: not a device with a blank name
        out.append(d)
    return out


# --------------------------------------------------------------------------
# self-probe
# --------------------------------------------------------------------------

def _probe_device(fd, idx):
    print("    protocol %d.%d" % root_ping(fd, idx))
    for label, fn in (("name", get_device_name), ("type", get_device_type),
                      ("battery", get_battery), ("dpi", get_dpi),
                      ("smartshift", get_smartshift)):
        try:
            print("    %-10s %s" % (label, fn(fd, idx)))
        except HidppError as e:
            print("    %-10s <%s>" % (label, e))


def _main():
    logging.basicConfig(level=logging.INFO)
    nodes = list_receivers_and_devices()
    if not nodes:
        print("no HID++ hidraw nodes found")
    for node in nodes:
        print("%s  %04x:%04x  %s  receiver=%s" % (node.path, node.vid, node.pid, node.name, node.is_receiver))
        try:
            fd = open_node(node.path, long_only=node.long_only)
        except OSError as e:
            print("  cannot open: %s" % e)
            continue
        try:
            if node.is_receiver:
                for d in get_paired_devices(fd, bolt=node.is_bolt):
                    print("  device idx %(index)d wpid=%(wpid)s name=%(name)r type=%(type)s" % {
                        **d, "wpid": "%04x" % d["wpid"] if d["wpid"] else None})
                    _probe_device(fd, d["index"])
            else:
                try:
                    _probe_device(fd, DEV_RECEIVER)
                except HidppError as e:
                    print("  no answer: %s" % e)
        finally:
            close_node(fd)


if __name__ == "__main__":
    _main()
