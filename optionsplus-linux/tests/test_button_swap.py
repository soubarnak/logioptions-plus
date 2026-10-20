"""Swap left and right buttons (ticket #24): a device-level remap through 0x1b04 when the mouse
allows it, xmodmap on X11 when it does not, an honest error on Wayland. The choice persists."""

import json
import os
import time

import pytest

from tests.fakes.hidpp_device import F_WAKE
from tests.fakes.runner import FakeRunner

TYPE = "type.googleapis.com/logi.protocol.mouse.MouseButtonSwap"
LEFT, RIGHT = 80, 81
SWAPPED = {LEFT: RIGHT, RIGHT: LEFT}


def wait_for(cond, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


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


def test_the_per_device_route_swaps_too(remappable, client):
    swap(client, "RIGHT", "/mouse/dev00000000/swap")
    assert remappable.mouse.remap == SWAPPED
    assert state(client, "/mouse/dev00000000/swap") == "RIGHT"


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
    assert json.load(open(os.path.join(workdir, "state", "button_swap.json"))) == {"state": "RIGHT"}


# ---- X11 fallback ------------------------------------------------------------------

def test_x11_fallback_runs_xmodmap_when_the_mouse_cannot_remap(agent_factory, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    runner = FakeRunner(installed={"xmodmap"})
    client = agent_factory(runner=runner).client()
    assert swap(client, "RIGHT")["result"]["code"] == "SUCCESS"
    assert runner.executed == [["xmodmap", "-e", "pointer = 3 2 1"]]
    assert state(client) == "RIGHT"
    swap(client, "LEFT")
    assert runner.executed[-1] == ["xmodmap", "-e", "pointer = default"]
    assert state(client) == "LEFT"
    client.close()


def test_x11_fallback_failure_is_an_error(agent_factory, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    runner = FakeRunner(installed={"xmodmap"}, exit_codes={"xmodmap": 1})
    client = agent_factory(runner=runner).client()
    assert swap(client, "RIGHT")["result"]["code"] != "SUCCESS"
    assert state(client) == "LEFT"
    client.close()


def test_x11_swap_is_reapplied_when_the_agent_starts(agent_factory, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    a = agent_factory(runner=FakeRunner(installed={"xmodmap"}))
    c = a.client()
    swap(c, "RIGHT")
    c.close()
    a.stop()
    second = FakeRunner(installed={"xmodmap"})
    agent_factory(runner=second)
    assert wait_for(lambda: ["xmodmap", "-e", "pointer = 3 2 1"] in second.executed)


def test_device_remap_wins_over_x11_so_buttons_swap_once(remappable, agent_factory, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    runner = FakeRunner(installed={"xmodmap"})
    client = agent_factory(runner=runner).client()
    swap(client, "RIGHT")
    assert remappable.mouse.remap == SWAPPED
    assert runner.executed == []
    client.close()


# ---- Wayland / nothing available ------------------------------------------------------

def test_wayland_without_device_support_is_reported_unsupported(agent_factory, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")               # XWayland is not the desktop's pointer map
    runner = FakeRunner(installed={"xmodmap"})
    client = agent_factory(runner=runner).client()
    reply = swap(client, "RIGHT")
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert runner.executed == []
    assert state(client) == "LEFT"
    client.close()


def test_no_display_and_no_device_support_is_unsupported(client):
    assert swap(client, "RIGHT")["result"]["code"] == "NOT_SUPPORTED"


def test_swapping_back_when_nothing_is_swapped_is_not_an_error(client):
    assert swap(client, "LEFT")["result"]["code"] == "SUCCESS"
