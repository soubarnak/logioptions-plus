"""Profiles v2: the default profile and its per-device assignments.

The UI lists a device on its home page only when the default profile holds at
least one assignment whose slot id starts with the device's slotPrefix
(``<slotPrefix>_c<controlId>``). The real agent seeds those assignments from
the device depot's default_configurations.json; this module does the same,
reading the Logitech-supplied depot at runtime (see resources.py).
"""

import copy
import json
import logging
import os

from . import resources, wheel_relay

log = logging.getLogger("opl.profiles")

TYPE = "type.googleapis.com/"
DEFAULT_PROFILE_ID = "420fd454-0c36-499d-bde4-146823b16147"


def _default_configurations(depot):
    path = os.path.join(resources.depot_root(depot), "default_configurations.json")
    try:
        return json.load(open(path, encoding="utf-8")).get("defaultConfigurations", [])
    except (OSError, ValueError) as exc:
        log.debug("no default configuration for depot %s: %s", depot, exc)
        return []


def default_assignments(slot_prefix, depot):
    """Assignments a fresh install gives one device, from its depot defaults."""
    out = []
    if not depot:
        return out
    for conf in _default_configurations(depot):
        settings = conf.get("settings") or {}
        for cid, card in (settings.get("control_id_mappings") or {}).items():
            out.append({"slotId": "%s_c%s" % (slot_prefix, cid), "cardId": card.get("id", ""),
                        "card": card})
        wheel = settings.get("mouseThumbWheelAction")
        if wheel:
            out.append({"slotId": slot_prefix + THUMB_ADAPTER_SUFFIX, "cardId": wheel.get("id", ""),
                        "card": wheel})
    return out


THUMB_ADAPTER_SUFFIX = "_thumb_wheel_adapter"
THUMB_KEY = "thumb"             # control_key of the thumb wheel's action slot


def control_key(slot_id, slot_prefix):
    """What a button-like slot controls: the control id (int) of <slotPrefix>_c<controlId>,
    THUMB_KEY for the thumb wheel's action slot, else None."""
    if slot_id == slot_prefix + THUMB_ADAPTER_SUFFIX:
        return THUMB_KEY
    head, _, cid = slot_id.rpartition("_c")
    return int(cid) if head == slot_prefix and cid.isdigit() else None


def is_button_slot(slot_id, slot_prefix):
    """True for a control's slot, <slotPrefix>_c<controlId>, and the thumb wheel's action slot."""
    return control_key(slot_id, slot_prefix) is not None


def _with_chosen_buttons(assignments, store, slot_prefix):
    """Replace default button assignments by the user's choice; append choices with no default."""
    chosen = {s: c for s, c in store.cards.items() if is_button_slot(s, slot_prefix)}
    out = []
    for a in assignments:
        card = chosen.pop(a["slotId"], None)
        out.append(a if card is None else {"slotId": a["slotId"], "cardId": card.get("id", ""), "card": card})
    out += [{"slotId": s, "cardId": c.get("id", ""), "card": c} for s, c in chosen.items()]
    return out


MOUSE_SETTINGS_SUFFIX = "_mouse_settings"


def mouse_settings_slot(slot_prefix):
    return slot_prefix + MOUSE_SETTINGS_SUFFIX


def dpi_from_value(value, rng):
    """Map a 0..1 slider position onto the sensor's DPI range/list."""
    value = min(1.0, max(0.0, float(value)))
    step = rng.get("step")
    if step:
        lo, hi = rng["min"], rng["max"]
        return lo + int(round(value * (hi - lo) / step)) * step
    values = rng["values"]
    return values[int(round(value * (len(values) - 1)))]


def value_from_dpi(dpi, rng):
    lo, hi = rng["min"], rng["max"]
    return 0.0 if hi == lo else min(1.0, max(0.0, (dpi - lo) / float(hi - lo)))


