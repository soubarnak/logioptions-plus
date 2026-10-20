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
byte5 = code.  Notifications (unsolicited) never carry our software id and are
dropped while waiting for a reply.

All functions block (select timeouts); call them from a thread executor.
"""

import errno
import logging
import os
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


def list_receivers_and_devices():
    """Scan /sys/class/hidraw for Logitech nodes exposing the HID++ collection.

    A node qualifies when its report descriptor declares report id 0x10 and/or
    0x11 under vendor usage page 0xFF00 or 0xFF43.
    """
    nodes = []
    base = "/sys/class/hidraw"
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
        nodes.append(HidppNode(
            path="/dev/" + n, vid=vid, pid=pid, name=ue.get("HID_NAME", ""),
            is_receiver=pid in RECEIVER_PIDS, is_bolt=pid in BOLT_PIDS))
    return nodes


# --------------------------------------------------------------------------
# raw I/O
# --------------------------------------------------------------------------

_lock = threading.RLock()
_feat_cache = {}


class HidrawHandle:
    """One open /dev/hidrawN node. The I/O surface hidpp needs from any
    transport handle: ``write``, ``read``, ``drain`` and ``close``."""

    def __init__(self, fd):
        self.fd = fd

    def write(self, data):
        os.write(self.fd, data)

    def drain(self):
        """Drop stale input so old replies/notifications cannot match."""
        while select.select([self.fd], [], [], 0)[0]:
            try:
                if not os.read(self.fd, 64):
                    break
            except BlockingIOError:
                break

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


def open_node(path):
    return HIDRAW.open(path)


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
    if long is None:
        long = len(params) > SHORT_LEN - 4
    size = LONG_LEN if long else SHORT_LEN
    if len(params) > size - 4:
        raise ValueError("too many parameters")
    msg = bytes([LONG if long else SHORT, idx, b2, b3]) + params
    msg = msg.ljust(size, b"\0")
    fn = b3 >> 4
    with _lock:
        fd.drain()
        log.debug("tx %s", msg.hex())
        fd.write(msg)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise HidppTimeout(b2, fn)
            r = fd.read(left)
            if r is None:
                raise HidppTimeout(b2, fn)
            log.debug("rx %s", r.hex())
            if len(r) < 4 or r[0] not in (SHORT, LONG, 0x12) or r[1] != idx:
                continue
            if r[2] in (0xFF, 0x8F) and len(r) >= 6 and r[3] == b2 and r[4] == b3:
                if r[2] == 0xFF:
                    raise HidppError(b2, fn, r[5], 2)
                raise HidppError(b2, None, r[5], 1)
            if r[2] == b2 and r[3] == b3:
                return r
            # else: notification / unrelated report -> discard


def call(fd, dev_index, feature_index, funcid, params=b"", long=None, timeout=DEFAULT_TIMEOUT):
    """HID++ 2.0 request. Returns reply params (bytes after the 4-byte header)."""
    r = _transact(fd, dev_index, feature_index, ((funcid & 0xF) << 4) | SW_ID,
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


def set_wheel(fd, idx, hires=None, invert=None):
    """fn2 setWheelMode. Starts from the mode the device reports, so bits the
    kernel driver set (including the diversion target) survive. Returns the
    state read back."""
    mode = get_wheel(fd, idx)["mode"]
    for flag, want in ((WHEEL_MODE_HIRES, hires), (WHEEL_MODE_INVERT, invert)):
        if want is not None:
            mode = (mode | flag) if want else (mode & ~flag)
    _fcall(fd, idx, F_HIRES_WHEEL, 2, bytes([mode]))
    return get_wheel(fd, idx)


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
        if proto[0] >= 2:
            for key, fn in (("name", get_device_name), ("type", get_device_type)):
                try:
                    d[key] = fn(fd, i)
                except HidppError as e:
                    log.debug("%s idx %d: %s", key, i, e)
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
            fd = open_node(node.path)
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
