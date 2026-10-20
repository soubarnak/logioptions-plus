"""Swap the left and right mouse buttons: ONE method system-wide.

Best case the mice do it: 0x1b04 setCidReporting remaps control 0x50 (left) to 0x51 and
back, so the swap holds in every program and on Wayland. That needs every connected pointer
to allow it (reprogrammable, and the target in its group mask). When one cannot, an X11
session falls back to ``xinput set-button-map`` on the Logitech pointers only (the virtual
device the agent injects clicks through is left alone, so a "Left click" card still clicks
left) and any device remap is cleared, so the buttons are never swapped twice. On Wayland
without device support there is nothing to do from here: the request is refused with the
reason, and the user is pointed at the desktop's own mouse settings.

The choice and the method that carries it are kept in ``button_swap.json`` and applied again
whenever a mouse connects (the device remap does not survive a power cycle).
"""

import json
import logging
import os
import re
import threading

from . import hidpp, uinput

log = logging.getLogger("opl.swap")

LEFT, RIGHT = 0x50, 0x51
STATE_LEFT, STATE_RIGHT = "LEFT", "RIGHT"        # logi.protocol.mouse.MouseButtonSwap.SwapState: who is primary
METHOD_DEVICE, METHOD_X11 = "device", "x11"
POINTERS = ("MOUSE", "TRACKBALL")
MAP_SWAPPED, MAP_DEFAULT = ["3", "2", "1"], ["1", "2", "3"]
XINPUT_POINTER = re.compile(r"^\W*(?P<name>.+?)\s+id=(?P<id>\d+)\s+\[slave\s+pointer")


class SwapUnsupported(Exception):
    """The buttons cannot be swapped on this system. Carries the text the UI shows."""


