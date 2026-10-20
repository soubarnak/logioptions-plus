"""Wheel relay: scroll and thumb wheel speed and direction through the virtual input device.

Linux has no per-device scroll speed, so a wheel whose speed is not neutral is diverted:
the mouse stops sending it as native HID motion (0x2121 target bit, 0x2150 reporting
mode) and reports it to the agent, which re-emits it scaled, as high-resolution wheel
events on the same virtual device the button remap uses. A wheel at neutral speed is never
diverted, so the common case costs nothing and a stopped agent leaves nothing behind.

The relay is available for a wheel when the mouse has the feature and the virtual device can
be created; speed controls are reported on only then. While a wheel is diverted the device's
own invert bit is kept clear and the direction is applied here, so nothing is inverted twice.

The thumb wheel has one other owner: while a thumb action card is bound the action engine
diverts it and consumes its motion. The relay then leaves that wheel alone (the speed is kept
but not applied, direction and smoothness still go to the device) and takes it over again,
through `resume_thumb`, once the card is gone.

`speed` is the card's slider value, 0..1: 0.5 leaves the motion alone, 1 is four times
faster, 0 is "never set" and also leaves it alone. The range is assumed, see `multiplier`.
"""

import logging
import math
import threading

from . import hidpp, uinput

log = logging.getLogger("opl.relay")

NEUTRAL_SPEED = 0.5
MAX_FACTOR = 4.0
HIRES_PER_NOTCH = 120               # kernel convention: REL_*_HI_RES counts 120 per notch

WHEEL, THUMB = "wheel", "thumb"
_AXES = {  # axis -> (hi-res code, notch code) of the virtual device
    WHEEL: (uinput.REL_WHEEL_HI_RES, uinput.REL_WHEEL),
    THUMB: (uinput.REL_HWHEEL_HI_RES, uinput.REL_HWHEEL),
}


def multiplier(speed):
    """The factor a card's speed value scales motion by. Unset or non-positive means 1; the
    slider runs 0..1 with 0.5 as 1x, a quarter as 0.5x and the top as 4x. That range is an
    assumption: confirm what the UI sends on hardware."""
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0:
        return 1.0
    return MAX_FACTOR ** (2 * min(float(speed), 1.0) - 1)


def is_neutral(speed):
    return abs(multiplier(speed) - 1.0) < 1e-9


def _trunc(x):
    """x cut toward zero, forgiving float noise just below a whole number."""
    return int(x + math.copysign(1e-6, x)) if x else 0


class _Axis:
    """One wheel of one device: the settings in force and the motion carried between events."""

    def __init__(self):
        self.speed = 0.0
        self.invert = False
        self.smooth = True
        self.diverted = False
        self.units = 0              # device motion units per notch while diverted
        self.carry = 0.0            # scaled hi-res motion not yet emitted
        self.notch_carry = 0        # emitted hi-res motion not yet a whole notch


