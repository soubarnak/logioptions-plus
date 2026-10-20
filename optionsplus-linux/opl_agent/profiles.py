"""Profiles v2: the default profile and its per-device assignments.

The UI lists a device on its home page only when the default profile holds at
least one assignment whose slot id starts with the device's slotPrefix
(``<slotPrefix>_c<controlId>``). The real agent seeds those assignments from
the device depot's default_configurations.json; this module does the same,
reading the Logitech-supplied depot at runtime (see resources.py).
"""

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


def profiles(devices):
    assignments = []
    for dev in devices:
        assignments += default_assignments(dev.slot_prefix, dev.depot)
    return {
        "@type": TYPE + "logi.protocol.profiles_v2.Profiles",
        "profiles": [{
            "id": DEFAULT_PROFILE_ID, "name": "PROFILE_NAME_DEFAULT",
            "applicationId": DEFAULT_PROFILE_ID, "activeForApplication": True,
            "assignments": assignments, "baseProfileId": "",
        }],
    }
