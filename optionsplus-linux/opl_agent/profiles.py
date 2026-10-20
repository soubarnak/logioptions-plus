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

from . import resources

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
            out.append({"slotId": slot_prefix + "_thumb_wheel_adapter", "cardId": wheel.get("id", ""),
                        "card": wheel})
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


THUMB_WHEEL_SUFFIX = "_mouse_thumb_wheel_settings"
THUMB_ATTRIBUTE = "MOUSE_THUMB_WHEEL_SETTINGS"
DIR_INVERTED, DIR_STANDARD = "NATURAL", "STANDARD"   # logi.protocol.mouse.WheelDirection


def thumb_wheel_slot(slot_prefix):
    return slot_prefix + THUMB_WHEEL_SUFFIX


def thumb_wheel_card(invert, smooth):
    """Thumb wheel card; speed stays 0 (not adjustable) until the relay exists."""
    return {"id": THUMB_ATTRIBUTE, "attribute": THUMB_ATTRIBUTE,
            "mouseThumbWheelSettings": {"dir": DIR_INVERTED if invert else DIR_STANDARD,
                                        "isSmooth": bool(smooth), "speed": 0}}


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
        try:
            with open(self.path) as fh:
                self.cards = dict(json.load(fh).get("assignments") or {})
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("ignoring unreadable %s: %s", self.path, exc)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"assignments": self.cards}, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot save profiles: %s", exc)

    def get(self, slot_id):
        return self.cards.get(slot_id)

    def set(self, slot_id, card):
        self.cards[slot_id] = copy.deepcopy(card)
        self._save()

    def remove(self, slot_id):
        if self.cards.pop(slot_id, None) is not None:
            self._save()


def profiles(devices, store=None, live=None):
    """The default profile. `live` maps device id -> {"dpi", "range"} from the device."""
    live = live or {}
    assignments = []
    for dev in devices:
        assignments += default_assignments(dev.slot_prefix, dev.depot)
        slot = mouse_settings_slot(dev.slot_prefix)
        card = store.get(slot) if store else None
        rd = live.get(dev.id) or {}
        if card is None and "dpi" in rd:
            card = mouse_settings_card(value_from_dpi(rd["dpi"], rd["range"]), rd["dpi"])
        if card is not None:
            assignments.append({"slotId": slot, "cardId": card.get("id", ""), "card": card})
        tslot = thumb_wheel_slot(dev.slot_prefix)
        tcard = store.get(tslot) if store else None
        if tcard is None and "thumb" in rd:
            tcard = thumb_wheel_card(rd["thumb"]["invert"], rd["thumb"]["smooth"])
        if tcard is not None:
            assignments.append({"slotId": tslot, "cardId": tcard.get("id", ""), "card": tcard})
    return {
        "@type": TYPE + "logi.protocol.profiles_v2.Profiles",
        "profiles": [{
            "id": DEFAULT_PROFILE_ID, "name": "PROFILE_NAME_DEFAULT",
            "applicationId": DEFAULT_PROFILE_ID, "activeForApplication": True,
            "assignments": assignments, "baseProfileId": "",
        }],
    }
