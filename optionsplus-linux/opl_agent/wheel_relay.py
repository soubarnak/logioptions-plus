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

`speed` is the card's slider value on the UI's own scale, -1..1 (it shows `round(50 * (speed + 1))`
percent): 0 is 50% and leaves the motion alone, 1 is four times faster, -1 is four times slower.
Unset also means 0. See `multiplier`.

The thumb wheel reports rotation in units per revolution, not per notch: getThumbwheelInfo gives
the native and the diverted resolution (18 and 120 on the MX Master 3S), so one notch is
diverted / native units. Which way a positive rotation points comes from the same answer.
"""

import logging
import math
import threading

from . import hidpp, uinput

log = logging.getLogger("opl.relay")

NEUTRAL_SPEED = 0.0                 # 50% on the UI's slider, and what a card holds when nothing was chosen
# What the thumb wheel capability carries while the relay can apply a speed (0 keeps the control
# off). Not a card value; it was the old neutral, kept as it was working live, whether the UI
# reads it as anything but "on" is unverified.
THUMB_OVERRIDE_SPEED = 0.5
MAX_FACTOR = 4.0                    # speed +1
HIRES_PER_NOTCH = 120               # kernel convention: REL_*_HI_RES counts 120 per notch

WHEEL, THUMB = "wheel", "thumb"
_AXES = {  # axis -> (hi-res code, notch code) of the virtual device
    WHEEL: (uinput.REL_WHEEL_HI_RES, uinput.REL_WHEEL),
    THUMB: (uinput.REL_HWHEEL_HI_RES, uinput.REL_HWHEEL),
}


def multiplier(speed):
    """The factor a card's speed value scales motion by: MAX_FACTOR ** speed on the UI's -1..1
    scale, so it is exponential and monotonic, 1 at 0 (50%), 4x at +1 (100%), 0.25x at -1 (0%)
    and 2x / 0.5x at the quarter points (75% / 25%). Values beyond the scale are clamped; unset
    or anything that is not a number is 1."""
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or math.isnan(speed):
        return 1.0
    return MAX_FACTOR ** max(-1.0, min(float(speed), 1.0))


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
        self.units = 0              # device motion units per notch while diverted (negative: a positive one points left)
        self.carry = 0.0            # scaled hi-res motion not yet emitted
        self.notch_carry = 0        # emitted hi-res motion not yet a whole notch
        self.lock = threading.RLock()   # held across reading the mouse, writing it and recording the result


class WheelRelay:
    """Per-device wheel diversion and the scaled re-emission. Methods that talk to the mouse block."""

    def __init__(self, registry, sink, enabled=True):
        self.reg = registry
        self._sink = sink               # callable -> the virtual device; raises OSError when it cannot
        self.enabled = enabled
        self._lock = threading.RLock()
        self._axes = {}                 # (dev_id, axis) -> _Axis
        self._timers = {}               # dev_id -> pending re-check of the diversions
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
    # Every method below that reads the mouse, writes it and then records the outcome does so
    # under the axis's lock, so two requests for one wheel (a UI change and a wake-up reapply)
    # cannot interleave their reads and writes. Event handling never takes it: it must not wait
    # on a device write.
    def set_wheel(self, dev, speed=None, invert=None, smooth=None):
        """Blocking: apply scroll wheel speed / direction / smoothness (None leaves one as it is).
        Diverts the wheel for a non-neutral speed, hands it back otherwise. Returns the settings
        in force as {"speed", "invert", "smooth"}; raises, changing nothing, when the device
        write fails or the virtual device is gone."""
        ax = self.axis(dev, WHEEL)
        with ax.lock:
            if not ax.diverted:
                native = self.reg.read_scroll(dev.id)["wheel"]
                cur = {"invert": native["invert"], "smooth": native["hires"]}
            else:
                cur = {"invert": ax.invert, "smooth": ax.smooth}
            if speed is None and invert is None and smooth is None:
                return {"speed": ax.speed, **cur}
            new = {"speed": ax.speed if speed is None else speed,
                   "invert": cur["invert"] if invert is None else bool(invert),
                   "smooth": cur["smooth"] if smooth is None else bool(smooth)}
            try:
                if not is_neutral(new["speed"]):
                    self._sink()        # a diverted wheel with nowhere to go would not scroll at all
                    units = self.reg.wheel_units_per_notch(dev.id)
                    if not units:
                        raise hidpp.HidppError(hidpp.F_HIRES_WHEEL, 0, 2)    # no scale to relay by: stay native
                    self._checked_wheel(dev, True, invert=False)
                else:
                    units = 0
                    # only hand back what this relay took: another driver's target bit is not ours to clear
                    self._checked_wheel(dev, False if ax.diverted else None,
                                        invert=new["invert"], hires=new["smooth"])
            except Exception:
                self._reconcile(dev, ax, WHEEL)
                raise
            self._commit(ax, new, units)
            return new

    def _checked_wheel(self, dev, divert, **kw):
        """divert_wheel, raising when the mouse did not keep the diversion asked for."""
        state = self.reg.divert_wheel(dev.id, divert, **kw)
        if divert is not None and isinstance(state, dict) and state.get("target") != divert:
            raise hidpp.HidppError(hidpp.F_HIRES_WHEEL, 2, 4)      # HW_ERROR: the device ignored it
        return state

    def _checked_thumb(self, dev, invert, smooth, divert):
        """write_thumb, raising when the mouse did not keep the reporting mode asked for."""
        state = self.reg.write_thumb(dev.id, invert, smooth, divert=divert)
        if divert is not None and isinstance(state, dict):
            want = hidpp.THUMB_DIVERTED if divert else hidpp.THUMB_NATIVE
            if state.get("mode") != want:
                raise hidpp.HidppError(hidpp.F_THUMB_WHEEL, 2, 4)
        return state

    def set_thumb(self, dev, speed=None, invert=None, smooth=None):
        """Blocking: as `set_wheel`, for the thumb wheel."""
        ax = self.axis(dev, THUMB)
        with ax.lock:
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
            try:
                if not is_neutral(new["speed"]):
                    self._sink()
                    units = self.reg.thumb_scale(dev.id)
                    if not units:
                        raise hidpp.HidppError(hidpp.F_THUMB_WHEEL, 0, 2)    # no scale to relay by
                    self._checked_thumb(dev, False, new["smooth"], True)
                else:
                    units = 0
                    self._checked_thumb(dev, new["invert"], new["smooth"], False if ax.diverted else None)
            except Exception:
                self._reconcile(dev, ax, THUMB)
                raise
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

    def _reconcile(self, dev, ax, axis):
        """After a failed write: if the mouse turns out not to be diverted, stop believing it is, so
        a later settle or resume tries again instead of returning early. Best effort."""
        if not ax.diverted:
            return
        try:
            diverted = self._device_diverted(dev, axis)
        except Exception:
            return
        if not diverted:
            with self._lock:
                ax.diverted = False
                ax.units = 0
                ax.carry, ax.notch_carry = 0.0, 0

    def _device_diverted(self, dev, axis):
        """Blocking: whether the mouse reports `axis` to software now."""
        if axis == WHEEL:
            return bool(self.reg.read_scroll(dev.id)["wheel"]["target"])
        return self.reg.read_thumb(dev.id)["mode"] == hidpp.THUMB_DIVERTED

    def holds(self, dev_id, axis):
        """Whether the relay has the wheel diverted now."""
        with self._lock:
            ax = self._axes.get((dev_id, axis))
            return ax is not None and ax.diverted

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

    # ---- a diversion nothing asked for ---------------------------------------------------------
    def recover(self, dev, speeds):
        """Blocking: hand back a wheel the mouse reports as diverted that this relay does not hold
        and `speeds` ({axis: stored speed}) does not ask for: what a crashed agent left behind, or
        what is still set when the relay was switched off. The thumb wheel of an action card is
        the engine's and stays. The direction is not touched; the settings applied after this
        restate it."""
        for axis in (WHEEL, THUMB):
            if not self.has(dev, axis):
                continue
            if self.available(dev, axis) and not is_neutral(speeds.get(axis) or 0):
                continue                # wanted: applying the stored speed takes it
            if axis == THUMB and self.thumb_owned(dev.id):
                continue
            ax = self.axis(dev, axis)
            with ax.lock:
                if ax.diverted:
                    continue            # ours: `release` hands it back
                try:
                    if not self._device_diverted(dev, axis):
                        continue
                    log.info("%s wheel of %s was left diverted; handing it back", axis, dev.id)
                    if axis == WHEEL:
                        self.reg.divert_wheel(dev.id, False)
                    else:
                        st = self.reg.read_thumb(dev.id)
                        self.reg.write_thumb(dev.id, st["invert"], st["smooth"], divert=False)
                except Exception as exc:
                    log.warning("handing the %s wheel of %s back failed: %s", axis, dev.id, exc)

    REASSERT_DELAY = 2.0                # seconds after a reapply before the diversion is checked

    def reassert_later(self, dev_id):
        """After a reapply (wake, reconnect): the kernel's hid-logitech-hidpp driver rewrites the
        wheel mode when it binds to a reconnected mouse, which may be after the reapply. Look again
        shortly and divert once more what the mouse no longer reports as diverted."""
        with self._lock:
            old = self._timers.pop(dev_id, None)
            if old is not None:
                old.cancel()
            if not any(self.holds(dev_id, a) for a in (WHEEL, THUMB)):
                return
            timer = threading.Timer(self.REASSERT_DELAY, self._reassert, (dev_id,))
            timer.daemon = True
            self._timers[dev_id] = timer
            timer.start()

    def _reassert(self, dev_id):
        dev = self.reg.devices.get(dev_id)
        if dev is None or getattr(self.reg, "closed", False):
            return
        for axis in (WHEEL, THUMB):
            with self._lock:
                ax = self._axes.get((dev_id, axis))
            if ax is None or not ax.diverted or (axis == THUMB and self.thumb_owned(dev_id)):
                continue
            with ax.lock:
                try:
                    if not ax.diverted or self._device_diverted(dev, axis):
                        continue
                    log.info("%s wheel of %s lost its diversion; diverting it again", axis, dev_id)
                    if axis == WHEEL:
                        units = self.reg.wheel_units_per_notch(dev_id)
                        if not units:
                            raise hidpp.HidppError(hidpp.F_HIRES_WHEEL, 0, 2)    # no scale to relay by: stay native
                        self._checked_wheel(dev, True, invert=False)
                    else:
                        units = self.reg.thumb_scale(dev_id)
                        if not units:
                            raise hidpp.HidppError(hidpp.F_THUMB_WHEEL, 0, 2)    # no scale to relay by: stay native
                        self._checked_thumb(dev, False, ax.smooth, True)
                    with self._lock:
                        ax.units = units
                except Exception as exc:
                    log.warning("diverting the %s wheel of %s again failed: %s", axis, dev_id, exc)

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
            with ax.lock:
                if reset:
                    ax.invert = False
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
        with self._lock:
            timers, self._timers = list(self._timers.values()), {}
        for timer in timers:
            timer.cancel()
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
            if not ax.units:
                return                  # no scale known: nothing to divide by
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
        motion *= multiplier(ax.speed) * (-1 if ax.invert else 1)
        if motion * ax.carry < 0:
            ax.carry = 0.0              # turned back: what was held back the other way no longer counts
        ax.carry += motion
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
