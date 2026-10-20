"""Input engine: divert buttons through 0x1b04 and turn their presses into injected events.

A button whose assignment is not the depot default is diverted: the mouse stops
acting on it and reports presses to the agent instead, and the engine runs the
assigned card through the virtual input device. A button on its default card is
never diverted. This ticket runs KEYSTROKE cards; every other card type is
refused with Unsupported, which the router turns into an error the UI shows.

The device is the source of truth for what is diverted: every write is read
back, and `sync` reads before it writes, so a mouse that slept, was replugged or
was left diverted by a crashed agent ends up as the store says.
"""

import logging
import threading

from . import hidpp, keymap, profiles, uinput

log = logging.getLogger("opl.engine")


class Unsupported(Exception):
    """The card or control cannot be run on this system. Carries the text the UI shows."""


def resolve_card(card):
    """The card that carries the macro: itself, or the nested card it has selected."""
    seen = 0
    while isinstance(card, dict) and not card.get("macro") and seen < 8:
        nested = (card.get("nestedCards") or {}).get(card.get("selectedNestedCard"))
        if not nested:
            break
        card, seen = nested, seen + 1
    return card or {}


def compile_card(card):
    """The events a press of `card` injects, as [(ev_type, code, value)]; raises Unsupported."""
    macro = resolve_card(card).get("macro") or {}
    kind = macro.get("type")
    if kind != "KEYSTROKE":
        raise Unsupported("%s actions are not supported yet" % (kind or "this kind of"))
    ks = macro.get("keystroke") or {}
    key = keymap.linux_key(ks.get("code"))
    if key is None:
        raise Unsupported("key code %r has no Linux equivalent" % (ks.get("code"),))
    mods = []
    for usage in ks.get("modifiers") or []:
        if usage not in keymap.MODIFIER_USAGES:
            raise Unsupported("modifier %r is not a modifier key" % (usage,))
        mods.append(keymap.linux_key(usage))
    ev = uinput.EV_KEY
    return ([(ev, m, 1) for m in mods] + [(ev, key, 1), (ev, key, 0)]
            + [(ev, m, 0) for m in reversed(mods)])


def _cid_of(slot_id):
    return int(slot_id.rpartition("_c")[2])


class InputEngine:
    """Per-device diversions and the actions behind them. Methods that talk to the mouse block."""

    def __init__(self, registry, store, sink_factory=None):
        self.reg = registry
        self.store = store
        self.sink_factory = sink_factory or uinput.open_default
        self._sink = None
        self._lock = threading.RLock()
        self._active = {}               # dev_id -> {cid: events}
        self._held = {}                 # dev_id -> set of cids held down now
        registry.on_button_event.append(self.on_event)

    # ---- the virtual device ------------------------------------------------
    def sink(self):
        """The one virtual input device, created on first use. Raises OSError when it cannot be."""
        with self._lock:
            if self._sink is None:
                self._sink = self.sink_factory()
            return self._sink

    def close(self):
        with self._lock:
            sink, self._sink = self._sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception:
                log.debug("closing the virtual device failed", exc_info=True)

    # ---- assignments -----------------------------------------------------------
    def _divertable(self, dev, cid):
        try:
            controls = self.reg.controls(dev.id)
        except hidpp.FeatureNotSupported:
            raise Unsupported("this device has no reprogrammable buttons")
        info = controls.get(cid)
        if info is None or not info["divertable"]:
            raise Unsupported("this button cannot be reassigned")

    def assign(self, dev, cid, card):
        """Make button `cid` run `card`. Blocking. Raises Unsupported for a card or button that
        cannot, any other error when the device write fails; nothing changes then."""
        events = compile_card(card)
        self._divertable(dev, cid)
        self.sink()                     # never divert a button that could not be injected for
        self.reg.set_divert(dev.id, cid, True)
        with self._lock:
            self._active.setdefault(dev.id, {})[cid] = events

    def release(self, dev, cid):
        """Hand button `cid` back to the mouse. Blocking; raises when the device write fails,
        and the button then keeps running its card."""
        try:
            self.reg.controls(dev.id)
        except hidpp.FeatureNotSupported:
            return                      # nothing was ever diverted
        self.reg.set_divert(dev.id, cid, False)
        with self._lock:
            self._active.get(dev.id, {}).pop(cid, None)

    def _desired(self, dev):
        """{cid: events} the store asks for on `dev`: non-default cards that can run."""
        defaults = {a["slotId"]: a["cardId"]
                    for a in profiles.default_assignments(dev.slot_prefix, dev.depot)}
        out = {}
        for slot_id, card in self.store.cards.items():
            if not profiles.is_button_slot(slot_id, dev.slot_prefix):
                continue
            if card.get("id") == defaults.get(slot_id):
                continue
            try:
                out[_cid_of(slot_id)] = compile_card(card)
            except Unsupported as exc:
                log.warning("button %s: %s", slot_id, exc)
        return out

    def sync(self, dev):
        """Blocking: bring the mouse's diversions in line with the store. Reads each divertable
        control first, so it also clears a diversion nothing asked for. A control that fails does
        not stop the others; the first error is raised at the end."""
        try:
            controls = self.reg.controls(dev.id)
        except hidpp.FeatureNotSupported:
            return
        desired = self._desired(dev)
        first_error = None
        for cid, info in controls.items():
            if not info["divertable"]:
                continue
            try:
                if cid in desired:
                    self.sink()
                self.reg.set_divert(dev.id, cid, cid in desired)
            except Exception as exc:
                log.warning("button %d on %s: %s", cid, dev.id, exc)
                desired.pop(cid, None)
                first_error = first_error or exc
                if isinstance(exc, hidpp.HidppTimeout):
                    break               # the mouse is not answering: do not wait per button
        with self._lock:
            self._active[dev.id] = desired
            self._held[dev.id] = set()
        if first_error is not None:
            raise first_error

    # ---- events -------------------------------------------------------------------
    def on_event(self, dev_id, function, params):
        """Reader-thread hook for 0x1b04 events: run the card of each newly pressed button."""
        if function != 0:
            return
        held = set(hidpp.parse_diverted_buttons(params))
        with self._lock:
            pressed = held - self._held.get(dev_id, set())
            self._held[dev_id] = held
            actions = [self._active.get(dev_id, {}).get(cid) for cid in sorted(pressed)]
        for events in actions:
            if not events:
                continue
            try:
                self.sink().emit(events)
            except Exception as exc:
                log.warning("injecting for %s failed: %s", dev_id, exc)
