"""Action picker catalogue: Logitech's Windows card lists, served through a Linux filter.

The UI reports platform "linux", which matches neither shipped variant, so the
agent reads the Windows files of the fetched main depot at runtime and hides
what cannot run here. The allow-list and relabel tables below are ours; nothing
of Logitech's is stored in the repository.

Files read from the main depot's data/ directory (JSON, the same shape as the
route payloads; a bare list is accepted too). Files whose name marks them for
macOS (osx/mac) are skipped:

    card_presets/*.json   {"cards": [...]}                      -> /macros/presets
    rap/*.json            RACContainer                          -> /rac
    defaults/*.json       {"assignments": [...]}                -> /unified_profiles/assignment/default
    macros/*.json         {"macroInfos": [...]}                 -> /macros/predefined
                          {"macroAssignmentVals": [...]}        -> /macro_assignments/all

Icons: cards point at pipeline://system_actions/; that is rewritten to the
file:// directory of the installed system_actions depot (blank when absent).
"""

import copy
import glob
import json
import logging
import os
import re

from . import resources

log = logging.getLogger("opl.actions")

TYPE = "type.googleapis.com/"
ICON_DEPOT = "system_actions"
STRING_PACKAGE = "logioptionsplus/data/strings"

# macro.type values the agent can (or will, in the input engine) run on Linux.
ALLOWED_MACRO_TYPES = frozenset({
    "KEYSTROKE", "TEXT_BLOCK", "MOUSE", "SYSTEM", "APP", "MEDIA", "SEQUENCE", "DO_NOTHING",
    "OPEN_FILE_FOLDER", "OPEN_WEB_PAGE", "ADVANCED_CLICK", "SCREEN_CAPTURE", "DELAY",
    "INPUT_SEQUENCE",
})

# macro.system.action values with a Linux equivalent (the keystroke table lives in the input engine).
ALLOWED_SYSTEM_ACTIONS = frozenset({
    "NONE", "CLOSE", "MINIMIZE", "MAXIMIZE", "FULLSCREEN", "COPY", "PASTE", "UNDO", "REDO",
    "DESKTOP", "SHOW_DESKTOP", "FILE_CLIENT", "INTERNET_CLIENT", "MAIL_CLIENT", "MEDIA_CLIENT",
    "CONSOLE_CLIENT", "ZOOM_IN", "ZOOM_OUT", "SCROLL_LEFT", "SCROLL_RIGHT", "SNAP_LEFT",
    "SNAP_RIGHT", "SWITCH_BETWEEN_DESKTOPS", "SWITCH_BETWEEN_DESKTOPS_LEFT",
    "SWITCH_BETWEEN_DESKTOPS_RIGHT", "SWITCH_APPS", "SHIFT_SWITCH_APPS", "BRIGHTNESS_UP",
    "BRIGHTNESS_DOWN", "WINDOWS_SCREEN_CAPTURE", "WINDOWS_PRINT_SCREEN", "LOCK", "SHUT_DOWN",
    "SLEEP", "HOME", "EJECT",
})

# macro.mouse.action values that are macOS gestures.
HIDDEN_MOUSE_ACTIONS = frozenset({"OSX_GESTURE_BACK", "OSX_GESTURE_FORWARD"})

# Windows-only features that arrive as plain keystroke or URI cards: matched on the card id.
HIDDEN_ID_PARTS = ("cortana", "windows_ink", "ink_workspace", "task_view", "copilot", "onedrive",
                   "xbox", "game_bar", "windows_search")

# Windows concepts with a Linux equivalent: system action -> label shown in the picker.
RELABEL_SYSTEM_ACTION = {
    "WINDOWS_SCREEN_CAPTURE": "Screenshot",
    "WINDOWS_PRINT_SCREEN": "Screenshot",
}
# Same for cards that are not system actions: string key of the card name -> label.
RELABEL_NAME_KEY = {}


def _macro(card):
    return card.get("macro") or {}


def is_linux_card(card):
    """True when the card may be offered on Linux (its own macro, not its nested cards)."""
    cid = str(card.get("id", "")).lower()
    if any(part in cid for part in HIDDEN_ID_PARTS):
        return False
    macro = _macro(card)
    mtype = macro.get("type")
    if not mtype:
        return True                     # settings, gesture and container cards carry no macro
    if mtype not in ALLOWED_MACRO_TYPES:
        return False
    if mtype == "SYSTEM":
        return (macro.get("system") or {}).get("action") in ALLOWED_SYSTEM_ACTIONS
    if mtype == "MOUSE":
        return (macro.get("mouse") or {}).get("action") not in HIDDEN_MOUSE_ACTIONS
    return True


def _filter_card(card):
    """A copy of the card without hidden nested cards, or None when the card itself is hidden."""
    if not isinstance(card, dict) or not is_linux_card(card):
        return None
    nested = card.get("nestedCards")
    if not nested:
        return card
    kept = {k: c for k, c in ((k, _filter_card(c)) for k, c in nested.items()) if c is not None}
    if not kept:
        return None                     # a folder or choice with nothing left to choose
    out = dict(card, nestedCards=kept)
    if "nestedCardsOrder" in card:
        out["nestedCardsOrder"] = [k for k in card["nestedCardsOrder"] if k in kept]
    if card.get("selectedNestedCard") not in kept:
        out["selectedNestedCard"] = ""
    return out


