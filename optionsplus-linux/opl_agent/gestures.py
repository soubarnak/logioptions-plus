"""Gesture button (ADAPTER_4WAYS) and thumb wheel (ADAPTER_2WAYS) cards.

Pure logic, no device I/O. A card of these kinds carries one nested card per action, keyed by a
direction word ("up", "down", "left", "right"; the gesture card also "click", the press with no
swipe). The key or the nested card's id may carry the word as one of its tokens, so
"gesture_left" and "left" both work. The real depot layout is not confirmed on hardware: see
feature-parity.md.

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

DEFAULT_LIMIT = 50              # raw XY counts of travel before a swipe counts, when the card has none
THUMB_STEP = 1                  # thumb wheel reporting units per action


def kind(card):
    """FOUR_WAYS, TWO_WAYS, or None for a card that is neither."""
    attribute = (card or {}).get("attribute")
    return attribute if attribute in WORDS else None


def _word(key, nested, allowed):
    for text in (key, (nested or {}).get("id")):
        for token in re.split(r"[^a-z0-9]+", str(text or "").lower()):
            token = ALIASES.get(token, token)
            if token in allowed:
                return token
    return None


def nested_actions(card):
    """{direction word: nested card} the card assigns, for a card of either kind."""
    allowed = WORDS[kind(card)]
    out = {}
    for key, nested in (card.get("nestedCards") or {}).items():
        word = _word(key, nested, allowed)
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
    """The compiled thumb wheel: events for a step left and right."""

    def __init__(self, actions):
        self.actions = actions
        self.pending = 0

    def turn(self, amount):
        """Rotation reported by the wheel (right is positive) -> events, one run per step."""
        if amount and self.pending and (amount > 0) != (self.pending > 0):
            self.pending = 0                    # reversed: the partial step does not carry over
        self.pending += amount
        out = []
        while abs(self.pending) >= THUMB_STEP:
            sign = 1 if self.pending > 0 else -1
            self.pending -= sign * THUMB_STEP
            events = self.actions.get("right" if sign > 0 else "left")
            if events:
                out.append(events)
        return out
