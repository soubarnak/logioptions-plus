"""Keystroke remap (ticket #21): a button assigned a keystroke is diverted through 0x1b04
and its presses are injected through the virtual input device."""

import time

from tests.fakes import depot as fx
from tests.fakes.hidpp_device import F_WAKE
from tests.test_wake_reapply import wait_for

TYPE = "type.googleapis.com/"
DEFAULT_PROFILE = "420fd454-0c36-499d-bde4-146823b16147"
BACK, FORWARD, LEFT = 82, 83, 80          # control ids of the simulated mouse (the test depot's defaults: 82 back, 83 forward)
# Linux evdev key codes, from linux/input-event-codes.h
KEY_LEFTCTRL, KEY_C, KEY_V = 29, 46, 47
BACK_DEFAULT = {"id": "card-back", "attribute": "MOUSE_BUTTON_ACTION"}


def slot(cid):
    return "%s_c%d" % (fx.SLOT_PREFIX, cid)


def keystroke(code, *modifiers, cid="card_copy"):
    return {"id": cid, "name": "Copy", "attribute": "MACRO_PLAYBACK", "readOnly": True,
            "macro": {"type": "KEYSTROKE", "keystroke": {"code": code, "modifiers": list(modifiers)}}}


COPY = keystroke(6, 224)                  # HID usage 6 = C, 224 = left Ctrl
PASTE = keystroke(25, 224, cid="card_paste")


def assign(client, cid, card):
    return client.call("SET", "/v2/profiles/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE, "assignment": {"slotId": slot(cid), "card": card}})


def assigned_card_id(client, cid):
    profile = client.call("GET", "/v2/profiles")["payload"]["profiles"][0]
    return next(a for a in profile["assignments"] if a["slotId"] == slot(cid))["card"]["id"]


def test_default_buttons_stay_undiverted(client, bolt):
    assert bolt.mouse.diverted == set()


def test_assigning_a_keystroke_diverts_that_button_only(client, bolt):
    reply = assign(client, BACK, COPY)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.diverted == {BACK}
    assert assigned_card_id(client, BACK) == "card_copy"


def test_pressing_the_diverted_button_injects_the_keystroke(client, bolt, uinput):
    assign(client, BACK, COPY)
    bolt.press_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= 4)
    assert uinput.keys() == [(KEY_LEFTCTRL, 1), (KEY_C, 1), (KEY_C, 0), (KEY_LEFTCTRL, 0)]


def test_releasing_the_button_injects_nothing_more_and_it_can_be_pressed_again(client, bolt, uinput):
    assign(client, BACK, COPY)
    for _ in range(2):
        bolt.press_button(BACK)
        bolt.release_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= 8)
    time.sleep(0.2)
    assert len(uinput.keys()) == 8


def test_each_button_runs_its_own_card(client, bolt, uinput):
    assign(client, BACK, COPY)
    assign(client, FORWARD, PASTE)
    bolt.press_button(FORWARD)
    assert wait_for(lambda: len(uinput.keys()) >= 4)
    assert uinput.keys() == [(KEY_LEFTCTRL, 1), (KEY_V, 1), (KEY_V, 0), (KEY_LEFTCTRL, 0)]


def test_only_one_virtual_device_is_created(client, bolt, uinput):
    assign(client, BACK, COPY)
    assign(client, FORWARD, PASTE)
    assert uinput.created == 1


def test_assigning_the_default_card_back_undiverts(client, bolt, uinput):
    assign(client, BACK, COPY)
    reply = assign(client, BACK, BACK_DEFAULT)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.diverted == set()
    bolt.press_button(BACK)
    time.sleep(0.2)
    assert uinput.keys() == []


def test_reset_undiverts_on_the_device(client, bolt):
    assign(client, BACK, COPY)
    reply = client.call("SET", "/v2/profiles/reset", {"deviceId": "dev00000000"})
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.diverted == set()
    assert assigned_card_id(client, BACK) == "card-back"