def filter_cards(cards):
    return [c for c in map(_filter_card, cards or []) if c is not None]


# ---- reading the depot --------------------------------------------------------

_files = {}
_MAC_FILE = re.compile(r"(^|[_.-])(osx|mac|macos)([_.-]|$)")


def _read(path):
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    cached = _files.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return None
    _files[path] = (stamp, data)
    return data


def _documents(sub):
    """Parsed JSON files of data/<sub>/ for this platform, in name order."""
    pattern = os.path.join(resources.DATA_DIR, sub, "*.json")
    for path in sorted(glob.glob(pattern)):
        name = os.path.basename(path).lower()
        if _MAC_FILE.search(name):
            continue
        data = _read(path)
        if data is not None:
            yield data


def _items(sub, key):
    out = []
    for doc in _documents(sub):
        out += doc if isinstance(doc, list) else (doc.get(key) or []) if isinstance(doc, dict) else []
    return out


def _dedupe(cards):
    seen, out = set(), []
    for c in cards:
        if c.get("id") in seen:
            continue
        seen.add(c.get("id"))
        out.append(c)
    return out


def _with_icons(card):
    """Deep copy with pipeline:// icon locations turned into file:// ones."""
    card = copy.deepcopy(card)

    def walk(c):
        icons = c.get("icons")
        if isinstance(icons, dict) and str(icons.get("uri", "")).startswith("pipeline://"):
            icons["uri"] = _icon_dir(icons["uri"])
        for n in (c.get("nestedCards") or {}).values():
            walk(n)
    walk(card)
    return card


def _icon_dir(uri):
    depot, _, rel = uri[len("pipeline://"):].partition("/")
    root = resources.depot_root(depot)
    path = os.path.join(root, rel) if rel else root
    return "file://" + path.rstrip("/") + "/" if os.path.isdir(path) else ""


def _serve(cards):
    return [_with_icons(c) for c in filter_cards(cards)]


# ---- route payloads -----------------------------------------------------------

def presets():
    return {"@type": TYPE + "logi.protocol.rap.CardList",
            "cards": _serve(_dedupe(_items("card_presets", "cards")))}


def _rac_list(key):
    out = []
    for doc in _documents("rap"):
        lst = ((doc.get(key) or {}).get("racList")) if isinstance(doc, dict) else None
        for rac in lst or []:
            slots = []
            for slot in rac.get("slotCardList") or []:
                slot = dict(slot)
                for field in ("cards", "appSpecificCards", "desktopCards"):
                    if field in slot:
                        slot[field] = _serve(slot[field])
                slots.append(slot)
            out.append(dict(rac, slotCardList=slots))
    return {"racList": out}


def rac():
    return {"@type": TYPE + "logi.protocol.rap.RACContainer",
            "availableCards": _rac_list("availableCards"),
            "recommendedCards": _rac_list("recommendedCards")}


def default_assignments():
    return {"@type": TYPE + "logi.protocol.profiles_v2.Defaults",
            "assignments": _items("defaults", "assignments")}


def predefined_macros():
    infos = []
    for info in _items("macros", "macroInfos"):
        cards = _serve(info.get("cards"))
        if cards or not info.get("cards"):
            infos.append(dict(info, cards=cards))
    return {"@type": TYPE + "logi.protocol.profiles_v2.MacroInfosCollection", "macroInfos": infos}


def macro_assignments():
    return {"@type": TYPE + "logi.protocol.profiles_v2.MacroAssignmentValCollection",
            "macroAssignmentVals": _items("macros", "macroAssignmentVals")}


ROUTES = {
    "/macros/presets": presets,
    "/rac": rac,
    "/unified_profiles/assignment/default": default_assignments,
    "/macros/predefined": predefined_macros,
    "/macro_assignments/all": macro_assignments,
}


# ---- relabelling ----------------------------------------------------------------

def _all_cards(cards):
    for c in cards:
        yield c
        yield from _all_cards((c.get("nestedCards") or {}).values())


def relabels():
    """{string key: Linux label} for every served card whose Windows name has an equivalent."""
    out = dict(RELABEL_NAME_KEY)
    cards = _items("card_presets", "cards")
    for info in _items("macros", "macroInfos"):
        cards += info.get("cards") or []
    for c in _all_cards(cards):
        label = RELABEL_SYSTEM_ACTION.get((_macro(c).get("system") or {}).get("action"))
        if label and c.get("name"):
            out[c["name"]] = label
    return out


def relabel_strings(strs):
    """Apply the relabels to a {key: text} string table (only keys the depot defines)."""
    if not strs:
        return strs
    table = {k: v for k, v in relabels().items() if k in strs}
    return dict(strs, **table) if table else strs
