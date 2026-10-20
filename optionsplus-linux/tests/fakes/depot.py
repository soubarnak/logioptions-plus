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


def system_card(cid, name, action):
    return keystroke_card(cid, name, macro={"type": "SYSTEM", "system": {"action": action}})


# Invented catalogue: card_task_view, card_quick_launch and card_cortana model entries Linux cannot run.


def catalogue_cards():
    return [
        keystroke_card("card_play", "NAME_PLAY"),
        system_card("card_next_desktop", "NAME_NEXT_DESKTOP", "SWITCH_BETWEEN_DESKTOPS_RIGHT"),
        system_card("card_snip", "NAME_SNIP_WINDOWS", "WINDOWS_SCREEN_CAPTURE"),
        system_card("card_task_view", "NAME_TASK_VIEW", "TASK_VIEW"),
        keystroke_card("card_quick_launch", "NAME_QUICK",
                       macro={"type": "QUICK_LAUNCH", "quickLaunch": {"action": "CALCULATOR"}}),
        keystroke_card("card_cortana", "NAME_CORTANA"),
    ]


def add_catalogue(main):
    """Card presets, recommended actions, defaults and macros, with Windows-only entries."""
    data = os.path.join(main, "data")
    cards = catalogue_cards()
    _write(os.path.join(data, "card_presets", "card_presets_win.json"),
           json.dumps({"cards": cards[:4]}))
    _write(os.path.join(data, "card_presets", "card_presets_001.json"),
           json.dumps({"cards": cards[4:] + [cards[0]]}))
    _write(os.path.join(data, "card_presets", "card_presets_osx.json"),
           json.dumps({"cards": [keystroke_card("card_mac_only", "NAME_PLAY")]}))
    _write(os.path.join(data, "rap", "rac_win.json"), json.dumps({
        "recommendedCards": {"racList": [{"appId": "app_test", "slotCardList": [
            {"slotId": "046d_4242_c82", "cards": [cards[0], cards[3]]}]}]},
        "availableCards": {"racList": []}}))
    _write(os.path.join(data, "defaults", "defaults_control_win.json"), json.dumps({"assignments": [
        {"slotId": "046d_4242_c82", "cardId": "card-back"}]}))
    _write(os.path.join(data, "defaults", "defaults_slot_win.json"), json.dumps({"assignments": [
        {"slotId": "046d_4242_c83", "cardId": "card-forward"}]}))
    _write(os.path.join(data, "macros", "macros_predefined_win.json"), json.dumps({"macroInfos": [
        {"id": "macros_preset_test", "name": "MACRO_NAME_TEST", "cards": [cards[0], cards[2], cards[3]]},
        {"id": "macros_preset_all_hidden", "name": "MACRO_NAME_HIDDEN", "cards": [cards[3], cards[5]]}]}))
    _write(os.path.join(data, "macros", "macro_assignments.json"), json.dumps({"macroAssignmentVals": [
        {"macroId": "macros_preset_test", "slotId": "046d_4242_c82"}]}))
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
        "settings": {"control_id_mappings": {"82": {"id": "card-back"}, "83": {"id": "card-forward"}},
                     "mouseThumbWheelAction": {"id": "card-wheel"},
                     "mouseSettings": {"pointerSpeed": {"active": {"value": 0.5}}}}}]}))
    return root
