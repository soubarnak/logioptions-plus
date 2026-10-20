"""Settings survive mouse sleep and wake (ticket #12): events trigger reapply."""

import time

import pytest

from tests.harness import wait_for
from tests.fakes import depot as fx
from tests.fakes.hidpp_device import F_CONFIG_CHANGE, F_WAKE
from tests.test_pointer_speed import slider

DEV = "dev00000000"


@pytest.mark.parametrize("feature", [F_WAKE, F_CONFIG_CHANGE])
def test_event_reapplies_stored_settings(client, bolt, feature):
    slider(client, 0.5)
    bolt.mouse.dpi = 1000                      # mouse slept and woke at its own value
    bolt.emit_event(feature)
    assert wait_for(lambda: bolt.mouse.dpi == 4100)


def test_wake_refreshes_battery_and_broadcasts_it(client, bolt):
    _, _ = client.subscribe("/battery/%s/state/changed" % DEV)
    bolt.mouse.battery_percent = 41
    bolt.emit_event(F_WAKE)
    while True:
        m = client.recv()
        if m.get("path") == "/battery/%s/state/changed" % DEV and m.get("verb") == "BROADCAST":
            break
    assert m["payload"]["percentage"] == 41
    reply = client.call("GET", "/battery/%s/state" % DEV)
    assert reply["payload"]["percentage"] == 41


def test_unrelated_event_does_not_touch_the_mouse(client, bolt):
    slider(client, 0.5)
    bolt.mouse.dpi = 1000
    bolt.emit_event(0x1004)                    # a battery event, not wake or config change
    bolt.settle()
    assert bolt.mouse.dpi == 1000


def test_event_from_another_device_index_is_ignored(client, bolt):
    slider(client, 0.5)
    bolt.mouse.dpi = 1000
    bolt.emit_event(F_WAKE, index=4)
    bolt.settle()
    assert bolt.mouse.dpi == 1000


# ---- the receiver's dispatch thread stays free for input (review of ticket #12) -------

import threading  # noqa: E402

from opl_agent import model  # noqa: E402


@pytest.fixture
def registry(bolt):
    reg = model.Registry(transport=bolt)
    reg.scan()
    yield reg
    reg.close()


def test_a_slow_resume_does_not_hold_back_button_events(bolt, registry):
    gate, started = threading.Event(), threading.Event()
    buttons = []

    def slow_resume(_dev_id):
        started.set()
        gate.wait(10)

    registry.on_resumed.append(slow_resume)
    registry.on_button_event.append(lambda dev_id, fn, params: buttons.append((dev_id, fn)))
    registry.set_divert(DEV, 82, True)
    try:
        bolt.emit_event(F_WAKE)
        assert started.wait(2)
        bolt.press_button(82)
        assert wait_for(lambda: buttons, timeout=2)         # delivered while the resume is still running
    finally:
        gate.set()


def test_wake_events_that_pile_up_are_merged_per_device(bolt, registry):
    gate, started = threading.Event(), threading.Event()
    runs = []

    def slow_resume(dev_id):
        runs.append(dev_id)
        started.set()
        gate.wait(5)

    registry.on_resumed.append(slow_resume)
    bolt.emit_event(F_WAKE)
    assert started.wait(2)
    for _ in range(5):
        bolt.emit_event(F_WAKE)
        bolt.emit_event(F_CONFIG_CHANGE)
    time.sleep(0.3)
    gate.set()
    assert wait_for(lambda: len(runs) == 2)      # the one running, and one more for all the rest
    time.sleep(0.3)
    assert runs == [DEV, DEV]


def test_a_mouse_reporting_its_link_up_is_reapplied_and_its_events_relearned(bolt, registry):
    ran = threading.Event()
    registry.on_resumed.append(lambda _d: ran.set())
    registry.devices[DEV].event_features.clear()    # it was asleep when discovered: nothing was learned
    bolt.emit_connection(link=True)
    assert ran.wait(2)
    assert sorted(registry.devices[DEV].event_features.values()) == [F_CONFIG_CHANGE, F_WAKE]
    bolt.emit_event(F_WAKE)                          # and a later wake event is understood
    ran.clear()
    assert ran.wait(2)


def test_a_lost_link_does_not_reapply(bolt, registry):
    ran = threading.Event()
    registry.on_resumed.append(lambda _d: ran.set())
    bolt.emit_connection(link=False)
    assert not ran.wait(0.4)
