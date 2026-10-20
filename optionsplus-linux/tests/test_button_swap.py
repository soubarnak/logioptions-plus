"""Swap left and right buttons (ticket #24): ONE method system-wide. A device-level remap through
0x1b04 when every connected mouse allows it, per-device `xinput set-button-map` on X11 when
one does not, an honest error on Wayland. The choice and the method persist."""

import json
import os
import struct
import time

import pytest

from tests.harness import wait_for
from tests.fakes.hidpp_device import F_WAKE
from tests.fakes.runner import FakeRunner

TYPE = "type.googleapis.com/logi.protocol.mouse.MouseButtonSwap"
LEFT, RIGHT = 80, 81
SWAPPED = {LEFT: RIGHT, RIGHT: LEFT}


def swap(client, state, path="/mouse/global/swap"):
    return client.call("SET", path, {"@type": TYPE, "state": state})


def state(client, path="/mouse/global/swap"):
    return client.call("GET", path)["payload"]["state"]


@pytest.fixture(autouse=True)
def no_session_type(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)


@pytest.fixture
def remappable(bolt):
    bolt.mouse.remappable_primary = True
    return bolt


# ---- device-level remap ---------------------------------------------------------

def test_swapping_remaps_the_primary_buttons_on_the_mouse(remappable, client):
    reply = swap(client, "RIGHT")
    assert reply["result"]["code"] == "SUCCESS"
    assert remappable.mouse.remap == SWAPPED
    assert state(client) == "RIGHT"


def test_swapping_back_restores_both_buttons(remappable, client):
    swap(client, "RIGHT")
    assert swap(client, "LEFT")["result"]["code"] == "SUCCESS"
    assert remappable.mouse.remap == {}
    assert state(client) == "LEFT"


def test_default_state_means_not_swapped(remappable, client):
    swap(client, "RIGHT")
    swap(client, "DEFAULT")
    assert remappable.mouse.remap == {}


def test_there_is_no_per_device_swap_route(remappable, client):
    assert swap(client, "RIGHT", "/mouse/dev00000000/swap")["result"]["code"] != "SUCCESS"
    assert client.call("GET", "/mouse/dev00000000/swap")["result"]["code"] == "NO_SUCH_PATH"
    assert remappable.mouse.remap == {}


def test_nothing_is_swapped_at_first(remappable, client):
    assert state(client) == "LEFT"
    assert remappable.mouse.remap == {}


def test_a_failed_device_write_is_an_error_and_not_remembered(remappable, client):
    remappable.mouse.fail_remap_write = True
    reply = swap(client, "RIGHT")
    assert reply["result"]["code"] != "SUCCESS"
    assert remappable.mouse.remap == {}
    assert state(client) == "LEFT"


def test_a_half_applied_swap_is_undone(remappable, client):
    remappable.mouse.fail_remap_write_after = 1       # the second control refuses
    reply = swap(client, "RIGHT")
    assert reply["result"]["code"] != "SUCCESS"
    assert remappable.mouse.remap == {}


def test_diverting_a_button_keeps_the_swap(remappable, client):
    from tests.test_button_remap import BACK, COPY, assign
    swap(client, "RIGHT")
    assign(client, BACK, COPY)
    assert remappable.mouse.remap == SWAPPED


# ---- persistence -----------------------------------------------------------------

def test_swap_is_reapplied_after_the_mouse_wakes(remappable, client):
    swap(client, "RIGHT")
    remappable.mouse.remap.clear()                     # the mouse slept and forgot it
    remappable.emit_event(F_WAKE)
    assert wait_for(lambda: remappable.mouse.remap == SWAPPED)


def test_swap_is_reapplied_after_the_receiver_is_replugged(remappable, client):
    swap(client, "RIGHT")
    remappable.unplug()
    time.sleep(0.5)
    remappable.mouse.remap.clear()
    remappable.plug()
    assert wait_for(lambda: remappable.mouse.remap == SWAPPED, timeout=10)


