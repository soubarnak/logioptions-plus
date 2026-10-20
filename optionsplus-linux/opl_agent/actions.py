"""Action picker catalogue: Logitech's Windows card lists, served through a Linux filter.

The UI reports platform "linux", which matches neither shipped variant, so the
agent reads the Windows files of the fetched main depot at runtime and hides
what cannot run here. The allow-list and relabel tables below are ours; nothing
of Logitech's is stored in the repository.

Files read from the main depot's data/ directory (JSON, the same shape as the
route payloads; a bare list is accepted too). Files whose name marks them for
macOS (osx/mac) are skipped:

    card_presets/*.json   {"cards": [...]}                      -> /macros/presets
    rap/*.json            tag and recommendation tables         -> /rac (composed, see below)
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
                   "xbox", "game_bar", "windows_search", "action_center", "dictation", "win_settings",
                   "this_pc", "presets_search")

# Windows paths and shell names inside a card's macro: an .exe, a %windir%-style variable, a drive
# letter, a shell:/CLSID location.
_WINDOWS_ONLY = re.compile(r"\.exe(?=$|[\s\"']|\\)|%[a-z_]+%|^[a-z]:\\|shell:|::\{|^\{[0-9a-f-]{36}\}$", re.I)

# Windows concepts with a Linux equivalent: system action -> label shown in the picker.
RELABEL_SYSTEM_ACTION = {
    "WINDOWS_SCREEN_CAPTURE": "Screenshot",
    "WINDOWS_PRINT_SCREEN": "Screenshot",
}
# Same for cards that are not system actions: string key of the card name -> label.
RELABEL_NAME_KEY = {}


def _macro(card):
    return card.get("macro") or {}


def _names_windows_software(node, key=""):
    """True when a macro field names a Windows program, path or Microsoft application."""
    if isinstance(node, dict):
        return any(_names_windows_software(v, k) for k, v in node.items())
    if isinstance(node, list):
        return any(_names_windows_software(v, key) for v in node)
    if isinstance(node, str):
        if node.lower().startswith(("http://", "https://")):
            return False
        if key.lower() == "appname" and node.lower().startswith("microsoft"):
            return True
        return bool(_WINDOWS_ONLY.search(node))
    return False


def _hidden_id(cid):
    return any(part in str(cid).lower() for part in HIDDEN_ID_PARTS)


def is_linux_card(card):
    """True when the card may be offered on Linux (its own macro, not its nested cards)."""
    if _hidden_id(card.get("id", "")):
        return False
    macro = _macro(card)
    if _names_windows_software(macro):
        return False
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
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return None
    if raw.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] not in (b"{", b"["):
        _files[path] = (stamp, None)
        return None                     # Logitech's encrypted wrappers (card_presets_001.json): not data for us
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError as exc:
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
    """file:// directory a pipeline:// icon location names, "" when it is not an installed
    directory inside its depot (no ../ out of it)."""
    depot, _, rel = uri[len("pipeline://"):].partition("/")
    if depot in ("", ".", ".."):
        return ""
    root = os.path.abspath(resources.depot_root(depot))
    path = os.path.normpath(os.path.join(root, rel)) if rel else root
    if path != root and not path.startswith(root + os.sep):
        return ""
    return "file://" + path.rstrip("/") + "/" if os.path.isdir(path) else ""


def _serve(cards):
    return [_with_icons(c) for c in filter_cards(cards)]


# ---- route payloads -----------------------------------------------------------

def presets():
    return {"@type": TYPE + "logi.protocol.rap.CardList",
            "cards": _serve(_dedupe(_items("card_presets", "cards")))}


# ---- /rac: composed from Logitech's tag and recommendation tables ----------------
#
# The depot ships no RACContainer. data/rap/ holds four tables, all
# {"data": {<app id>: {"infoList": [{"id": ..., "infos": [...]}]}}}:
#
#     available_card_tags_slot.json      slot id    -> preset tags the slot accepts
#     available_card_tags_control.json   control id -> preset tags (devices without a slot row)
#     recommendations_slot_win.json      slot id    -> recommended card ids
#     recommendations_control_win.json   control id -> recommended card ids
#
# A slot id is "<slotPrefix>_c<control id>" (or "<slotPrefix>_thumb_wheel_adapter"). The
# available cards of a slot are the presets whose tags meet the slot's tags.
#
# The UI's rac reducer reads the two lists from different fields of a slot entry: the
# available list from "desktopCards" (and "appSpecificCards"), the recommended list from
# "cards". An available list sent as "cards" is stored empty, so the picker shows only the
# recommended cards and search finds nothing else.

_TABLES = {"tags_slot": "available_card_tags_slot.json", "tags_control": "available_card_tags_control.json",
           "rec_slot": "recommendations_slot_win.json", "rec_control": "recommendations_control_win.json"}
_CONTROL_SLOT = re.compile(r"_c(\d+)$")
_rac_cache = {}


def _table(name):
    """{app id: {row id: [infos]}} of one rap table; malformed parts are left out."""
    doc = _read(os.path.join(resources.DATA_DIR, "rap", _TABLES[name]))
    data = doc.get("data") if isinstance(doc, dict) else None
    out = {}
    for app, body in (data.items() if isinstance(data, dict) else ()):
        rows = body.get("infoList") if isinstance(body, dict) else None
        out[app] = {r["id"]: r["infos"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
                    and isinstance(r.get("infos"), list)} if isinstance(rows, list) else {}
    return out


def _catalogue_entry(prefix):
    return next((e for e in resources._load_catalogue().values() if e.get("slotPrefix") == prefix), {})


def _installed_prefixes():
    """Slot prefixes of the catalogued devices whose depot is installed, in catalogue order."""
    out = []
    for entry in resources._load_catalogue().values():
        prefix = entry.get("slotPrefix")
        if prefix and prefix not in out and resources.depot_installed(entry.get("depot")):
            out.append(prefix)
    return out


def _slot_ids(prefix, tables, app):
    """Slots of one app for one device. A device that has slot rows in any table is listed by
    those rows (this app's own, in table order); one that has none is listed by the controls of
    its depot defaults, whose cards then come from the control tables."""
    own = [sid for name in ("tags_slot", "rec_slot") for app_id in tables[name]
           for sid in tables[name][app_id] if sid.startswith(prefix + "_")]
    if own:
        seen = []
        for name in ("tags_slot", "rec_slot"):
            for sid in tables[name].get(app, {}):
                if sid.startswith(prefix + "_") and sid not in seen:
                    seen.append(sid)
        return seen
    from .profiles import default_assignments
    entry = _catalogue_entry(prefix)
    return [a["slotId"] for a in default_assignments(prefix, entry.get("depot", ""))]


def _compose(prefixes):
    from .profiles import DEFAULT_PROFILE_ID
    tables = {name: _table(name) for name in _TABLES}
    presets_by_id = {c["id"]: c for c in _dedupe(_items("card_presets", "cards"))
                     if isinstance(c, dict) and c.get("id")}
    served = {}

    def serve(card):
        if card["id"] not in served:
            served[card["id"]] = _serve([card])
        return served[card["id"]]

    apps = []
    for name in _TABLES:
        for app in tables[name]:
            if app not in apps:
                apps.append(app)
    apps.sort(key=lambda a: a != DEFAULT_PROFILE_ID)        # default first, rest in table order

    def row(kind, sid, app):
        """The app's own slot row, else its own control row; the default app's rows stand in
        only for an app that has no rows in any table."""
        cid = (_CONTROL_SLOT.search(sid) or [None, None])[1]
        owners = [app] if any(tables[n].get(app) for n in _TABLES) else [app, DEFAULT_PROFILE_ID]
        for owner in dict.fromkeys(owners):
            if sid in tables[kind + "_slot"].get(owner, {}):
                return tables[kind + "_slot"][owner][sid]
            if cid is not None and cid in tables[kind + "_control"].get(owner, {}):
                return tables[kind + "_control"][owner][cid]
        return None

    available, recommended = [], []
    for app in apps:
        slots_a, slots_r = [], []
        for prefix in prefixes:
            for sid in _slot_ids(prefix, tables, app):
                tags = row("tags", sid, app)
                if tags is not None:
                    want = set(t for t in tags if isinstance(t, str))
                    cards = [c for c in presets_by_id.values() if want.intersection(c.get("tags") or ())]
                    slots_a.append({"slotId": sid, "desktopCards": [x for c in cards for x in serve(c)]})
                rec = row("rec", sid, app)
                if rec is not None:
                    cards = [presets_by_id[i] for i in rec if i in presets_by_id]
                    slots_r.append({"slotId": sid, "cards": [x for c in cards for x in serve(c)]})
        if slots_a:
            available.append({"appId": app, "slotCardList": slots_a})
        if slots_r:
            recommended.append({"appId": app, "slotCardList": slots_r})
    return {"@type": TYPE + "logi.protocol.rap.RACContainer",
            "availableCards": {"racList": available}, "recommendedCards": {"racList": recommended}}


def _rac_stamp(prefixes):
    files = [os.path.join(resources.DATA_DIR, "rap", n) for n in _TABLES.values()]
    files += sorted(glob.glob(os.path.join(resources.DATA_DIR, "card_presets", "*.json")))
    files += [os.path.join(resources.depot_root(_catalogue_entry(p).get("depot", "")), "default_configurations.json")
              for p in prefixes if _catalogue_entry(p).get("depot")]
    stamps = []
    for f in files:
        try:
            stamps.append((f, os.stat(f).st_mtime_ns))
        except OSError:
            stamps.append((f, None))
    return tuple(stamps), _icon_dir("pipeline://%s/" % ICON_DEPOT)


def rac(payload=None):
    """The action picker container for one device ({"slotPrefix"}), or, without one, for every
    catalogued device whose depot is installed. One cache entry per device set, dropped when
    a depot file it was built from changes."""
    prefix = payload.get("slotPrefix") if isinstance(payload, dict) else None
    prefixes = (prefix,) if isinstance(prefix, str) and prefix else tuple(_installed_prefixes())
    stamp = _rac_stamp(prefixes)
    hit = _rac_cache.get(prefixes)
    if hit is None or hit[0] != stamp:
        hit = _rac_cache[prefixes] = (stamp, _compose(prefixes))
    return copy.deepcopy(hit[1])


def default_assignments():
    return {"@type": TYPE + "logi.protocol.profiles_v2.Defaults",
            "assignments": _items("defaults", "assignments")}


def _fully_visible(card):
    """True when neither the card nor any card nested inside it is hidden."""
    return is_linux_card(card) and all(_fully_visible(c) for c in (card.get("nestedCards") or {}).values())


def _served_macros():
    """Predefined macros that can run here whole. A macro is a sequence: with one step hidden at
    any depth the rest would do something other than what the macro is named for, so it goes."""
    return [dict(info, cards=_serve(info.get("cards"))) for info in _items("macros", "macroInfos")
            if not _hidden_id(info.get("id", "")) and all(_fully_visible(c) for c in info.get("cards") or [])]


def predefined_macros():
    return {"@type": TYPE + "logi.protocol.profiles_v2.MacroInfosCollection", "macroInfos": _served_macros()}


def macro_assignments():
    served = {info.get("id") for info in _served_macros()}
    return {"@type": TYPE + "logi.protocol.profiles_v2.MacroAssignmentValCollection",
            "macroAssignmentVals": [v for v in _items("macros", "macroAssignmentVals")
                                    if not isinstance(v, dict) or v.get("macroId") in served]}


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