def test_assignment_is_diverted_again_after_the_mouse_wakes(client, bolt, uinput):
    assign(client, BACK, COPY)
    bolt.mouse.diverted.clear()               # the mouse slept and forgot its diversions
    bolt.emit_event(F_WAKE)
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    bolt.press_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= 4)


def test_assignment_is_diverted_again_after_the_receiver_is_replugged(client, bolt):
    assign(client, BACK, COPY)
    bolt.unplug()
    time.sleep(0.5)
    bolt.mouse.diverted.clear()
    bolt.plug()
    assert wait_for(lambda: bolt.mouse.diverted == {BACK}, timeout=10)


def test_assignment_survives_an_agent_restart(agent, agent_factory, bolt):
    c = agent.client()
    assign(c, BACK, COPY)
    c.close()
    agent.stop()
    bolt.mouse.diverted.clear()
    agent_factory()
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})


def test_a_diversion_left_by_a_crashed_agent_is_cleared_at_start(agent_factory, bolt):
    bolt.mouse.diverted.add(BACK)             # nothing assigned: the button would be dead
    agent_factory()
    assert wait_for(lambda: bolt.mouse.diverted == set())


# ---- errors the UI shows ------------------------------------------------------

def test_unsupported_card_type_is_an_error_and_changes_nothing(client, bolt):
    card = {"id": "card_vol", "attribute": "MACRO_PLAYBACK",
            "macro": {"type": "MEDIA", "media": {"action": "VOLUME_UP"}}}
    reply = assign(client, BACK, card)
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set()
    assert assigned_card_id(client, BACK) == "card-back"


def test_a_key_with_no_linux_equivalent_is_an_error(client, bolt):
    reply = assign(client, BACK, keystroke(0xF0))
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set()


def test_a_control_that_cannot_be_diverted_is_an_error(client, bolt):
    reply = assign(client, LEFT, COPY)
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert bolt.mouse.diverted == set()


def test_a_mouse_without_reprogrammable_controls_is_an_error(agent_factory, bolt):
    bolt.mouse.features.remove(0x1B04)
    c = agent_factory().client()
    assert assign(c, BACK, COPY)["result"]["code"] == "NOT_SUPPORTED"


def test_failed_divert_write_is_a_failure_and_keeps_the_old_assignment(client, bolt):
    bolt.mouse.fail_divert_write = True
    reply = assign(client, BACK, COPY)
    assert reply["result"]["code"] == "FAULTED"
    assert assigned_card_id(client, BACK) == "card-back"
    assert bolt.mouse.diverted == set()


def test_a_divert_the_mouse_ignored_is_a_failure(client, bolt):
    bolt.mouse.ignore_divert = True
    assert assign(client, BACK, COPY)["result"]["code"] == "FAULTED"
    assert assigned_card_id(client, BACK) == "card-back"


def test_failed_undivert_is_a_failure_and_the_button_keeps_working(client, bolt, uinput):
    assign(client, BACK, COPY)
    bolt.mouse.fail_divert_write = True
    assert assign(client, BACK, BACK_DEFAULT)["result"]["code"] == "FAULTED"
    assert assigned_card_id(client, BACK) == "card_copy"
    bolt.press_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= 4)


def test_no_virtual_device_means_no_diversion(client, bolt, uinput):
    uinput.fail_create = True
    assert assign(client, BACK, COPY)["result"]["code"] == "FAULTED"
    assert bolt.mouse.diverted == set()
    assert assigned_card_id(client, BACK) == "card-back"


def test_a_failed_injection_does_not_stop_later_presses(client, bolt, uinput):
    assign(client, BACK, COPY)
    uinput.fail_emit = True
    bolt.press_button(BACK)
    bolt.release_button(BACK)
    time.sleep(0.2)
    uinput.fail_emit = False
    bolt.press_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= 4)
