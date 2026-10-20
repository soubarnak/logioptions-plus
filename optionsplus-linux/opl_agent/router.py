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

import copy
import json
import logging
import os

from . import profiles, resources, responses

log = logging.getLogger("opl.router")

TYPE = responses.TYPE


def _state_dir(explicit=None):
    if explicit:
        return explicit
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.environ.get("OPL_STATE_DIR") or os.path.join(base, "optionsplus-linux")


class Router:
    def __init__(self, registry, state_dir=None):
        self.reg = registry
        self.server = None          # set by main; used for change broadcasts
        self.config_path = os.path.join(_state_dir(state_dir), "configuration.json")
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
        if path == "/lps/endpoint/info_changed":
            return [("/lps/endpoint/info_changed",
                     {"@type": TYPE + "logi.protocol.lps.Endpoint.Information", "state": "LOADED"})]
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

        if verb == "GET" and path == "/v2/profiles":
            return "SUCCESS", profiles.profiles(self.reg.devices.values()), ""

        # user configuration: persisted, and broadcast on change like the real agent
        if path == "/configuration":
            if verb == "SET" and isinstance(payload, dict):
                self._set_config(payload)
            return "SUCCESS", self.config, ""

        # 2. static boot table
        if verb == "GET" and path in responses.BOOT:
            return "SUCCESS", responses.BOOT[path], ""
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
            log.info("unhandled SET %s accepted", path)
            return "SUCCESS", payload, ""
        return "NO_SUCH_PATH", None, ""

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
