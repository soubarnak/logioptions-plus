"""Review fixes for gestures, the thumb wheel and per-application switching (tickets #23, #25, #27):
held buttons across a switch, a switch that fails and is retried, the UI told of a switch,
reapplying under the same lock as everything else."""

import threading
import time

import pytest

from tests.fakes.hidpp_device import F_REPROG, F_WAKE
from tests.test_app_profiles import (FIREFOX, RELOAD, RELOAD_KEYS, TERMINAL, assign_to, press, settled,
                                     windows)  # noqa: F401  (windows is a fixture)
from tests.test_button_remap import BACK, COPY, assign
from tests.test_gestures import (ACTIONS, GESTURE, KEY_OF, NOTCH, assign_gesture, assign_thumb, gesture_card,
                                 keys_down, settle, swipe)
from tests.test_wake_reapply import wait_for


@pytest.fixture
def tracked(agent_factory, windows):
    return agent_factory(window_source=windows, focus_seconds=0.02, focus_retry=(0.05, 0.2))


@pytest.fixture
def client(tracked):
    c = tracked.client()
    yield c
    c.close()


# ---- #27 item 11: the UI is told when the profile in force changes ------------------------------

def test_a_focus_switch_is_broadcast_to_the_ui(tracked, client, bolt, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    sub = tracked.client()
    try:
        sub.subscribe("/v2/profiles/update")
        windows.focus(("Navigator", "firefox"))
        assert wait_for(lambda: bolt.mouse.diverted == {BACK})
        deadline, seen = time.time() + 5, None
        while time.time() < deadline and seen is None:
            msg = sub.recv()
            if msg.get("path") == "/v2/profiles/update":
                seen = msg
        assert seen is not None
        active = [p for p in seen["payload"]["profiles"] if p.get("activeForApplication")]
        assert any(p["applicationId"] == FIREFOX for p in active)
    finally:
        sub.close()


# ---- #27 item 8: a failed switch is tried again for the same window ---------------------------------

def test_a_failed_switch_is_retried_while_the_focus_stays(client, bolt, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    bolt.mouse.fail_divert_write = True
    windows.focus(("Navigator", "firefox"))
    time.sleep(0.3)
    assert bolt.mouse.diverted == set()
    bolt.mouse.fail_divert_write = False              # the mouse answers again; the window did not change
    assert wait_for(lambda: bolt.mouse.diverted == {BACK}, timeout=5)


def test_a_failed_switch_leaves_the_old_profile_in_force(tracked, client, bolt, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    bolt.mouse.fail_divert_write = True
    windows.focus(("Navigator", "firefox"))
    time.sleep(0.3)
    assert tracked.agent.router.engine.active_app is None


# ---- #27 item 6: a button held across a switch ---------------------------------------------------------------

def test_a_button_held_while_its_card_goes_away_does_not_swallow_the_next_press(tracked, client, bolt, uinput, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    bolt.press_button(BACK)
    assert wait_for(lambda: uinput.keys() == RELOAD_KEYS)
    windows.focus(("x", "XTerm"))                       # Back is the mouse's again, with the release unseen
    assert wait_for(lambda: bolt.mouse.diverted == set())
    assert BACK not in tracked.agent.router.engine._held.get("dev00000000", set())
    bolt.mouse.held.clear()
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    before = len(uinput.keys())
    bolt.press_button(BACK)
    assert wait_for(lambda: len(uinput.keys()) >= before + 2)


# ---- #27 item 4: one lock for everything that decides what is diverted -----------------------------------------

def test_reapplying_after_a_wake_waits_for_a_switch_in_progress(tracked, client, bolt):
    router = tracked.agent.router
    done = threading.Event()
    with router._switch_lock:
        t = threading.Thread(target=lambda: (router.apply_stored("dev00000000"), done.set()))
        t.start()
        assert not done.wait(0.3)
    assert done.wait(5)
    t.join()


def test_resetting_an_application_profile_waits_for_a_switch_in_progress(tracked, client, bolt):
    router = tracked.agent.router
    assign_to(client, FIREFOX, BACK, RELOAD)
    done = threading.Event()
    devices = list(router.reg.devices.values())
    with router._switch_lock:
        t = threading.Thread(target=lambda: (router._reset_app(FIREFOX, devices), done.set()))
        t.start()
        assert not done.wait(0.3)
        assert FIREFOX in router.store.apps
    assert done.wait(5)
    t.join()
    assert FIREFOX not in router.store.apps


def test_focus_and_app_resets_hammering_each_other_stay_consistent(tracked, client, bolt, windows):
    router = tracked.agent.router
    devices = list(router.reg.devices.values())
    errors = []

    def resets():
        try:
            for _ in range(15):
                router.store.set("046d_4242_c82", RELOAD, FIREFOX)
                router._reset_app(FIREFOX, devices)
        except Exception as exc:                          # "dictionary changed size during iteration"
            errors.append(exc)
    t = threading.Thread(target=resets)
    t.start()
    for i in range(30):
        router.on_focus(None if i % 2 else __import__("opl_agent.focus", fromlist=["Window"]).Window(
            wm_class=("Navigator", "firefox"), exe=""))
    t.join()
    assert errors == []


# ---- #23 item 10: a press in progress when its card is replaced ------------------------------------------------------

def test_reassigning_the_gesture_button_while_held_runs_no_stale_click(client, bolt, uinput):
    assign_gesture(client)
    bolt.press_button(GESTURE)
    assign_gesture(client, gesture_card({"click": 10}, cid="card_gesture_other"))
    bolt.release_button(GESTURE)
    settle()
    assert keys_down(uinput) == []


# ---- #23 item 11: a thumb wheel on a mouse with no reprogrammable controls ----------------------------------------------

def test_thumb_wheel_is_diverted_without_the_button_feature(agent_factory, bolt, uinput):
    first = agent_factory()
    c = first.client()
    assign_thumb(c)
    c.close()
    first.stop()
    bolt.mouse.thumb["mode"] = 0
    bolt.mouse.features[bolt.mouse.features.index(F_REPROG)] = 0xFFFF      # no 0x1b04 on this mouse
    agent_factory()
    assert wait_for(lambda: bolt.mouse.thumb["mode"] == 1)
    bolt.rotate_thumb(NOTCH)
    assert wait_for(lambda: keys_down(uinput) == [KEY_OF["right"]])


# ---- #23 item 14: the thumb event as the real mouse lays it out -----------------------------------------------------------

def test_the_thumb_event_has_rotation_timestamp_status_and_flags(bolt):
    from opl_agent import hidpp
    seen = []
    from tests.fakes.hidpp_device import MOUSE_INDEX
    bolt.emit_event = lambda fid, function=0, params=b"", index=MOUSE_INDEX: seen.append(bytes(params))
    from tests.fakes.hidpp_device import SimulatedBolt
    SimulatedBolt.turn_thumb(bolt, 5)
    bolt.mouse.thumb["mode"] = 1
    SimulatedBolt.turn_thumb(bolt, -5)
    assert len(seen[0]) == 6
    assert hidpp.parse_thumb_rotation(seen[0]) == -5 and hidpp.parse_thumb_event(seen[0]) == -5
    assert hidpp.parse_thumb_rotation(b"\x00\x05") == 5          # a short report still parses


# ---- #27 item 3 / 6: the thumb wheel goes back to the speed relay when the profile hands it back ------------

def test_a_thumb_card_of_the_focused_app_and_the_speed_relay_take_turns(client, bolt, uinput, windows):
    from tests.test_gestures import THUMB_SLOT, thumb_card
    from tests.test_wheel_relay import FAST, REL_HWHEEL_HI_RES, thumb
    thumb(client, speed=FAST)
    assert bolt.mouse.thumb["mode"] == 1                         # relayed, scaled
    reply = client.call("SET", "/v2/profiles/assignment", {
        "profileId": FIREFOX, "assignment": {"slotId": THUMB_SLOT, "card": thumb_card()}})
    assert reply["result"]["code"] == "SUCCESS"
    windows.focus(("Navigator", "firefox"))
    settled(windows)
    bolt.turn_thumb(NOTCH)
    assert wait_for(lambda: keys_down(uinput) == [KEY_OF["right"]])
    assert uinput.rels(REL_HWHEEL_HI_RES) == []                    # the card owns the wheel: no scaled output too
    windows.focus(("x", "XTerm"))
    settled(windows)
    bolt.turn_thumb(NOTCH)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES))        # the relay has it again
    assert keys_down(uinput) == [KEY_OF["right"]]