def test_swap_survives_an_agent_restart(remappable, agent, agent_factory):
    c = agent.client()
    swap(c, "RIGHT")
    c.close()
    agent.stop()
    remappable.mouse.remap.clear()
    again = agent_factory()
    assert wait_for(lambda: remappable.mouse.remap == SWAPPED)
    c = again.client()
    assert state(c) == "RIGHT"
    c.close()


def test_an_unswapped_agent_clears_a_swap_the_mouse_still_holds(remappable, agent_factory):
    remappable.mouse.remap.update(SWAPPED)             # left by an earlier session
    agent_factory()
    assert wait_for(lambda: remappable.mouse.remap == {})


def test_swap_is_kept_in_a_file_in_the_state_directory(remappable, client, workdir):
    swap(client, "RIGHT")
    assert json.load(open(os.path.join(workdir, "state", "button_swap.json"))) == {"state": "RIGHT", "method": "device"}


# ---- 0x1b04 semantics ---------------------------------------------------------------

def test_a_divert_write_naming_no_target_keeps_the_mapping(bolt):
    mouse = bolt.mouse
    mouse.remappable_primary = True
    idx = mouse.features.index(0x1B04)
    mouse.handle(idx, 3, struct.pack(">HBH", LEFT, 0, RIGHT))
    assert mouse.remap == {LEFT: RIGHT}
    mouse.handle(idx, 3, struct.pack(">HBH", LEFT, 0x03, 0))          # divert on, remap 0
    assert mouse.remap == {LEFT: RIGHT}
    mouse.handle(idx, 3, struct.pack(">HBH", LEFT, 0, LEFT))          # naming itself undoes it
    assert mouse.remap == {}


def test_a_mouse_whose_group_mask_forbids_the_swap_uses_x11_not_a_failing_write(bolt, agent_factory, monkeypatch):
    bolt.mouse.remappable_primary = True
    bolt.mouse.primary_group_mask = 0
    x11(monkeypatch)
    runner = xinput_runner()
    client = agent_factory(runner=runner).client()
    assert swap(client, "RIGHT")["result"]["code"] == "SUCCESS"
    assert bolt.mouse.remap == {}
    assert maps(runner) == [("11", "3 2 1"), ("14", "3 2 1")]
    client.close()


def test_a_rejected_device_write_falls_back_to_x11(remappable, agent_factory, monkeypatch):
    remappable.mouse.fail_remap_write = True
    x11(monkeypatch)
    runner = xinput_runner()
    client = agent_factory(runner=runner).client()
    assert swap(client, "RIGHT")["result"]["code"] == "SUCCESS"
    assert remappable.mouse.remap == {}
    assert maps(runner) == [("11", "3 2 1"), ("14", "3 2 1")]
    assert state(client) == "RIGHT"
    client.close()


# ---- X11 fallback ------------------------------------------------------------------

XINPUT_LIST = (
    "\u23a1 Virtual core pointer                    \tid=2\t[master pointer  (3)]\n"
    "\u239c   \u21b3 Virtual core XTEST pointer              \tid=4\t[slave  pointer  (2)]\n"
    "\u239c   \u21b3 Test Mouse X1                           \tid=11\t[slave  pointer  (2)]\n"
    "\u239c   \u21b3 SynPS/2 Synaptics TouchPad              \tid=12\t[slave  pointer  (2)]\n"
    "\u239c   \u21b3 optionsplus-linux virtual input         \tid=13\t[slave  pointer  (2)]\n"
    "\u239c   \u21b3 Logitech USB Receiver Mouse             \tid=14\t[slave  pointer  (2)]\n"
    "\u239c   \u21b3 Logitech USB Receiver Consumer Control  \tid=15\t[slave  keyboard (3)]\n")


