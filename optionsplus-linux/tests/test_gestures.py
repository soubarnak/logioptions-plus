"""Gesture button and thumb wheel action cards (ticket #23): the gesture button is diverted with raw
XY and a press-and-swipe runs the card of its direction; the thumb wheel's two-way card runs its
left and right actions. Card layout is an assumption: nested cards keyed by direction word."""


import json
import os

import pytest

from opl_agent import gestures as gestures_mod
from tests.fakes import depot as fx
from tests.fakes.hidpp_device import F_WAKE
from tests.test_button_remap import BACK, DEFAULT_PROFILE, TYPE, assigned_card_id, keystroke, slot
from tests.harness import keys_down, wait_for  # noqa: F401  (keys_down is re-exported)

GESTURE = 195
NOTCH = 14                  # thumb wheel units per notch the simulated mouse reports when diverted
THUMB_SLOT = fx.SLOT_PREFIX + "_thumb_wheel_adapter"
# HID usage -> evdev key: 4 a (30), 5 b (48), 7 d (32), 8 e (18), 9 f (33); no modifiers, so one key each
ACTIONS = {"up": 4, "down": 5, "left": 7, "right": 8, "click": 9}
KEY_OF = {"up": 30, "down": 48, "left": 32, "right": 18, "click": 33}
GESTURE_DEFAULT = {"id": "card-gesture", "attribute": "ADAPTER_4WAYS"}
THUMB_DEFAULT = {"id": "card-wheel", "attribute": "ADAPTER_2WAYS"}


def threshold(n):
    return {"minimumSensitivityThreshold": n, "maximumSensitivityThreshold": n, "units": "DEVICE"}


def gesture_card(actions=None, limit=None, cid="card_gesture_custom"):
    actions = ACTIONS if actions is None else actions
    card = {"id": cid, "name": "Gestures", "attribute": "ADAPTER_4WAYS",
            "nestedCards": {d: keystroke(u, cid="%s_%s" % (cid, d)) for d, u in actions.items()},
            "nestedCardsOrder": list(actions), "selectedNestedCard": ""}
    if limit is not None:
        card["gestureInfo"] = {"x": {"actionThreshold": threshold(limit)},
                               "y": {"actionThreshold": threshold(limit)}}
    return card


def thumb_card(left=7, right=8, cid="card_thumb_custom"):
    return {"id": cid, "name": "Thumb", "attribute": "ADAPTER_2WAYS",
            "nestedCards": {"left": keystroke(left, cid=cid + "_left"),
                            "right": keystroke(right, cid=cid + "_right")},
            "nestedCardsOrder": ["left", "right"], "selectedNestedCard": ""}


def assign_slot(client, slot_id, card):
    return client.call("SET", "/v2/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE, "assignment": {"slotId": slot_id, "card": card}})


def assign_gesture(client, card=None):
    return assign_slot(client, slot(GESTURE), card or gesture_card())


def swipe(bolt, *moves):
    bolt.press_button(GESTURE)
    for dx, dy in moves:
        bolt.move_mouse(dx, dy)
    bolt.release_button(GESTURE)


# ---- the gesture button ---------------------------------------------------------

def test_assigning_a_gesture_card_diverts_the_button_with_raw_xy(client, bolt):
    assert assign_gesture(client)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.diverted == {GESTURE}
    assert bolt.mouse.raw_xy == {GESTURE}
    assert assigned_card_id(client, GESTURE) == "card_gesture_custom"


@pytest.mark.parametrize("direction,move", [("up", (0, -80)), ("down", (0, 80)),
                                            ("left", (-80, 0)), ("right", (80, 0))])
def test_a_swipe_runs_the_action_of_its_direction(client, bolt, uinput, direction, move):
    assign_gesture(client)
    swipe(bolt, move)
    assert wait_for(lambda: keys_down(uinput))
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF[direction]]


def test_motion_over_several_reports_adds_up(client, bolt, uinput):
    assign_gesture(client)
    swipe(bolt, (-20, 0), (-20, 3), (-20, -2))
    assert wait_for(lambda: keys_down(uinput))
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["left"]]