class WheelRelay:
    """Per-device wheel diversion and the scaled re-emission. Methods that talk to the mouse block."""

    def __init__(self, registry, sink, enabled=True):
        self.reg = registry
        self._sink = sink               # callable -> the virtual device; raises OSError when it cannot
        self.enabled = enabled
        self._lock = threading.RLock()
        self._axes = {}                 # (dev_id, axis) -> _Axis
        self.thumb_owned = lambda dev_id: False   # True while the action engine owns the thumb wheel
        registry.on_wheel_event.append(self.on_event)

    # ---- availability -----------------------------------------------------------
    @staticmethod
    def has(dev, axis):
        return bool(dev.thumb_wheel if axis == THUMB else dev.scroll_caps["highResolution"])

    def _backed(self):
        if not self.enabled:
            return False
        try:
            self._sink()
            return True
        except Exception as exc:
            log.debug("wheel speed is off, no virtual input device: %s", exc)
            return False

    def probe(self, dev):
        """Set which speed controls `dev` offers (no device writes); returns whether any."""
        backed = self._backed()
        dev.relay = {axis: backed and self.has(dev, axis) for axis in (WHEEL, THUMB)}
        return any(dev.relay.values())

    def available(self, dev, axis):
        self.probe(dev)
        return dev.relay[axis]

    def axis(self, dev, axis):
        """The state of one wheel of `dev` (created neutral on first use)."""
        with self._lock:
            return self._axes.setdefault((dev.id, axis), _Axis())

    # ---- applying settings ------------------------------------------------------------
    def set_wheel(self, dev, speed=None, invert=None, smooth=None):
        """Blocking: apply scroll wheel speed / direction / smoothness (None leaves one as it is).
        Diverts the wheel for a non-neutral speed, hands it back otherwise. Returns the settings
        in force as {"speed", "invert", "smooth"}; raises, changing nothing, when the device
        write fails or the virtual device is gone."""
        ax = self.axis(dev, WHEEL)
        if not ax.diverted:
            native = self.reg.read_scroll(dev.id)["wheel"]
            cur = {"invert": native["invert"], "smooth": native["hires"]}
        else:
            cur = {"invert": ax.invert, "smooth": ax.smooth}
        new = {"speed": ax.speed if speed is None else speed,
               "invert": cur["invert"] if invert is None else bool(invert),
               "smooth": cur["smooth"] if smooth is None else bool(smooth)}
        if speed is None and invert is None and smooth is None:
            return {"speed": ax.speed, **cur}
        if not is_neutral(new["speed"]):
            self._sink()                # a diverted wheel with nowhere to go would not scroll at all
            units = self.reg.wheel_units_per_notch(dev.id)
            self.reg.divert_wheel(dev.id, True, invert=False)
        else:
            units = 0
            # only hand back what this relay took: another driver's target bit is not ours to clear
            self.reg.divert_wheel(dev.id, False if ax.diverted else None,
                                  invert=new["invert"], hires=new["smooth"])
        self._commit(ax, new, units)
        return new

    def set_thumb(self, dev, speed=None, invert=None, smooth=None):
        """Blocking: as `set_wheel`, for the thumb wheel."""
        ax = self.axis(dev, THUMB)
        if self.thumb_owned(dev.id):
            return self._set_thumb_owned(dev, ax, speed, invert, smooth)
        if not ax.diverted:
            native = self.reg.read_thumb(dev.id)
            cur = {"invert": native["invert"], "smooth": native["smooth"]}
        else:
            cur = {"invert": ax.invert, "smooth": ax.smooth}
        new = {"speed": ax.speed if speed is None else speed,
               "invert": cur["invert"] if invert is None else bool(invert),
               "smooth": cur["smooth"] if smooth is None else bool(smooth)}
        if not is_neutral(new["speed"]):
            self._sink()
            units = self.reg.thumb_units_per_notch(dev.id)
            if units <= 0:
                raise hidpp.HidppError(hidpp.F_THUMB_WHEEL, 0, 2)    # no scale to relay by
            self.reg.write_thumb(dev.id, False, new["smooth"], divert=True)
        else:
            units = 0
            self.reg.write_thumb(dev.id, new["invert"], new["smooth"],
                                 divert=False if ax.diverted else None)
        self._commit(ax, new, units)
        return new

    def _set_thumb_owned(self, dev, ax, speed, invert, smooth):
        """The action engine has the thumb wheel diverted: remember the speed for later, write
        direction and smoothness without touching the reporting mode, relay nothing."""
        native = self.reg.read_thumb(dev.id)
        new = {"speed": ax.speed if speed is None else speed,
               "invert": native["invert"] if invert is None else bool(invert),
               "smooth": native["smooth"] if smooth is None else bool(smooth)}
        self.reg.write_thumb(dev.id, new["invert"], new["smooth"], divert=None)
        self._commit(ax, new, 0)
        return new

    def yield_thumb(self, dev):
        """The action engine took the thumb wheel: stop relaying it. Writes nothing to the mouse
        (the engine already holds the diversion); the speed is kept for `resume_thumb`."""
        with self._lock:
            ax = self._axes.get((dev.id, THUMB))
            if ax is not None and ax.diverted:
                ax.diverted = False
                ax.units = 0
                ax.carry, ax.notch_carry = 0.0, 0

    def resume_thumb(self, dev):
        """Blocking: the action engine let the thumb wheel go; divert it again when its speed is
        not neutral. Does nothing while the engine still owns it or the relay is not available."""
        with self._lock:
            ax = self._axes.get((dev.id, THUMB))
        if ax is None or ax.diverted or is_neutral(ax.speed) or self.thumb_owned(dev.id):
            return
        if self.available(dev, THUMB):
            # the device's own invert bit was cleared while diverted: restate the user's
            self.set_thumb(dev, invert=ax.invert, smooth=ax.smooth)

    def _commit(self, ax, new, units):
        with self._lock:
            ax.speed, ax.invert, ax.smooth = new["speed"], new["invert"], new["smooth"]
            ax.diverted = not is_neutral(new["speed"])
            ax.units = units
            ax.carry, ax.notch_carry = 0.0, 0

    # ---- handing wheels back -----------------------------------------------------------------
    def release(self, dev, axis=None, reset=False):
        """Blocking: hand the diverted wheel(s) of `dev` back to the mouse with the direction
        and smoothness they had. `reset` also returns them to the defaults (forgetting the speed).
        Raises the first device error after trying them all."""
        first_error = None
        for name in (WHEEL, THUMB) if axis is None else (axis,):
            with self._lock:
                ax = self._axes.get((dev.id, name))
            if ax is None:
                continue
            if reset:
                ax.invert = False
                if name == THUMB:
                    ax.smooth = False
            # a thumb wheel the action engine holds is not ours to hand back
            if ax.diverted and not (name == THUMB and self.thumb_owned(dev.id)):
                try:
                    if name == WHEEL:
                        self.reg.divert_wheel(dev.id, False, invert=ax.invert, hires=ax.smooth)
                    else:
                        self.reg.write_thumb(dev.id, ax.invert, ax.smooth, divert=False)
                except Exception as exc:
                    log.warning("handing the %s wheel of %s back failed: %s", name, dev.id, exc)
                    first_error = first_error or exc
                    continue
            with self._lock:
                ax.diverted = False
                if reset:
                    ax.speed = 0.0
        if first_error is not None:
            raise first_error

    def close(self):
        """Best effort at shutdown: no wheel stays diverted to a process that is gone."""
        for dev in list(self.reg.devices.values()):
            try:
                self.release(dev)
            except Exception:
                pass

    # ---- events --------------------------------------------------------------------------------
    def on_event(self, dev_id, feature_id, function, params):
        """Reader-thread hook for 0x2121 / 0x2150 events: re-emit the motion of a diverted wheel."""
        if function != 0:
            return
        if feature_id == hidpp.F_HIRES_WHEEL:
            axis, parsed = WHEEL, hidpp.parse_wheel_event(params)
        elif feature_id == hidpp.F_THUMB_WHEEL:
            axis, parsed = THUMB, hidpp.parse_thumb_event(params)
        else:
            return
        if parsed is None:
            return
        with self._lock:
            ax = self._axes.get((dev_id, axis))
            if ax is None or not ax.diverted:
                return
            if axis == THUMB and self.thumb_owned(dev_id):
                return                  # the action engine consumes this motion
            if axis == WHEEL:
                hires, delta = parsed
                motion = delta * HIRES_PER_NOTCH / (ax.units if hires else 1)
            else:
                motion = parsed * HIRES_PER_NOTCH / ax.units
            events = self._scale(ax, axis, motion)
            if not events:
                return
            try:
                self._sink().emit(events)
            except Exception as exc:
                log.warning("relaying the %s wheel of %s failed: %s", axis, dev_id, exc)

    @staticmethod
    def _scale(ax, axis, motion):
        """Scaled hi-res and whole-notch events for `motion` (hi-res units, device direction);
        what does not amount to one yet stays in the axis for the next event."""
        ax.carry += motion * multiplier(ax.speed) * (-1 if ax.invert else 1)
        if ax.smooth:
            hires = _trunc(ax.carry)
            ax.carry -= hires
        else:
            hires = _trunc(ax.carry / HIRES_PER_NOTCH) * HIRES_PER_NOTCH
            ax.carry -= hires
        ax.notch_carry += hires
        notches = _trunc(ax.notch_carry / HIRES_PER_NOTCH)
        ax.notch_carry -= notches * HIRES_PER_NOTCH
        hires_code, notch_code = _AXES[axis]
        out = []
        if hires:
            out.append((uinput.EV_REL, hires_code, hires))
        if notches:
            out.append((uinput.EV_REL, notch_code, notches))
        return out
