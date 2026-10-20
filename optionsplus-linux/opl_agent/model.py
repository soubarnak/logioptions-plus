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

log = logging.getLogger("opl.model")

TYPE = "type.googleapis.com/"

# hidpp.get_device_type() string -> logi.protocol.devices.Device.Type name.
HIDPP_TYPE = {
    "keyboard": "KEYBOARD", "remote control": "REMOTE", "numpad": "NUMPAD",
    "mouse": "MOUSE", "trackpad": "TOUCHPAD", "trackball": "TRACKBALL",
    "presenter": "PRESENTER", "receiver": "RECEIVER", "headset": "HEADSET",
    "webcam": "CAMERA", "steering wheel": "STEERING_WHEEL",
}


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
        self.battery_cache = None    # last {percent, charging, status} from HID++
        self.scroll_caps = {"smartshift": False, "highResolution": False}   # features present
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
            "firmwareVersion": self.fw,
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
            # speed 0: the thumb wheel speed control stays disabled until the relay exists
            caps["mouseThumbWheelOverride"] = {"speed": 0}
        return out

    def _scroll_capabilities(self):
        return {"highResolution": self.scroll_caps["highResolution"],
                "smartshift": self.scroll_caps["smartshift"],
                "adjustableSpeed": False}       # needs the wheel relay (not built yet)

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
    """A Unifying/Bolt receiver, surfaced as a Device.Info of type RECEIVER."""

    def __init__(self, rcv_id, node):
        self.id = rcv_id
        self.node = node
        self.pid = node.pid
        self.bolt = bool(getattr(node, "is_bolt", False))
        self.connected = True
        self.pairing_index = None    # feature index of the pairing feature, None when absent
        self.pairing = pairing_state(rcv_id, "IDLE")

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


# Pairing state names. The UI enum values are not confirmed against a live agent log
# (schema_defaults only names the UNKNOWN_* defaults): check them on hardware.
PAIRING_STATES = ("IDLE", "STARTED", "PASSKEY_DISPLAYED", "PAIRED", "FAILED", "CANCELLED")


