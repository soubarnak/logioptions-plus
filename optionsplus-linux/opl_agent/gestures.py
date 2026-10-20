"""Gesture button (ADAPTER_4WAYS) and thumb wheel (ADAPTER_2WAYS) cards.

Pure logic, no device I/O. A card of these kinds carries one nested card per action, keyed by a
direction word ("up", "down", "left", "right"; the gesture card also "click", the press with no
swipe). The key of the nested card carries the word as one of its tokens, so "gesture_left" and
"left" both work. The nested card's own id never counts: it names the action ("..._volume_up"),
not the slot it sits in. A key without a word falls back to its place in `nestedCardsOrder`,
matched with the same place in `gestureInfo.directions` (UP, DOWN, LEFT, RIGHT) when the two lists
are as long as each other. The real depot layout is not confirmed on hardware: see feature-parity.md.

Presets: a card whose `gestureInfo.implementation` names a Logitech preset (CUSTOM_SWITCH_DESKTOPS,
CUSTOM_APP_EXPOSE_MISSION_CONTROL, ...) and that carries no nested cards would need the preset's own
actions, which are not in the data. Those are refused (`preset_name`), except the ones in `PRESETS`,
whose actions are system actions with a Linux keystroke: CUSTOM_SWITCH_DESKTOPS swipes left and right
to the desktop on that side (assumed to match the Windows preset; confirm on hardware). GENERIC, and
presets that carry their actions as nested cards, run.

The swipe distance comes from `gestureInfo.x/y.actionThreshold` (a protobuf field of the real
schema, profiles_v2.proto GestureInfo.AxisInfo). Captures of the real card carry only
directionLocked, implementation and directions, so in practice DEFAULT_LIMIT applies; the
threshold path is exercised by tests only.

The engine compiles each nested card to events and hands them to `Gesture` or `Thumb`, which only
decide *which* events a stream of raw motion asks for.
"""

import re

FOUR_WAYS, TWO_WAYS = "ADAPTER_4WAYS", "ADAPTER_2WAYS"
SWIPES = ("up", "down", "left", "right")
CLICK = "click"
WORDS = {FOUR_WAYS: SWIPES + (CLICK,), TWO_WAYS: ("left", "right")}
# other words a depot may use for the press without movement
ALIASES = {"press": CLICK, "tap": CLICK, "center": CLICK, "centre": CLICK}

# preset -> {direction word: SYSTEM action} for a preset card that carries no nested cards
PRESETS = {"CUSTOM_SWITCH_DESKTOPS": {"left": "SWITCH_BETWEEN_DESKTOPS_LEFT",
                                      "right": "SWITCH_BETWEEN_DESKTOPS_RIGHT"}}

DEFAULT_LIMIT = 50              # raw XY counts of travel before a swipe counts, when the card has none
# Thumb wheel reporting units per action when the wheel's own resolution is unknown. Assumption:
# getThumbwheelInfo's diverted resolution is the units one notch-equivalent turn reports, and
# 14 is the value the MX Master 3 family is believed to give; confirm on hardware.
THUMB_STEP = 14


def kind(card):
    """FOUR_WAYS, TWO_WAYS, or None for a card that is neither."""
    attribute = (card or {}).get("attribute")
    return attribute if attribute in WORDS else None


def adapter(card):
    """The gesture adapter card `card` is or selects: a ONE_OF card (the depot's default for the gesture
    button) hands over to its selected nested card. None when no adapter is reached."""
    for _ in range(4):
        if kind(card):
            return card
        card = ((card or {}).get("nestedCards") or {}).get((card or {}).get("selectedNestedCard"))
        if not isinstance(card, dict):
            return None
    return None


def _word(key, allowed):
    """The direction word in a nested card's slot key, else None."""
    for token in re.split(r"[^a-z0-9]+", str(key or "").lower()):
        token = ALIASES.get(token, token)
        if token in allowed:
            return token
    return None


