"""Input engine: divert buttons through 0x1b04 and turn their presses into injected events.

A button whose assignment is not the depot default is diverted: the mouse stops
acting on it and reports presses to the agent instead, and the engine runs the
assigned card through the virtual input device. A button on its default card is
never diverted. A card that cannot run is refused with Unsupported, which the router
turns into an error the UI shows.

A press plays the card's steps once (a tap): keystroke, system, media and macro cards.
Only a mouse button card is held: its virtual button goes down and up with the real
one, so a remapped click can drag. A keystroke is not held (no auto-repeat from the
mouse; the focused application repeats a key it sees held, so holding would repeat a
shortcut). A macro may wait 5 s in all (MAX_WAIT); the player runs one press at a time
and at most PLAY_QUEUE wait their turn.

The device is the source of truth for what is diverted: every write is read
back, and `sync` reads before it writes, so a mouse that slept, was replugged or
was left diverted by a crashed agent ends up as the store says.
"""

import logging
import queue
import threading
import time

from . import gestures, hidpp, keymap, profiles, sysactions, uinput

log = logging.getLogger("opl.engine")


class Unsupported(Exception):
    """The card or control cannot be run on this system. Carries the text the UI shows."""


class NoVirtualDevice(OSError):
    """/dev/uinput could not be opened (usually a missing permission). The text is the UI's."""


MAX_WAIT = 5.0                  # seconds a card may wait in all; one macro must not hold the player for long
PLAY_QUEUE = 16                 # runs waiting for the player; a press beyond that is dropped


def resolve_card(card):
    """The card that carries the macro: itself, or the nested card it has selected."""
    seen = 0
    while isinstance(card, dict) and not card.get("macro") and seen < 8:
        nested = (card.get("nestedCards") or {}).get(card.get("selectedNestedCard"))
        if not nested:
            break
        card, seen = nested, seen + 1
    return card or {}


# Mouse button hidUsage (HID usage page 9) -> BTN_*; the WIN_ actions are what the kernel sends today.
_MOUSE_BUTTONS = {1: uinput.BTN_LEFT, 2: uinput.BTN_RIGHT, 3: uinput.BTN_MIDDLE,
                  4: uinput.BTN_SIDE, 5: uinput.BTN_EXTRA}
_MOUSE_ACTIONS = {"WIN_BACK": uinput.BTN_SIDE, "WIN_FORWARD": uinput.BTN_EXTRA,
                  "OSX_GESTURE_BACK": uinput.BTN_SIDE, "OSX_GESTURE_FORWARD": uinput.BTN_EXTRA}


class Hold:
    """A mouse button card on a button: the virtual button goes down when the real one does and up
    when it is let go, so a remapped click can drag. Inside a gesture's nested cards, where there
    is no release to wait for, the same action is a tap."""

    def __init__(self, codes):
        self.codes = tuple(codes)


def _tap(codes):
    """Events of a chord: pressed in order, released in reverse."""
    ev = uinput.EV_KEY
    return [(ev, k, 1) for k in codes] + [(ev, k, 0) for k in reversed(codes)]


def _key_event(usage, down):
    """The event for a HID keyboard usage going down or up; raises Unsupported when it has no key."""
    key = keymap.linux_key(usage)
    if key is None:
        raise Unsupported("key code %r has no Linux equivalent" % (usage,))
    return (uinput.EV_KEY, key, 1 if down else 0)