def pairing_state(rcv_id, state, error="NO_PAIRING_ERROR", slot=0, device=None):
    """logi.protocol.devices.Receiver.PairingState payload."""
    out = {"@type": TYPE + "logi.protocol.devices.Receiver.PairingState",
           "receiverId": rcv_id, "state": state, "error": error, "pairingSlot": slot}
    if device is not None:
        out["pairedDevice"] = device
    return out


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
        self.on_resumed = []       # callables(dev_id), after a wake or config-change event
        self.receivers = {}        # id -> Receiver
        self.absent_receivers = {}
        self._next_receiver = 0
        self._probed_receivers = []
        self._scan_lock = threading.RLock()
        self.on_pairing = []       # callables(rcv_id, state, passkey, events), on every pairing change

    def _fd(self, node):
        fd = self._fds.get(node.path)
        if fd is None:
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
        """Bring `receivers` in line with the receiver nodes the probe found."""
        present = {n.pid: n for n in self._probed_receivers}
        for rid, r in list(self.receivers.items()):
            node = present.pop(r.pid, None)
            if node is None:
                r.connected = False
                self.absent_receivers[rid] = r
                del self.receivers[rid]
                events["receiver_removed"].append(r)
                continue
            if force or node.path != r.node.path:
                r.node = node
                self._setup_receiver(r)
                if not force:
                    events["receiver_added"].append(rid)
        for rid, r in list(self.absent_receivers.items()):
            node = present.pop(r.pid, None)
            if node is None:
                continue
            del self.absent_receivers[rid]
            r.node, r.connected = node, True
            self._setup_receiver(r)
            self.receivers[rid] = r
            events["receiver_added"].append(rid)
        for node in present.values():
            rid = "rcv%08d" % self._next_receiver
            self._next_receiver += 1
            r = Receiver(rid, node)
            self._setup_receiver(r)
            self.receivers[rid] = r
            events["receiver_added"].append(rid)
        self.receivers = dict(sorted(self.receivers.items()))

    def _setup_receiver(self, r):
        try:
            r.pairing_index = hidpp.feature_index(self._fds[r.node.path], hidpp.DEV_RECEIVER,
                                                  hidpp.F_RECEIVER_PAIRING)
        except Exception:
            r.pairing_index = None

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
        if index == hidpp.DEV_RECEIVER:
            self._on_pairing_event(node, feature_index, function, params)
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
            fid = d.event_features.get(feature_index)
            if fid is None:
                continue
            log.info("device %s: event 0x%04x, reapplying settings", dev_id, fid)
            try:
                self.refresh_battery(dev_id)
            except Exception as exc:
                log.debug("battery refresh after event failed: %s", exc)
            for hook in list(self.on_resumed):
                try:
                    hook(dev_id)
                except Exception:
                    log.exception("resume hook failed for %s", dev_id)

    # ---- receiver pairing ---------------------------------------------------
    def open_pairing(self, rcv_id, timeout=60):
        """Blocking: open the receiver's pairing lock. Raises when the write fails (the
        state then stays as it was). Returns the pairing state."""
        r = self.receivers[rcv_id]
        if r.pairing_index is None:
            raise hidpp.FeatureNotSupported(hidpp.F_RECEIVER_PAIRING)
        hidpp.pairing_open(self._fds[r.node.path], r.pairing_index, timeout)
        return self._set_pairing(r, pairing_state(r.id, "STARTED"))

    def cancel_pairing(self, rcv_id):
        """Blocking: close the pairing lock. Raises when the write fails."""
        r = self.receivers[rcv_id]
        if r.pairing_index is None:
            raise hidpp.FeatureNotSupported(hidpp.F_RECEIVER_PAIRING)
        hidpp.pairing_cancel(self._fds[r.node.path], r.pairing_index)
        return self._set_pairing(r, pairing_state(r.id, "CANCELLED"))

    def _set_pairing(self, r, state, passkey=None, events=None):
        r.pairing = state
        for hook in list(self.on_pairing):
            try:
                hook(r.id, state, passkey, events)
            except Exception:
                log.exception("pairing hook failed for %s", r.id)
        return state

    def _on_pairing_event(self, node, feature_index, function, params):
        """Reader thread: the receiver reported pairing progress."""
        r = next((x for x in self.receivers.values()
                  if x.node.path == node.path and x.pairing_index == feature_index), None)
        if r is None:
            return
        if function == 1:                                  # passkey to show the user
            digits = bytes(params).rstrip(b"\0").decode("ascii", "replace")
            self._set_pairing(r, pairing_state(r.id, "PASSKEY_DISPLAYED"), passkey=digits)
            return
        if function != 0 or len(params) < 3:
            return
        status, error, slot = params[0], params[1], params[2]
        if status == 1:
            self._set_pairing(r, pairing_state(r.id, "STARTED"))
        elif status == 0:
            if r.pairing["state"] in ("STARTED", "PASSKEY_DISPLAYED"):
                self._set_pairing(r, pairing_state(r.id, "CANCELLED"))
        elif status == 2:
            events = self.rescan()
            dev = next((d for d in self.devices.values()
                        if d.node.path == r.node.path and d.index == slot), None)
            basic = None if dev is None else {"id": dev.id, "displayName": dev.name,
                                              "deviceType": dev.dtype, "pid": dev.pid}
            self._set_pairing(r, pairing_state(r.id, "PAIRED", slot=slot, device=basic), events=events)
        elif status == 3:
            log.warning("receiver %s: pairing failed, code %d", r.id, error)
            self._set_pairing(r, pairing_state(r.id, "FAILED", error="UNKNOWN_PAIRING_ERROR"))

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

    def dpi_state(self, dev_id):
        d = self.devices.get(dev_id)
        fd = self._fds.get(d.node.path)
        dpi = hidpp.get_dpi(fd, d.index)
        return {"@type": TYPE + "logi.protocol.mouse.Dpi.State", "deviceId": dev_id, "dpi": int(dpi)}

    def dpi_table(self, dev_id):
        d = self.devices.get(dev_id)
        fd = self._fds.get(d.node.path)
        info = hidpp.get_dpi_list(fd, d.index)   # expect {levels, default, current}
        levels = info.get("levels") if isinstance(info, dict) else list(info or [])
        return {"@type": TYPE + "logi.protocol.mouse.Dpi.Table",
                "levels": [int(x) for x in levels],
                "defaultDpi": int(info.get("default", 0)) if isinstance(info, dict) else 0,
                "activeDpi": int(info.get("current", 0)) if isinstance(info, dict) else 0}

    def set_dpi(self, dev_id, dpi):
        d = self.devices.get(dev_id)
        fd = self._fds.get(d.node.path)
        hidpp.set_dpi(fd, d.index, int(dpi))

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

    def set_divert(self, dev_id, cid, divert):
        """Blocking: divert a control to software or hand it back; reads the device back and
        raises when it did not take. Writes only when the device is not already as asked."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        d.button_feature = hidpp.feature_index(fd, d.index, hidpp.F_REPROG_CONTROLS)
        if hidpp.get_cid_diverted(fd, d.index, cid) != bool(divert):
            hidpp.set_cid_diverted(fd, d.index, cid, divert)

    def read_thumb(self, dev_id):
        """Blocking 0x2150 status read: {mode, invert, smooth}."""
        d = self.devices[dev_id]
        return hidpp.get_thumbwheel(self._fds[d.node.path], d.index)

    def write_thumb(self, dev_id, invert, smooth):
        """Blocking 0x2150 write (reporting mode untouched); returns the read-back status."""
        d = self.devices[dev_id]
        fd = self._fds[d.node.path]
        mode = hidpp.get_thumbwheel(fd, d.index)["mode"]
        return hidpp.set_thumbwheel(fd, d.index, invert, smooth, mode)

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
