"""Device registry: turn live HID++ discovery into protocol payloads.

Each physical HID++ endpoint (a device index behind a receiver, or a corded
device) becomes one Device with an assigned id ``dev%08d``. The registry builds
the proto-JSON the frontend expects for ``/devices/list``,
``/devices/ever_connected/list``, ``/devices/<id>/info``, ``/battery/<id>/state``
and the mouse DPI routes, straight from HID++ feature reads -- no vendor depot,
string package or thumbnail is required for the device to appear and be
configurable. When the Logitech catalogue and the device's depot are
installed (see resources.py), Device.Info carries the catalogue identity and
the UI loads the device images and metadata from that depot.
"""

import logging
import threading
import time

from . import hidpp, resources
from .wheel_relay import NEUTRAL_SPEED

log = logging.getLogger("opl.model")

TYPE = "type.googleapis.com/"

# hidpp.get_device_type() string -> logi.protocol.devices.Device.Type name.
HIDPP_TYPE = {
    "keyboard": "KEYBOARD", "remote control": "REMOTE", "numpad": "NUMPAD",
    "mouse": "MOUSE", "trackpad": "TOUCHPAD", "trackball": "TRACKBALL",
    "presenter": "PRESENTER", "receiver": "RECEIVER", "headset": "HEADSET",
    "webcam": "CAMERA", "steering wheel": "STEERING_WHEEL",
}


# Bolt advertisement kind byte (Solaar's hidpp10 DEVICE_KIND; unconfirmed on hardware).
BOLT_KINDS = {1: "keyboard", 2: "mouse", 3: "numpad", 4: "presenter", 7: "remote control",
              8: "trackball", 9: "trackpad"}


def _dtype(raw):
    if isinstance(raw, str):
        return HIDPP_TYPE.get(raw.lower(), "MOUSE")
    return "MOUSE"

# Features whose events mean the mouse lost state: wireless status (wake), config change.
RESUME_FEATURES = (0x1D4B, 0x0020)

# Connection enum names (logi.protocol.devices.Device.Interface.Connection).
CONN_BOLT = "BLE_PRO"
CONN_USB = "USB"


def format_firmware(fw):
    """Windows-style firmware string from DeviceFwInfo (0x0003) entities.

    "RBM 22.01.B0006": name, major.minor, then "B" and the 4-digit build.
    Uses the main application entity (type 0), else the first one.
    """
    if not fw or not isinstance(fw, list):
        return ""
    ent = next((e for e in fw if e.get("type") == 0), fw[0])
    return "%s %s.B%04X" % (ent["name"], ent["version"], ent["build"])


class Device:
    def __init__(self, dev_id, node, index, name, dtype, pid):
        self.id = dev_id
        self.node = node            # hidpp.HidppNode of the transport (receiver)
        self.index = index          # HID++ device index (1..6, 0xFF corded)
        self.name = name
        self.dtype = dtype          # Device.Type enum name
        self.pid = pid              # wireless PID (int) or node PID
        self.connected = True
        self.first_connected = int(time.time())
        self.serial = ""
        self.fw = ""
        self.thumb_wheel = False     # has 0x2150
        self.easy_switch = False     # has 0x1814 and 0x1815
        self.battery_caps = None     # 0x1004 capabilities {levels, rechargeable, soc}
        self.event_features = {}     # feature index -> 0x1d4b / 0x0020 (resume events)
        self.controls = None         # 0x1b04 {cid: {...}}, read on first use
        self.button_feature = None   # feature index of 0x1b04, once looked up
        self.thumb_feature = None    # feature index of 0x2150, once looked up
        self.battery_cache = None    # last {percent, charging, status} from HID++
        self.scroll_caps = {"smartshift": False, "highResolution": False}   # features present
        self.wheel_features = {}     # feature index -> 0x2121 / 0x2150, whose events the wheel relay reads
        self.relay = {"wheel": False, "thumb": False}   # speed can be applied: the wheel relay is available
        self.catalogue = resources.catalogue_entry(pid) or {}

    @property
    def model_hex(self):
        return format(self.pid, "x") if self.pid else ""

    @property
    def model_id(self):
        """Catalogue model id (e.g. "2b034"), else the bare PID hex."""
        return self.catalogue.get("modelId") or self.model_hex

    @property
    def depot(self):
        return self.catalogue.get("depot", "")

    @property
    def slot_prefix(self):
        if self.catalogue.get("slotPrefix"):
            return self.catalogue["slotPrefix"]
        return "046d_" + self.model_hex if self.model_hex else self.id

    def resources(self):
        return resources.device_resources(self.depot, self.model_id)

    # ---- payload builders -------------------------------------------------
    def info(self):
        """logi.protocol.devices.Device.Info for /devices/list and /devices/<id>/info."""
        out = {
            "id": self.id,
            "pid": self.pid,
            "state": "ACTIVE" if self.connected else "ABSENT",
            "connectionType": CONN_BOLT,
            "hasWirelessInterface": True,
            "deviceType": self.catalogue.get("type") or self.dtype,
            "deviceModel": self.model_id,
            "displayName": self.catalogue.get("displayName") or self.name,
            "extendedDisplayName": self.catalogue.get("extendedDisplayName") or self.name,
            "slotPrefix": self.slot_prefix,
            "depot": self.depot,
            "thumbnail": resources.resolve(self.catalogue.get("thumbnail", "")),
            "supported": True,
            "optionsDevice": False,      # True marks a legacy Logi Options device: UI shows NOT SUPPORTED
            "connected": self.connected,
            "isOwned": True,
            "modelId": self.model_id,
            "udid": self.serial,
            "deviceSerialNumber": self.serial,
            "firmwareVersion": format_firmware(self.fw),
            "resourcesAvailable": resources.depot_installed(self.depot),
            "fetchResource": False,
            "firstConnectedTimestamp": str(self.first_connected),
            **self._battery_fields(),
        }
        caps = out.setdefault("capabilities", {})
        caps["scrollWheelCapabilities"] = self._scroll_capabilities()
        if self.easy_switch:
            caps["hostInfos"] = True
        if self.thumb_wheel:
            # speed 0 keeps the thumb wheel speed control disabled: it is on only with the relay
            caps["mouseThumbWheelOverride"] = {"speed": NEUTRAL_SPEED if self.relay["thumb"] else 0}
        return out

    def _scroll_capabilities(self):
        return {"highResolution": self.scroll_caps["highResolution"],
                "smartshift": self.scroll_caps["smartshift"],
                "adjustableSpeed": self.relay["wheel"]}     # speed needs the wheel relay

    def _battery_fields(self):
        """Capability flags and level that make the UI show the battery indicator."""
        bc = self.battery_caps
        if bc is None and not self.battery_cache:
            return {}
        out = {"capabilities": {"hasBatteryStatus": True}}
        if bc is not None:
            out["capabilities"].update({
                "unifiedBattery": True,
                "battery": {"rechargeable": bc["rechargeable"], "stateOfCharge": bc["soc"],
                            "showBatteryStatus": False, "supportedLevels": bc["levels"]}})
        if self.battery_cache and self.battery_cache.get("percent") is not None:
            out["batteryDischargeLevel"] = int(self.battery_cache["percent"])
        return out

    def permanent(self):
        """logi.protocol.devices.DevicePermanentData for /devices/ever_connected/list."""
        return {
            "deviceType": self.dtype,
            "deviceModel": self.model_id,
            "extendedModelId": 0,
            "udid": self.serial or self.id,
            "connectionType": CONN_BOLT,
            # the real agent keeps the interface-id form here
            "slotPrefix": "046d_" + self.model_hex if self.model_hex else self.id,
            "firstConnectedTimestamp": str(self.first_connected),
            "serialNumber": self.serial,
        }