def _compile_sequence(seq):
    steps = []
    waited = 0.0
    for lst in seq.get("componentLists") or []:
        for comp in lst.get("components") or []:
            if "delay" in comp:
                try:
                    seconds = max(0, int((comp["delay"] or {}).get("durationMs") or 0)) / 1000.0
                except (TypeError, ValueError):
                    raise Unsupported("a delay in a macro is not a number of milliseconds")
                waited += seconds
                if waited > MAX_WAIT:
                    raise Unsupported("a macro that waits more than %d seconds is not supported" % MAX_WAIT)
                steps.append(("wait", seconds))
            elif "keyboard" in comp:
                kbd = comp["keyboard"] or {}
                steps.append(("emit", [_key_event(kbd.get("hidUsage"), kbd.get("isDown"))]))
            elif "mouse" in comp:
                btn = (comp["mouse"] or {}).get("button") or {}
                code = _MOUSE_BUTTONS.get(btn.get("hidUsage"))
                if code is None:
                    raise Unsupported("this mouse action in a macro is not supported")
                steps.append(("emit", [(uinput.EV_KEY, code, 1 if btn.get("isDown") else 0)]))
            else:
                raise Unsupported("a %s step in a macro is not supported" % (next(iter(comp), "this kind of")))
    return steps


# Cards of the depot, by id, that carry a Windows chord (Win+Ctrl+Left, Win+D, the Windows key) that no
# Linux desktop binds: the nested cards of the default gesture card and the top-level preset cards
# (card_global_presets_*). They compile to the SYSTEM action of the same meaning, whose chord the user
# can retune in actions.toml. Any other keystroke card injects its chord unchanged. Each entry is
# (action, key code, modifiers) of the depot's own chord: a card of that id whose keystroke has been
# changed is the user's own chord and plays as recorded. The depot marks these cards read-only, so the
# UI should not let a preset keep its id after an edit; the check is a guard, not a known case.
WINDOWS_CHORD_ACTIONS = {
    "inner_card_vd_desktop_left": ("SWITCH_BETWEEN_DESKTOPS_LEFT", 80, (224, 227)),
    "inner_card_vd_desktop_right": ("SWITCH_BETWEEN_DESKTOPS_RIGHT", 79, (224, 227)),
    "inner_card_vd_hide_show_desktop": ("SHOW_DESKTOP", 7, (227,)),
    "inner_card_vd_start_menu": ("TASK_VIEW", None, (227,)),                 # the overview
    "card_global_presets_desktop_left": ("SWITCH_BETWEEN_DESKTOPS_LEFT", 80, (224, 227)),
    "card_global_presets_desktop_right": ("SWITCH_BETWEEN_DESKTOPS_RIGHT", 79, (224, 227)),
    "card_global_presets_win_hide_show_desktop": ("SHOW_DESKTOP", 7, (227,)),
    "card_global_presets_win_switch_language": ("SWITCH_LANGUAGE", 44, (227,)),
    "card_global_presets_win_screen_snip": ("WINDOWS_SCREEN_CAPTURE", 22, (225, 227)),
}


def _windows_chord_action(card):
    """The SYSTEM action `card` stands for, when it is a depot card still holding its Windows chord."""
    entry = WINDOWS_CHORD_ACTIONS.get(card.get("id"))
    macro = card.get("macro") or {}
    if not entry or macro.get("type") != "KEYSTROKE":
        return None
    ks = macro.get("keystroke") or {}
    action, code, mods = entry
    if ks.get("code") == code and tuple(sorted(ks.get("modifiers") or ())) == mods:
        return action
    return None


