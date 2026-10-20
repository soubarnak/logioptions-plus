"""Request router for the native agent.

Resolution order for a GET/SET:
  1. exact static boot route (responses.BOOT / responses.ERRORS / SET echo)
  2. device-scoped route (/devices, /battery, ...) resolved against the live
     Registry
  3. unknown SET: NOT_SUPPORTED (SETs the boot sagas send without catching are in stubs.py)
  4. otherwise NO_SUCH_PATH, which the UI treats as "feature absent" and skips.

SUBSCRIBE/UNSUBSCRIBE are handled by the IPC Server itself; this router only
sees GET/SET/REMOVE/OPTIONS.
"""

import asyncio
import functools
import copy
import json
import logging
import os
import threading

from . import actions, applications, easy_switch, engine as input_engine, focus, model, profiles, resources, responses, stubs, swap, updates, wheel_relay
from .runner import SubprocessRunner

DEFAULT_PROFILE = profiles.DEFAULT_PROFILE_ID

log = logging.getLogger("opl.router")

TYPE = responses.TYPE


def _state_dir(explicit=None):
    if explicit:
        return explicit
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.environ.get("OPL_STATE_DIR") or os.path.join(base, "optionsplus-linux")


class Router:
    def __init__(self, registry, state_dir=None, runner=None, app_dirs=None,
                 updater=None, os_release_path=None, input_factory=None):
        self.reg = registry
        self.updater = updater or updates.Updater()
        self.os_release_path = os_release_path or updates.OS_RELEASE
        self.runner = runner or SubprocessRunner()
        self.app_dirs = app_dirs         # None: system, per-user and Flatpak locations
        self.apps = None                 # last scan result; None until first scan
        # Everything that decides which buttons are diverted runs under this lock, one at a time:
        # assignments, focus switches, resets and reapplying after a wake. Reentrant: they call each other.
        self._switch_lock = threading.RLock()
        self.loop = None                 # the event loop, set by main; worker threads schedule broadcasts on it
        self._focused = None             # the window that has the focus, when known
        self.window_probe = None         # set while the tracker idles (no profile): reads the focused window on demand
        self.server = None          # set by main; used for change broadcasts
        self._pushes = set()             # broadcasts queued behind a reply (kept so they are not collected)
        self.config_path = os.path.join(_state_dir(state_dir), "configuration.json")
        self.store = profiles.ProfileStore(_state_dir(state_dir))
        self.engine = input_engine.InputEngine(self.reg, self.store, input_factory,
                                             os.path.join(_state_dir(state_dir), "actions.toml"))
        self.relay = wheel_relay.WheelRelay(self.reg, self.engine.sink,
                                            enabled=os.environ.get("OPL_WHEEL_RELAY", "1") != "0")
        self.relay.thumb_owned = self.engine.owns_thumb     # a thumb action card outranks thumb speed
        self.engine.held_elsewhere = lambda dev_id, key: (
            key == profiles.THUMB_KEY and self.relay.holds(dev_id, wheel_relay.THUMB))
        self.engine.on_stale.append(self._card_gone)
        self.button_swap = swap.ButtonSwap(self.reg, _state_dir(state_dir), self.runner)
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
            for r in self.reg.receivers.values():
                out.append(("/devices/state/changed",
                            {**r.info(), "@type": TYPE + "logi.protocol.devices.Device.Info"}))
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
            reply = resources.localized_packages(payload, lang)
            for pkg in reply["packages"]:
                if pkg["name"] == actions.STRING_PACKAGE:
                    pkg["strings"] = actions.relabel_strings(pkg["strings"])
            return "SUCCESS", reply, ""

        # action picker catalogue, from the fetched depot through the Linux filter
        if verb == "GET" and path == "/rac":
            dev = self.reg.devices.get((payload or {}).get("deviceId")) if isinstance(payload, dict) else None
            return "SUCCESS", actions.rac({"slotPrefix": dev.slot_prefix} if dev else None), ""
        if verb == "GET" and path in actions.ROUTES:
            return "SUCCESS", actions.ROUTES[path](), ""

        if path == "/applications/scan/installed" and verb == "SET":
            await self._scan_applications(announce=True)
            return "SUCCESS", payload, ""
        if verb == "GET" and path in ("/applications/installed", "/applications/all"):
            if self.apps is None:
                await self._scan_applications(announce=False)
            return "SUCCESS", applications.payload(self.apps), ""

        if verb == "GET" and path == "/v2/profiles":
            return "SUCCESS", await self._profiles_payload(), ""

        if verb == "SET" and path == "/v2/profiles":
            return await self._profiles_update(payload or {})

        if verb == "SET" and (path == "/v2/assignment" or path.startswith("/v2/profiles/")):
            r = await self._profiles_set(path, payload)
            if r is not None:
                return r

        if verb == "SET" and path == "/device/restore":
            return await self._device_restore(payload)

        parts = path.strip("/").split("/")
        if verb in ("GET", "SET") and parts[0] == "mouse" and parts[-1] == "swap" \
                and (len(parts) == 2 or len(parts) == 3 and parts[1] == "global"):
            return await self._swap_route(verb, payload)

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
                if code == "NOT_SUPPORTED":
                    return code, None, "only the stable channel exists; pre-releases are not offered"
                if code != "SUCCESS":
                    return code, None, "channel must be stable"
            return "SUCCESS", self.updater.channel(), ""
        if path == "/dfu/config/update" and verb in ("GET", "SET"):
            if verb == "SET" and self.updater.set_dfu_config(payload) != "SUCCESS":
                return "INVALID_ARG", None, "autoInstallEnabled must be a boolean"
            return "SUCCESS", self.updater.dfu_config(), ""
        if verb == "SET" and path == "/updates/check_now":
            self.updater.request_check()
            return "SUCCESS", payload, ""
        if verb == "SET" and path in ("/updates/download", "/updates/install", "/updates/restart_now"):
            action = {"/updates/download": self.updater.download,
                      "/updates/install": self.updater.install,
                      "/updates/restart_now": self.updater.restart_now}[path]
            code, what = action()
            return code, (payload if code == "SUCCESS" else None), what

        # user configuration: persisted, and broadcast on change like the real agent
        if path == "/configuration":
            if verb == "SET" and isinstance(payload, dict):
                self._set_config(payload)
            return "SUCCESS", self.config, ""

        # 2. static boot table
        if verb == "GET" and path in responses.BOOT:
            return "SUCCESS", responses.BOOT[path], ""
        stub = stubs.lookup(verb, path, payload)
        if stub is not None:
            return stub[0], stub[1], ""
        if verb == "SET" and path == "/system/open_settings":
            self._open_settings()
            return "SUCCESS", payload, ""
        if path in responses.ERRORS:
            return responses.ERRORS[path], None, ""

        # 2. device-scoped routes
        parts = path.strip("/").split("/")
        if parts[0] == "devices" and len(parts) >= 3 and parts[2] == "pair" and parts[1] in self.reg.receivers:
            return await self._pair_route(verb, parts[1], "/".join(parts[3:]), payload)
        if parts[0] == "devices" and len(parts) >= 3 and parts[2] == "discovery" and parts[1] in self.reg.receivers:
            return await self._discovery_route(verb, parts[1], "/".join(parts[3:]), payload)
        if parts[0] == "devices" and len(parts) >= 3 and parts[2] == "locking" and parts[1] in self.reg.receivers \
                and verb == "GET":
            return "SUCCESS", self.reg.locking_state(self.reg.receivers[parts[1]]), ""
        if parts[0] == "devices" and len(parts) >= 3 and parts[2] in ("pair", "discovery", "locking") \
                and parts[1] not in self.reg.receivers:
            return "NO_SUCH_PATH", None, ""
        if parts[:1] == ["devices"] and len(parts) in (3, 4) and parts[2] == "easy_switch" \
                and parts[3:] in ([], ["change"]):
            return await self._easy_switch(verb, parts[1], payload)
        if parts[0] == "change_host" and len(parts) == 3 and parts[2] == "host":
            return await self._easy_switch(verb, parts[1], payload, switch_only=True)
        try:
            r = self._device_route(verb, parts, path, payload)
            if r is not None:
                return r
        except KeyError:
            return "INVALID_DEVICE", None, "unknown device id"
        except Exception as exc:  # a HID++ read failed; keep the UI alive
            log.warning("device route %s failed: %s", path, exc)
            return "FAULTED", None, str(exc)

        # The SETs boot sagas send without catching a failure are in stubs.py; any other
        # SET is a feature we do not have, and says so instead of pretending to succeed.
        if verb == "SET":
            log.warning("unhandled SET %s refused (feature not implemented)", path)
            return "NOT_SUPPORTED", None, "not supported on Linux"
        return "NO_SUCH_PATH", None, ""

    # ---- receiver pairing -------------------------------------------------
    async def _pair_route(self, verb, rcv_id, sub, payload=None):
        """/devices/<receiver>/pair: SET opens (payload may carry deviceAddress and
        timeoutSeconds), GET reads, SET .../cancel cancels. A write the receiver rejects is
        FAULTED and leaves the pairing state as it was."""
        loop = asyncio.get_running_loop()
        payload = payload if isinstance(payload, dict) else {}
        if sub == "" and verb == "GET":
            return "SUCCESS", self.reg.receivers[rcv_id].pairing, ""
        if (sub, verb) == ("", "SET"):
            kwargs = {"address": payload.get("deviceAddress") or None}
            if payload.get("timeoutSeconds"):
                kwargs["timeout"] = int(payload["timeoutSeconds"])
            fn = functools.partial(self.reg.open_pairing, rcv_id, **kwargs)
        elif (sub, verb) == ("cancel", "SET"):
            fn = functools.partial(self.reg.cancel_pairing, rcv_id)
        else:
            return "NO_SUCH_PATH", None, ""
        try:
            return "SUCCESS", await loop.run_in_executor(None, fn), ""
        except Exception as exc:
            log.warning("pairing request on %s failed: %s", rcv_id, exc)
            return "FAULTED", None, "pairing request failed: %s" % exc

    async def _discovery_route(self, verb, rcv_id, sub, payload):
        """/devices/<receiver>/discovery: SET starts looking for advertising Bolt devices
        (payload timeoutSeconds), GET .../status reads; SET .../cancel stops."""
        loop = asyncio.get_running_loop()
        r = self.reg.receivers[rcv_id]
        if (sub, verb) == ("status", "GET"):
            return "SUCCESS", self.reg.discovery_state(r), ""
        if (sub, verb) == ("", "SET"):
            timeout = int((payload or {}).get("timeoutSeconds") or 30)
            fn = functools.partial(self.reg.open_discovery, rcv_id, timeout)
        elif (sub, verb) == ("cancel", "SET"):
            fn = functools.partial(self.reg.cancel_pairing, rcv_id)
        else:
            return "NO_SUCH_PATH", None, ""
        try:
            await loop.run_in_executor(None, fn)
            return "SUCCESS", self.reg.discovery_state(r), ""
        except Exception as exc:
            log.warning("discovery request on %s failed: %s", rcv_id, exc)
            return "FAULTED", None, "discovery request failed: %s" % exc

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
                live.setdefault(dev_id, {})["scroll"] = self._scroll_card(self.reg.devices[dev_id])
            except Exception as exc:
                log.debug("no live scroll state for %s: %s", dev_id, exc)
            if self.reg.devices[dev_id].thumb_wheel:
                try:
                    live.setdefault(dev_id, {})["thumb"] = self._thumb_state(self.reg.devices[dev_id])
                except Exception as exc:
                    log.debug("no live thumb wheel status for %s: %s", dev_id, exc)
        return live

    async def _profiles_payload(self):
        live = await asyncio.get_running_loop().run_in_executor(None, self._read_live)
        if self.apps is None:
            await self._scan_applications(announce=False)
        return profiles.profiles(self.reg.devices.values(), self.store, live, self.engine.active_app, self.apps)

    async def _broadcast_profiles(self, removed=()):
        """Push the profiles as a profiles_v2.Update, the shape the UI's UPDATE_PROFILES reducer
        applies (a Profiles message is ignored). `removed` are profile ids the UI must drop."""
        if self.server is not None:
            update = {"@type": TYPE + "logi.protocol.profiles_v2.Update",
                      "updatedProfiles": (await self._profiles_payload()).get("profiles", [])}
            if removed:
                update["removedProfileIds"] = list(removed)
            self.server.broadcast("/v2/profiles/update", update)

    def _broadcast_after_reply(self):
        """Push the profiles once the current reply is out. The UI drops a profiles push for a
        slot whose own SET is still pending, so a push sent before the reply leaves the device
        picture one change behind."""
        task = asyncio.get_running_loop().create_task(self._broadcast_profiles())
        self._pushes.add(task)
        task.add_done_callback(self._pushes.discard)

    def _schedule_broadcast(self):
        """Any thread: push the profiles to the UI from the event loop. Never waits."""
        loop = self.loop
        if loop is None or self.server is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast_profiles(), loop)
        except RuntimeError:
            pass                         # the loop is closed: the agent is stopping

    def _card_gone(self, dev_id, slot_id, app, message):
        """Engine hook: a stored card cannot run any more. Forget it, so the mouse and what the UI
        shows agree (the button is native again), and tell the UI."""
        log.warning("dropping the stored card of %s%s: %s", slot_id, " for " + app if app else "", message)
        self.store.discard(slot_id, app)
        self._schedule_broadcast()

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

    def _scroll_card(self, dev):
        """Blocking: the scroll wheel card for the mouse as it is now, with the relay's speed."""
        ax = self.relay.axis(dev, wheel_relay.WHEEL)
        speed = profiles.shown_speed(ax.speed, self.relay.available(dev, wheel_relay.WHEEL))
        state = self.reg.read_scroll(dev.id)
        if ax.diverted and state["wheel"] is not None:      # the device shows the diverted bits, not the user's
            state["wheel"] = {**state["wheel"], "hires": ax.smooth, "invert": ax.invert}
        return profiles.scroll_card(state, speed)

    def _thumb_state(self, dev):
        """Blocking: {invert, smooth, speed} of the thumb wheel as the user has it."""
        ax = self.relay.axis(dev, wheel_relay.THUMB)
        st = self.reg.read_thumb(dev.id)
        if ax.diverted:
            st = {**st, "invert": ax.invert, "smooth": ax.smooth}
        return {**st, "speed": profiles.shown_speed(ax.speed, self.relay.available(dev, wheel_relay.THUMB))}

    def _apply_scroll(self, dev, req):
        """Blocking: write the requested wheel settings; returns the card read back."""
        if self.relay.available(dev, wheel_relay.WHEEL):
            self.relay.set_wheel(dev, speed=req["speed"], invert=req["invert"], smooth=req["hires"])
            self.reg.write_scroll(dev.id, ratchet=req["ratchet"], threshold=req["threshold"])
            return self._scroll_card(dev)
        self.relay.release(dev, wheel_relay.WHEEL)         # no relay now: nothing may stay diverted
        state = self.reg.write_scroll(dev.id, ratchet=req["ratchet"], threshold=req["threshold"],
                                      hires=req["hires"], invert=req["invert"])
        prior = self.store.get(profiles.scroll_slot(dev.slot_prefix))
        kept = (profiles.scroll_request(prior) or {}).get("speed") if prior else None
        return profiles.scroll_card(state, kept or 0)      # the speed chosen before stays stored

    def _thumb_device_for_slot(self, slot_id):
        for dev in self.reg.devices.values():
            if dev.thumb_wheel and slot_id == profiles.thumb_wheel_slot(dev.slot_prefix):
                return dev
        return None

    def _apply_thumb(self, dev, card):
        """Blocking: write thumb wheel direction/smooth from `card` over what the
        device reports now; returns the card read back (speed is not adjustable)."""
        invert, smooth = profiles.thumb_wheel_fields(card)
        if self.relay.available(dev, wheel_relay.THUMB):
            self.relay.set_thumb(dev, speed=profiles.thumb_speed(card), invert=invert, smooth=smooth)
            st = self._thumb_state(dev)
            return profiles.thumb_wheel_card(st["invert"], st["smooth"], st["speed"])
        self.relay.release(dev, wheel_relay.THUMB)
        cur = self.reg.read_thumb(dev.id)
        st = self.reg.write_thumb(dev.id, cur["invert"] if invert is None else invert,
                                  cur["smooth"] if smooth is None else smooth)
        prior = self.store.get(profiles.thumb_wheel_slot(dev.slot_prefix))
        return profiles.thumb_wheel_card(st["invert"], st["smooth"],
                                         (profiles.thumb_speed(prior) if prior else None) or 0)

    def apply_stored(self, dev_id):
        """Blocking: reapply stored settings to a device (discovery, reconnect)."""
        with self._switch_lock:
            self._apply_stored(dev_id)

    def _apply_stored(self, dev_id):
        dev = self.reg.devices.get(dev_id)
        if dev is None:
            return
        self.relay.probe(dev)               # which speed controls the device offers, before it is listed
        card = self.store.get(profiles.mouse_settings_slot(dev.slot_prefix))
        value = profiles.pointer_value(card) if card else None
        if value is not None:
            try:
                self._apply_pointer(dev, value)
            except Exception as exc:
                log.warning("reapplying pointer speed failed: %s", exc)
        card = self.store.get(profiles.scroll_slot(dev.slot_prefix))
        req = profiles.scroll_request(card) if card else None
        if req is not None:
            try:
                self._apply_scroll(dev, req)
            except Exception as exc:
                log.warning("reapplying scroll wheel settings failed: %s", exc)
        try:
            self.engine.sync(dev)
        except Exception as exc:
            log.warning("reapplying button diversions failed: %s", exc)
        scard = self.store.get(profiles.scroll_slot(dev.slot_prefix))
        stored = {wheel_relay.WHEEL: (profiles.scroll_request(scard) or {}).get("speed") if scard else None,
                  wheel_relay.THUMB: None}
        tcard = self.store.get(profiles.thumb_wheel_slot(dev.slot_prefix)) if dev.thumb_wheel else None
        stored[wheel_relay.THUMB] = profiles.thumb_speed(tcard) if tcard else None
        self.relay.recover(dev, stored)     # a diversion a crashed agent left that no stored speed asks for
        if tcard is not None:
            try:
                self._apply_thumb(dev, tcard)
            except Exception as exc:
                log.warning("reapplying thumb wheel settings failed: %s", exc)
        self._settle_thumb(dev)
        self.relay.reassert_later(dev.id)
        try:
            self.button_swap.apply_stored(dev)
        except Exception as exc:
            log.warning("reapplying the button swap failed: %s", exc)

    async def _swap_route(self, verb, payload):
        if verb == "SET":
            wanted = (payload or {}).get("state") == swap.STATE_RIGHT
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.button_swap.set, wanted)
            except swap.SwapUnsupported as exc:
                # the UI ignores the reply text and leaves the toggle where the user put it:
                # push the real state back and say why on the desktop
                self._broadcast_swap()
                self._notify("Cannot swap the mouse buttons", str(exc))
                return "NOT_SUPPORTED", None, str(exc)
            except Exception as exc:
                log.warning("swapping the buttons failed: %s", exc)
                return "FAULTED", None, "could not swap the buttons: %s" % exc
        return "SUCCESS", {"@type": TYPE + "logi.protocol.mouse.MouseButtonSwap",
                           "state": self.button_swap.state}, ""

    def _broadcast_swap(self):
        if self.server is not None:
            self.server.broadcast("/mouse/global/swap", {"@type": TYPE + "logi.protocol.mouse.MouseButtonSwap",
                                                         "state": self.button_swap.state})

    def _notify(self, summary, body):
        """A desktop notification, when notify-send is installed; never an error."""
        if self.runner.which("notify-send"):
            self.runner.run(["notify-send", "--app-name=Options+", summary, body])

    async def _app_for(self, profile_id):
        """The installed application a non-default profile id is for, else "" (a stale or made-up id)."""
        if self.apps is None:
            await self._scan_applications(announce=False)
        app = self.store.app_of(profile_id)
        if self.store.has_profile(app) or any(a.get("applicationId") == app for a in self.apps):
            return app
        return ""

    async def _profiles_update(self, update):
        """SET /v2/profiles: the UI creates profiles (each for an application, under an id of its
        own) and removes them."""
        if self.apps is None:
            await self._scan_applications(announce=False)
        installed = {a.get("applicationId") for a in self.apps}
        changed = False
        removed = []
        for prof in update.get("updatedProfiles") or []:
            pid = prof.get("id") or ""
            app = prof.get("applicationId") or pid
            if DEFAULT_PROFILE in (pid, app):
                continue
            if not pid or (app not in installed and not self.store.has_profile(app)):
                return "INVALID_ARG", None, "no installed application %r" % app
            try:
                self.store.link(pid, app)
            except OSError as exc:
                return "FAULTED", None, "cannot save the profile: %s" % exc
            changed = True
        for pid in update.get("removedProfileIds") or []:
            if pid == DEFAULT_PROFILE:
                continue
            app = self.store.app_of(pid)
            if not self.store.has_profile(app):
                continue
            code, _, msg = await self._app_profile_reset(app, {}, unlink=pid, broadcast=False)
            if code != "SUCCESS":
                if changed:
                    await self._broadcast_profiles(removed)
                return code, None, msg
            removed.append(pid)
            changed = True
        if changed:
            await self._broadcast_profiles(removed)
        return "SUCCESS", update, ""

    async def _profiles_set(self, path, payload):
        loop = asyncio.get_running_loop()
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
            app = entry.get("profileId") or DEFAULT_PROFILE
            if app == DEFAULT_PROFILE:
                app = None
            else:
                app = await self._app_for(app)
                if not app:
                    return await self._profiles_failed(handled, "INVALID_ARG", "no such profile")
            dev = self._device_for_slot(slot_id)
            value = profiles.pointer_value(requested)
            if app is not None or dev is None or value is None:
                dev = self._device_for_scroll_slot(slot_id)
                req = profiles.scroll_request(requested)
            if app is None and dev is not None and value is not None:
                apply_fn, arg, what = self._apply_pointer, value, "pointer speed"
            elif app is None and dev is not None and req is not None:
                apply_fn, arg, what = self._apply_scroll, req, "scroll wheel"
            else:
                dev = self._thumb_device_for_slot(slot_id) if app is None else None
                if dev is None:
                    target = self._button_target(slot_id)
                    if target is None and app is not None and requested:
                        return "NOT_SUPPORTED", None, "only button assignments can differ per application"
                    if target is None:
                        return await self._profiles_failed(handled, "INVALID_ARG", "no such slot %s" % slot_id)
                    if not requested:
                        return await self._profiles_failed(handled, "INVALID_ARG", "no card")
                    try:
                        await loop.run_in_executor(None, self._assign_button, *target, slot_id, requested, app)
                    except input_engine.Unsupported as exc:
                        return await self._profiles_failed(handled, "NOT_SUPPORTED", str(exc))
                    except Exception as exc:
                        log.warning("button write failed: %s", exc)
                        what = str(exc) if isinstance(exc, input_engine.NoVirtualDevice) \
                            else "device write failed: %s" % exc
                        return await self._profiles_failed(handled, "FAULTED", what)
                    asg["card"], asg["cardId"] = requested, requested.get("id", asg.get("cardId", ""))
                    handled = payload
                    continue
                apply_fn, arg, what = self._apply_thumb, requested, "thumb wheel"
            try:
                card = await loop.run_in_executor(None, apply_fn, dev, arg)
            except Exception as exc:
                log.warning("%s write failed: %s", what, exc)
                return await self._profiles_failed(handled, "FAULTED", "device write failed: %s" % exc)
            if card is None:
                continue
            try:
                self.store.set(asg["slotId"], card)
            except OSError as exc:
                return await self._profiles_failed(handled, "FAULTED", "cannot save the setting: %s" % exc)
            asg["card"], asg["cardId"] = card, card["id"]
            handled = payload
        if handled is None:
            if not entries:
                return None
            return "INVALID_ARG", None, "nothing to apply"
        self._broadcast_after_reply()
        return "SUCCESS", handled, ""

    async def _profiles_failed(self, stored, code, what):
        """Reply to a batch that stopped part way: what the earlier operations stored stays, so
        the UI is told about it rather than left showing the old profiles."""
        if stored is not None:
            await self._broadcast_profiles()
        return code, None, what

    def _button_target(self, slot_id):
        """(device, control key) for a button or thumb wheel action slot of a connected device, else None."""
        for dev in self.reg.devices.values():
            key = profiles.control_key(slot_id, dev.slot_prefix)
            if key is not None:
                return dev, key
        return None

    def _assign_button(self, dev, cid, slot_id, card, app=None):
        """Blocking: make the button do `card` (the depot default card hands it back to the
        mouse), then keep the choice for the default profile or application `app`. Only a
        choice that is in force now touches the mouse: the active application's own, or the
        default's where the active application has no card of its own for the button. Raises,
        storing nothing, when the device cannot do it."""
        with self._switch_lock:
            default = next((a["cardId"] for a in profiles.default_assignments(dev.slot_prefix, dev.depot)
                            if a["slotId"] == slot_id), None)
            active = self.engine.active_app
            in_force = (app == active) if app is not None else slot_id not in self.store.app_cards(active)
            native = card.get("id") == default and not profiles.needs_software(card)
            if not in_force:
                if not native:
                    self.engine.validate(dev, cid, card)
            elif native:
                self.engine.release(dev, cid)
            else:
                self.engine.assign(dev, cid, card)
            if cid == profiles.THUMB_KEY:
                self._settle_thumb(dev)
            before = self.store.snapshot()
            self.store.set(slot_id, card, app)
            if app is not None:
                try:
                    self._refocus()           # the focused window may belong to a profile just made
                except Exception:
                    self._undo(before)        # not stored: the reply is a failure
                    raise

    def _undo(self, snapshot):
        """Blocking, under the switch lock: put the store back as `snapshot` held it and bring the
        mouse in line with it again. Best effort: the failure that called for it is the one reported."""
        try:
            self.store.restore(snapshot)
        except OSError as exc:
            log.warning("could not put the saved profiles back: %s", exc)
        try:
            self._refocus()
        except Exception as exc:
            log.warning("could not put the profile back in force: %s", exc)

    def _settle_thumb(self, dev):
        """Blocking: the thumb wheel has one owner. A bound thumb action card gives it to the
        engine and the relay stops scaling it; with no card the relay takes it back when its
        speed is not neutral."""
        if not dev.thumb_wheel:
            return
        if self.engine.owns_thumb(dev.id):
            self.relay.yield_thumb(dev)
            return
        try:
            self.relay.resume_thumb(dev)
        except Exception as exc:
            log.warning("handing the thumb wheel back to the speed relay failed: %s", exc)

    def set_wheel_relay(self, enabled):
        """Blocking: turn the wheel relay on or off. Off hands every diverted wheel back to the
        mouse and takes the speed controls away; on diverts the wheels the store asks for."""
        self.relay.enabled = bool(enabled)
        for dev in list(self.reg.devices.values()):
            if not enabled:
                try:
                    self.relay.release(dev)
                except Exception as exc:
                    log.warning("handing the wheels back failed: %s", exc)
                self.relay.probe(dev)
            else:
                self.apply_stored(dev.id)

    async def _device_restore(self, info):
        """SET /device/restore (RESET TO DEFAULT SETTINGS): the payload is the device info object."""
        info = info if isinstance(info, dict) else {}
        dev_id = info.get("id") or info.get("deviceId")
        if dev_id not in self.reg.devices:
            return "INVALID_DEVICE", None, "unknown device id"
        code, _, what = await self._profiles_reset({"deviceId": dev_id})
        if code != "SUCCESS":
            return code, None, what
        if self.server is not None:
            for path, state in self.initial_state("/devices/state/changed"):
                if state.get("id") == dev_id:
                    self.server.broadcast(path, state)
        return "SUCCESS", info, ""

    async def _profiles_reset(self, info):
        loop = asyncio.get_running_loop()
        app = info.get("profileId")
        if app and app != DEFAULT_PROFILE:
            return await self._app_profile_reset(self.store.app_of(app), info)
        for dev in list(self.reg.devices.values()):
            if info.get("deviceId") and info["deviceId"] != dev.id:
                continue
            try:
                await loop.run_in_executor(None, self._reset_device, dev)
            except Exception as exc:
                log.warning("applying defaults failed: %s", exc)
                return "FAULTED", None, "device write failed: %s" % exc
        await self._broadcast_profiles()
        return "SUCCESS", info, ""

    def _reset_device(self, dev):
        """Blocking: forget the default profile's choices for `dev` and put its defaults back.
        Nothing stays forgotten when the mouse cannot be brought in line."""
        with self._switch_lock:
            before = self.store.snapshot()
            try:
                self.store.remove(profiles.mouse_settings_slot(dev.slot_prefix))
                self.store.remove(profiles.scroll_slot(dev.slot_prefix))
                self.store.remove(profiles.thumb_wheel_slot(dev.slot_prefix))
                self.store.remove_matching(lambda slot: profiles.is_button_slot(slot, dev.slot_prefix))
                self._apply_defaults(dev)
            except Exception:
                self._undo(before)
                raise

    async def _app_profile_reset(self, app, info, unlink=None, broadcast=True):
        """Reset an application's profile: it forgets its buttons and follows the default again.
        Nothing is forgotten when the mouse cannot be brought in line."""
        devices = [d for d in self.reg.devices.values() if not info.get("deviceId") or info["deviceId"] == d.id]
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._reset_app, app, devices, unlink)
        except OSError as exc:
            return "FAULTED", None, "cannot save the profile: %s" % exc
        except Exception as exc:
            return "FAULTED", None, "device write failed: %s" % exc
        if broadcast:
            await self._broadcast_profiles([unlink] if unlink else ())
        return "SUCCESS", info, ""

    def _reset_app(self, app, devices, unlink=None):
        """Blocking: forget the application's buttons for `devices` (and the UI's id `unlink`), and
        change the mouse when that application's profile is in force."""
        with self._switch_lock:
            before = self.store.snapshot()
            try:
                self.store.remove_app_buttons(app, lambda slot: any(profiles.is_button_slot(slot, d.slot_prefix)
                                                                      for d in devices))
                if unlink:
                    self.store.unlink(unlink)
                if self.engine.active_app == app:
                    self.engine.activate(app if self.store.apps.get(app) else None, devices)
                    for dev in devices:
                        self._settle_thumb(dev)
            except Exception:
                self._undo(before)
                raise

    def on_focus(self, window):
        """Blocking: the focused window changed; put the profile of its application (else the
        default) in force. Returns False when the switch failed, so the caller tries again;
        the UI is told when the profile in force changed."""
        with self._switch_lock:
            self._focused = window
            try:
                switched = self._refocus()
            except Exception as exc:
                log.warning("switching profile failed: %s", exc)
                return False
        if switched:
            self._schedule_broadcast()       # worker thread: the loop does the sending
        return True

    def has_app_profiles(self):
        """Whether any application has a profile of its own: only then does the focus matter."""
        return bool(self.store.apps)

    def _refocus(self):
        """Blocking, under the switch lock: bring the profile in force in line with the focused
        window. Returns whether the profile in force changed."""
        if self.apps is None:
            self.apps = applications.scan(self._app_dirs())
        if self.window_probe is not None:       # nobody has watched the focus while no profile existed
            try:
                self._focused = self.window_probe()
            except Exception as exc:
                log.debug("cannot tell the focused window: %s", exc)
        app = focus.match_profile(self._focused, sorted(self.store.apps), self.apps)
        if app == self.engine.active_app:
            return False
        devices = list(self.reg.devices.values())
        self.engine.activate(app, devices)
        for dev in devices:
            self._settle_thumb(dev)     # a profile switch can give the thumb wheel to the engine or take it back
        return True

    def _apply_defaults(self, dev):
        self.engine.sync(dev)               # the store holds no button choices now: hand them all back
        try:
            self.relay.release(dev, reset=True)
        except Exception as exc:
            log.warning("handing the wheels back failed: %s", exc)
        if dev.scroll_caps["highResolution"]:
            # only a mouse with the hi-res wheel (0x2121) has these bits to clear
            self.reg.divert_wheel(dev.id, None, invert=False, hires=False)    # the relay only writes a wheel it holds
        if dev.thumb_wheel:
            self.reg.write_thumb(dev.id, False, False)
        value = profiles.default_pointer_value(dev.depot)
        if value is not None:
            self._apply_pointer(dev, value)
        else:
            self.reg.write_dpi(dev.id, self.reg.read_dpi(dev.id)["default"])

    async def _easy_switch(self, verb, dev_id, payload, switch_only=False):
        dev = self.reg.devices.get(dev_id)
        if dev is None or not dev.easy_switch:
            return ("NO_SUCH_PATH" if verb == "GET" else "INVALID_DEVICE"), None, ""
        if switch_only and (verb != "SET" or not isinstance(payload, dict) or "host" not in payload):
            return "NO_SUCH_PATH", None, ""
        loop = asyncio.get_running_loop()

        def read():
            return easy_switch.hosts_payload(dev, self.reg.read_hosts(dev_id))

        try:
            if verb == "SET":
                code, msg, state = await loop.run_in_executor(
                    None, easy_switch.apply_change, self.reg, dev, payload)
                if code != "SUCCESS":
                    return code, None, msg
                self._announce_hosts(dev, state, payload.get("host"))
                if "host" in payload:
                    # the mouse has left for the other computer: no read-back possible
                    return "SUCCESS", payload, ""
            return "SUCCESS", await loop.run_in_executor(None, read), ""
        except Exception as exc:
            log.warning("easy switch %s failed: %s", verb, exc)
            return "FAULTED", None, "device access failed: %s" % exc

    def _announce_hosts(self, dev, state, host=None):
        """Tell subscribers the host slots changed (after a switch or a rename). The payload
        types of these paths are inferred from their names (re/boot_routes_ref.txt)."""
        if self.server is None:
            return
        self.server.broadcast("/devices/easy_switch", easy_switch.hosts_payload(dev, state))
        self.server.broadcast("/devices/state/easy_switch_cache_updated", easy_switch.cache_payload(dev, state))
        if host is not None:
            self.server.broadcast("/devices/host_changed", easy_switch.host_changed_payload(host))

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

    def _app_dirs(self):
        return self.app_dirs if self.app_dirs is not None else applications.default_dirs()

    async def _scan_applications(self, announce):
        """Scan off the event loop. An unreadable tree or a scan error counts
        as "nothing found": the completion broadcast is sent regardless, so the
        UI's scanning spinner always stops."""
        dirs = self._app_dirs()
        try:
            self.apps = await asyncio.get_running_loop().run_in_executor(None, applications.scan, dirs)
        except Exception as exc:
            log.warning("application scan failed: %s", exc)
            self.apps = []
        if announce and self.server is not None:
            # after the SET reply, which the caller is about to send
            asyncio.get_running_loop().call_soon(self._announce_applications)

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
            infos = [d.info() for d in reg.devices.values()] + [r.info() for r in reg.receivers.values()]
            return "SUCCESS", {"@type": TYPE + "logi.protocol.devices.Device.Info.List",
                               "deviceInfos": infos}, ""
        if parts[:3] == ["devices", "ever_connected", "list"]:
            return "SUCCESS", {"@type": TYPE + "logi.protocol.devices.DevicePermanentDatas",
                               "devices": [d.permanent() for d in reg.devices.values()]}, ""

        # GET /devices/resources {id, deviceModel}: images and metadata
        if parts == ["devices", "resources"]:
            dev = reg.devices.get((payload or {}).get("id", ""))
            if dev is None and (payload or {}).get("id", "") in reg.receivers:
                return "SUCCESS", {}, ""
            if dev is None:
                return "INVALID_DEVICE", None, "unknown device id"
            return "SUCCESS", dev.resources(), ""

        # /devices/<id>/<sub>
        if parts[0] == "devices" and len(parts) >= 3:
            dev_id, sub = parts[1], parts[2]
            if dev_id in reg.receivers and sub == "info":
                return "SUCCESS", reg.receivers[dev_id].info(), ""
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

        # SET /dfu/<id>/check (CHECK FOR UPDATE): there is never a firmware update to offer
        if verb == "SET" and parts[0] == "dfu" and len(parts) == 3 and parts[2] == "check":
            if parts[1] not in reg.devices:
                return "INVALID_DEVICE", None, "unknown device id"
            if self.server is not None:
                # the UI ends "Checking for updates..." only on a Device.Info that names the device
                dev = reg.devices[parts[1]]
                self.server.broadcast("/dfu/update/available", {
                    "@type": TYPE + "logi.protocol.devices.Device.Info",
                    "id": dev.id, "state": dev.info()["state"],
                    "fwUpdate": {"requireFwUpdate": False}})
            return "SUCCESS", {"@type": TYPE + "logi.protocol.dfu.Info"}, ""

        # /battery/<id>/state
        if parts[0] == "battery" and len(parts) >= 3 and parts[2] == "state":
            dev_id = parts[1]
            if dev_id not in reg.devices:
                return "NO_SUCH_PATH", None, ""
            pl, _ = reg.battery(dev_id)
            return "SUCCESS", pl, ""

        return None
