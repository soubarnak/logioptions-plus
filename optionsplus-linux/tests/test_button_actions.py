"""Mouse-button, system, media and macro-sequence cards on a remapped button (ticket #22).

Seam: assign a card over the profiles route, press the simulated button, read what the fake
virtual input device was asked to emit."""

import os
import time

import pytest

from tests.fakes.hidpp_device import F_WAKE
from tests.test_button_remap import BACK, FORWARD, assign
from tests.harness import wait_for

# Linux evdev codes, from linux/input-event-codes.h
CTRL, SHIFT, ALT, META, TAB, F4 = 29, 42, 56, 125, 15, 62
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114
KEY_MUTE, KEY_VOLUMEDOWN, KEY_VOLUMEUP, KEY_PLAYPAUSE, KEY_NEXTSONG, KEY_PREVIOUSSONG = 113, 114, 115, 164, 163, 165
KEY_BRIGHTNESSDOWN, KEY_BRIGHTNESSUP = 224, 225
KEY_LEFT, KEY_RIGHT, KEY_D, KEY_L, KEY_C, KEY_V = 105, 106, 32, 38, 46, 47


def card(macro, cid="card_x"):
    return {"id": cid, "name": "x", "attribute": "MACRO_PLAYBACK", "readOnly": True, "macro": macro}


def mouse(action, usage=0):
    return card({"type": "MOUSE", "mouse": {"action": action, "hidUsage": usage}})


def system(action):
    return card({"type": "SYSTEM", "system": {"action": action, "currentTiltSpeed": 0}})


def media(usage):
    return card({"type": "MEDIA", "media": {"usage": usage}})


def kb(usage, down):
    return {"keyboard": {"hidUsage": usage, "isDown": down, "displayName": "", "virtualKeyId": ""}}


def delay(ms):
    return {"delay": {"durationMs": ms}}


def sequence(*components):
    return card({"type": "INPUT_SEQUENCE", "inputSequence": {
        "componentLists": [{"components": list(components)}],
        "useDefaultDelay": True, "defaultDelay": 21, "showUpDown": False}})


def press_and_read(client, bolt, uinput, c, count, cid=BACK):
    assert assign(client, cid, c)["result"]["code"] == "SUCCESS"
    bolt.press_button(cid)
    assert wait_for(lambda: len(uinput.keys()) >= count), uinput.keys()
    bolt.settle()
    return uinput.keys()


def tap(*codes):
    """Keys pressed in order and released in reverse: the shape of a chord."""
    return [(k, 1) for k in codes] + [(k, 0) for k in reversed(codes)]


# ---- mouse buttons -----------------------------------------------------------

@pytest.mark.parametrize("usage,button", [(1, BTN_LEFT), (2, BTN_RIGHT), (3, BTN_MIDDLE),
                                          (4, BTN_SIDE), (5, BTN_EXTRA)])
def test_a_mouse_button_card_holds_that_button_while_the_real_one_is_held(client, bolt, uinput, usage, button):
    assert press_and_read(client, bolt, uinput, mouse("BUTTON", usage), 1) == [(button, 1)]
    bolt.release_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= 2)
    assert uinput.keys() == [(button, 1), (button, 0)]


@pytest.mark.parametrize("action,button", [("WIN_BACK", BTN_SIDE), ("WIN_FORWARD", BTN_EXTRA)])
def test_back_and_forward_cards_hold_the_side_buttons(client, bolt, uinput, action, button):
    assert press_and_read(client, bolt, uinput, mouse(action), 1) == [(button, 1)]
    bolt.release_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= 2)
    assert uinput.keys() == [(button, 1), (button, 0)]


@pytest.mark.parametrize("action,button", [("OSX_GESTURE_BACK", BTN_SIDE), ("OSX_GESTURE_FORWARD", BTN_EXTRA)])
def test_the_mac_gesture_back_and_forward_cards_are_the_side_buttons(client, bolt, uinput, action, button):
    assert press_and_read(client, bolt, uinput, mouse(action), 1) == [(button, 1)]


def test_a_held_mouse_button_is_lifted_when_the_mouse_is_resynced(client, bolt, uinput):
    press_and_read(client, bolt, uinput, mouse("BUTTON", 1), 1)
    bolt.mouse.held.clear()                   # the release was lost: the mouse slept
    bolt.emit_event(F_WAKE)
    assert wait_for(lambda: uinput.keys() == [(BTN_LEFT, 1), (BTN_LEFT, 0)])


def test_a_held_mouse_button_is_lifted_when_its_card_is_removed(client, bolt, uinput):
    press_and_read(client, bolt, uinput, mouse("BUTTON", 1), 1)
    assign(client, BACK, {"id": "card-back", "attribute": "MOUSE_BUTTON_ACTION"})
    assert wait_for(lambda: uinput.keys() == [(BTN_LEFT, 1), (BTN_LEFT, 0)])


