"""Settings survive mouse sleep and wake (ticket #12): events trigger reapply."""

import time

import pytest

from tests.fakes import depot as fx
from tests.fakes.hidpp_device import F_CONFIG_CHANGE, F_WAKE
from tests.test_pointer_speed import slider

DEV = "dev00000000"


def wait_for(cond, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


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
    time.sleep(0.4)
    assert bolt.mouse.dpi == 1000


def test_event_from_another_device_index_is_ignored(client, bolt):
    slider(client, 0.5)
    bolt.mouse.dpi = 1000
    bolt.emit_event(F_WAKE, index=4)
    time.sleep(0.4)
    assert bolt.mouse.dpi == 1000