def test_the_stronger_axis_decides_the_direction(client, bolt, uinput):
    assign_gesture(client)
    swipe(bolt, (-30, -90))
    assert wait_for(lambda: keys_down(uinput))
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["up"]]


def test_a_press_without_movement_runs_the_click_action(client, bolt, uinput):
    assign_gesture(client)
    swipe(bolt)
    assert wait_for(lambda: keys_down(uinput))
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["click"]]


def test_a_small_wobble_is_still_a_click(client, bolt, uinput):
    assign_gesture(client)
    swipe(bolt, (4, -3), (-5, 2))
    assert wait_for(lambda: keys_down(uinput))
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["click"]]


def test_a_swipe_runs_one_action_and_no_click_on_release(client, bolt, uinput):
    assign_gesture(client)
    swipe(bolt, (-80, 0), (-80, 0), (0, 90))
    assert wait_for(lambda: keys_down(uinput))
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["left"]]


def test_each_press_starts_from_zero(client, bolt, uinput):
    assign_gesture(client)
    swipe(bolt, (-30, 0))                     # below the threshold: a click
    swipe(bolt, (0, 80))
    assert wait_for(lambda: len(keys_down(uinput)) >= 2)
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["click"], KEY_OF["down"]]


def test_motion_with_the_button_up_is_ignored(client, bolt, uinput):
    assign_gesture(client)
    bolt.emit_event(0x1B04, 1, b"\xff\xb0\x00\x00")       # a stray raw report
    bolt.settle()
    assert uinput.keys() == []


def test_the_threshold_comes_from_the_card_gesture_info(client, bolt, uinput):
    assign_gesture(client, gesture_card(limit=200))
    swipe(bolt, (-150, 0))                    # below 200: a click
    swipe(bolt, (-250, 0))
    assert wait_for(lambda: len(keys_down(uinput)) >= 2)
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["click"], KEY_OF["left"]]


def test_a_gesture_missing_an_action_does_nothing_for_it(client, bolt, uinput):
    assign_gesture(client, gesture_card({"left": 7}))
    swipe(bolt, (80, 0))
    swipe(bolt, (-80, 0))
    assert wait_for(lambda: keys_down(uinput))
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["left"]]


def test_assigning_the_default_card_back_undiverts_and_clears_raw_xy(client, bolt, uinput):
    assign_gesture(client)
    assert assign_gesture(client, GESTURE_DEFAULT)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.diverted == set() and bolt.mouse.raw_xy == set()
    swipe(bolt, (-80, 0))
    bolt.settle()
    assert uinput.keys() == []


def test_reset_undiverts_the_gesture_button(client, bolt):
    assign_gesture(client)
    assert client.call("SET", "/v2/profiles/reset", {"deviceId": "dev00000000"})["result"]["code"] == "SUCCESS"
    assert bolt.mouse.diverted == set() and bolt.mouse.raw_xy == set()


def test_a_keystroke_replacing_a_gesture_card_clears_raw_xy(client, bolt):
    assign_gesture(client)
    assign_slot(client, slot(GESTURE), keystroke(6, 224))
    assert bolt.mouse.diverted == {GESTURE} and bolt.mouse.raw_xy == set()


def test_gesture_is_diverted_again_after_the_mouse_wakes(client, bolt, uinput):
    assign_gesture(client)
    bolt.mouse.diverted.clear()
    bolt.mouse.raw_xy.clear()
    bolt.emit_event(F_WAKE)
    assert wait_for(lambda: bolt.mouse.raw_xy == {GESTURE} and bolt.mouse.diverted == {GESTURE})
    swipe(bolt, (80, 0))
    assert wait_for(lambda: keys_down(uinput))


def test_gesture_survives_an_agent_restart(agent, agent_factory, bolt, uinput):
    c = agent.client()
    assign_gesture(c)
    c.close()
    agent.stop()
    bolt.mouse.diverted.clear()
    bolt.mouse.raw_xy.clear()
    agent_factory()
    assert wait_for(lambda: bolt.mouse.raw_xy == {GESTURE})
    swipe(bolt, (0, -80))
    assert wait_for(lambda: keys_down(uinput))
    assert keys_down(uinput)[0] == KEY_OF["up"]


