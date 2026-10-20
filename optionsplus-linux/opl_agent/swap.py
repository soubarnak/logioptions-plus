"""Swap the left and right mouse buttons.

Best case the mouse does it: 0x1b04 setCidReporting remaps control 0x50 (left) to 0x51 and
back, so the swap holds in every program and on Wayland. A mouse whose primary buttons are
not remappable falls back to the X11 core pointer map (``xmodmap``). On Wayland without
device support there is no way to do it from here: the request is refused with the reason,
and the user is pointed at the desktop's own mouse settings.

The choice is kept in ``button_swap.json`` and applied again whenever a mouse connects. The
device remap is not persistent (a power cycle drops it), which is why it is reapplied.
"""

import json
import logging
import os
import threading

from . import hidpp

log = logging.getLogger("opl.swap")

LEFT, RIGHT = 0x50, 0x51
STATE_LEFT, STATE_RIGHT = "LEFT", "RIGHT"        # logi.protocol.mouse.MouseButtonSwap.SwapState: who is primary
XMODMAP_SWAPPED = "pointer = 3 2 1"
XMODMAP_DEFAULT = "pointer = default"


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
        try:
            with open(self.path) as fh:
                self.swapped = json.load(fh).get("state") == STATE_RIGHT
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
                json.dump({"state": self.state}, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot save the button swap: %s", exc)

    # ---- what this system can do ------------------------------------------------
    def _device_can_swap(self, dev):
        try:
            controls = self.reg.controls(dev.id)
        except hidpp.FeatureNotSupported:
            return False
        return all(controls.get(cid, {}).get("reprogrammable") for cid in (LEFT, RIGHT))

    def _swappable_devices(self):
        out = []
        for dev in list(self.reg.devices.values()):
            try:
                if self._device_can_swap(dev):
                    out.append(dev)
            except Exception as exc:
                log.debug("cannot read the buttons of %s: %s", dev.id, exc)
        return out

    def _x11_available(self):
        env = self.environ
        if not env.get("DISPLAY") or env.get("WAYLAND_DISPLAY") or env.get("XDG_SESSION_TYPE") == "wayland":
            return False
        return bool(self.runner.which("xmodmap"))

    def _unsupported(self):
        if self.environ.get("WAYLAND_DISPLAY") or self.environ.get("XDG_SESSION_TYPE") == "wayland":
            return SwapUnsupported(
                "This mouse cannot swap its buttons itself, and Wayland offers no way for an app "
                "to do it. Use the mouse settings of your desktop instead.")
        return SwapUnsupported(
            "This mouse cannot swap its buttons itself, and no X11 session with xmodmap was "
            "found. Use the mouse settings of your desktop instead.")

    # ---- applying ---------------------------------------------------------------------
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

    def _write_x11(self, swapped):
        code = self.runner.run_wait(["xmodmap", "-e", XMODMAP_SWAPPED if swapped else XMODMAP_DEFAULT])
        if code != 0:
            raise RuntimeError("xmodmap failed (exit status %s)" % code)

    def set(self, swapped):
        """Blocking: swap (or unswap) the buttons and remember it. Raises SwapUnsupported when
        nothing here can do it and any other error when a write fails; nothing is remembered
        then."""
        swapped = bool(swapped)
        with self._lock:
            devices = self._swappable_devices()
            if devices:
                done = []
                try:
                    for dev in devices:
                        self._write_device(dev, swapped)
                        done.append(dev)
                except Exception:
                    for dev in done:
                        try:
                            self._write_device(dev, self.swapped)
                        except Exception as exc:
                            log.warning("could not undo the swap on %s: %s", dev.id, exc)
                    raise
            elif self._x11_available():
                self._write_x11(swapped)
            elif swapped:
                raise self._unsupported()
            self.swapped = swapped
            self._save()

    def apply_stored(self, dev):
        """Blocking: put one connecting mouse's buttons as remembered (clearing a swap it
        still holds from an earlier session when none is wanted)."""
        with self._lock:
            if self._device_can_swap(dev):
                self._write_device(dev, self.swapped)
            elif self.swapped and self._x11_available():
                self._write_x11(True)
