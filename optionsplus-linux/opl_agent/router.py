"""Request router for the native agent.

Resolution order for a GET/SET:
  1. exact static boot route (responses.BOOT / responses.ERRORS / SET echo)
  2. device-scoped route (/devices, /battery, ...) resolved against the live
     Registry
  3. unknown SET: SUCCESS (UI sagas do not catch SET failures)
  4. otherwise NO_SUCH_PATH, which the UI treats as "feature absent" and skips.

SUBSCRIBE/UNSUBSCRIBE are handled by the IPC Server itself; this router only
sees GET/SET/REMOVE/OPTIONS.
"""

import asyncio
import copy
import json
import logging
import os

from . import applications, model, profiles, resources, responses, stubs, updates
from .runner import SubprocessRunner

log = logging.getLogger("opl.router")

TYPE = responses.TYPE


def _state_dir(explicit=None):
    if explicit:
        return explicit
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.environ.get("OPL_STATE_DIR") or os.path.join(base, "optionsplus-linux")


class Router:
    def __init__(self, registry, state_dir=None, runner=None, app_dirs=None,
                 updater=None, os_release_path=None):
        self.reg = registry
        self.updater = updater or updates.Updater()
        self.os_release_path = os_release_path or updates.OS_RELEASE
        self.runner = runner or SubprocessRunner()
        self.app_dirs = app_dirs         # None: system, per-user and Flatpak locations
        self.apps = None                 # last scan result; None until first scan
        self.server = None          # set by main; used for change broadcasts
        self.config_path = os.path.join(_state_dir(state_dir), "configuration.json")
        self.store = profiles.ProfileStore(_state_dir(state_dir))
        self.reg.on_discovered.append(self.apply_stored)
        self.reg.on_resumed.append(self.apply_stored)
        self.config = copy.deepcopy(responses.BOOT["/configuration"])
        try:
            with open(self.config_path) as fh:
                self.config.update(json.load(fh))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("ignoring unreadable %s: %s", self.config_path, exc)
        # dev diagnostic: OPL_REPLAY=<capture-derived json> overrides boot replies
        self.replay = None
        rp = os.environ.get("OPL_REPLAY")
        if rp and os.path.exists(rp):
            self.replay = json.load(open(rp))
            log.warning("REPLAY mode: %d captured routes", len(self.replay))

    def initial_state(self, path):
        """State the frontend's boot sagas expect pushed right after they SUBSCRIBE.

        The real agent, on a fresh subscription, immediately broadcasts the
        current value on several paths; the splash stepper and the device list
        advance on these pushes (devices arrive via /devices/state/changed, not
        from the /devices/list reply). Returns a list of (path, payload).
        """
        if path == "/configuration":
            return [("/configuration", self.config)]
        if path == "/permissions":
            return [("/permissions", responses.BOOT["/permissions"])]
        if path == "/updates/status":
            return [("/updates/status", self.updater.status())]
        if path == "/lps/endpoint/info_changed":
            return [("/lps/endpoint/info_changed",
                     {"@type": TYPE + "logi.protocol.lps.Endpoint.Information", "state": "LOADED"})]
        if path == "/applications/installed" and self.apps is not None:
            return [(path, applications.payload(self.apps))]
        if path == "/devices/state/changed":
            out = []
            for d in self.reg.devices.values():
                info = d.info()
                bs = self.reg.battery_status_for_info(d.id)
                if bs:
                    info["batteryStatus"] = bs
                info["@type"] = TYPE + "logi.protocol.devices.Device.Info"
                out.append(("/devices/state/changed", info))
            return out
        if path == "/battery/state/changed":
            return [(path, self.reg.battery(i)[0]) for i in self.reg.devices
                    if self.reg.devices[i].battery_cache]
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "battery" and parts[2:] == ["state", "changed"]:
            if parts[1] in self.reg.devices and self.reg.devices[parts[1]].battery_cache:
                return [(path, self.reg.battery(parts[1])[0])]
        return []

    async def handle(self, verb, path, payload):
        if self.replay is not None:
            r = self.replay.get(verb + " " + path)
            if r is not None:
                return r.get("code", "SUCCESS"), r.get("payload"), ""
        # 1. runtime resource packages (Logitech-supplied data dir)
        if verb == "GET" and path == "/resources/localized_packages":
            lang = (self.config.get("language") or {}).get("value", "en-US")
            return "SUCCESS", resources.localized_packages(payload, lang), ""

        if path == "/applications/scan/installed" and verb == "SET":
            await self._scan_applications(announce=True)
            return "SUCCESS", payload, ""
        if verb == "GET" and path in ("/applications/installed", "/applications/all"):
            if self.apps is None:
                await self._scan_applications(announce=False)
            return "SUCCESS", applications.payload(self.apps), ""

        if verb == "GET" and path == "/v2/profiles":
            return "SUCCESS", await self._profiles_payload(), ""

        if verb == "SET" and path.startswith("/v2/profiles/"):
            r = await self._profiles_set(path, payload)
            if r is not None:
                return r

        if verb == "GET" and path == "/system/info":
            return "SUCCESS", updates.system_info(self.os_release_path), ""
        if verb == "GET" and path == "/updates/info":
            return "SUCCESS", self.updater.info(), ""
        if verb == "GET" and path == "/updates/status":
            return "SUCCESS", self.updater.status(), ""
        if path == "/updates/periodic_check" and verb in ("GET", "SET"):
            if verb == "SET":
                code = self.updater.set_periodic_check(payload)
                if code != "SUCCESS":
                    return code, None, "intervalInSeconds must be a positive integer, checkPeriodically a boolean"
            return "SUCCESS", self.updater.periodic_check(), ""
        if path == "/updates/channel" and verb in ("GET", "SET"):
            if verb == "SET":
                code = self.updater.set_channel(payload)
                if code != "SUCCESS":
                    return code, None, "channel must be stable or testing"
            return "SUCCESS", self.updater.channel(), ""
        if verb == "SET" and path == "/updates/check_now":
            self.updater.request_check()
            return "SUCCESS", payload, ""
        if verb == "SET" and path in ("/updates/download", "/updates/install", "/updates/restart_now",
                                      "/updates/payload/download"):
            action = {"/updates/download": self.updater.download,
                      "/updates/install": self.updater.install,
                      "/updates/restart_now": self.updater.restart_now,
                      "/updates/payload/download": self.updater.download_payload}[path]
            code, what = action()
            return code, (payload if code == "SUCCESS" else None), what
        if verb == "GET" and path == "/updates/payload":
            return "SUCCESS", self.updater.payload_status(), ""

        # user configuration: persisted, and broadcast on change like the real agent
        if path == "/configuration":
            if verb == "SET" and isinstance(payload, dict):
                self._set_config(payload)
            return "SUCCESS", self.config, ""

        # 2. static boot table
        if verb == "GET" and path in responses.BOOT:
            return "SUCCESS", responses.BOOT[path], ""
        stub = stubs.lookup(verb, path)
        if stub is not None:
            return stub[0], stub[1], ""
        if verb == "SET" and path == "/system/open_settings":
            self._open_settings()
            return "SUCCESS", payload, ""
        if path in responses.ERRORS:
            return responses.ERRORS[path], None, ""
        if verb == "SET" and path in responses.SET_ECHO:
            return "SUCCESS", payload, ""

        # 2. device-scoped routes
        parts = path.strip("/").split("/")
        try:
            r = self._device_route(verb, parts, path, payload)
            if r is not None:
                return r
        except KeyError:
            return "INVALID_DEVICE", None, "unknown device id"
        except Exception as exc:  # a HID++ read failed; keep the UI alive
            log.warning("device route %s failed: %s", path, exc)
            return "FAULTED", None, str(exc)

        # Several boot sagas issue a SET without catching a failure; one
        # rejected SET cancels the whole saga tree. Accept unknown SETs.
        if verb == "SET":
            log.warning("unhandled SET %s accepted (feature not implemented)", path)
            return "SUCCESS", payload, ""
        return "NO_SUCH_PATH", None, ""

    # ---- profiles / pointer speed ---------------------------------------
    def _read_live(self):
        """Blocking: current DPI and range per device, for seeding cards."""
        live = {}
        for dev_id in list(self.reg.devices):
            try:
                live[dev_id] = {"dpi": self.reg.read_dpi(dev_id)["dpi"],
                                "range": self.reg.dpi_range(dev_id)}
            except Exception as exc:
                log.debug("no live DPI for %s: %s", dev_id, exc)
            try:
                live.setdefault(dev_id, {})["scroll"] = profiles.scroll_card(self.reg.read_scroll(dev_id))
            except Exception as exc:
                log.debug("no live scroll state for %s: %s", dev_id, exc)
            if self.reg.devices[dev_id].thumb_wheel:
                try:
                    live.setdefault(dev_id, {})["thumb"] = self.reg.read_thumb(dev_id)
                except Exception as exc:
                    log.debug("no live thumb wheel status for %s: %s", dev_id, exc)
        return live

    async def _profiles_payload(self):
        live = await asyncio.get_event_loop().run_in_executor(None, self._read_live)
        return profiles.profiles(self.reg.devices.values(), self.store, live)

    async def _broadcast_profiles(self):
        if self.server is not None:
            self.server.broadcast("/v2/profiles/update", await self._profiles_payload())

    def _device_for_slot(self, slot_id):
        for dev in self.reg.devices.values():
            if slot_id == profiles.mouse_settings_slot(dev.slot_prefix):
                return dev
        return None

    def _apply_pointer(self, dev, value):
        """Blocking: write DPI for slider `value`; returns the card read back."""
        rng = self.reg.dpi_range(dev.id)
        dpi = self.reg.write_dpi(dev.id, profiles.dpi_from_value(value, rng))
        return profiles.mouse_settings_card(profiles.value_from_dpi(dpi, rng), dpi)

    def _device_for_scroll_slot(self, slot_id):
        for dev in self.reg.devices.values():
            if slot_id == profiles.scroll_slot(dev.slot_prefix):
                return dev
        return None

    def _apply_scroll(self, dev, req):
        """Blocking: write the requested wheel settings; returns the card read back."""
        state = self.reg.write_scroll(dev.id, ratchet=req["ratchet"], threshold=req["threshold"],
                                      hires=req["hires"], invert=req["invert"])
        return profiles.scroll_card(state)

    def _thumb_device_for_slot(self, slot_id):
        for dev in self.reg.devices.values():
            if dev.thumb_wheel and slot_id == profiles.thumb_wheel_slot(dev.slot_prefix):
                return dev
        return None

    def _apply_thumb(self, dev, card):
        """Blocking: write thumb wheel direction/smooth from `card` over what the
        device reports now; returns the card read back (speed is not adjustable)."""
        invert, smooth = profiles.thumb_wheel_fields(card)
        cur = self.reg.read_thumb(dev.id)
        st = self.reg.write_thumb(dev.id, cur["invert"] if invert is None else invert,
                                  cur["smooth"] if smooth is None else smooth)
        return profiles.thumb_wheel_card(st["invert"], st["smooth"])

    def apply_stored(self, dev_id):
        """Blocking: reapply stored settings to a device (discovery, reconnect)."""
        dev = self.reg.devices.get(dev_id)
        if dev is None:
            return
        card = self.store.get(profiles.mouse_settings_slot(dev.slot_prefix))
        value = profiles.pointer_value(card) if card else None
        if value is not None:
            self._apply_pointer(dev, value)
        card = self.store.get(profiles.scroll_slot(dev.slot_prefix))
        req = profiles.scroll_request(card) if card else None
        if req is not None:
            try:
                self._apply_scroll(dev, req)
            except Exception as exc:
                log.warning("reapplying scroll wheel settings failed: %s", exc)
        tcard = self.store.get(profiles.thumb_wheel_slot(dev.slot_prefix)) if dev and dev.thumb_wheel else None
        if tcard is not None:
            try:
                self._apply_thumb(dev, tcard)
            except Exception as exc:
                log.warning("reapplying thumb wheel settings failed: %s", exc)

    async def _profiles_set(self, path, payload):
        loop = asyncio.get_event_loop()
        if path == "/v2/profiles/reset":
            return await self._profiles_reset(payload or {})
        payload = payload or {}
        entries = [op.get("set") for op in payload.get("ops") or [] if op.get("set")]
        if "assignment" in payload:
            entries.append(payload)
        handled = None
        for entry in entries:
            asg = entry.get("assignment") or {}
            slot_id = asg.get("slotId", "")
            requested = asg.get("card") or entry.get("card") or {}
            dev = self._device_for_slot(slot_id)
            value = profiles.pointer_value(requested)
            if dev is not None and value is not None:
                apply_fn, arg, what = self._apply_pointer, value, "pointer speed"
            else:
                dev = self._device_for_scroll_slot(slot_id)
                req = profiles.scroll_request(requested)
                if dev is not None and req is not None:
                    apply_fn, arg, what = self._apply_scroll, req, "scroll wheel"
                else:
                    dev = self._thumb_device_for_slot(slot_id)
                    if dev is None:
                        continue
                    apply_fn, arg, what = self._apply_thumb, requested, "thumb wheel"
            try:
                card = await loop.run_in_executor(None, apply_fn, dev, arg)
            except Exception as exc:
                log.warning("%s write failed: %s", what, exc)
                return "FAULTED", None, "device write failed: %s" % exc
            if card is None:
                continue
            self.store.set(asg["slotId"], card)
            asg["card"], asg["cardId"] = card, card["id"]
            handled = payload
        if handled is None:
            return None
        await self._broadcast_profiles()
        return "SUCCESS", handled, ""

    async def _profiles_reset(self, info):
        loop = asyncio.get_event_loop()
        for dev in list(self.reg.devices.values()):
            if info.get("deviceId") and info["deviceId"] != dev.id:
                continue
            self.store.remove(profiles.mouse_settings_slot(dev.slot_prefix))
            self.store.remove(profiles.scroll_slot(dev.slot_prefix))
            self.store.remove(profiles.thumb_wheel_slot(dev.slot_prefix))
            try:
                await loop.run_in_executor(None, self._apply_defaults, dev)
            except Exception as exc:
                log.warning("applying defaults failed: %s", exc)
                return "FAULTED", None, "device write failed: %s" % exc
        await self._broadcast_profiles()
        return "SUCCESS", info, ""

    def _apply_defaults(self, dev):
        if dev.thumb_wheel:
            self.reg.write_thumb(dev.id, False, False)
        value = profiles.default_pointer_value(dev.depot)
        if value is not None:
            self._apply_pointer(dev, value)
        else:
            self.reg.write_dpi(dev.id, self.reg.read_dpi(dev.id)["default"])

    def _open_settings(self):
        """Open the desktop's mouse settings if a known tool is installed."""
        tools = [["gnome-control-center", "mouse"], ["systemsettings", "kcm_mouse"]]
        if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
            tools.reverse()
        for argv in tools:
            if self.runner.which(argv[0]):
                self.runner.run(argv)
                return
        log.info("no known desktop mouse settings tool; nothing to open")

    async def _scan_applications(self, announce):
        """Scan off the event loop. An unreadable tree or a scan error counts
        as "nothing found": the completion broadcast is sent regardless, so the
        UI's scanning spinner always stops."""
        dirs = self.app_dirs if self.app_dirs is not None else applications.default_dirs()
        try:
            self.apps = await asyncio.get_event_loop().run_in_executor(None, applications.scan, dirs)
        except Exception as exc:
            log.warning("application scan failed: %s", exc)
            self.apps = []
        if announce and self.server is not None:
            # after the SET reply, which the caller is about to send
            asyncio.get_event_loop().call_soon(self._announce_applications)

    def _announce_applications(self):
        for path in ("/applications/installed", "/applications"):
            self.server.broadcast(path, applications.payload(self.apps))

    def _set_config(self, payload):
        for k, v in payload.items():
            if k != "@type":
                self.config[k] = v
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            tmp = self.config_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({k: v for k, v in self.config.items() if k != "@type"}, fh, indent=1)
            os.replace(tmp, self.config_path)
        except OSError as exc:
            log.warning("cannot save configuration: %s", exc)
        if self.server is not None:
            self.server.broadcast("/configuration", self.config)

    # ---- device routes ----------------------------------------------------
    def _device_route(self, verb, parts, path, payload):
        reg = self.reg
        if parts[:2] == ["devices", "list"]:
            infos = [d.info() for d in reg.devices.values()]
            return "SUCCESS", {"@type": TYPE + "logi.protocol.devices.Device.Info.List",
                               "deviceInfos": infos}, ""
        if parts[:3] == ["devices", "ever_connected", "list"]:
            return "SUCCESS", {"@type": TYPE + "logi.protocol.devices.DevicePermanentDatas",
                               "devices": [d.permanent() for d in reg.devices.values()]}, ""

        # GET /devices/resources {id, deviceModel}: images and metadata
        if parts == ["devices", "resources"]:
            dev = reg.devices.get((payload or {}).get("id", ""))
            if dev is None:
                return "INVALID_DEVICE", None, "unknown device id"
            return "SUCCESS", dev.resources(), ""

        # /devices/<id>/<sub>
        if parts[0] == "devices" and len(parts) >= 3:
            dev_id, sub = parts[1], parts[2]
            if dev_id not in reg.devices:
                return "NO_SUCH_PATH", None, ""
            if sub == "info":
                info = reg.devices[dev_id].info()
                bs = reg.battery_status_for_info(dev_id)
                if bs:
                    info["batteryStatus"] = bs
                return "SUCCESS", info, ""
            if sub == "easy_switch":
                return "NO_SUCH_PATH", None, ""   # matches real agent for BLE devices
            return "NO_SUCH_PATH", None, ""

        # Firmware update check: report "no update" with the current version.
        # Real DFU is out of scope (needs Logitech's signed payloads).
        if parts[:2] == ["dfu", "update"] and parts[2:] == ["available"]:
            return "SUCCESS", {"@type": TYPE + "google.protobuf.BoolValue", "value": False}, ""
        if parts[0] == "dfu" and len(parts) == 3 and parts[2] == "info":
            dev = reg.devices.get(parts[1])
            if dev is None:
                return "NO_SUCH_PATH", None, ""
            return "SUCCESS", {"@type": TYPE + "logi.protocol.dfu.Info",
                               "requireFwUpdate": False, "dfuResult": "SUCCESS",
                               "eligibleVersion": model.format_firmware(dev.fw)}, ""

        # /battery/<id>/state
        if parts[0] == "battery" and len(parts) >= 3 and parts[2] == "state":
            dev_id = parts[1]
            if dev_id not in reg.devices:
                return "NO_SUCH_PATH", None, ""
            pl, _ = reg.battery(dev_id)
            return "SUCCESS", pl, ""

        # /mouse/<id>/swap  (per-device button swap mirrors the global default)
        if parts[0] == "mouse" and len(parts) >= 3 and parts[1] in reg.devices:
            return "SUCCESS", {"@type": TYPE + "logi.protocol.mouse.MouseButtonSwap", "state": "LEFT"}, ""

        return None