class ButtonSwap:
    def __init__(self, registry, state_dir, runner, environ=None):
        self.reg = registry
        self.runner = runner
        self.environ = environ if environ is not None else os.environ
        self.path = os.path.join(state_dir, "button_swap.json")
        self._lock = threading.Lock()
        self.swapped = False
        self.method = None              # what carries the swap now: METHOD_DEVICE, METHOD_X11 or None
        try:
            with open(self.path) as fh:
                saved = json.load(fh)
            self.swapped = saved.get("state") == STATE_RIGHT
            if saved.get("method") in (METHOD_DEVICE, METHOD_X11):
                self.method = saved["method"]
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("ignoring unreadable %s: %s", self.path, exc)

    @property
    def state(self):
        return STATE_RIGHT if self.swapped else STATE_LEFT

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"state": self.state, "method": self.method}, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot save the button swap: %s", exc)

    # ---- what this system can do ------------------------------------------------
    def _pointers(self):
        return [d for d in list(self.reg.devices.values()) if d.dtype in POINTERS]

    def _device_can_swap(self, dev):
        try:
            controls = self.reg.controls(dev.id)
        except hidpp.FeatureNotSupported:
            return False
        except Exception as exc:
            log.debug("cannot read the buttons of %s: %s", dev.id, exc)
            return False
        return hidpp.can_remap(controls, LEFT, RIGHT) and hidpp.can_remap(controls, RIGHT, LEFT)

    def _x11_available(self):
        """An X11 session (not XWayland under a Wayland desktop) with xinput installed."""
        env = self.environ
        if not env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
            return False
        if env.get("XDG_SESSION_TYPE") not in ("x11", "tty"):
            return False
        return bool(self.runner.which("xinput"))

    def _plan(self):
        """(method, capable devices) for the mice connected now. The device method needs every
        pointer to allow it; otherwise X11 if there is one; otherwise the device method on
        whichever mice can (the rest cannot be swapped from here); otherwise None."""
        pointers = self._pointers()
        capable = [d for d in pointers if self._device_can_swap(d)]
        if pointers and len(capable) == len(pointers):
            return METHOD_DEVICE, capable
        if self._x11_available():
            return METHOD_X11, capable
        if capable:
            return METHOD_DEVICE, capable
        return None, capable

    def _unsupported(self):
        if self.environ.get("WAYLAND_DISPLAY") or self.environ.get("XDG_SESSION_TYPE") == "wayland":
            return SwapUnsupported(
                "This mouse cannot swap its buttons itself, and Wayland offers no way for an app "
                "to do it. Use the mouse settings of your desktop instead.")
        return SwapUnsupported(
            "This mouse cannot swap its buttons itself, and no X11 session with xinput was "
            "found. Use the mouse settings of your desktop instead.")

    # ---- writing ----------------------------------------------------------------------
    def _write_device(self, dev, swapped):
        """Blocking. Raises when a write fails; the buttons are put back as they were then."""
        targets = ((LEFT, RIGHT if swapped else LEFT), (RIGHT, LEFT if swapped else RIGHT))
        before = [(cid, self.reg.remap_of(dev.id, cid)) for cid, _ in targets]
        try:
            for cid, target in targets:
                self.reg.set_remap(dev.id, cid, target)
        except Exception:
            for cid, target in before:
                try:
                    self.reg.set_remap(dev.id, cid, target)
                except Exception as exc:
                    log.warning("could not undo the swap on %s: %s", dev.id, exc)
            raise

    def _write_devices(self, devices, swapped, previous):
        """All or nothing across `devices`; `previous` is the state to go back to on failure."""
        done = []
        try:
            for dev in devices:
                self._write_device(dev, swapped)
                done.append(dev)
        except Exception:
            for dev in done:
                try:
                    self._write_device(dev, previous)
                except Exception as exc:
                    log.warning("could not undo the swap on %s: %s", dev.id, exc)
            raise

    def _x11_pointer_ids(self):
        code, out = self.runner.run_output(["xinput", "list"])
        if code != 0:
            raise RuntimeError("xinput list failed (exit status %s)" % code)
        names = {d.name.lower() for d in self._pointers() if d.name}
        ids = []
        for line in out.splitlines():
            m = XINPUT_POINTER.match(line)
            if not m:
                continue
            name = m.group("name").lower()
            if name == uinput.DEVICE_NAME.lower():
                continue                # our own injected clicks must reach the desktop as sent
            if "logitech" in name or any(n in name for n in names):
                ids.append(m.group("id"))
        return ids

    def _write_x11(self, swapped):
        ids = self._x11_pointer_ids()
        if swapped and not ids:
            raise RuntimeError("xinput lists no Logitech pointer to swap")
        for pid in ids:
            argv = ["xinput", "set-button-map", pid] + (MAP_SWAPPED if swapped else MAP_DEFAULT)
            code = self.runner.run_wait(argv)
            if code != 0:
                raise RuntimeError("xinput set-button-map failed (exit status %s)" % code)

    def _clear_other_method(self, method, capable):
        """Put the method that is not carrying the swap back to normal, so it is not swapped twice."""
        if method == METHOD_X11:
            for dev in capable:
                try:
                    self._write_device(dev, False)
                except Exception as exc:
                    log.warning("could not clear the remap on %s: %s", dev.id, exc)
        elif self.method == METHOD_X11 and self._x11_available():
            try:
                self._write_x11(False)
            except Exception as exc:
                log.warning("could not reset the X11 button map: %s", exc)

    def _apply(self, swapped):
        """Blocking: bring every mouse to `swapped`; returns the method that carries it (None
        when not swapped or when no mouse is connected yet). Raises when a write fails."""
        method, capable = self._plan()
        if not swapped:
            for dev in capable:
                self._write_device(dev, False)
            if self.method == METHOD_X11 and self._x11_available():
                self._write_x11(False)
            return None
        if method == METHOD_DEVICE:
            try:
                self._write_devices(capable, True, self.swapped and self.method == METHOD_DEVICE)
            except Exception:
                if not self._x11_available():
                    raise
                log.warning("the mouse rejected the button remap; using X11 instead", exc_info=True)
                method = METHOD_X11
            else:
                self._clear_other_method(METHOD_DEVICE, capable)
                return METHOD_DEVICE
        if method == METHOD_X11:
            self._clear_other_method(METHOD_X11, capable)
            self._write_x11(True)
            return METHOD_X11
        if self._pointers():
            raise self._unsupported()
        return None             # no mouse yet: remembered, applied when one connects

    def set(self, swapped):
        """Blocking: swap (or unswap) the buttons and remember it. Raises SwapUnsupported when
        nothing here can do it and any other error when a write fails; nothing is remembered
        then."""
        swapped = bool(swapped)
        with self._lock:
            method = self._apply(swapped)
            self.swapped, self.method = swapped, method
            self._save()

    def apply_stored(self, dev):
        """Blocking: put the buttons as remembered after a mouse connects or wakes (clearing a
        swap it still holds from an earlier session when none is wanted)."""
        with self._lock:
            if not self.swapped:
                if self._device_can_swap(dev):
                    self._write_device(dev, False)
                return
            try:
                method = self._apply(True)
            except SwapUnsupported:
                return
            if method != self.method:
                self.method = method
                self._save()