def pointer_value(card):
    """The slider position (0..1) in a MOUSE_SETTINGS card, or None."""
    try:
        return float(card["mouseSettings"]["pointerSpeed"]["active"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def mouse_settings_card(value, dpi):
    return {"id": "MOUSE_SETTINGS", "attribute": "MOUSE_SETTINGS",
            "mouseSettings": {"pointerSpeed": {"active": {"value": value, "dpiLevel": int(dpi)}}}}


def default_pointer_value(depot):
    """Pointer speed (0..1) from the depot's default configuration, or None."""
    for conf in _default_configurations(depot):
        card = (conf.get("settings") or {}).get("mouseSettings")
        v = pointer_value({"mouseSettings": card}) if card else None
        if v is not None:
            return v
    return None


DIR_INVERTED, DIR_STANDARD = "NATURAL", "STANDARD"   # logi.protocol.mouse.WheelDirection
SCROLL_SUFFIX = "_mouse_scroll_wheel_settings"
SCROLL_ATTRIBUTE = "MOUSE_SCROLL_WHEEL_SETTINGS"
SENSITIVITY_MIN, SENSITIVITY_MAX = 1, 50       # the panel's SmartShift slider; the 0x2110 threshold


def scroll_slot(slot_prefix):
    return slot_prefix + SCROLL_SUFFIX


def shown_speed(speed, adjustable):
    """The speed a card shows: 0 while the relay cannot apply one, else the value, neutral when unset."""
    if not adjustable:
        return 0
    return speed if isinstance(speed, (int, float)) and speed > 0 else wheel_relay.NEUTRAL_SPEED


def scroll_card(state, speed=0):
    """The scroll wheel card for what the mouse reports ({"smartshift", "wheel"} from
    Registry.read_scroll), or None when it has neither feature. `speed` is the wheel speed
    in force, 0 when it cannot be adjusted."""
    settings = {}
    ss, wheel = state.get("smartshift"), state.get("wheel")
    if ss is not None:
        settings["smartshift"] = {
            "isEnabled": ss["mode"] == "ratchet",
            "sensitivity": min(SENSITIVITY_MAX, max(SENSITIVITY_MIN, int(ss["threshold"])))}
    if wheel is not None:
        settings["isSmooth"] = wheel["hires"]
        settings["dir"] = DIR_INVERTED if wheel["invert"] else DIR_STANDARD
        settings["speed"] = speed
    if not settings:
        return None
    return {"id": SCROLL_ATTRIBUTE, "attribute": SCROLL_ATTRIBUTE, "mouseScrollWheelSettings": settings}


def scroll_request(card):
    """What a scroll wheel card asks for: {ratchet, threshold, hires, invert, speed}, each None
    when the card leaves it alone; None when it is not a scroll wheel card at all."""
    try:
        s = card["mouseScrollWheelSettings"]
    except (KeyError, TypeError):
        return None
    if not isinstance(s, dict):
        return None
    ss = s.get("smartshift") or {}
    req = {"ratchet": ss.get("isEnabled"), "threshold": None,
           "hires": s.get("isSmooth"), "invert": None, "speed": _speed(s.get("speed"))}
    if ss.get("sensitivity") is not None:
        req["threshold"] = min(SENSITIVITY_MAX, max(SENSITIVITY_MIN, int(ss["sensitivity"])))
    if s.get("dir") in (DIR_STANDARD, DIR_INVERTED, "NORMAL", "INVERTED"):    # the last two: old profiles.json
        req["invert"] = s["dir"] in (DIR_INVERTED, "INVERTED")
    return req

THUMB_WHEEL_SUFFIX = "_mouse_thumb_wheel_settings"
THUMB_ATTRIBUTE = "MOUSE_THUMB_WHEEL_SETTINGS"


def thumb_wheel_slot(slot_prefix):
    return slot_prefix + THUMB_WHEEL_SUFFIX


def _speed(value):
    """A card's speed value as a number, or None when it carries none."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def thumb_wheel_card(invert, smooth, speed=0):
    """Thumb wheel card; speed is 0 while the wheel relay cannot apply one."""
    return {"id": THUMB_ATTRIBUTE, "attribute": THUMB_ATTRIBUTE,
            "mouseThumbWheelSettings": {"dir": DIR_INVERTED if invert else DIR_STANDARD,
                                        "isSmooth": bool(smooth), "speed": speed}}


def thumb_speed(card):
    """The speed a thumb wheel card asks for, None where it says nothing."""
    try:
        return _speed(card["mouseThumbWheelSettings"].get("speed"))
    except (KeyError, TypeError, AttributeError):
        return None


def thumb_wheel_fields(card):
    """(invert, smooth) from a thumb wheel card; None where the card says nothing."""
    try:
        s = card["mouseThumbWheelSettings"]
    except (KeyError, TypeError):
        return None, None
    if not isinstance(s, dict):
        return None, None
    invert = {DIR_INVERTED: True, DIR_STANDARD: False}.get(s.get("dir"))
    smooth = s["isSmooth"] if isinstance(s.get("isSmooth"), bool) else None
    return invert, smooth


class ProfileStore:
    """User settings that override the depot defaults, persisted as profiles.json.

    Maps slot id -> card. Written atomically on every change."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "profiles.json")
        self.cards = {}
        self.apps = {}                  # application id -> {slot id: card}: that application's buttons
        self.profile_ids = {}           # profile id the UI made up -> application id it is for
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            self.cards = dict(data.get("assignments") or {})
            self.apps = {a: dict(c) for a, c in (data.get("applications") or {}).items()}
            self.profile_ids = dict(data.get("profileIds") or {})
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("ignoring unreadable %s: %s", self.path, exc)

    def _save(self):
        """Write profiles.json; raises OSError (after putting the in-memory state back as the
        caller found it) so a choice that could not be kept is never reported as stored."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"assignments": self.cards, "applications": self.apps, "profileIds": self.profile_ids},
                          fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot save profiles: %s", exc)
            raise

    def _change(self, mutate):
        """Apply `mutate()` to the cards, save, and undo it when saving fails."""
        before = self.snapshot()
        mutate()
        try:
            self._save()
        except OSError:
            self.cards, self.apps, self.profile_ids = before
            raise

    def snapshot(self):
        return copy.deepcopy((self.cards, self.apps, self.profile_ids))

    def restore(self, snapshot):
        """Put the state back as `snapshot` held it, and save that. Raises OSError when it cannot be saved."""
        self.cards, self.apps, self.profile_ids = copy.deepcopy(snapshot)
        self._save()

    def app_of(self, profile_id):
        """The application a profile id is for: the UI's own id maps to its application, any other id is one."""
        return self.profile_ids.get(profile_id, profile_id)

    def link(self, profile_id, app_id):
        """Remember that the UI's profile `profile_id` is for application `app_id`."""
        if profile_id and profile_id != app_id and self.profile_ids.get(profile_id) != app_id:
            self._change(lambda: self.profile_ids.__setitem__(profile_id, app_id))

    def unlink(self, profile_id):
        """Drop the UI's profile id (the application's buttons are removed separately)."""
        if profile_id in self.profile_ids:
            self._change(lambda: self.profile_ids.pop(profile_id))

    def has_profile(self, app_id):
        return app_id in self.apps or app_id in self.profile_ids.values()

    def get(self, slot_id):
        return self.cards.get(slot_id)

    def set(self, slot_id, card, app_id=None):
        """Keep `card` for a slot, in the default profile or in application `app_id`'s."""
        def put():
            (self.apps.setdefault(app_id, {}) if app_id else self.cards)[slot_id] = copy.deepcopy(card)
        self._change(put)

    def app_cards(self, app_id):
        return self.apps.get(app_id) or {}

    def effective_cards(self, app_id=None):
        """Slot -> card in force while application `app_id` has the focus: the default
        profile's, with the application's own over them."""
        return {**self.cards, **self.app_cards(app_id)}

    def remove_app_buttons(self, app_id, predicate):
        """Forget the application's cards for the slots `predicate` accepts; an application
        left with none has no profile any more."""
        gone = [s for s in self.apps.get(app_id) or {} if predicate(s)]

        def drop():
            cards = self.apps.get(app_id) or {}
            for s in gone:
                del cards[s]
            if not cards:
                self.apps.pop(app_id, None)
        if gone:
            self._change(drop)

    def discard(self, slot_id, app_id=None):
        """Forget one stored card: the application's when `app_id` is given, else the default profile's."""
        if app_id:
            self.remove_app_buttons(app_id, lambda slot: slot == slot_id)
        else:
            self.remove(slot_id)

    def remove(self, slot_id):
        if slot_id in self.cards:
            self._change(lambda: self.cards.pop(slot_id))

    def remove_matching(self, predicate):
        gone = [s for s in self.cards if predicate(s)]
        if gone:
            self._change(lambda: [self.cards.pop(s) for s in gone])


def profiles(devices, store=None, live=None, active_app=None, apps=()):
    """The default profile and one per application. `live` maps device id -> {"dpi", "range"} from the
    device; `apps` are the scanned applications, which name the application profiles."""
    live = live or {}
    assignments = []
    for dev in devices:
        defaults = default_assignments(dev.slot_prefix, dev.depot)
        assignments += _with_chosen_buttons(defaults, store, dev.slot_prefix) if store else defaults
        slot = mouse_settings_slot(dev.slot_prefix)
        card = store.get(slot) if store else None
        rd = live.get(dev.id) or {}
        if card is None and "dpi" in rd:
            card = mouse_settings_card(value_from_dpi(rd["dpi"], rd["range"]), rd["dpi"])
        if card is not None:
            assignments.append({"slotId": slot, "cardId": card.get("id", ""), "card": card})
        slot = scroll_slot(dev.slot_prefix)
        card = (store.get(slot) if store else None) or (live.get(dev.id) or {}).get("scroll")
        if card is not None:
            assignments.append({"slotId": slot, "cardId": card.get("id", ""), "card": card})
        tslot = thumb_wheel_slot(dev.slot_prefix)
        tcard = store.get(tslot) if store else None
        if tcard is None and "thumb" in rd:
            tcard = thumb_wheel_card(rd["thumb"]["invert"], rd["thumb"]["smooth"], rd["thumb"].get("speed", 0))
        if tcard is not None:
            assignments.append({"slotId": tslot, "cardId": tcard.get("id", ""), "card": tcard})
    active = active_app if store and active_app in store.apps else None
    out = [{
        "id": DEFAULT_PROFILE_ID, "name": "PROFILE_NAME_DEFAULT",
        "applicationId": DEFAULT_PROFILE_ID, "activeForApplication": active is None,
        "assignments": assignments, "baseProfileId": "",
    }]
    names = {a.get("applicationId"): a.get("name") for a in apps}
    ids = {app: pid for pid, app in (store.profile_ids if store else {}).items()}
    for app_id in sorted(set(store.apps if store else ()) | set(ids)):
        out.append({
            "id": ids.get(app_id, app_id), "name": names.get(app_id) or app_id, "applicationId": app_id,
            "activeForApplication": app_id == active,
            "assignments": _app_assignments(assignments, store.apps.get(app_id) or {}),
            "baseProfileId": DEFAULT_PROFILE_ID,
        })
    return {"@type": TYPE + "logi.protocol.profiles_v2.Profiles", "profiles": out}


def _app_assignments(base, cards):
    """The default profile's assignments with an application's own cards over its buttons."""
    cards = dict(cards)
    out = []
    for a in base:
        card = cards.pop(a["slotId"], None)
        out.append(a if card is None else {"slotId": a["slotId"], "cardId": card.get("id", ""), "card": card})
    return out + [{"slotId": s, "cardId": c.get("id", ""), "card": c} for s, c in cards.items()]
