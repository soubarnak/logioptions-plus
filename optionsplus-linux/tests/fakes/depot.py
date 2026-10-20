"""Builds a synthetic Logitech-shaped depot tree containing only invented values."""

import json
import os

DEPOT = "test_mouse_x1"
MODEL_ID = "2test-x1"
SLOT_PREFIX = "046d_4242"
DISPLAY_NAME = "Test Mouse X1"
THUMB_BYTES = b"not-a-real-image"
EASYSWITCH_BYTES = b"not-a-real-easyswitch-image"


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as fh:
        fh.write(data)


def keystroke_card(cid, name, **extra):
    card = {"id": cid, "name": name, "attribute": "MACRO_PLAYBACK", "readOnly": True,
            "macro": {"type": "KEYSTROKE", "keystroke": {"code": 4, "modifiers": [224]}},
            "icons": {"uri": "pipeline://system_actions/", "icons": [cid + ".png"]},
            "nestedCards": {}, "nestedCardsOrder": [], "tags": ["PRESET_TAG_KEY_OR_BUTTON"]}
    card.update(extra)
    return card


def system_card(cid, name, action, **extra):
    return keystroke_card(cid, name, macro={"type": "SYSTEM", "system": {"action": action}}, **extra)


# Invented catalogue: card_task_view, card_quick_launch and card_cortana model entries Linux cannot run.


def catalogue_cards():
    return [
        keystroke_card("card_play", "NAME_PLAY"),
        system_card("card_next_desktop", "NAME_NEXT_DESKTOP", "SWITCH_BETWEEN_DESKTOPS_RIGHT",
                    tags=["PRESET_TAG_DESKTOP"]),
        system_card("card_snip", "NAME_SNIP_WINDOWS", "WINDOWS_SCREEN_CAPTURE"),
        system_card("card_task_view", "NAME_TASK_VIEW", "TASK_VIEW"),
        keystroke_card("card_quick_launch", "NAME_QUICK",
                       macro={"type": "QUICK_LAUNCH", "quickLaunch": {"action": "CALCULATOR"}}),
        keystroke_card("card_cortana", "NAME_CORTANA"),
    ]


ENCRYPTED_WRAPPER = b"\x21\x05\x21\x20\x82\x00\x00\x00" + bytes(range(200, 256))
DEFAULT_APP = "420fd454-0c36-499d-bde4-146823b16147"
OTHER_APP = "app_test"
KEY = "PRESET_TAG_KEY_OR_BUTTON"
DESKTOP = "PRESET_TAG_DESKTOP"


def info_table(per_app):
    """Logitech's rap table shape: {"data": {app: {"infoList": [{"id", "infos"}]}}}."""
    return json.dumps({"data": {app: {"infoList": [{"id": i, "infos": infos} for i, infos in rows]}
                                for app, rows in per_app.items()}})


def add_rap_tables(data):
    """Tag and recommendation tables, shaped like the real ones. c195 has a recommendation row
    but no tag row (the control tag row applies, as for the real gesture button); c86 has a tag
    row but no recommendation row (the control row applies). The other-device rows and the mac
    file must never reach this device's picker."""
    rap = os.path.join(data, "rap")
    _write(os.path.join(rap, "available_card_tags_slot.json"), info_table({
        DEFAULT_APP: [("046d_4242_c83", [KEY]), ("046d_4242_c86", [DESKTOP]), ("other-9999_c83", [KEY])],
        OTHER_APP: [("046d_4242_c83", [DESKTOP])]}))
    _write(os.path.join(rap, "available_card_tags_control.json"), info_table({
        DEFAULT_APP: [("83", [KEY, DESKTOP]), ("195", [DESKTOP])]}))
    _write(os.path.join(rap, "recommendations_slot_win.json"), info_table({
        DEFAULT_APP: [("046d_4242_c83", ["card_play", "card_task_view", "card_not_in_presets"]),
                      ("046d_4242_c195", ["card_snip"]),
                      ("other-9999_c83", ["card_play"])]}))
    _write(os.path.join(rap, "recommendations_control_win.json"), info_table({
        DEFAULT_APP: [("195", ["card_next_desktop"]), ("86", ["card_next_desktop"])]}))
    _write(os.path.join(rap, "recommendations_slot_osx.json"), info_table({
        DEFAULT_APP: [("046d_4242_c83", ["card_mac_only"])]}))