def _ordered_words(card, allowed):
    """{slot key: word} read from `nestedCardsOrder` against `gestureInfo.directions`, when both
    list the same number of places; the key itself carries no word then."""
    order = card.get("nestedCardsOrder") or []
    directions = (card.get("gestureInfo") or {}).get("directions") or []
    if not order or len(order) != len(directions):
        return {}
    out = {}
    for key, entry in zip(order, directions):
        name = str((entry or {}).get("direction", "")).lower() if isinstance(entry, dict) else ""
        if name in allowed:
            out[key] = name
    return out


def preset_name(card):
    """The Logitech preset a card's gestureInfo names, or None for a generic card."""
    impl = ((card or {}).get("gestureInfo") or {}).get("implementation")
    return None if impl in (None, "", 0, "GENERIC") else str(impl)


def nested_actions(card):
    """{direction word: nested card} the card assigns, for a card of either kind."""
    allowed = WORDS[kind(card)]
    nested_cards = card.get("nestedCards") or {}
    by_order = _ordered_words(card, allowed)
    out = {}
    for key, nested in nested_cards.items():
        word = _word(key, allowed) or by_order.get(key)
        if word and word not in out:
            out[word] = nested
    return out


def swipe_limit(card):
    """Travel in raw XY counts a swipe needs: the mid point of the card's device-unit action
    threshold (gestureInfo, x axis before y), else DEFAULT_LIMIT."""
    info = (card or {}).get("gestureInfo") or {}
    for axis in ("x", "y"):
        t = (info.get(axis) or {}).get("actionThreshold") or {}
        if t.get("units", "DEVICE") not in ("DEVICE", 0):
            continue
        low, high = t.get("minimumSensitivityThreshold") or 0, t.get("maximumSensitivityThreshold") or 0
        if low or high:
            return max(1, (low + high) // 2)
    return DEFAULT_LIMIT


class Gesture:
    """The compiled gesture button: events per action word and the swipe distance."""

    def __init__(self, actions, limit=DEFAULT_LIMIT):
        self.actions = actions
        self.limit = limit

    def start(self):
        return Swipe(self)


class Swipe:
    """One press of the gesture button, from press to release."""

    def __init__(self, gesture):
        self.gesture = gesture
        self.dx = self.dy = 0
        self.done = False           # a swipe already ran its action: no more for this press

    def move(self, dx, dy):
        """Raw motion while held -> the events of the swipe it completes, else None."""
        if self.done:
            return None
        self.dx, self.dy = self.dx + dx, self.dy + dy
        if max(abs(self.dx), abs(self.dy)) < self.gesture.limit:
            return None
        self.done = True
        if abs(self.dx) >= abs(self.dy):
            word = "right" if self.dx > 0 else "left"
        else:
            word = "down" if self.dy > 0 else "up"
        return self.gesture.actions.get(word)

    def release(self):
        """The events of the click action when the press never became a swipe, else None."""
        return None if self.done else self.gesture.actions.get(CLICK)


class Thumb:
    """The compiled thumb wheel: events for a step left and right. A step is `step` reporting
    units (the wheel's diverted units per notch, set by the engine), so a short flick is not
    dozens of actions."""

    def __init__(self, actions, step=THUMB_STEP):
        self.actions = actions
        self.step = max(1, int(step))   # reporting units that make one action
        self.pending = 0

    def turn(self, amount):
        """Rotation reported by the wheel (right is positive) -> events, one run per step."""
        if amount and self.pending and (amount > 0) != (self.pending > 0):
            self.pending = 0                    # reversed: the partial step does not carry over
        self.pending += amount
        out = []
        while abs(self.pending) >= self.step:
            sign = 1 if self.pending > 0 else -1
            self.pending -= sign * self.step
            events = self.actions.get("right" if sign > 0 else "left")
            if events:
                out.append(events)
        return out