class Receiver:
    """A Unifying/Bolt receiver, surfaced as a Device.Info of type RECEIVER.

    One Receiver per hidraw node: two receivers of the same model are two Receivers.
    `lock` guards every pairing transition and what goes with it (the state, the
    watchdog, the slots seen when pairing began); the pairing code never holds it
    across a wait for the Reader."""

    def __init__(self, rcv_id, node):
        self.id = rcv_id
        self.node = node
        self.pid = node.pid
        self.bolt = bool(getattr(node, "is_bolt", False))
        self.serial = b""            # Bolt unique id (register 0x2FB); identifies it across a replug
        self.connected = True
        self.lock = threading.RLock()
        self.pairing = pairing_state(rcv_id, "UNKNOWN_PAIRING_STATE")
        self.generation = 0          # counts pairing starts; a watchdog only ends the one it was set for
        self.watchdog = None         # threading.Timer ending a pairing nobody reports on
        self.closing = False         # we are closing the lock ourselves: its notification is no news
        self.slots_before = set()    # device indexes on the node when pairing began
        self.locked = False          # Unifying lock open, as the receiver last said
        self.discovery = "UNKNOWN_STATE"    # Bolt discovery: START / STOP / CANCEL
        self.phase = None            # Bolt: "discovery" or "pairing" while pairing is in progress
        self.advertised = {}         # Bolt: device address (hex) -> {"payload", "authentication", "kind"}
        self.candidate = None        # Bolt: the 0x4F device being assembled
        self.counter = None          # Bolt: counter of the 0x4F notifications being followed
        self.auto_pair = False       # pair the first advertised device without being told which
        self.address = ""            # the device address being paired (hex), if known
        self.timeout = 60            # seconds the lock or discovery was opened for

    @property
    def name(self):
        return "Logi Bolt Receiver" if self.bolt else (self.node.name or "Logitech Receiver")

    def info(self):
        """logi.protocol.devices.Device.Info for the receiver."""
        return {
            "id": self.id,
            "pid": self.pid,
            "state": "ACTIVE" if self.connected else "ABSENT",
            "connectionType": CONN_USB,
            "hasWirelessInterface": True,
            "deviceType": "RECEIVER",
            "deviceModel": format(self.pid, "x"),
            "displayName": self.name,
            "extendedDisplayName": self.name,
            "slotPrefix": "046d_" + format(self.pid, "x"),
            "supported": True,
            "optionsDevice": False,
            "connected": self.connected,
            "isOwned": True,
            "modelId": format(self.pid, "x"),
            "maxPairableDeviceCount": 6,
            "pairingMethod": "BLE_PRO" if self.bolt else "UNIFYING",
        }


# logi.protocol.devices.Receiver.PairingState.State and .Error names (devices.proto, from the
# #18 review): the state is UNKNOWN_PAIRING_STATE, START, CANCEL or STOP; a finished pairing is
# STOP with NO_PAIRING_ERROR (done), TIMEOUT, FAILED or DISCONNECTED. A passkey is not a state:
# it goes out as Receiver.PassKeyDisplay.
PAIRING_MARGIN = 5.0            # seconds past the lock's timeout before the watchdog gives up
SETTLE_SECONDS, SETTLE_TRIES = 0.3, 4   # a paired device may take a moment to answer the receiver


def pairing_state(rcv_id, state, error="NO_PAIRING_ERROR", slot=0, device=None, address=""):
    """logi.protocol.devices.Receiver.PairingState payload."""
    out = {"@type": TYPE + "logi.protocol.devices.Receiver.PairingState",
           "receiverId": rcv_id, "state": state, "error": error, "pairingSlot": slot}
    if address:
        out["deviceAddress"] = address
    if device is not None:
        out["pairedDevice"] = device
    return out


def unifying_error(code):
    """Receiver.Pairing error for a Unifying lock-closed error code (Solaar's PairingError:
    1 device timeout, 2 device not supported, 3 too many devices, 6 sequence timeout)."""
    return "TIMEOUT" if code in (0, 1, 6) else "FAILED"


def bolt_error(code):
    """Receiver.Pairing error for a Bolt error byte (1 device timeout, 2 failed)."""
    return "TIMEOUT" if code in (0, hidpp.BOLT_ERROR_TIMEOUT) else "FAILED"


def basic_info(d):
    """logi.protocol.devices.Device.Info.Basic of a device."""
    return {"id": d.id, "deviceType": d.dtype, "deviceModel": d.model_id,
            "slotPrefix": d.slot_prefix, "pid": d.pid}