@pytest.mark.parametrize("c", [mouse("BUTTON", 99), mouse("BUTTON", 0), mouse("DPI_NEXT")])
def test_a_mouse_card_with_no_button_is_an_error(client, bolt, c):
    assert assign(client, BACK, c)["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set()


# ---- system actions ------------------------------------------------------------

@pytest.mark.parametrize("action,keys", [
    ("SWITCH_APPS", tap(ALT, TAB)),
    ("SHIFT_SWITCH_APPS", tap(ALT, SHIFT, TAB)),
    ("SHOW_DESKTOP", tap(META, KEY_D)),
    ("SWITCH_BETWEEN_DESKTOPS_RIGHT", tap(META, 109)),               # GNOME 40+: Super+Page Down
    ("SWITCH_BETWEEN_DESKTOPS_LEFT", tap(META, 104)),                # Super+Page Up
    ("MINIMIZE", tap(META, 35)),                                      # Super+H
    ("TASK_VIEW", tap(META)),
    ("APP_EXPOSE", tap(META)),
    ("SWITCH_LANGUAGE", tap(META, 57)),                               # Super+Space
    ("LOCK", tap(META, KEY_L)),
    ("CLOSE", tap(ALT, F4)),
    ("COPY", tap(CTRL, KEY_C)),
    ("BRIGHTNESS_UP", tap(KEY_BRIGHTNESSUP)),
    ("BRIGHTNESS_DOWN", tap(KEY_BRIGHTNESSDOWN)),
])
def test_a_system_card_sends_its_default_linux_keystroke(client, bolt, uinput, action, keys):
    assert press_and_read(client, bolt, uinput, system(action), len(keys)) == keys


@pytest.mark.parametrize("action,sign", [("SCROLL_LEFT", -1), ("SCROLL_RIGHT", 1)])
def test_scroll_left_and_right_cards_move_the_horizontal_wheel(client, bolt, uinput, action, sign):
    assert assign(client, BACK, system(action))["result"]["code"] == "SUCCESS"
    bolt.press_button(BACK)
    assert wait_for(lambda: uinput.rels(6))
    assert uinput.rels(6) == [sign] and uinput.rels(12) == [sign * 120]        # REL_HWHEEL, REL_HWHEEL_HI_RES


def test_a_system_action_without_a_linux_equivalent_is_an_error(client, bolt):
    assert assign(client, BACK, system("WINDOWS_HIGH_CONTRAST"))["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set()


@pytest.mark.parametrize("usage,key", [("MUTE", KEY_MUTE), ("VOLUME_UP", KEY_VOLUMEUP),
                                       ("VOLUME_DOWN", KEY_VOLUMEDOWN), ("PLAY_PAUSE", KEY_PLAYPAUSE),
                                       ("NEXT_TRACK", KEY_NEXTSONG), ("PREVIOUS_TRACK", KEY_PREVIOUSSONG)])
def test_a_media_card_sends_the_media_key(client, bolt, uinput, usage, key):
    assert press_and_read(client, bolt, uinput, media(usage), 2) == tap(key)


def test_an_unknown_media_usage_is_an_error(client, bolt):
    assert assign(client, BACK, media("TELEPORT"))["result"]["code"] == "NOT_SUPPORTED"


# ---- per-user override file ----------------------------------------------------

@pytest.fixture
def overrides(workdir):
    path = os.path.join(workdir, "state", "actions.toml")

    def write(text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        # a rewrite within the same clock tick must still be noticed
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + write.n * 10_000_000))
        write.n += 1
    write.n = 1
    write.path = path
    return write


def press_once(bolt, uinput, cid=BACK):
    before = len(uinput.keys())
    bolt.press_button(cid)
    bolt.release_button(cid)
    assert wait_for(lambda: len(uinput.keys()) > before)
    bolt.settle()
    return uinput.keys()[before:]


def test_the_override_file_changes_a_system_keystroke(client, bolt, uinput, overrides):
    overrides('[system]\nSWITCH_BETWEEN_DESKTOPS_RIGHT = "ctrl+alt+right"\n')
    assign(client, BACK, system("SWITCH_BETWEEN_DESKTOPS_RIGHT"))
    assert press_once(bolt, uinput) == tap(CTRL, ALT, KEY_RIGHT)


def test_editing_the_override_file_applies_without_a_restart(client, bolt, uinput, overrides):
    assign(client, BACK, system("SWITCH_BETWEEN_DESKTOPS_RIGHT"))
    assert press_once(bolt, uinput) == tap(META, 109)
    overrides('[system]\nSWITCH_BETWEEN_DESKTOPS_RIGHT = "ctrl+alt+right"\n')
    assert press_once(bolt, uinput) == tap(CTRL, ALT, KEY_RIGHT)
    overrides('[system]\nSWITCH_BETWEEN_DESKTOPS_RIGHT = "ctrl+alt+down"\n')
    assert press_once(bolt, uinput) == tap(CTRL, ALT, 108)
    os.remove(overrides.path)
    assert press_once(bolt, uinput) == tap(META, 109)


def test_an_override_can_be_a_single_media_key(client, bolt, uinput, overrides):
    overrides('[system]\nLOCK = "XF86Sleep"\n')
    assign(client, BACK, system("LOCK"))
    assert press_once(bolt, uinput) == tap(142)


def test_a_broken_override_file_falls_back_to_the_defaults(client, bolt, uinput, overrides):
    overrides("this is [ not toml")
    assign(client, BACK, system("LOCK"))
    assert press_once(bolt, uinput) == tap(META, KEY_L)


def test_a_bad_override_entry_only_loses_that_entry(client, bolt, uinput, overrides):
    overrides('[system]\nLOCK = "ctrl+nonsensekey"\nSHOW_DESKTOP = "ctrl+alt+d"\n')
    assign(client, BACK, system("LOCK"))
    assign(client, FORWARD, system("SHOW_DESKTOP"))
    assert press_once(bolt, uinput) == tap(META, KEY_L)
    assert press_once(bolt, uinput, FORWARD) == tap(CTRL, ALT, KEY_D)


def test_an_override_can_give_a_system_action_a_keystroke_it_lacks(client, bolt, uinput, overrides):
    overrides('[system]\nWINDOWS_HIGH_CONTRAST = "ctrl+alt+h"\n')
    assert assign(client, BACK, system("WINDOWS_HIGH_CONTRAST"))["result"]["code"] == "SUCCESS"
    assert press_once(bolt, uinput) == tap(CTRL, ALT, 35)


# ---- macro playback ------------------------------------------------------------

def test_macro_steps_wait_for_their_delay(client, bolt, uinput):
    seq = sequence(kb(6, True), kb(6, False), delay(300), kb(25, True), kb(25, False))
    assert assign(client, BACK, seq)["result"]["code"] == "SUCCESS"
    bolt.press_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= 2)
    started = time.monotonic()
    assert len(uinput.keys()) == 2              # the rest is still waiting out its delay
    assert wait_for(lambda: len(uinput.keys()) == 4)
    assert time.monotonic() - started >= 0.2