def compile_card(card, table=None):
    """The steps a press of `card` plays: ("emit", events), ("wait", seconds) or ("system", action).
    Raises Unsupported. System actions are resolved when pressed, so the user's file applies live."""
    table = table or sysactions.ActionTable()
    card = resolve_card(card)                       # a wrapper plays the card it has selected
    action = _windows_chord_action(card)
    if action:
        action = table.card_action(card.get("id")) or action      # a desktop may read the card differently
        return compile_card({"macro": {"type": "SYSTEM", "system": {"action": action}}}, table)
    macro = card.get("macro") or {}
    kind = macro.get("type")
    if kind == "KEYSTROKE":
        ks = macro.get("keystroke") or {}
        mods = []
        for usage in ks.get("modifiers") or []:
            if usage not in keymap.MODIFIER_USAGES:
                raise Unsupported("modifier %r is not a modifier key" % (usage,))
            mods.append(keymap.linux_key(usage))
        if ks.get("code") is None and mods:
            return [("emit", _tap(mods))]               # a modifier on its own, such as the Windows key
        key = keymap.linux_key(ks.get("code"))
        if key is None:
            raise Unsupported("key code %r has no Linux equivalent" % (ks.get("code"),))
        return [("emit", _tap(mods + [key]))]
    if kind == "MOUSE":
        mouse = macro.get("mouse") or {}
        code = (_MOUSE_BUTTONS.get(mouse.get("hidUsage")) if mouse.get("action") == "BUTTON"
                else _MOUSE_ACTIONS.get(mouse.get("action")))
        if code is None:
            raise Unsupported("mouse action %r is not supported" % (mouse.get("action"),))
        return [("emit", _tap([code]))]
    if kind == "SYSTEM":
        action = (macro.get("system") or {}).get("action")
        if action in sysactions.SCROLL_ACTIONS:
            sign = sysactions.SCROLL_ACTIONS[action]
            return [("emit", [(uinput.EV_REL, uinput.REL_HWHEEL_HI_RES, sign * 120),
                              (uinput.EV_REL, uinput.REL_HWHEEL, sign)])]
        if table.chord(action) is None:
            raise Unsupported("%s has no Linux keystroke; set one in actions.toml" % (action,))
        return [("system", action)]
    if kind == "MEDIA":
        usage = (macro.get("media") or {}).get("usage")
        key = sysactions.MEDIA_KEYS.get(usage)
        if key is None:
            raise Unsupported("media action %r is not supported" % (usage,))
        return [("emit", _tap([key]))]
    if kind == "INPUT_SEQUENCE":
        return _compile_sequence(macro.get("inputSequence") or {})
    raise Unsupported("%s actions are not supported yet" % (kind or "this kind of"))


def _mouse_codes(card):
    """The virtual buttons a MOUSE card presses, or None when it is not one that can be held."""
    macro = resolve_card(card).get("macro") or {}
    if macro.get("type") != "MOUSE":
        return None
    steps = compile_card(card)
    return [code for t, code, v in steps[0][1] if v]


def compile_binding(card, table=None):
    """What a press does: the steps of a plain card, a `Hold` for a mouse button card, a
    `gestures.Gesture` for a four-way card, a `gestures.Thumb` for a two-way card. Raises
    Unsupported when any action cannot run."""
    card = gestures.adapter(card) or card           # a ONE_OF card runs the adapter it selects
    kind = gestures.kind(card)
    if kind is None:
        codes = _mouse_codes(card)
        return Hold(codes) if codes else compile_card(card, table)
    nested = gestures.nested_actions(card)
    if not nested:
        preset = gestures.preset_name(card)
        if kind == gestures.FOUR_WAYS and preset in gestures.PRESETS:
            actions = {word: compile_card({"macro": {"type": "SYSTEM", "system": {"action": action}}}, table)
                       for word, action in gestures.PRESETS[preset].items()}
            return gestures.Gesture(actions, gestures.swipe_limit(card))
        if preset:
            raise Unsupported("the %s gesture preset is not supported; assign an action to each direction" % preset)
        raise Unsupported("this action card has no actions assigned")
    actions = {word: compile_card(sub, table) for word, sub in nested.items()}
    if kind == gestures.FOUR_WAYS:
        return gestures.Gesture(actions, gestures.swipe_limit(card))
    return gestures.Thumb(actions)