def x11(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")


def xinput_runner(**kw):
    return FakeRunner(installed={"xinput"}, outputs={"xinput": XINPUT_LIST}, **kw)


def maps(runner):
    """(device id, map) of every `xinput set-button-map` the agent ran."""
    return [(a[2], " ".join(a[3:])) for a in runner.executed if a[:2] == ["xinput", "set-button-map"]]


def test_x11_fallback_sets_the_button_map_of_the_logitech_pointers_only(agent_factory, monkeypatch):
    x11(monkeypatch)
    runner = xinput_runner()
    client = agent_factory(runner=runner).client()
    assert swap(client, "RIGHT")["result"]["code"] == "SUCCESS"
    # the mouse by its HID++ name and the receiver pointer by "Logitech"; not the touchpad,
    # not the virtual master pointers and not our own uinput device (whose clicks must stay as sent)
    assert sorted(maps(runner)) == [("11", "3 2 1"), ("14", "3 2 1")]
    assert state(client) == "RIGHT"
    swap(client, "LEFT")
    assert maps(runner)[-2:] == [("11", "1 2 3"), ("14", "1 2 3")]
    assert state(client) == "LEFT"
    client.close()


def test_x11_fallback_failure_is_an_error(agent_factory, monkeypatch):
    x11(monkeypatch)
    runner = xinput_runner(exit_codes={"xinput": 1})
    client = agent_factory(runner=runner).client()
    assert swap(client, "RIGHT")["result"]["code"] != "SUCCESS"
    assert state(client) == "LEFT"
    client.close()


def test_x11_swap_and_its_method_are_kept_and_reapplied_when_the_agent_starts(agent_factory, monkeypatch, workdir):
    x11(monkeypatch)
    a = agent_factory(runner=xinput_runner())
    c = a.client()
    swap(c, "RIGHT")
    c.close()
    a.stop()
    assert json.load(open(os.path.join(workdir, "state", "button_swap.json"))) == {"state": "RIGHT", "method": "x11"}
    second = xinput_runner()
    agent_factory(runner=second)
    assert wait_for(lambda: ("11", "3 2 1") in maps(second))


def test_device_remap_wins_over_x11_so_buttons_swap_once(remappable, agent_factory, monkeypatch):
    x11(monkeypatch)
    runner = xinput_runner()
    client = agent_factory(runner=runner).client()
    swap(client, "RIGHT")
    assert remappable.mouse.remap == SWAPPED
    assert maps(runner) == []
    client.close()


def test_switching_from_x11_to_the_device_method_resets_the_x11_map(remappable, agent_factory, monkeypatch, workdir):
    """A swap saved by the X11 method, then every mouse can remap: not swapped twice."""
    os.makedirs(os.path.join(workdir, "state"))
    json.dump({"state": "RIGHT", "method": "x11"}, open(os.path.join(workdir, "state", "button_swap.json"), "w"))
    x11(monkeypatch)
    runner = xinput_runner()
    agent_factory(runner=runner)
    assert wait_for(lambda: remappable.mouse.remap == SWAPPED)
    assert wait_for(lambda: ("11", "1 2 3") in maps(runner))
    assert ("11", "3 2 1") not in maps(runner)


def test_x11_needs_an_x11_session_not_just_a_display(agent_factory, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")               # XWayland looks the same, but is not the pointer map
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    runner = xinput_runner()
    client = agent_factory(runner=runner).client()
    assert swap(client, "RIGHT")["result"]["code"] == "NOT_SUPPORTED"
    assert maps(runner) == []
    client.close()


# ---- Wayland / nothing available ------------------------------------------------------

def test_wayland_without_device_support_is_reported_unsupported(agent_factory, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")               # XWayland is not the desktop's pointer map
    runner = xinput_runner()
    client = agent_factory(runner=runner).client()
    reply = swap(client, "RIGHT")
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert maps(runner) == []
    assert state(client) == "LEFT"
    client.close()


def test_no_display_and_no_device_support_is_unsupported(client):
    assert swap(client, "RIGHT")["result"]["code"] == "NOT_SUPPORTED"


def test_with_no_mouse_connected_the_choice_is_kept_for_when_one_connects(bolt, agent_factory, workdir):
    bolt.unplug()
    c = agent_factory().client()
    assert swap(c, "RIGHT")["result"]["code"] == "SUCCESS"
    assert state(c) == "RIGHT"
    c.close()
    bolt.mouse.remappable_primary = True
    bolt.plug()
    assert wait_for(lambda: bolt.mouse.remap == SWAPPED, timeout=10)


def test_swapping_back_when_nothing_is_swapped_is_not_an_error(client):
    assert swap(client, "LEFT")["result"]["code"] == "SUCCESS"