def add_catalogue(main):
    """Card presets, recommended actions, defaults and macros, with Windows-only entries."""
    data = os.path.join(main, "data")
    cards = catalogue_cards()
    _write(os.path.join(data, "card_presets", "card_presets_win.json"),
           json.dumps({"cards": cards[:4]}))
    # Logitech's card_presets_001.json is an encrypted wrapper, not JSON: it must be skipped quietly.
    _write(os.path.join(data, "card_presets", "card_presets_001.json"), ENCRYPTED_WRAPPER)
    _write(os.path.join(data, "card_presets", "card_presets_002.json"),
           json.dumps({"cards": cards[4:] + [cards[0]]}))
    _write(os.path.join(data, "card_presets", "card_presets_osx.json"),
           json.dumps({"cards": [keystroke_card("card_mac_only", "NAME_PLAY")]}))
    add_rap_tables(data)
    _write(os.path.join(data, "defaults", "defaults_control_win.json"), json.dumps({"assignments": [
        {"slotId": "046d_4242_c83", "cardId": "card-back"}]}))
    _write(os.path.join(data, "defaults", "defaults_slot_win.json"), json.dumps({"assignments": [
        {"slotId": "046d_4242_c86", "cardId": "card-forward"}]}))
    _write(os.path.join(data, "macros", "macros_predefined_win.json"), json.dumps({"macroInfos": [
        {"id": "macros_preset_test", "name": "MACRO_NAME_TEST", "cards": [cards[0], cards[2], cards[3]]},
        {"id": "macros_preset_ok", "name": "MACRO_NAME_OK", "cards": [cards[0], cards[2]]},
        {"id": "macros_preset_all_hidden", "name": "MACRO_NAME_HIDDEN", "cards": [cards[3], cards[5]]}]}))
    _write(os.path.join(data, "macros", "macro_assignments.json"), json.dumps({"macroAssignmentVals": [
        {"macroId": "macros_preset_test", "slotId": "046d_4242_c83"},
        {"macroId": "macros_preset_ok", "slotId": "046d_4242_c86"},
        {"macroId": "macros_preset_all_hidden", "slotId": "046d_4242_c82"}]}))
    _write(os.path.join(os.path.dirname(main), "system_actions", "card_play.png"), b"not-a-real-icon")


def build_depot(root):
    """Create the tree under `root`; returns it (the OPL_DEPOT_DIR)."""
    main = os.path.join(root, "logioptionsplus")
    _write(os.path.join(main, "data", "devices", "devices_initial.json"), json.dumps({"devices": [{
        "modelId": MODEL_ID, "depot": DEPOT, "type": "MOUSE", "displayName": DISPLAY_NAME,
        "extendedDisplayName": DISPLAY_NAME + " Wireless", "slotPrefix": SLOT_PREFIX,
        "thumbnail": "pipeline://%s/thumb.png" % DEPOT,
        "modes": [{"interfaces": [{"id": "046d_4242"}]}],
    }]}))
    _write(os.path.join(main, "data", "strings", "en-US.yaml"),
           'HELLO_KEY: "Hello"\nBYE_KEY: Bye\nNAME_PLAY: Play/Pause\n'
           'NAME_NEXT_DESKTOP: Next desktop\nNAME_SNIP_WINDOWS: Snipping Tool\n'
           'NAME_TASK_VIEW: Task View\nNAME_QUICK: Quick launch\nNAME_CORTANA: Cortana\n')
    add_catalogue(main)
    dev = os.path.join(root, DEPOT)
    _write(os.path.join(dev, "thumb.png"), THUMB_BYTES)
    _write(os.path.join(dev, "easyswitch.png"), EASYSWITCH_BYTES)
    _write(os.path.join(dev, "layout.json"), json.dumps({"buttons": []}))
    _write(os.path.join(dev, "manifest.json"), json.dumps({"devices": [{
        "modelId": MODEL_ID, "resources": [
            {"key": "thumbnail", "src": "thumb.png"},
            {"key": "layout", "src": "layout.json"},
            {"key": "device_easyswitch_image", "src": "easyswitch.png"}]}]}))
    _write(os.path.join(dev, "default_configurations.json"), json.dumps({"defaultConfigurations": [{
        "settings": {"control_id_mappings": {"83": {"id": "card-back"}, "86": {"id": "card-forward"},
                                                 "195": {"id": "card-gesture"}},
                     "mouseThumbWheelAction": {"id": "card-wheel"},
                     "mouseSettings": {"pointerSpeed": {"active": {"value": 0.5}}}}}]}))
    return root