def test_an_unsupported_nested_action_is_an_error_and_changes_nothing(client, bolt):
    card = gesture_card()
    card["nestedCards"]["up"] = {"id": "x", "attribute": "MACRO_PLAYBACK",
                                 "macro": {"type": "NO_SUCH_ACTION"}}
    reply = assign_gesture(client, card)
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set() and bolt.mouse.raw_xy == set()
    assert assigned_card_id(client, GESTURE) == "card-gesture"


def test_a_gesture_card_without_any_action_is_an_error(client, bolt):
    assert assign_gesture(client, gesture_card({}))["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set()


def test_a_gesture_card_on_a_plain_button_is_an_error(client, bolt):
    reply = assign_slot(client, slot(BACK), gesture_card())
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set()


def test_a_failed_raw_xy_write_is_a_failure(client, bolt):
    bolt.mouse.ignore_divert = True
    assert assign_gesture(client)["result"]["code"] == "FAULTED"
    assert assigned_card_id(client, GESTURE) == "card-gesture"


def test_no_virtual_device_means_no_gesture_diversion(agent_factory, bolt, uinput):
    uinput.fail_create = True               # the device is created when the agent starts (wheel speed probes it)
    client = agent_factory().client()
    try:
        assert assign_gesture(client)["result"]["code"] == "FAULTED"
        assert bolt.mouse.diverted == set() and bolt.mouse.raw_xy == set()
    finally:
        client.close()


def test_a_failed_injection_does_not_stop_later_gestures(client, bolt, uinput):
    assign_gesture(client)
    uinput.fail_emit = True
    swipe(bolt, (80, 0))
    bolt.settle()
    uinput.fail_emit = False
    swipe(bolt, (80, 0))
    assert wait_for(lambda: keys_down(uinput))


# ---- the thumb wheel -----------------------------------------------------------------

def assign_thumb(client, card=None):
    return assign_slot(client, THUMB_SLOT, card or thumb_card())


def test_assigning_a_two_way_card_diverts_the_thumb_wheel(client, bolt):
    assert assign_thumb(client)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 1


def test_thumb_wheel_turned_right_and_left_runs_the_two_actions(client, bolt, uinput):
    assign_thumb(client)
    bolt.rotate_thumb(NOTCH)
    bolt.rotate_thumb(-NOTCH)
    assert wait_for(lambda: len(keys_down(uinput)) >= 2)
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["right"], KEY_OF["left"]]


def test_a_turn_of_several_steps_runs_the_action_once_per_step(client, bolt, uinput):
    assign_thumb(client)
    bolt.rotate_thumb(3 * NOTCH)
    assert wait_for(lambda: len(keys_down(uinput)) >= 3)
    bolt.settle()
    assert keys_down(uinput) == [KEY_OF["right"]] * 3


def test_thumb_wheel_default_card_gives_the_wheel_back(client, bolt, uinput):
    assign_thumb(client)
    assert assign_thumb(client, THUMB_DEFAULT)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 0
    bolt.rotate_thumb(3)
    bolt.settle()
    assert uinput.keys() == []


def test_reset_gives_the_thumb_wheel_back(client, bolt):
    assign_thumb(client)
    client.call("SET", "/v2/profiles/reset", {"deviceId": "dev00000000"})
    assert bolt.mouse.thumb["mode"] == 0


def test_thumb_diversion_is_reapplied_after_the_mouse_wakes(client, bolt):
    assign_thumb(client)
    bolt.mouse.thumb["mode"] = 0
    bolt.emit_event(F_WAKE)
    assert wait_for(lambda: bolt.mouse.thumb["mode"] == 1)


def test_thumb_diversion_keeps_the_direction_setting(client, bolt):
    bolt.mouse.thumb["invert"] = 1
    assign_thumb(client)
    assert bolt.mouse.thumb["mode"] == 1 and bolt.mouse.thumb["invert"] == 1