class InputEngine:
    """Per-device diversions and the actions behind them. Methods that talk to the mouse block.

    Threads: the device writes of one mouse (`assign`, `release`, `sync`, `activate`) run one at a
    time under that device's lock, so a wake-up resync cannot interleave its reads and writes with
    a UI request; the data the event hooks read (`_active`, `_held`, `_swipes`) sits behind
    `_lock`, which is never held across device I/O."""

    def __init__(self, registry, store, sink_factory=None, actions_path=None):
        self.reg = registry
        self.store = store
        self.sink_factory = sink_factory or uinput.open_default
        self.table = sysactions.ActionTable(actions_path)
        self._sink = None
        self._plays = queue.Queue(PLAY_QUEUE)   # (dev_id, generation, steps) of presses, played one at a time
        self._player = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._io = {}                   # dev_id -> RLock held across the device writes of that mouse
        self._gen = {}                  # dev_id -> generation; a run of an older one is abandoned
        self._active = {}               # dev_id -> {control key: steps | Hold | Gesture | Thumb}
        self._held = {}                 # dev_id -> set of cids held down now
        self._holding = {}              # dev_id -> {cid: key codes} a Hold keeps pressed now
        self.active_app = None          # application id whose profile is in force; None: the default
        self._thumb_warned = set()      # dev ids whose unreadable thumb wheel info was already logged
        self._swipes = {}              # dev_id -> (cid, Swipe) of the gesture button press in progress
        self.held_elsewhere = lambda dev_id, key: False   # True: another owner (the wheel relay) diverted `key`
        self.on_stale = []              # callables(dev_id, slot_id, app_id, message): a stored card that no longer runs
        registry.on_button_event.append(self.on_event)
        registry.on_thumb_event.append(self.on_thumb_event)
        registry.on_link_lost.append(self.link_lost)

    def _dev_lock(self, dev_id):
        with self._lock:
            return self._io.setdefault(dev_id, threading.RLock())

    # ---- the virtual device ------------------------------------------------
    def sink(self):
        """The one virtual input device, created on first use. Raises NoVirtualDevice (an OSError)
        when it cannot be."""
        with self._lock:
            if self._sink is None:
                try:
                    self._sink = self.sink_factory()
                except OSError as exc:
                    raise NoVirtualDevice("%s could not be opened: %s" % (uinput.UINPUT_PATH, exc)) from exc
            return self._sink

    def _drop_sink(self, dev):
        """Close and forget `dev` after a failed write: the kernel lifts its keys, and the next
        use makes a fresh one."""
        with self._lock:
            if self._sink is dev:
                self._sink = None
        try:
            dev.close()
        except Exception:
            log.debug("closing the virtual device failed", exc_info=True)

    def close(self):
        self._stop.set()
        with self._plays.mutex:
            self._plays.unfinished_tasks -= len(self._plays.queue)
            self._plays.queue.clear()
        try:
            self._plays.put_nowait(None)
        except queue.Full:
            pass
        player = self._player
        if player is not None and player is not threading.current_thread():
            player.join(2.0)            # a macro in its wait ends at once; one in a write gets a moment
            if player.is_alive():
                log.warning("the player thread did not stop; closing the virtual device under it")
        with self._lock:
            sink, self._sink = self._sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception:
                log.debug("closing the virtual device failed", exc_info=True)

    def release_all(self):
        """Blocking, best effort at shutdown: hand every control this engine diverted (the thumb
        wheel included) back to its mouse, so none stays dead after the agent is gone. A mouse
        that stops answering is given up on; the others still get their turn."""
        with self._lock:
            held = {dev_id: list(keys) for dev_id, keys in self._active.items() if keys}
        for dev_id, keys in held.items():
            dev = self.reg.devices.get(dev_id)
            if dev is None:
                continue
            with self._dev_lock(dev_id):
                for key in keys:
                    try:
                        self._write(dev, key, None)
                    except Exception as exc:
                        log.warning("handing control %s of %s back failed: %s", key, dev_id, exc)
                        if isinstance(exc, hidpp.HidppTimeout):
                            break
                        continue
                    with self._lock:
                        self._active.get(dev_id, {}).pop(key, None)

    def owns_thumb(self, dev_id):
        """Whether a thumb action card is bound on `dev_id`: the engine then owns the thumb wheel."""
        with self._lock:
            return profiles.THUMB_KEY in self._active.get(dev_id, {})

    # ---- assignments -----------------------------------------------------------
    def _divertable(self, dev, cid):
        try:
            controls = self.reg.controls(dev.id)
        except hidpp.FeatureNotSupported:
            raise Unsupported("this device has no reprogrammable buttons")
        info = controls.get(cid)
        if info is None or not info["divertable"]:
            raise Unsupported("this button cannot be reassigned")
        return info

    def _controls(self, dev):
        """The mouse's reprogrammable controls; empty when it has none."""
        try:
            return self.reg.controls(dev.id)
        except hidpp.FeatureNotSupported:
            return {}

    def _write(self, dev, key, binding):
        """Blocking: put the mouse in the state `binding` (None: native) needs for control `key`."""
        if key == profiles.THUMB_KEY:
            self.reg.set_thumb_divert(dev.id, binding is not None)
            return
        info = self.reg.controls(dev.id)[key]
        raw = isinstance(binding, gestures.Gesture)
        self.reg.set_divert(dev.id, key, binding is not None, raw if info["raw_xy"] else None)

    def _tune(self, dev, binding):
        """Blocking: give a thumb binding its step, the wheel's diverted units per notch, and
        the direction a positive rotation points."""
        if isinstance(binding, gestures.Thumb):
            try:
                info = self.reg.thumb_info(dev.id)
                units, right = info["per_notch"], info["positive_is_right"]
            except Exception as exc:
                if dev.id not in self._thumb_warned:       # once per device, not on every sync
                    self._thumb_warned.add(dev.id)
                    log.warning("thumb wheel resolution unknown for %s, assuming the usual direction: %s", dev.id, exc)
                units, right = 0, False       # the only mouse seen reports a turn to the right as negative
            binding.step = units if units and units > 0 else gestures.THUMB_STEP
            binding.sign = 1 if right else -1

    def _check(self, dev, key, binding):
        """Raise Unsupported unless control `key` of `dev` can run `binding`."""
        if key == profiles.THUMB_KEY:
            if not dev.thumb_wheel:
                raise Unsupported("this device has no thumb wheel")
            if not isinstance(binding, gestures.Thumb):
                raise Unsupported("only a two-way action can be assigned to the thumb wheel")
        else:
            info = self._divertable(dev, key)
            if isinstance(binding, gestures.Thumb):
                raise Unsupported("a two-way action can only be assigned to the thumb wheel")
            if isinstance(binding, gestures.Gesture) and not info["raw_xy"]:
                raise Unsupported("this button cannot do gestures")

    def assign(self, dev, key, card):
        """Make control `key` (a control id, or profiles.THUMB_KEY) run `card`. Blocking. Raises
        Unsupported for a card or control that cannot, any other error when the device write
        fails; nothing changes then."""
        binding = compile_binding(card, self.table)
        self._check(dev, key, binding)
        with self._dev_lock(dev.id):
            self.sink()                 # never divert a control that could not be injected for
            self._tune(dev, binding)
            self._write(dev, key, binding)
            with self._lock:
                self._active.setdefault(dev.id, {})[key] = binding
                swipe = self._swipes.get(dev.id)
                if swipe and swipe[0] == key:
                    del self._swipes[dev.id]    # the press in progress belonged to the old card
            self._let_go(dev.id, key)

    def validate(self, dev, key, card):
        """Raise Unsupported unless control `key` could run `card`; touches nothing."""
        self._check(dev, key, compile_binding(card, self.table))
        self.sink()

    def release(self, dev, key):
        """Hand control `key` back to the mouse. Blocking; raises when the device write fails,
        and the control then keeps running its card."""
        with self._dev_lock(dev.id):
            if key == profiles.THUMB_KEY:
                if not dev.thumb_wheel:
                    return
            else:
                try:
                    self.reg.controls(dev.id)
                except hidpp.FeatureNotSupported:
                    return              # nothing was ever diverted
            self._write(dev, key, None)
            with self._lock:
                self._active.get(dev.id, {}).pop(key, None)
                swipe = self._swipes.get(dev.id)
                if swipe and swipe[0] == key:
                    del self._swipes[dev.id]
            self._cancel(dev.id)
            self._let_go(dev.id, key)

    def _desired(self, dev):
        """{control key: binding} the store asks for on `dev`: non-default cards that can run. A
        stored card that no longer compiles is reported through `on_stale`."""
        defaults = {a["slotId"]: a["cardId"]
                    for a in profiles.default_assignments(dev.slot_prefix, dev.depot)}
        out, stale = {}, []
        own = self.store.app_cards(self.active_app)
        # a default card the mouse cannot run itself (the gesture button's) is diverted with no choice stored
        cards = {a["slotId"]: a["card"] for a in profiles.default_assignments(dev.slot_prefix, dev.depot)
                 if profiles.needs_software(a["card"])}
        cards.update(self.store.effective_cards(self.active_app))
        for slot_id, card in cards.items():
            key = profiles.control_key(slot_id, dev.slot_prefix)
            if key is None or (card.get("id") == defaults.get(slot_id) and not profiles.needs_software(card)):
                continue
            try:
                binding = compile_binding(card, self.table)
            except Unsupported as exc:
                log.warning("button %s: %s", slot_id, exc)
                if slot_id in self.store.effective_cards(self.active_app):
                    stale.append((slot_id, self.active_app if slot_id in own else None, str(exc)))
                continue
            self._tune(dev, binding)
            out[key] = binding
        for slot_id, app, message in stale:
            for hook in list(self.on_stale):
                try:
                    hook(dev.id, slot_id, app, message)
                except Exception:
                    log.exception("stale card hook failed")
        return out

    def activate(self, app_id, devices):
        """Blocking: put application `app_id`'s profile in force (None: the default one). Per
        device, only the controls whose diversion differs are written; a control that stays
        diverted the same way just runs its new card. The first error is raised after every
        device is tried, and the profile in force is then the one before, so the switch is tried
        again."""
        previous, self.active_app = self.active_app, app_id
        first_error = None
        for dev in devices:
            try:
                self._rebase(dev)
            except Exception as exc:
                log.warning("switching %s to the profile of %s failed: %s", dev.id, app_id or "the default", exc)
                first_error = first_error or exc
        if first_error is not None:
            self.active_app = previous
            raise first_error

    @staticmethod
    def _diversion(binding):
        """What the mouse must be told for `binding`: nothing differs between plain cards."""
        return None if binding is None else isinstance(binding, gestures.Gesture)

    def _rebase(self, dev):
        with self._dev_lock(dev.id):
            controls = self._controls(dev)
            if not controls and not dev.thumb_wheel:
                return
            desired = {k: b for k, b in self._desired(dev).items()
                       if k in controls or (k == profiles.THUMB_KEY and dev.thumb_wheel)}
            with self._lock:
                current = dict(self._active.get(dev.id, {}))
            had = set(current)
            first_error = None
            for key in sorted(set(current) | set(desired), key=str):
                old, new = current.get(key), desired.get(key)
                if self._diversion(old) == self._diversion(new):
                    if new is not None:
                        current[key] = new
                    continue
                try:
                    if new is not None:
                        self.sink()
                    self._write(dev, key, new)
                except Exception as exc:
                    first_error = first_error or exc
                    if isinstance(exc, hidpp.HidppTimeout):
                        break
                    continue
                if new is not None:
                    current[key] = new
                else:
                    del current[key]
            with self._lock:
                self._active[dev.id] = current
                held = self._held.get(dev.id)
                if held is not None:
                    held &= set(current)        # a button handed back sends no release any more
                swipe = self._swipes.get(dev.id)
                if swipe and not isinstance(current.get(swipe[0]), gestures.Gesture):
                    del self._swipes[dev.id]
                stale_holds = [k for k in self._holding.get(dev.id, {}) if not isinstance(current.get(k), Hold)]
            if had - set(current):
                self._cancel(dev.id)        # a button handed back: its queued actions go with it
            self._let_go(dev.id, *stale_holds)
            if first_error is not None:
                raise first_error

    def sync(self, dev):
        """Blocking: bring the mouse's diversions in line with the store. Reads each divertable
        control first, so it also clears a diversion nothing asked for. A control that fails does
        not stop the others; the first error is raised at the end. What is tracked afterwards is
        what was written: a control the mouse stopped answering for keeps what was known."""
        with self._dev_lock(dev.id):
            controls = self._controls(dev)
            todo = [(cid, info) for cid, info in controls.items() if info["divertable"]]
            if dev.thumb_wheel:
                todo.append((profiles.THUMB_KEY, None))
            if not todo:
                return
            self._cancel(dev.id)
            desired = self._desired(dev)
            with self._lock:
                before = dict(self._active.get(dev.id, {}))
            now, first_error, tried = {}, None, set()
            for key, _info in todo:
                binding = desired.get(key)
                if binding is None and self.held_elsewhere(dev.id, key):
                    continue            # not ours to hand back; writing it would only be undone again
                tried.add(key)
                try:
                    if binding is not None:
                        try:
                            self.sink()
                        except Exception:
                            # no way to inject: hand the control back so the mouse does not
                            # keep a diversion nothing tracks any more
                            self._write(dev, key, None)
                            raise
                    self._write(dev, key, binding)
                except Exception as exc:
                    log.warning("control %s on %s: %s", key, dev.id, exc)
                    first_error = first_error or exc
                    if isinstance(exc, hidpp.HidppTimeout):
                        break           # the mouse is not answering: do not wait per control
                    continue
                if binding is not None:
                    now[key] = binding
            for key, _info in todo:
                if key not in tried and key in before:
                    now[key] = before[key]      # never reached: keep what was known
            with self._lock:
                self._active[dev.id] = now
                self._held[dev.id] = set()
                self._swipes.pop(dev.id, None)
            self._let_go(dev.id)
            if first_error is not None:
                raise first_error

    # ---- events -------------------------------------------------------------------
    def on_event(self, dev_id, function, params):
        """Reader-thread hook for 0x1b04 events: button presses and releases (function 0), and
        raw motion while a gesture button is held (function 1)."""
        if function == 1:
            dx, dy = hidpp.parse_raw_xy(params)
            with self._lock:
                swipe = self._swipes.get(dev_id)
                events = swipe[1].move(dx, dy) if swipe else None
            self._emit(dev_id, [events])
            return
        if function != 0:
            return
        held = set(hidpp.parse_diverted_buttons(params))
        with self._lock:
            before = self._held.get(dev_id, set())
            self._held[dev_id] = held
            active = self._active.get(dev_id, {})
            holding = self._holding.setdefault(dev_id, {})
            actions = []
            for cid in sorted(held - before):
                binding = active.get(cid)
                if isinstance(binding, gestures.Gesture):
                    self._swipes[dev_id] = (cid, binding.start())
                elif isinstance(binding, Hold):
                    holding[cid] = binding.codes
                    actions.append([("hold", binding.codes)])
                elif binding:
                    actions.append(binding)
            for cid in sorted(before - held):
                codes = holding.pop(cid, None)
                if codes:
                    actions.append([("lift", codes)])
            swipe = self._swipes.get(dev_id)
            if swipe and swipe[0] in before - held:
                del self._swipes[dev_id]
                actions.append(swipe[1].release())
        self._emit(dev_id, actions)

    def on_thumb_event(self, dev_id, function, params):
        """Reader-thread hook for 0x2150 events: the thumb wheel turned."""
        if function != 0:
            return
        with self._lock:
            binding = self._active.get(dev_id, {}).get(profiles.THUMB_KEY)
            rotation = hidpp.parse_thumb_event(params)
            actions = binding.turn(rotation) if binding and rotation is not None else []
        self._emit(dev_id, actions)

    def link_lost(self, dev_id):
        """Registry hook: the mouse went out of reach, and a release it would have sent is lost.
        Forget what was held (else the next press of that button looks like a repeat and is
        swallowed), end a swipe in progress and lift the virtual buttons held for it."""
        with self._lock:
            self._held[dev_id] = set()
            self._swipes.pop(dev_id, None)
        self._let_go(dev_id)

    def _let_go(self, dev_id, *keys):
        """Lift the virtual buttons held for `keys` (all of the device's when none are named)."""
        with self._lock:
            holding = self._holding.get(dev_id, {})
            lifts = [holding.pop(k) for k in (keys or list(holding)) if k in holding]
        self._emit(dev_id, [[("lift", codes)] for codes in lifts])

    def _emit(self, dev_id, actions):
        """Queue the steps of each action for the player thread, in order."""
        for steps in actions:
            if not steps or self._stop.is_set():
                continue
            self._start_player()
            with self._lock:
                gen = self._gen.get(dev_id, 0)
            try:
                self._plays.put_nowait((dev_id, gen, steps))
            except queue.Full:
                log.warning("too many actions waiting; dropped one")

    def _cancel(self, dev_id):
        """Drop what `dev_id` has queued or is playing: its mouse was handed back or resynced.
        A lift (letting go of a held button) always survives."""
        with self._lock:
            self._gen[dev_id] = self._gen.get(dev_id, 0) + 1
        with self._plays.mutex:
            keep = [it for it in self._plays.queue
                    if it is None or it[0] != dev_id or all(k == "lift" for k, _ in it[2])]
            self._plays.unfinished_tasks -= len(self._plays.queue) - len(keep)
            self._plays.queue.clear()
            self._plays.queue.extend(keep)

    # ---- playback ------------------------------------------------------------------
    def _start_player(self):
        with self._lock:
            if self._player is None and not self._stop.is_set():
                self._player = threading.Thread(target=self._play_loop, name="opl-play", daemon=True)
                self._player.start()

    def _play_loop(self):
        while not self._stop.is_set():
            item = self._plays.get()
            if item is None:
                return
            try:
                self._play(*item)
            except Exception as exc:
                log.warning("running the card failed: %s", exc)
            finally:
                self._plays.task_done()     # tests wait on unfinished_tasks to know playback is idle

    def _abandoned(self, dev_id, gen):
        with self._lock:
            return self._stop.is_set() or self._gen.get(dev_id, 0) != gen

    def _wait(self, dev_id, gen, seconds):
        """Sleep `seconds`, in slices; False when the run was abandoned or the engine closed."""
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return True
            if self._stop.wait(min(left, 0.05)) or self._abandoned(dev_id, gen):
                return False

    def _play(self, dev_id, gen, steps):
        """Run the steps in order. Keys still down when the run ends or fails are released; if
        that fails too the device is closed (the kernel lifts what it holds)."""
        dev, down = None, []
        try:
            for kind, arg in steps:
                if kind != "lift" and self._abandoned(dev_id, gen):
                    return
                if kind == "wait":
                    if not self._wait(dev_id, gen, arg):
                        return
                    continue
                if kind == "system":
                    chord = self.table.chord(arg)
                    if chord is None:
                        log.warning("system action %s has no keystroke any more", arg)
                        continue
                    kind, arg = "emit", _tap(chord)
                held = kind in ("hold", "lift")
                if kind == "hold":
                    arg = [(uinput.EV_KEY, c, 1) for c in arg]
                elif kind == "lift":
                    arg = [(uinput.EV_KEY, c, 0) for c in reversed(arg)]
                if dev is None:
                    dev = self.sink()
                if held:
                    dev.emit(arg)           # a held button outlives the run: not released below
                    continue
                for ev_type, code, value in arg:
                    if ev_type == uinput.EV_KEY and value:
                        down.append(code)   # noted first: a write cut short may still have pressed it
                dev.emit(arg)
                for ev_type, code, value in arg:
                    if ev_type == uinput.EV_KEY and not value and code in down:
                        down.remove(code)
        finally:
            for code in reversed(down):
                try:
                    dev.emit([(uinput.EV_KEY, code, 0)])
                except Exception:
                    log.warning("releasing key %d failed; closing the virtual device", code, exc_info=True)
                    self._drop_sink(dev)
                    break