class CoalescingJobs:
    """Runs blocking jobs off the Reader's dispatch thread, one at a time per key.

    A job posted while one with the same key runs is not queued many times over: it is
    remembered once and runs again when the first ends, so a burst of wake events costs
    one more reapply, not one each. Different keys run in parallel."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = {}              # key -> [fn to run again, or None]
        self._closed = False

    def post(self, key, fn):
        with self._lock:
            if self._closed:
                return
            if key in self._active:
                self._active[key][0] = fn
                return
            self._active[key] = [None]
        threading.Thread(target=self._run, args=(key, fn), daemon=True, name="opl-job").start()

    def _run(self, key, fn):
        while True:
            try:
                fn()
            except Exception:
                log.exception("background job %s failed", key)
            with self._lock:
                again = self._active[key][0]
                if again is None or self._closed:
                    del self._active[key]
                    return
                self._active[key][0], fn = None, again

    def close(self):
        with self._lock:
            self._closed = True


class Registry:
    """Owns open HID++ transports and the set of live devices."""

    def __init__(self, transport=None):
        self.transport = transport or hidpp.HIDRAW   # node discovery + open
        self.devices = {}          # id -> Device
        self._fds = {}             # node.path -> fd
        self.absent = {}           # id -> Device that went away; a returning one keeps its id
        self._next_id = 0
        self.on_discovered = []    # callables(dev_id), run in scan() for each new device
        self.on_button_event = []  # callables(dev_id, function, params): a 0x1b04 event
        self.on_thumb_event = []   # callables(dev_id, function, params): a 0x2150 event (the action engine)
        self.on_wheel_event = []   # callables(dev_id, feature_id, function, params): a 0x2121 / 0x2150 event (the wheel relay)
        self.on_resumed = []       # callables(dev_id), after a wake or config-change event
        self.on_link_lost = []     # callables(dev_id): the mouse's link dropped (asleep, out of range, switched away)
        self.receivers = {}        # id -> Receiver
        self.absent_receivers = {}
        self._next_receiver = 0
        self._probed_receivers = []
        self._scan_lock = threading.RLock()
        self.on_pairing = []       # callables(rcv_id, state, passkey, events), on every pairing change;
                                   # state is None when only a passkey is shown
        self.on_discovery = []     # callables(rcv_id, Discovery payload, Advertise payload or None)
        self.on_locking = []       # callables(rcv_id, Locking payload): the Unifying lock opened or closed
        self._jobs = CoalescingJobs()   # work an event starts, off the Reader's dispatch thread
        self.closed = False        # set when shutting down: nothing is opened or rescanned any more

    def _fd(self, node):
        fd = self._fds.get(node.path)
        if fd is not None and fd.dead is not None:
            log.info("reopening %s: its reader stopped (%s)", node.path, fd.dead)
            self._fds.pop(node.path, None)
            try:
                hidpp.close_node(fd)
            except OSError:
                pass
            fd = None
        if fd is None:
            if self.closed:
                raise OSError("the registry is closed")
            fd = hidpp.Reader(self.transport.open(node.path),
                              long_only=getattr(node, "long_only", False))
            fd.add_listener(lambda idx, fidx, fn, params, node=node: self._on_event(node, idx, fidx, fn, params))
            self._fds[node.path] = fd
        return fd

    @staticmethod
    def _identity(info, node):
        """Hardware identity that survives a replug (hidraw node and index may change)."""
        wpid = info.get("wpid")
        pid = int(wpid, 16) if isinstance(wpid, str) else (wpid or getattr(node, "pid", 0))
        return (pid, info.get("name") or "Logitech Device")

    def _probe(self):
        """Blocking: ask every node what is behind it -> [(node, info)], or None when
        discovery itself failed (leave the device table alone)."""
        try:
            nodes = self.transport.list_nodes()
        except Exception:
            log.exception("HID++ discovery failed")
            return None
        live = {n.path for n in nodes}
        for path in [p for p in self._fds if p not in live]:
            try:
                hidpp.close_node(self._fds.pop(path))
            except OSError:
                pass
        out = []
        receivers = []
        for node in nodes:
            try:
                fd = self._fd(node)
            except OSError as exc:
                log.warning("cannot open %s: %s", node.path, exc)
                continue
            if node.is_receiver:
                receivers.append(node)
            bolt = getattr(node, "is_bolt", False)
            try:
                if node.is_receiver:
                    found = hidpp.get_paired_devices(fd, bolt=bolt)
                else:
                    # corded / directly-attached device at the fixed index 0xFF
                    proto = hidpp.root_ping(fd, 0xFF)
                    found = [{"index": 0xFF, "protocol": proto, "wpid": None,
                              "name": hidpp.get_device_name(fd, 0xFF),
                              "type": hidpp.get_device_type(fd, 0xFF)}]
            except hidpp.HidppError:
                found = []
            out += [(node, info) for info in found]
        self._probed_receivers = receivers
        return out

    def _reconcile_receivers(self, force, events):
        """Bring `receivers` in line with the receiver nodes the probe found.

        A receiver is its hidraw node: two receivers of one model stay two. One that
        comes back on another node is recognised by its unique id (Bolt), else by model."""
        free = {n.path: n for n in self._probed_receivers}
        for rid, r in list(self.receivers.items()):
            node = free.pop(r.node.path, None)
            if node is None:
                r.connected = False
                self._receiver_gone(r)
                self.absent_receivers[rid] = r
                del self.receivers[rid]
                events["receiver_removed"].append(r)
                continue
            r.node = node
            if force:
                self._setup_receiver(r)
        for rid, r in sorted(self.absent_receivers.items()):
            node = self._returning_node(r, free)
            if node is None:
                continue
            del free[node.path]
            del self.absent_receivers[rid]
            r.node, r.connected = node, True
            self._setup_receiver(r)
            self.receivers[rid] = r
            events["receiver_added"].append(rid)
        for node in free.values():
            rid = "rcv%08d" % self._next_receiver
            self._next_receiver += 1
            r = Receiver(rid, node)
            self._setup_receiver(r)
            self.receivers[rid] = r
            events["receiver_added"].append(rid)
        self.receivers = dict(sorted(self.receivers.items()))

    def _returning_node(self, r, free):
        """The node among `free` that receiver `r` came back on, or None."""
        if r.node.path in free:
            return free[r.node.path]
        for node in free.values():
            if node.pid != r.pid:
                continue
            serial = self._serial(node)
            if not (r.serial and serial and r.serial != serial):
                return node
        return None

    def _serial(self, node):
        """Unique id of a Bolt receiver (register 0x2FB), b"" when it has none or does not answer."""
        fd = self._fds.get(node.path)
        if fd is None or not getattr(node, "is_bolt", False):
            return b""
        try:
            return hidpp.bolt_unique_id(fd)
        except (hidpp.HidppError, OSError):
            return b""

    def _setup_receiver(self, r):
        r.serial = self._serial(r.node)

    def _build(self, dev_id, node, info, previous=None):
        fd = self._fds[node.path]
        idx = info["index"]
        name = info.get("name") or "Logitech Device"
        wpid = info.get("wpid")
        pid = int(wpid, 16) if isinstance(wpid, str) else (wpid or getattr(node, "pid", 0))
        d = Device(dev_id, node, idx, name, _dtype(info.get("type")), pid)
        if previous is not None:
            d.first_connected = previous.first_connected
        try:
            d.fw = hidpp.get_fw_info(fd, idx) or ""
        except Exception:
            pass
        try:
            d.thumb_wheel = hidpp.has_feature(fd, idx, hidpp.F_THUMB_WHEEL)
        except Exception:
            pass
        try:
            d.easy_switch = (hidpp.has_feature(fd, idx, hidpp.F_CHANGE_HOST)
                             and hidpp.has_feature(fd, idx, hidpp.F_HOSTS_INFO))
        except Exception:
            pass
        try:
            d.battery_caps = hidpp.get_battery_caps(fd, idx)
        except Exception:
            pass
        try:
            d.battery_cache = hidpp.get_battery(fd, idx)
        except Exception:
            pass
        for fid in RESUME_FEATURES:
            try:
                d.event_features[hidpp.feature_index(fd, idx, fid)] = fid
            except hidpp.HidppError:
                pass
        try:
            d.scroll_caps = {
                "smartshift": (hidpp.has_feature(fd, idx, hidpp.F_SMART_SHIFT)
                               or hidpp.has_feature(fd, idx, hidpp.F_SMART_SHIFT_ENH)),
                "highResolution": hidpp.has_feature(fd, idx, hidpp.F_HIRES_WHEEL)}
        except Exception:
            pass
        for fid in (hidpp.F_HIRES_WHEEL, hidpp.F_THUMB_WHEEL):
            try:
                d.wheel_features[hidpp.feature_index(fd, idx, fid)] = fid
            except hidpp.HidppError:
                pass
        return d

    def scan(self):
        """Discover receivers/devices; every device found counts as (re)discovered."""
        return self.rescan(force=True)

    def rescan(self, force=False):
        """Blocking: reconcile the device table with what is plugged in and on.

        A device that returns keeps the id it had when its hardware identity
        matches. Hooks in `on_discovered` run for each new or returned device.
        Returns {"added": [...], "returned": [...], "removed": [...]} of device ids
        (Device objects for removed ones, marked not connected). Receivers report as
        "receiver_added" (ids, new or returned) and "receiver_removed" (Receiver objects)."""
        with self._scan_lock:
            if self.closed:
                return {"added": [], "returned": [], "removed": [],
                        "receiver_added": [], "receiver_removed": []}
            return self._rescan(force)

    def _rescan(self, force):
        probed = self._probe()
        events = {"added": [], "returned": [], "removed": [],
                  "receiver_added": [], "receiver_removed": []}
        if probed is None:
            return events
        self._reconcile_receivers(force, events)
        by_key = {}
        for node, info in probed:
            by_key.setdefault(self._identity(info, node), (node, info))
        new_devices = {}
        for dev_id, d in list(self.devices.items()):
            key = (d.pid, d.name)
            hit = by_key.pop(key, None)
            if hit is None:
                continue
            node, info = hit
            if not force and d.node.path == node.path and d.index == info["index"]:
                new_devices[dev_id] = d
                continue
            new_devices[dev_id] = self._build(dev_id, node, info, d)
            events["returned"].append(dev_id)
        for dev_id, d in list(self.devices.items()):
            if dev_id not in new_devices:
                d.connected = False
                events["removed"].append(d)
        for dev_id, d in list(self.absent.items()):
            hit = by_key.pop((d.pid, d.name), None)
            if hit is None:
                continue
            del self.absent[dev_id]
            new_devices[dev_id] = self._build(dev_id, hit[0], hit[1], d)
            events["returned"].append(dev_id)
        for key, (node, info) in by_key.items():
            dev_id = "dev%08d" % self._next_id
            self._next_id += 1
            new_devices[dev_id] = self._build(dev_id, node, info)
            events["added"].append(dev_id)
        for d in events["removed"]:
            self.absent[d.id] = d
            log.info("device %s (%s) went away", d.id, d.name)
        self.devices = dict(sorted(new_devices.items()))
        for dev_id in events["added"] + events["returned"]:
            d = self.devices[dev_id]
            log.info("device %s: %s (%s) index=%d on %s", dev_id, d.name, d.dtype, d.index, d.node.path)
            for hook in list(self.on_discovered):
                try:
                    hook(dev_id)
                except Exception:
                    log.exception("discovery hook failed for %s", dev_id)
        return events

    def _on_event(self, node, index, feature_index, function=0, params=b""):
        """Reader listener (own thread): a receiver pairing event, or the mouse
        woke or changed configuration."""
        if hidpp.is_hidpp10_notification(feature_index):
            if index != hidpp.DEV_RECEIVER:
                self._on_device_notification(node, index, feature_index, function, params)
            elif feature_index == hidpp.N_PAIRING_LOCK:
                self._on_unifying_lock(node, function, params)
            else:
                self._on_bolt_notification(node, feature_index, function, params)
            return
        if index == hidpp.DEV_RECEIVER:
            return
        for dev_id, d in list(self.devices.items()):
            if d.node.path != node.path or d.index != index:
                continue
            if feature_index == d.button_feature:
                for hook in list(self.on_button_event):
                    try:
                        hook(dev_id, function, params)
                    except Exception:
                        log.exception("button hook failed for %s", dev_id)
                continue
            if feature_index == d.thumb_feature:
                for hook in list(self.on_thumb_event):
                    try:
                        hook(dev_id, function, params)
                    except Exception:
                        log.exception("thumb wheel hook failed for %s", dev_id)
                if feature_index not in d.wheel_features:
                    continue
            wheel = d.wheel_features.get(feature_index)
            if wheel is not None:          # the thumb wheel's event reaches both hooks; its owner is decided there
                for hook in list(self.on_wheel_event):
                    try:
                        hook(dev_id, wheel, function, params)
                    except Exception:
                        log.exception("wheel hook failed for %s", dev_id)
                continue
            fid = d.event_features.get(feature_index)
            if fid is None:
                continue
            log.info("device %s: event 0x%04x, reapplying settings", dev_id, fid)
            self._jobs.post(("resume", dev_id), lambda dev_id=dev_id: self._resume(dev_id))

    def _resume(self, dev_id):
        """Job: the mouse woke or changed configuration. Refresh the battery, run the hooks."""
        try:
            self.refresh_battery(dev_id)
        except Exception as exc:
            log.debug("battery refresh after event failed: %s", exc)
        for hook in list(self.on_resumed):
            try:
                hook(dev_id)
            except Exception:
                log.exception("resume hook failed for %s", dev_id)

    def _on_device_notification(self, node, index, sub_id, address, data):
        """Dispatch thread: a HID++ 1.0 notification from the device at `index`."""
        if sub_id != hidpp.N_DEVICE_CONNECT:
            return
        flags = data[0] if data else 0
        if flags & 0x40:                       # the link was lost, not made
            for dev_id, d in list(self.devices.items()):
                if d.node.path == node.path and d.index == index:
                    for hook in list(self.on_link_lost):
                        try:
                            hook(dev_id)
                        except Exception:
                            log.exception("link-lost hook failed for %s", dev_id)
            return
        for dev_id, d in list(self.devices.items()):
            if d.node.path == node.path and d.index == index:
                log.info("device %s: link up, reapplying settings", dev_id)
                self._jobs.post(("resume", dev_id), lambda d=d: self._reconnected(d))

    def _reconnected(self, d):
        """Job: a mouse that woke (or was asleep at discovery) says its link is up. Learn
        the feature indexes whose events mean a wake, then reapply as after any wake."""
        fd = self._fds.get(d.node.path)
        if fd is not None:
            for fid in RESUME_FEATURES:
                try:
                    d.event_features[hidpp.feature_index(fd, d.index, fid)] = fid
                except hidpp.HidppError:
                    pass
        self._resume(d.id)

    # ---- receiver pairing ---------------------------------------------------
    #
    # Unifying: register 0xB2 opens and closes the lock; the receiver says so with 0x4A.
    # Bolt: register 0xC0 starts device discovery (0x53 status, 0x4F one notification pair per
    # advertising device), then register 0x2C1 pairs the chosen address (0x54 status, 0x4D passkey).
    # The register layouts and notification fields come from Solaar; none is confirmed on
    # hardware yet. Every state change happens under Receiver.lock and is announced from there,
    # so subscribers see the changes in the order they happened. Work that talks to the
    # receiver (the rescan that finds the new device, the pair write after an advertisement)
    # runs as a job, never on the Reader's dispatch thread.
    PAIRING_FROM = frozenset({"UNKNOWN_PAIRING_STATE", "STOP", "CANCEL"})

    def _transition(self, r, state, from_states, error="NO_PAIRING_ERROR", slot=0, device=None,
                    events=None):
        """Move the pairing state if it is in `from_states` (else change nothing, announce
        nothing); returns the new state, or None when it did not move."""
        with r.lock:
            if r.pairing["state"] not in from_states:
                return None
            new = pairing_state(r.id, state, error=error, slot=slot, device=device, address=r.address)
            r.pairing = new
            if state == "START":
                self._arm(r)
            else:
                self._disarm(r)
                r.phase = None
            self._announce_pairing(r, new, None, events)
            return new

    def _announce_pairing(self, r, state, passkey=None, events=None):
        for hook in list(self.on_pairing):
            try:
                hook(r.id, state, passkey, events)
            except Exception:
                log.exception("pairing hook failed for %s", r.id)

    def _announce_discovery(self, r, advertise=None):
        payload = {"@type": TYPE + "logi.protocol.devices.Receiver.Discovery",
                   "receiverId": r.id, "state": r.discovery, "timeoutSeconds": r.timeout}
        for hook in list(self.on_discovery):
            try:
                hook(r.id, payload, advertise)
            except Exception:
                log.exception("discovery hook failed for %s", r.id)

    def locking_state(self, r):
        """logi.protocol.devices.Receiver.Locking: whether the Unifying lock is open (OPENED/CLOSED;
        the names are from the #19 review)."""
        return {"@type": TYPE + "logi.protocol.devices.Receiver.Locking",
                "receiverId": r.id, "state": "OPENED" if r.locked else "CLOSED"}

    def discovery_state(self, r):
        return {"@type": TYPE + "logi.protocol.devices.Receiver.Discovery",
                "receiverId": r.id, "state": r.discovery, "timeoutSeconds": r.timeout}

    def _arm(self, r):
        """Start the watchdog: a pairing nobody reports on ends as a timeout."""
        with r.lock:
            self._disarm(r)
            r.generation += 1
            timer = threading.Timer(r.timeout + PAIRING_MARGIN, self._expired, (r, r.generation))
            timer.daemon = True
            r.watchdog = timer
            timer.start()

    def _disarm(self, r):
        with r.lock:
            if r.watchdog is not None:
                r.watchdog.cancel()
                r.watchdog = None

    def _expired(self, r, generation):
        with r.lock:
            if r.generation != generation or r.pairing["state"] != "START":
                return
            log.warning("receiver %s: no word on the pairing in time, giving up", r.id)
            self._transition(r, "STOP", {"START"}, error="TIMEOUT")
            if r.discovery == "START":
                r.discovery = "STOP"
                self._announce_discovery(r)

    def _receiver_gone(self, r):
        """The receiver left: a pairing in progress ends as disconnected."""
        with r.lock:
            self._transition(r, "STOP", {"START"}, error="DISCONNECTED")
            self._disarm(r)
            r.locked, r.closing, r.phase = False, False, None
            if r.discovery == "START":
                r.discovery = "STOP"

    def _receiver_at(self, node, bolt):
        return next((x for x in list(self.receivers.values())
                     if x.node.path == node.path and x.bolt == bolt), None)

    @staticmethod
    def _address(address):
        """6 bytes from a hex string (colons and dashes allowed); ValueError otherwise."""
        if isinstance(address, (bytes, bytearray)):
            raw = bytes(address)
        else:
            raw = bytes.fromhex(str(address).replace(":", "").replace("-", ""))
        if len(raw) != 6:
            raise ValueError("a Bolt device address has 6 bytes")
        return raw

    def _begin(self, r, timeout, address=""):
        """Under no lock: reset what the last pairing left. Returns False when one is running."""
        with r.lock:
            if r.pairing["state"] == "START":
                return False
            r.closing = False
            r.timeout = timeout
            r.address = address
            r.slots_before = {d.index for d in self.devices.values() if d.node.path == r.node.path}
            r.advertised.clear()
            r.candidate = None
            return True

    def open_pairing(self, rcv_id, timeout=60, address=None, authentication=None):
        """Blocking: start pairing. A Unifying receiver opens its lock. A Bolt receiver pairs
        `address` when given (hex string), else looks for advertising devices and pairs the
        first one. A write the receiver rejects raises and leaves the state as it was.
        Returns the pairing state."""
        r = self.receivers[rcv_id]
        if not 0 < timeout <= 255:
            raise ValueError("pairing timeout must be 1..255 seconds")
        raw = self._address(address) if address and r.bolt else None
        if not self._begin(r, timeout, raw.hex() if raw else ""):
            return r.pairing
        fd = self._fds[r.node.path]
        if not r.bolt:
            hidpp.unifying_lock(fd, True, timeout)
            r.phase = "pairing"
        elif raw is not None:
            self._bolt_pair(r, raw, authentication or 0, 2)
        else:
            hidpp.bolt_discover(fd, timeout)
            with r.lock:
                r.phase, r.auto_pair = "discovery", True
                r.discovery = "START"
                self._announce_discovery(r)
        return self._transition(r, "START", self.PAIRING_FROM) or r.pairing

    def open_discovery(self, rcv_id, timeout=30):
        """Blocking: Bolt only. Look for advertising devices without pairing any; each one is
        announced. Raises when the receiver cannot do that or rejects the write."""
        r = self.receivers[rcv_id]
        if not r.bolt:
            raise hidpp.FeatureNotSupported(hidpp.REG_BOLT_DISCOVERY)
        if not 0 < timeout <= 255:
            raise ValueError("discovery timeout must be 1..255 seconds")
        with r.lock:
            if r.pairing["state"] == "START":
                return self.discovery_state(r)
            r.closing, r.timeout = False, timeout
            r.advertised.clear()
            r.candidate = None
        hidpp.bolt_discover(self._fds[r.node.path], timeout)
        with r.lock:
            r.discovery, r.auto_pair = "START", False
            self._announce_discovery(r)
            return self.discovery_state(r)

    def _bolt_pair(self, r, address, authentication, kind):
        """Blocking: write the pair request for `address`. Marks the pairing phase first, so
        the end of discovery that follows is not mistaken for a failure."""
        with r.lock:
            was = r.phase
            r.phase, r.address = "pairing", address.hex()
        try:
            hidpp.bolt_pair(self._fds[r.node.path], address, authentication,
                            entropy=20 if kind == 2 else 10)
        except Exception:
            with r.lock:
                r.phase = was
            raise

    def cancel_pairing(self, rcv_id):
        """Blocking: stop pairing (and discovery). Raises when the write fails. Does nothing,
        announces nothing, when no pairing or discovery is in progress."""
        r = self.receivers[rcv_id]
        with r.lock:
            pairing = r.pairing["state"] == "START"
            if not pairing and r.discovery != "START":
                return r.pairing
            phase = r.phase
            r.closing = True            # the receiver's own "closed" notification is no news
        fd = self._fds[r.node.path]
        try:
            if not r.bolt:
                hidpp.unifying_lock(fd, False)
            elif phase == "pairing":
                hidpp.bolt_pair_cancel(fd)
            else:
                hidpp.bolt_discover(fd, 1, cancel=True)
        except Exception:
            with r.lock:
                r.closing = False
            raise
        with r.lock:
            if r.discovery == "START":
                r.discovery = "CANCEL"
                self._announce_discovery(r)
            return self._transition(r, "CANCEL", {"START"}) or r.pairing

    # ---- Unifying notifications ---------------------------------------------
    def _on_unifying_lock(self, node, address, params):
        """Dispatch thread: 0x4A lock status (address bit0 = lock open, params[0] = error)."""
        r = self._receiver_at(node, bolt=False)
        if r is None:
            return
        code = params[0] if params else 0
        with r.lock:
            r.locked = bool(address & 0x01)
            for hook in list(self.on_locking):
                try:
                    hook(r.id, self.locking_state(r))
                except Exception:
                    log.exception("locking hook failed for %s", r.id)
            if r.locked:
                r.closing = False
                if r.pairing["state"] != "START":       # opened by another tool, or by us a moment ago
                    r.phase = "pairing"
                    if not r.slots_before:
                        r.slots_before = {d.index for d in self.devices.values() if d.node.path == r.node.path}
                    self._transition(r, "START", self.PAIRING_FROM)
                return
            if r.closing or r.pairing["state"] != "START":
                return                                  # we closed it, or nobody opened it
            generation = r.generation
        self._jobs.post(("pairing", r.id), lambda: self._unifying_finish(r, code, generation))

    def _joined(self, r, slot=None):
        """Devices on the receiver's node that were not there when pairing began (or at `slot`)."""
        return [d for d in list(self.devices.values()) if d.node.path == r.node.path
                and (d.index == slot if slot else d.index not in r.slots_before)]

    def _settle(self, r, slot=None, tries=SETTLE_TRIES):
        """Job: rescan until the paired device shows up. -> (devices, rescan events)."""
        new, events = [], None
        for attempt in range(max(tries, 1)):
            events = self.rescan()
            new = self._joined(r, slot)
            if new or attempt == tries - 1:
                break
            time.sleep(SETTLE_SECONDS)
        return new, events

    def _unifying_finish(self, r, code, generation):
        """Job: the lock closed while pairing was open. A device that was not there when
        pairing began, and no error code, means success (it does not matter which rescan
        found the device first)."""
        new, events = self._settle(r, tries=1 if code else SETTLE_TRIES)
        if code:
            log.warning("receiver %s: pairing ended with code %d", r.id, code)
        with r.lock:
            if r.generation != generation or r.closing or r.pairing["state"] != "START":
                return
            if new and not code:
                d = new[0]
                self._transition(r, "STOP", {"START"}, slot=d.index, device=basic_info(d), events=events)
            else:
                self._transition(r, "STOP", {"START"}, error=unifying_error(code), events=events)

    # ---- Bolt notifications -------------------------------------------------
    def _on_bolt_notification(self, node, sub_id, address, data):
        """Dispatch thread: 0x53 discovery status, 0x4F advertising device, 0x54 pairing
        status, 0x4D passkey. Only state changes here; anything that talks to the receiver
        is posted as a job."""
        r = self._receiver_at(node, bolt=True)
        if r is None:
            return
        data = bytes(data)
        if sub_id == hidpp.N_DISCOVERY_STATUS:
            self._on_discovery_status(r, address, data[0] if data else 0)
        elif sub_id == hidpp.N_DEVICE_DISCOVERY:
            self._on_advertised(r, address, data)
        elif sub_id == hidpp.N_PAIRING_STATUS:
            self._on_bolt_pairing_status(r, address, data)
        elif sub_id == hidpp.N_PASSKEY_REQUEST:
            digits = data[:6].decode("ascii", "replace")
            with r.lock:
                self._announce_pairing(r, None, digits)

    def _on_discovery_status(self, r, address, code):
        with r.lock:
            if r.closing:
                return
            if address == 0:                            # discovery is running
                r.discovery, r.candidate = "START", None
                self._announce_discovery(r)
                return
            r.discovery = "STOP"
            self._announce_discovery(r)
            if r.phase == "discovery":                  # it ended without a device being paired
                self._transition(r, "STOP", {"START"}, error=bolt_error(code))

    def _on_advertised(self, r, address, data):
        parsed = hidpp.parse_device_discovery(address, data)
        if parsed is None:
            return
        counter, part, fields = parsed
        with r.lock:
            if part == 0:
                r.candidate = (counter, fields)
                return
            if r.candidate is None or r.candidate[0] != counter:
                return
            first = r.candidate[1]
            r.candidate = None
            raw = first["address"]
            kind = _dtype(BOLT_KINDS.get(first["kind"], ""))
            advertise = {"@type": TYPE + "logi.protocol.devices.Receiver.Advertise",
                         "receiverId": r.id, "deviceAddress": raw.hex(), "name": fields["name"],
                         "deviceType": kind}
            r.advertised[raw.hex()] = {"authentication": first["authentication"], "kind": first["kind"]}
            self._announce_discovery(r, advertise)
            auto = r.auto_pair and r.pairing["state"] == "START" and r.phase == "discovery"
            if auto:
                r.auto_pair = False
        if auto:
            self._jobs.post(("pair", r.id), lambda: self._pair_advertised(r, raw, first))

    def _pair_advertised(self, r, raw, first):
        """Job: pair the first device that advertised itself."""
        with r.lock:
            if r.closing or r.pairing["state"] != "START":
                return
        try:
            self._bolt_pair(r, raw, first["authentication"], first["kind"])
        except Exception as exc:
            log.warning("receiver %s: pairing %s failed: %s", r.id, raw.hex(), exc)
            self._transition(r, "STOP", {"START"}, error="FAILED")

    def _on_bolt_pairing_status(self, r, address, data):
        code = data[0] if data else 0
        slot = data[7] if len(data) > 7 else 0
        with r.lock:
            if r.closing or r.pairing["state"] != "START":
                return
            if code:
                log.warning("receiver %s: pairing error %d", r.id, code)
                self._transition(r, "STOP", {"START"}, error=bolt_error(code))
            elif address == 2:                          # paired, in `slot`
                generation = r.generation
                self._jobs.post(("pairing", r.id), lambda: self._bolt_finish(r, slot, generation))

    def _bolt_finish(self, r, slot, generation):
        """Job: the receiver says a device paired in `slot`; find it."""
        new, events = self._settle(r, slot=slot)
        with r.lock:
            if r.generation != generation or r.closing or r.pairing["state"] != "START":
                return
            if new:
                self._transition(r, "STOP", {"START"}, slot=slot, device=basic_info(new[0]), events=events)
            else:
                log.warning("receiver %s: device in slot %d did not show up", r.id, slot)
                self._transition(r, "STOP", {"START"}, error="FAILED", slot=slot, events=events)

    # ---- per-device feature reads ----------------------------------------
    def refresh_battery(self, dev_id):
        """Blocking HID++ battery read; caches the result. Returns the cache dict."""
        d = self.devices.get(dev_id)
        if not d:
            return None
        fd = self._fds.get(d.node.path)
        d.battery_cache = hidpp.get_battery(fd, d.index)
        return d.battery_cache

    def battery(self, dev_id):
        """Payload for /battery/<id>/state, from cache (request path stays non-blocking)."""
        d = self.devices.get(dev_id)
        if not d:
            return None, None
        b = d.battery_cache or {}
        pct = float(b.get("percent") or 0)
        return {
            "@type": TYPE + "logi.protocol.wireless.Battery",
            "deviceId": dev_id,
            "percentage": pct,
            "charging": bool(b.get("charging")),
            "criticalLevel": pct <= 15,
            "level": _warn_level(pct),
            "simpleBattery": b.get("percent") is None,
        }, b

    def battery_status_for_info(self, dev_id):
        d = self.devices.get(dev_id)
        if not d or not d.battery_cache:
            return None
        b = d.battery_cache
        return {"stateOfCharge": int(b.get("percent") or 0),
                "chargingStatus": "CHARGING" if b.get("charging") else "DISCHARGING"}

    def dpi_range(self, dev_id):
        """Blocking read of the sensor's DPI list: {values, min, max, step}."""
        d = self.devices[dev_id]
        return hidpp.get_dpi_list(self._fds[d.node.path], d.index)

    def read_dpi(self, dev_id):
        """Blocking read of the current and default DPI: {dpi, default, ...}."""
        d = self.devices[dev_id]
        return hidpp.get_dpi(self._fds[d.node.path], d.index)

    def controls(self, dev_id):
        """Blocking: the mouse's reprogrammable controls {cid: {...}} (read once, then cached).
        Raises FeatureNotSupported when it has no 0x1b04. Also learns the feature index the
        button events arrive under."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        d.button_feature = hidpp.feature_index(fd, d.index, hidpp.F_REPROG_CONTROLS)
        if d.controls is None:
            d.controls = hidpp.get_controls(fd, d.index)
        return d.controls

    def set_divert(self, dev_id, cid, divert, raw_xy=None):
        """Blocking: divert a control to software (with its raw XY motion when `raw_xy`) or hand it
        back; reads the device back and raises when it did not take. Writes only when the device
        is not already as asked. `raw_xy` None leaves the motion reporting alone."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        d.button_feature = hidpp.feature_index(fd, d.index, hidpp.F_REPROG_CONTROLS)
        got_divert, got_raw, _ = hidpp.get_cid_reporting(fd, d.index, cid)
        if got_divert != bool(divert) or (raw_xy is not None and got_raw != bool(raw_xy)):
            hidpp.set_cid_diverted(fd, d.index, cid, divert, raw_xy)

    def set_thumb_divert(self, dev_id, divert):
        """Blocking: report the thumb wheel to software (or give it back to the HID stack),
        keeping its direction setting; reads back and raises when it did not take."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        d.thumb_feature = hidpp.feature_index(fd, d.index, hidpp.F_THUMB_WHEEL)
        want = hidpp.THUMB_DIVERTED if divert else hidpp.THUMB_NATIVE
        cur = hidpp.get_thumbwheel(fd, d.index)
        if cur["mode"] == want:
            return
        got = hidpp.set_thumbwheel(fd, d.index, cur["invert"], cur["smooth"], want)
        if got["mode"] != want:
            raise hidpp.HidppError(hidpp.F_THUMB_WHEEL, 2, 4)      # HW_ERROR: the device ignored it

    def remap_of(self, dev_id, cid):
        """Blocking: the control id button `cid` currently acts as (itself unless remapped)."""
        d = self.devices[dev_id]
        return hidpp.get_cid_remap(self._fds[d.node.path], d.index, cid)

    def set_remap(self, dev_id, cid, target):
        """Blocking: make button `cid` act as button `target`; reads back, raises when it did
        not take. Writes only when the device is not already as asked."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        if hidpp.get_cid_remap(fd, d.index, cid) != target:
            hidpp.set_cid_remap(fd, d.index, cid, target)

    def read_thumb(self, dev_id):
        """Blocking 0x2150 status read: {mode, invert, smooth}."""
        d = self.devices[dev_id]
        return hidpp.get_thumbwheel(self._fds[d.node.path], d.index)

    def write_thumb(self, dev_id, invert, smooth, divert=None):
        """Blocking 0x2150 write; returns the read-back status. The reporting mode stays as it
        is unless `divert` says to send the wheel to software (True) or hand it back (False)."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        if divert is None:
            mode = hidpp.get_thumbwheel(fd, d.index)["mode"]
        else:
            mode = hidpp.THUMB_DIVERTED if divert else hidpp.THUMB_NATIVE
        return hidpp.set_thumbwheel(fd, d.index, invert, smooth, mode)

    def thumb_units_per_notch(self, dev_id):
        """Blocking 0x2150 read: rotation units the diverted thumb wheel reports per notch."""
        d = self.devices[dev_id]
        return hidpp.get_thumbwheel_info(self._fds[d.node.path], d.index)["diverted"]

    def wheel_units_per_notch(self, dev_id):
        """Blocking 0x2121 read: hi-res steps the diverted wheel reports per notch."""
        d = self.devices[dev_id]
        return hidpp.get_wheel(self._fds[d.node.path], d.index)["multiplier"]

    def divert_wheel(self, dev_id, divert, invert=None, hires=None):
        """Blocking 0x2121 write: send the wheel's motion to software (True), hand it back (False)
        or leave the target as it is (None);
        `invert` and `hires` set the device's own direction and resolution (None: as they are).
        Returns the wheel state read back."""
        d = self.devices[dev_id]
        return hidpp.set_wheel(self._fds[d.node.path], d.index, hires=hires, invert=invert,
                               target=None if divert is None else bool(divert))

    def read_hosts(self, dev_id):
        """Blocking 0x1814 / 0x1815 read: {current, max_name, hosts}."""
        d = self.devices[dev_id]
        return hidpp.get_hosts(self._fds[d.node.path], d.index)

    def switch_host(self, dev_id, host):
        """Blocking 0x1814 setCurrentHost; raises on a device error."""
        d = self.devices[dev_id]
        hidpp.set_current_host(self._fds[d.node.path], d.index, host)

    def write_host_name(self, dev_id, host, name):
        """Blocking 0x1815 setHostFriendlyName; raises on a device error."""
        d = self.devices[dev_id]
        hidpp.set_host_name(self._fds[d.node.path], d.index, host, name)

    def write_dpi(self, dev_id, dpi):
        """Blocking 0x2201 write; returns the DPI read back from the device."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        hidpp.set_dpi(fd, d.index, int(dpi))
        return hidpp.get_dpi(fd, d.index)["dpi"]

    def read_scroll(self, dev_id):
        """Blocking: {"smartshift": {...}|None, "wheel": {...}|None} as the mouse has it now."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        out = {"smartshift": None, "wheel": None}
        if d.scroll_caps["smartshift"]:
            out["smartshift"] = hidpp.get_smartshift(fd, d.index)
        if d.scroll_caps["highResolution"]:
            out["wheel"] = hidpp.get_wheel(fd, d.index)
        return out

    def write_scroll(self, dev_id, ratchet=None, threshold=None, hires=None, invert=None):
        """Blocking 0x2110 / 0x2121 writes; None leaves a setting alone. Returns read_scroll()."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        if d.scroll_caps["smartshift"] and (ratchet is not None or threshold is not None):
            mode = None if ratchet is None else ("ratchet" if ratchet else "freespin")
            hidpp.set_smartshift(fd, d.index, mode=mode, threshold=threshold)
        if d.scroll_caps["highResolution"] and (hires is not None or invert is not None):
            hidpp.set_wheel(fd, d.index, hires=hires, invert=invert)
        return self.read_scroll(dev_id)

    def close(self):
        self.closed = True
        self._jobs.close()
        with self._scan_lock:           # a rescan in flight finishes before the descriptors go
            for r in list(self.receivers.values()) + list(self.absent_receivers.values()):
                self._disarm(r)
            for fd in self._fds.values():
                try:
                    hidpp.close_node(fd)
                except OSError:
                    pass
            self._fds.clear()


def _warn_level(pct):
    for name, thr in (("FULL", 100), ("GREAT", 90), ("GOOD", 50), ("LOW", 30), ("CRITICAL", 15)):
        if pct >= thr:
            return name
    return "DEAD"