def test_an_unsupported_thumb_action_is_an_error_and_the_wheel_stays_native(client, bolt):
    card = thumb_card()
    card["nestedCards"]["left"]["macro"] = {"type": "NO_SUCH_ACTION"}
    assert assign_thumb(client, card)["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.thumb["mode"] == 0


def test_a_failed_thumb_write_is_a_failure(client, bolt):
    bolt.mouse.fail_thumb_write = True
    assert assign_thumb(client)["result"]["code"] == "FAULTED"


def test_thumb_assignment_survives_an_agent_restart(agent, agent_factory, bolt, uinput):
    c = agent.client()
    assign_thumb(c)
    c.close()
    agent.stop()
    bolt.mouse.thumb["mode"] = 0
    agent_factory()
    assert wait_for(lambda: bolt.mouse.thumb["mode"] == 1)
    bolt.rotate_thumb(2 * NOTCH)
    assert wait_for(lambda: keys_down(uinput))


def test_a_short_flick_of_the_thumb_wheel_is_not_an_action(client, bolt, uinput):
    assign_thumb(client)
    for _ in range(NOTCH - 1):
        bolt.rotate_thumb(1)              # one reporting unit each: less than a notch in all
    bolt.settle()
    assert keys_down(uinput) == []
    bolt.rotate_thumb(1)                  # the unit that completes the notch
    assert wait_for(lambda: keys_down(uinput) == [KEY_OF["right"]])


def test_the_thumb_step_is_the_wheels_own_resolution(client, bolt, uinput):
    bolt.mouse.thumb_diverted_units = 28
    assign_thumb(client)
    bolt.rotate_thumb(NOTCH)
    bolt.settle()
    assert keys_down(uinput) == []
    bolt.rotate_thumb(NOTCH)
    assert wait_for(lambda: keys_down(uinput) == [KEY_OF["right"]])


def test_the_thumb_step_falls_back_when_the_resolution_is_unknown(client, bolt, uinput):
    bolt.mouse.thumb_diverted_units = 0
    assign_thumb(client)
    bolt.rotate_thumb(gestures_mod.THUMB_STEP)
    assert wait_for(lambda: keys_down(uinput) == [KEY_OF["right"]])


# ---- which nested card is which direction ----------------------------------------------------

def test_a_nested_cards_own_id_does_not_name_its_slot():
    from opl_agent import gestures
    card = {"attribute": "ADAPTER_4WAYS", "nestedCards": {
        "down": {"id": "card_global_presets_volume_up"}, "up": {"id": "card_global_presets_volume_down"}}}
    found = gestures.nested_actions(card)
    assert found["down"]["id"] == "card_global_presets_volume_up"
    assert found["up"]["id"] == "card_global_presets_volume_down"


def test_slots_without_a_word_take_their_place_from_the_directions_list():
    from opl_agent import gestures
    card = {"attribute": "ADAPTER_4WAYS",
            "nestedCards": {"s1": {"id": "a"}, "s2": {"id": "b"}, "s3": {"id": "c"}, "s4": {"id": "d"}},
            "nestedCardsOrder": ["s1", "s2", "s3", "s4"],
            "gestureInfo": {"directions": [{"direction": d, "enabled": True}
                                           for d in ("UP", "DOWN", "LEFT", "RIGHT")]}}
    found = gestures.nested_actions(card)
    assert {w: c["id"] for w, c in found.items()} == {"up": "a", "down": "b", "left": "c", "right": "d"}


def test_slots_without_a_word_and_no_directions_are_not_guessed():
    from opl_agent import gestures
    card = {"attribute": "ADAPTER_4WAYS", "nestedCards": {"s1": {"id": "x"}}, "nestedCardsOrder": ["s1"]}
    assert gestures.nested_actions(card) == {}


def test_a_preset_gesture_card_with_no_actions_is_refused_by_name(client):
    card = {"id": "card_expose", "attribute": "ADAPTER_4WAYS",
            "gestureInfo": {"implementation": "CUSTOM_APP_EXPOSE_MISSION_CONTROL", "directionLocked": True}}
    reply = assign_gesture(client, card)
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert "CUSTOM_APP_EXPOSE_MISSION_CONTROL" in str(reply["result"])


@pytest.mark.parametrize("dx,keys", [(-60, [(125, 1), (104, 1), (104, 0), (125, 0)]),
                                     (60, [(125, 1), (109, 1), (109, 0), (125, 0)])])
def test_the_switch_desktops_preset_swipes_to_the_desktop_on_that_side(client, bolt, uinput, dx, keys):
    card = {"id": "card_switch_desktops", "attribute": "ADAPTER_4WAYS",
            "gestureInfo": {"implementation": "CUSTOM_SWITCH_DESKTOPS", "directionLocked": True}}
    assert assign_gesture(client, card)["result"]["code"] == "SUCCESS"
    swipe(bolt, (dx, 0))
    assert wait_for(lambda: len(uinput.keys()) >= 4)
    assert uinput.keys() == keys


# ---- the depot's default gesture card (ticket #21, #23) ------------------------------------
# The shape of Logitech's default for the gesture button: a ONE_OF card whose selected nested
# card is a four-way adapter with Windows chords. Ids and keys copy the structure only.
META, CTRL, KEY_META, KEY_CTRL, KEY_PAGEUP, KEY_PAGEDOWN, KEY_D = 227, 224, 125, 29, 104, 109, 32


def default_gesture_card(cid="card_gesture_default"):
    def inner(inner_id, macro):
        return {"id": inner_id, "attribute": "MACRO_PLAYBACK", "continuous": True, "macro": macro}

    def chord(code, *mods):
        return {"type": "KEYSTROKE", "keystroke": {"code": code, "modifiers": list(mods)}}

    adapter = {"id": cid + "_virtual_desktops", "attribute": "ADAPTER_4WAYS", "continuous": True,
               "gestureInfo": {"x": {"speedControl": True}, "y": {"speedControl": True}},
               "nestedCards": {
                   "click": inner("inner_card_vd_task_view", {"type": "SYSTEM", "system": {"action": "TASK_VIEW"}}),
                   "down": inner("inner_card_vd_hide_show_desktop", chord(7, META)),
                   "left": inner("inner_card_vd_desktop_left", chord(80, CTRL, META)),
                   "right": inner("inner_card_vd_desktop_right", chord(79, CTRL, META)),
                   "up": inner("inner_card_vd_start_menu", {"type": "KEYSTROKE", "keystroke": {"modifiers": [META]}})}}
    return {"id": cid, "attribute": "ONE_OF", "continuous": True, "selectedNestedCard": "virtual_desktops",
            "nestedCardsOrder": ["virtual_desktops"], "nestedCards": {"virtual_desktops": adapter}}


def test_a_modifier_only_keystroke_taps_the_modifier():
    from opl_agent import engine
    card = {"id": "x", "attribute": "MACRO_PLAYBACK",
            "macro": {"type": "KEYSTROKE", "keystroke": {"modifiers": [META]}}}
    assert engine.compile_card(card) == [("emit", [(1, KEY_META, 1), (1, KEY_META, 0)])]


def test_a_keystroke_without_key_or_modifier_is_refused():
    from opl_agent import engine
    card = {"id": "x", "attribute": "MACRO_PLAYBACK", "macro": {"type": "KEYSTROKE", "keystroke": {}}}
    with pytest.raises(engine.Unsupported):
        engine.compile_card(card)


# Top-level preset cards (ticket #22): the depot ships them as the same Windows chords.
PRESET_CHORDS = {
    "card_global_presets_desktop_left": ((80, CTRL, META), "SWITCH_BETWEEN_DESKTOPS_LEFT"),
    "card_global_presets_desktop_right": ((79, CTRL, META), "SWITCH_BETWEEN_DESKTOPS_RIGHT"),
    "card_global_presets_win_hide_show_desktop": ((7, META), "SHOW_DESKTOP"),
    "card_global_presets_win_switch_language": ((44, META), "SWITCH_LANGUAGE"),
    "card_global_presets_win_screen_snip": ((22, 225, META), "WINDOWS_SCREEN_CAPTURE"),
}


@pytest.mark.parametrize("card_id", sorted(PRESET_CHORDS))
def test_a_windows_chord_preset_card_compiles_to_the_linux_desktop_action(card_id):
    from opl_agent import engine
    (code, *mods), action = PRESET_CHORDS[card_id]
    card = {"id": card_id, "attribute": "MACRO_PLAYBACK",
            "macro": {"type": "KEYSTROKE", "keystroke": {"code": code, "modifiers": mods}}}
    assert engine.compile_card(card) == [("system", action)]


def test_a_preset_card_without_a_linux_equivalent_keeps_its_chord():
    from opl_agent import engine
    card = {"id": "card_global_presets_win_switch_screen", "attribute": "MACRO_PLAYBACK",
            "macro": {"type": "KEYSTROKE", "keystroke": {"code": 19, "modifiers": [META]}}}
    assert engine.compile_card(card) == [("emit", [(1, KEY_META, 1), (1, 25, 1), (1, 25, 0), (1, KEY_META, 0)])]


def test_the_default_gesture_card_compiles_with_linux_desktop_actions():
    from opl_agent import engine
    binding = engine.compile_binding(default_gesture_card())
    assert {word: steps for word, steps in binding.actions.items()} == {
        "click": [("system", "TASK_VIEW")],
        "left": [("system", "SWITCH_BETWEEN_DESKTOPS_LEFT")],
        "right": [("system", "SWITCH_BETWEEN_DESKTOPS_RIGHT")],
        "down": [("system", "SHOW_DESKTOP")],
        "up": [("system", "TASK_VIEW")]}


def test_only_a_card_that_resolves_to_a_four_way_adapter_needs_software():
    from opl_agent import profiles
    assert profiles.needs_software(default_gesture_card())
    assert profiles.needs_software(gesture_card())
    assert not profiles.needs_software(GESTURE_DEFAULT)          # nothing for the agent to run
    assert not profiles.needs_software(thumb_card())
    assert not profiles.needs_software(keystroke(6, 224))
    assert not profiles.needs_software({"id": "card_global_presets_win_back", "attribute": "MACRO_PLAYBACK",
                                        "macro": {"type": "MOUSE", "mouse": {"action": "WIN_BACK"}}})


@pytest.fixture
def default_gesture_agent(workdir, agent_factory):
    path = os.path.join(workdir, "depots", fx.DEPOT, "default_configurations.json")
    with open(path) as f:
        conf = json.load(f)
    conf["defaultConfigurations"][0]["settings"]["control_id_mappings"]["195"] = default_gesture_card()
    with open(path, "w") as f:
        json.dump(conf, f)
    running = agent_factory()
    c = running.client()
    yield running, c
    c.close()


def test_the_default_gesture_card_diverts_the_button_with_no_stored_choice(default_gesture_agent, bolt):
    assert wait_for(lambda: bolt.mouse.diverted == {GESTURE})      # and not back, forward or middle
    assert bolt.mouse.raw_xy == {GESTURE}


def test_swiping_left_on_the_default_gesture_card_injects_the_desktop_chord(default_gesture_agent, bolt, uinput):
    assert wait_for(lambda: bolt.mouse.diverted == {GESTURE})
    swipe(bolt, (-80, 0))
    assert wait_for(lambda: len(keys_down(uinput)) >= 2)
    bolt.settle()
    assert keys_down(uinput) == [KEY_META, KEY_PAGEUP]


def test_assigning_the_default_gesture_card_back_keeps_the_button_diverted(default_gesture_agent, bolt):
    _agent, client = default_gesture_agent
    assert wait_for(lambda: bolt.mouse.diverted == {GESTURE})
    assert assign_gesture(client, default_gesture_card())["result"]["code"] == "SUCCESS"
    assert bolt.mouse.diverted == {GESTURE}