def test_a_macro_replays_its_sequence_in_order(client, bolt, uinput):
    seq = sequence(kb(224, True), kb(6, True), kb(6, False), delay(20), kb(25, True), kb(25, False),
                   kb(224, False))
    keys = press_and_read(client, bolt, uinput, seq, 6)
    assert keys == [(CTRL, 1), (KEY_C, 1), (KEY_C, 0), (KEY_V, 1), (KEY_V, 0), (CTRL, 0)]


def test_a_second_press_waits_for_the_running_macro(client, bolt, uinput):
    assign(client, BACK, sequence(kb(6, True), kb(6, False), delay(150), kb(6, True), kb(6, False)))
    bolt.press_button(BACK)
    bolt.release_button(BACK)
    bolt.press_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) == 8, timeout=5)
    assert uinput.keys() == [(KEY_C, 1), (KEY_C, 0)] * 4


def test_a_macro_can_click_mouse_buttons(client, bolt, uinput):
    seq = sequence({"mouse": {"button": {"hidUsage": 1, "isDown": True}}},
                   {"mouse": {"button": {"hidUsage": 1, "isDown": False}}})
    assert press_and_read(client, bolt, uinput, seq, 2) == tap(BTN_LEFT)


def test_a_macro_key_with_no_linux_equivalent_is_an_error(client, bolt):
    reply = assign(client, BACK, sequence(kb(6, True), kb(0xF0, True)))
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set()


def test_a_macro_component_we_cannot_run_is_an_error(client, bolt):
    reply = assign(client, BACK, sequence(kb(6, True), {"lighting": {}}))
    assert reply["result"]["code"] == "NOT_SUPPORTED"


def test_a_failed_write_mid_macro_does_not_leave_keys_held(client, bolt, uinput):
    assign(client, BACK, sequence(kb(224, True), kb(6, True), kb(6, False), kb(224, False)))
    uinput.fail_emit_on = 2                   # the write that would press C fails
    bolt.press_button(BACK)
    assert wait_for(lambda: uinput.keys().count((CTRL, 0)) >= 1)
    down = {}
    for key, value in uinput.keys():
        down[key] = value
    assert not any(down.values()), uinput.keys()


def test_the_macro_keeps_working_after_a_failed_run(client, bolt, uinput):
    assign(client, BACK, sequence(kb(6, True), kb(6, False)))
    uinput.fail_emit_on = 1
    bolt.press_button(BACK)
    bolt.release_button(BACK)
    bolt.settle()
    uinput.events.clear()
    assert press_once(bolt, uinput) == tap(KEY_C)

