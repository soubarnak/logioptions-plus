"""Live hot-plug of receivers and devices (ticket #13), driven through the simulated transport."""

import os
import time

import pytest

from tests.harness import wait_for
from tests.test_battery import next_push
from tests.test_pointer_speed import slider

DEV = "dev00000000"


@pytest.fixture
def live(bolt, agent_factory):
    """A client subscribed to both device-state paths on an agent that rescans quickly."""
    c = agent_factory(rescan_seconds=0.1).client()
    c.subscribe("/devices/state/changed")
    c.subscribe("/devices/state/deactivated")
    yield c
    c.close()


def device_ids(client):
    infos = client.call("GET", "/devices/list")["payload"].get("deviceInfos", [])
    return [d["id"] for d in infos if d["deviceType"] != "RECEIVER"]     # the receiver is covered in test_pairing


# ---- receiver unplug / replug ----------------------------------------------

def test_unplugging_the_receiver_deactivates_the_device_and_drops_its_card(bolt, live):
    bolt.unplug()
    gone = next_push(live, "/devices/state/deactivated")
    assert gone["id"] == DEV and gone["state"] == "ABSENT" and gone["connected"] is False
    assert device_ids(live) == []


def test_replugging_the_receiver_brings_the_same_device_back_active(bolt, live):
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.plug()
    back = next_push(live, "/devices/state/changed")
    assert back["id"] == DEV and back["state"] == "ACTIVE" and back["connected"] is True
    assert device_ids(live) == [DEV]


def test_device_keeps_its_id_when_the_receiver_returns_as_another_hidraw_node(bolt, live):
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.plug("/dev/hidraw-sim7")
    assert next_push(live, "/devices/state/changed")["id"] == DEV
    assert live.call("GET", "/battery/%s/state" % DEV)["payload"]["percentage"] == 73.0


def test_a_different_mouse_on_the_replugged_receiver_gets_a_new_id(bolt, live):
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.mouse.wpid = 0x4343
    bolt.plug()
    assert next_push(live, "/devices/state/changed")["id"] == "dev00000001"
    assert device_ids(live) == ["dev00000001"]


def test_battery_is_refreshed_on_return(bolt, live):
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.mouse.battery_percent = 55
    bolt.plug()
    back = next_push(live, "/devices/state/changed")
    assert back["batteryDischargeLevel"] == 55
    assert live.call("GET", "/battery/%s/state" % DEV)["payload"]["percentage"] == 55.0


def test_settings_are_reapplied_when_the_receiver_returns(bolt, live):
    slider(live, 0.5)
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.mouse.dpi = 1000
    bolt.plug()
    next_push(live, "/devices/state/changed")
    assert bolt.mouse.dpi == 4100


# ---- the mouse switching off and on, receiver staying put ------------------

def test_switching_the_mouse_off_deactivates_it(bolt, live):
    bolt.mouse.power(False)
    assert next_push(live, "/devices/state/deactivated")["id"] == DEV
    assert device_ids(live) == []


def test_switching_the_mouse_on_restores_it_with_settings_and_battery(bolt, live):
    slider(live, 0.8)
    bolt.mouse.power(False)
    next_push(live, "/devices/state/deactivated")
    bolt.mouse.battery_percent = 64
    bolt.mouse.power(True)                     # comes back at its own default DPI
    back = next_push(live, "/devices/state/changed")
    assert back["id"] == DEV and back["batteryDischargeLevel"] == 64
    assert wait_for(lambda: bolt.mouse.dpi == 6450)


def test_a_mouse_switched_off_mid_probe_is_removed_not_re_announced_as_a_new_device(bolt, monkeypatch):
    from opl_agent import hidpp
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg.scan()
    real_name = hidpp.get_device_name

    def name_after_switch_off(fd, idx):
        bolt.mouse.power(False)                # it answered the ping, then went off
        return real_name(fd, idx)

    monkeypatch.setattr(hidpp, "get_device_name", name_after_switch_off)
    events = reg.rescan()
    assert events["added"] == []               # no ghost with a nameless identity
    assert [d.id for d in events["removed"]] == [DEV]
    monkeypatch.undo()
    bolt.mouse.power(True)
    events = reg.rescan()
    assert events["added"] == [] and events["returned"] == [DEV]


def test_a_steady_device_is_not_re_announced(bolt, live):
    live.sock.settimeout(0.6)
    with pytest.raises((TimeoutError, OSError)):
        live.recv()


# ---- node watcher (production side of the transport) -----------------------

def make_nodes(root, *names):
    os.makedirs(root, exist_ok=True)
    for n in names:
        open(os.path.join(root, n), "w").close()


@pytest.mark.parametrize("inotify", [True, False], ids=["inotify", "polling"])
def test_node_watcher_notices_hidraw_nodes_added_and_removed(tmp_path, inotify):
    from opl_agent.hidpp import NodeWatcher
    dev = str(tmp_path / "dev")
    sysfs = str(tmp_path / "sys")
    make_nodes(dev, "hidraw0")
    make_nodes(sysfs, "hidraw0")
    w = NodeWatcher(dev_dir=dev, sysfs_dir=sysfs, use_inotify=inotify, poll_seconds=0.05)
    try:
        assert w.wait(0.2) is False
        make_nodes(dev, "hidraw1")
        make_nodes(sysfs, "hidraw1")
        assert w.wait(2) is True
        os.remove(os.path.join(dev, "hidraw1"))
        os.remove(os.path.join(sysfs, "hidraw1"))
        assert w.wait(2) is True
        assert w.wait(0.2) is False
    finally:
        w.close()


def test_node_watcher_ignores_other_dev_entries(tmp_path):
    from opl_agent.hidpp import NodeWatcher
    dev, sysfs = str(tmp_path / "dev"), str(tmp_path / "sys")
    make_nodes(dev)
    make_nodes(sysfs)
    w = NodeWatcher(dev_dir=dev, sysfs_dir=sysfs, use_inotify=True, poll_seconds=0.05)
    try:
        make_nodes(dev, "tty0", "null")
        assert w.wait(0.3) is False
    finally:
        w.close()
