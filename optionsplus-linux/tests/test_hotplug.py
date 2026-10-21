"""Live hot-plug of receivers and devices (ticket #13), driven through the simulated transport."""

import os
import time

import pytest

from tests.fakes.hidpp_device import SimulatedBolt, SimulatedMouse

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


@pytest.fixture
def ui(bolt, agent_factory):
    """A client subscribed to exactly the device paths the Options+ UI subscribes (not
    /devices/state/deactivated, which the agent only delivers to a subscriber)."""
    c = agent_factory(rescan_seconds=0.1).client()
    for path in ("/devices/devio/device_removal", "/devices/options/device_removal",
                 "/devices/options/device_arrival", "/devices/state/changed"):
        c.subscribe(path)
    yield c
    c.close()


def next_active(client):
    """Next /devices/state/changed push of a device that is not absent (a removal also travels there)."""
    while True:
        p = next_push(client, "/devices/state/changed")
        if p["state"] != "ABSENT":
            return p


def device_ids(client):
    infos = client.call("GET", "/devices/list")["payload"].get("deviceInfos", [])
    return [d["id"] for d in infos if d["deviceType"] != "RECEIVER"]     # the receiver is covered in test_pairing


# ---- receiver unplug / replug ----------------------------------------------

def test_unplugging_the_receiver_deactivates_the_device_and_drops_its_card(bolt, live):
    bolt.unplug()
    gone = next_push(live, "/devices/state/deactivated")
    assert gone["id"] == DEV and gone["state"] == "ABSENT" and gone["connected"] is False
    assert device_ids(live) == []


def test_the_ui_drops_the_card_on_unplug_through_the_paths_it_subscribes(bolt, ui):
    """The UI removes a card when /devices/state/changed carries the device with state ABSENT;
    /devices/devio/device_removal carries {deviceId} (its firmware-update unplug handling)."""
    bolt.unplug()
    seen = {}
    while len(seen) < 2:
        m = ui.recv()
        if m.get("verb") != "BROADCAST":
            continue
        if m["path"] == "/devices/state/changed" and m["payload"].get("id") == DEV:
            seen["changed"] = m["payload"]
        elif m["path"] == "/devices/devio/device_removal" and m["payload"].get("deviceId") == DEV:
            seen["removal"] = m["payload"]
    assert seen["changed"]["state"] == "ABSENT" and seen["changed"]["connected"] is False
    assert seen["removal"]["deviceId"] == DEV
    assert device_ids(ui) == []


def test_the_ui_gets_the_card_back_active_on_replug(bolt, ui):
    bolt.unplug()
    assert next_push(ui, "/devices/state/changed")["state"] == "ABSENT"
    bolt.plug()
    back = next_active(ui)
    while back["id"] != DEV:
        back = next_active(ui)
    assert back["state"] == "ACTIVE" and back["connected"] is True


def test_replugging_the_receiver_brings_the_same_device_back_active(bolt, live):
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.plug()
    back = next_active(live)
    assert back["id"] == DEV and back["state"] == "ACTIVE" and back["connected"] is True
    assert device_ids(live) == [DEV]


def test_device_keeps_its_id_when_the_receiver_returns_as_another_hidraw_node(bolt, live):
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.plug("/dev/hidraw-sim7")
    assert next_active(live)["id"] == DEV
    assert live.call("GET", "/battery/%s/state" % DEV)["payload"]["percentage"] == 73.0


def test_a_different_mouse_on_the_replugged_receiver_gets_a_new_id(bolt, live):
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.mouse.wpid = 0x4343
    bolt.plug()
    assert next_active(live)["id"] == "dev00000001"
    assert device_ids(live) == ["dev00000001"]


def test_battery_is_refreshed_on_return(bolt, live):
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.mouse.battery_percent = 55
    bolt.plug()
    back = next_active(live)
    assert back["batteryDischargeLevel"] == 55
    assert live.call("GET", "/battery/%s/state" % DEV)["payload"]["percentage"] == 55.0


def test_settings_are_reapplied_when_the_receiver_returns(bolt, live):
    slider(live, 0.5)
    bolt.unplug()
    next_push(live, "/devices/state/deactivated")
    bolt.mouse.dpi = 1000
    bolt.plug()
    next_active(live)
    assert bolt.mouse.dpi == 4500


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
    back = next_active(live)
    assert back["id"] == DEV and back["batteryDischargeLevel"] == 64
    assert wait_for(lambda: bolt.mouse.dpi == 6600)


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


# ---- a node that errors while unplugged or not yet ready --------------------

def test_a_receiver_that_fails_with_eio_mid_unplug_counts_as_gone_not_as_a_crash(bolt, monkeypatch):
    import errno
    from opl_agent import hidpp
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg.scan()
    real = hidpp.get_paired_devices

    def eio(fd, **kw):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(hidpp, "get_paired_devices", eio)
    events = reg.rescan()                       # must not raise
    assert [d.id for d in events["removed"]] == [DEV] and len(events["receiver_removed"]) == 1
    assert reg.receivers == {} and reg.devices == {}
    assert bolt.path not in reg._fds            # the handle on the dying node is released
    monkeypatch.setattr(hidpp, "get_paired_devices", real)
    events = reg.rescan()                       # the very next scan brings both back
    assert events["returned"] == [DEV] and events["receiver_added"] == ["rcv00000000"]


@pytest.mark.parametrize("code", [13, 19])      # EACCES (udev ACL not applied yet), ENODEV
def test_a_node_that_cannot_be_opened_yet_is_retried_on_the_next_scan(bolt, monkeypatch, code):
    from opl_agent.model import Registry
    reg = Registry(bolt)
    real_open = bolt.open

    def refuse(path):
        raise OSError(code, "refused", path)

    monkeypatch.setattr(bolt, "open", refuse)
    events = reg.rescan()                       # must not raise, finds nothing
    assert events["added"] == [] and events["receiver_added"] == []
    monkeypatch.setattr(bolt, "open", real_open)
    events = reg.rescan()
    assert events["added"] == [DEV] and events["receiver_added"] == ["rcv00000000"]


def test_a_mouse_comes_back_on_the_first_scan_where_it_answers_a_ping(bolt):
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg.scan()
    bolt.mouse.power(False)
    assert [d.id for d in reg.rescan()["removed"]] == [DEV]
    assert reg.rescan()["returned"] == []        # still silent: nothing waits on a timer
    bolt.mouse.power(True)
    assert reg.rescan()["returned"] == [DEV]


def test_a_transient_error_on_a_node_that_is_still_there_does_not_drop_the_receiver(bolt, monkeypatch):
    import errno
    from opl_agent import hidpp
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg.scan()
    real = hidpp.get_paired_devices
    for code in (errno.ETIMEDOUT, errno.EPIPE):
        monkeypatch.setattr(hidpp, "get_paired_devices",
                            lambda fd, code=code, **kw: (_ for _ in ()).throw(OSError(code, "hiccup")))
        events = reg.rescan()
        assert events["removed"] == [] and events["receiver_removed"] == []
        assert list(reg.devices) == [DEV] and list(reg.receivers) == ["rcv00000000"]
    monkeypatch.setattr(hidpp, "get_paired_devices", real)
    events = reg.rescan()
    assert events["removed"] == [] and events["returned"] == []


def test_a_reader_that_died_on_eio_is_reopened_by_the_next_scan(bolt):
    import errno
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg.scan()
    old = reg._fds[bolt.path]

    def eio(timeout):
        raise OSError(errno.EIO, "Input/output error")

    old.handle.read = eio
    assert wait_for(lambda: old.dead is not None)
    events = reg.rescan()                       # the node is still there: nothing is removed
    assert events["removed"] == [] and events["receiver_removed"] == []
    assert reg._fds[bolt.path] is not old and reg._fds[bolt.path].dead is None
    assert list(reg.devices) == [DEV]


def test_a_transient_error_while_a_returning_device_is_built_loses_neither_receiver_nor_device_id(bolt, monkeypatch):
    import errno
    from opl_agent import hidpp
    from opl_agent.model import RESUME_FEATURES, Registry
    reg = Registry(bolt)
    reg.scan()
    bolt.unplug()
    events = reg.rescan()
    assert [d.id for d in events["removed"]] == [DEV] and DEV in reg.absent
    bolt.plug()
    real = hidpp.feature_index

    def eio(fd, idx, fid):
        if fid in RESUME_FEATURES:              # the probe's own lookups work; _build's loop hits EIO
            raise OSError(errno.ETIMEDOUT, "timed out")
        return real(fd, idx, fid)

    monkeypatch.setattr(hidpp, "feature_index", eio)
    events = reg.rescan()                       # must not raise, must still report the receiver
    assert events["receiver_added"] == ["rcv00000000"]
    assert events["returned"] == [] and DEV in reg.absent and reg.devices == {}
    monkeypatch.setattr(hidpp, "feature_index", real)
    events = reg.rescan()
    assert events["returned"] == [DEV] and events["added"] == []   # the same id, not a new device
    assert list(reg.devices) == [DEV] and reg.absent == {}


def test_a_failed_announcement_does_not_end_the_watch_task(bolt, monkeypatch):
    import asyncio
    from opl_agent import main as agent_main
    from opl_agent.model import Registry

    class Server:
        def __init__(self):
            self.sent = []

        def broadcast(self, path, payload=None):
            self.sent.append(path)

    reg = Registry(bolt)
    reg.scan()
    calls = []

    def flaky(server, reg_, events):
        calls.append(1)
        if len(calls) == 1:
            raise KeyError("gone")

    monkeypatch.setattr(agent_main, "_announce", flaky)

    async def run():
        task = asyncio.ensure_future(agent_main._watch(Server(), reg, 0.01))
        await asyncio.sleep(0.5)
        alive = not task.done()
        task.cancel()
        return alive

    assert asyncio.run(run()) and len(calls) > 1


def test_announce_skips_ids_a_pairing_rescan_dropped(bolt):
    from opl_agent import main as agent_main
    from opl_agent.model import Registry

    class Server:
        def __init__(self):
            self.sent = []

        def broadcast(self, path, payload=None):
            self.sent.append((path, payload))

    reg = Registry(bolt)
    events = reg.scan()
    reg.devices.clear()
    reg.receivers.clear()
    server = Server()
    agent_main._announce(server, reg, events)       # must not raise
    assert server.sent == []


# ---- review round 2: gone node mid-build, hiccups, pairing on a released handle ----

class Both:
    """One transport over several simulated receivers (their nodes list together)."""

    def __init__(self, *parts):
        self.parts = parts

    def list_nodes(self):
        return [n for t in self.parts for n in t.list_nodes()]

    def open(self, path):
        return next(t for t in self.parts if t.path == path).open(path)

    def node_exists(self, path):
        return any(t.node_exists(path) for t in self.parts)


def _fail_in_build(monkeypatch, errno_):
    """feature_index fails with `errno_` for the features _build's own loops look up."""
    from opl_agent import hidpp
    from opl_agent.model import RESUME_FEATURES
    real = hidpp.feature_index

    def fail(fd, idx, fid):
        if fid in RESUME_FEATURES:
            raise OSError(errno_, "injected")
        return real(fd, idx, fid)

    monkeypatch.setattr(hidpp, "feature_index", fail)
    return real


def test_eio_while_building_the_first_of_two_devices_keeps_both_ids_for_the_next_scan(bolt, monkeypatch):
    import errno
    from opl_agent import hidpp
    from opl_agent.model import Registry
    second = "dev00000001"
    bolt.add_device()
    reg = Registry(bolt)
    reg.scan()
    assert sorted(reg.devices) == [DEV, second]
    bolt.unplug()
    reg.rescan()
    bolt.plug()
    real = _fail_in_build(monkeypatch, errno.EIO)
    events = reg.rescan()                           # no KeyError for the second device
    assert events["returned"] == [] and events["added"] == []
    assert events["receiver_added"] == [] and reg.receivers == {}      # no receiver left without a handle
    assert sorted(reg.absent) == [DEV, second] and reg.devices == {}
    assert bolt.path not in reg._fds
    monkeypatch.setattr(hidpp, "feature_index", real)
    events = reg.rescan()
    assert sorted(events["returned"]) == [DEV, second] and events["added"] == []
    assert events["receiver_added"] == ["rcv00000000"]


def test_a_node_that_keeps_timing_out_does_not_block_another_receivers_unplug(monkeypatch):
    import errno
    from opl_agent import hidpp
    from opl_agent.model import Registry
    one = SimulatedBolt()
    two = SimulatedBolt(path="/dev/hidraw-sim1", unique_id=b"\x0b\x02\x0b\x02",
                        mouse=SimulatedMouse("Other Mouse", 0x4343))
    reg = Registry(Both(one, two))
    reg.scan()
    assert sorted(reg.receivers) == ["rcv00000000", "rcv00000001"]
    one_fd = reg._fds[one.path]
    real = hidpp.get_paired_devices

    def probe(fd, **kw):
        if fd is one_fd:
            raise OSError(errno.ETIMEDOUT, "hiccup")
        return real(fd, **kw)

    monkeypatch.setattr(hidpp, "get_paired_devices", probe)
    two.unplug()
    events = reg.rescan()
    assert [r.id for r in events["receiver_removed"]] == ["rcv00000001"]
    assert [d.id for d in events["removed"]] == ["dev00000001"]
    assert list(reg.receivers) == ["rcv00000000"] and list(reg.devices) == [DEV]    # the hiccuping one is kept


def test_a_hiccup_while_a_device_is_rebuilt_in_place_keeps_it_without_flicker(bolt, monkeypatch):
    import errno
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg.scan()
    old = reg.devices[DEV]
    _fail_in_build(monkeypatch, errno.ETIMEDOUT)
    events = reg.rescan(force=True)                 # rebuilt on the node it is already on
    assert events["removed"] == [] and events["returned"] == []
    assert reg.devices[DEV] is old and reg.absent == {}


def test_a_hiccup_while_a_device_is_rebuilt_on_another_node_does_not_keep_the_old_node(bolt, monkeypatch):
    import errno
    from opl_agent import hidpp
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg.scan()
    bolt.unplug()
    bolt.plug("/dev/hidraw-sim7")                   # back on another node before any scan
    real = _fail_in_build(monkeypatch, errno.ETIMEDOUT)
    events = reg.rescan()
    assert [d.id for d in events["removed"]] == [DEV]
    assert reg.devices == {} and DEV in reg.absent  # not kept on the node that went away
    monkeypatch.setattr(hidpp, "feature_index", real)
    events = reg.rescan()
    assert events["returned"] == [DEV] and events["added"] == []
    assert reg.devices[DEV].node.path == "/dev/hidraw-sim7"


def test_pairing_on_a_receiver_whose_handle_was_released_fails_cleanly(bolt):
    import errno
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg.scan()
    r = reg.receivers["rcv00000000"]
    reg._forget_fd(bolt.path)
    with pytest.raises(OSError) as exc:
        reg._receiver_fd(r)
    assert exc.value.errno == errno.ENODEV


# ---- a node held for transient errors is not held forever ----

def _hiccup_probe(monkeypatch, errno_):
    """get_paired_devices fails with `errno_` while the returned switch is on."""
    from opl_agent import hidpp
    real = hidpp.get_paired_devices
    on = {"fail": True}

    def probe(fd, **kw):
        if on["fail"]:
            raise OSError(errno_, "hiccup")
        return real(fd, **kw)

    monkeypatch.setattr(hidpp, "get_paired_devices", probe)
    return on


class Clock:
    """A clock the test moves by hand, for the held-node budget."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _held_registry(bolt):
    from opl_agent.model import Registry
    reg = Registry(bolt)
    reg._now = clock = Clock()
    reg.scan()
    return reg, clock


def test_a_node_that_keeps_timing_out_is_dropped_once_its_time_runs_out(bolt, monkeypatch):
    import errno
    from opl_agent.model import HELD_SECONDS
    reg, clock = _held_registry(bolt)
    _hiccup_probe(monkeypatch, errno.ETIMEDOUT)
    for _ in range(3):
        events = reg.rescan()
        assert events["removed"] == [] and events["receiver_removed"] == []
        clock.t += HELD_SECONDS / 3 - 0.1
    events = reg.rescan()
    assert events["removed"] == [] and events["receiver_removed"] == []     # just inside the budget
    clock.t += 0.5
    events = reg.rescan()
    assert [r.id for r in events["receiver_removed"]] == ["rcv00000000"]
    assert [d.id for d in events["removed"]] == [DEV]
    assert reg.devices == {} and reg.receivers == {} and DEV in reg.absent


def test_rapid_rescans_while_pairing_settles_do_not_use_up_the_budget(bolt, monkeypatch):
    import errno
    from opl_agent.model import SETTLE_SECONDS, SETTLE_TRIES
    reg, clock = _held_registry(bolt)
    _hiccup_probe(monkeypatch, errno.ETIMEDOUT)
    for _ in range(5):                              # five pairing settles, back to back
        for _ in range(SETTLE_TRIES):
            events = reg.rescan()
            assert events["removed"] == [] and events["receiver_removed"] == []
            clock.t += SETTLE_SECONDS
    assert list(reg.devices) == [DEV] and list(reg.receivers) == ["rcv00000000"]


def test_an_answer_between_hiccups_starts_the_held_time_again(bolt, monkeypatch):
    import errno
    from opl_agent.model import HELD_SECONDS
    reg, clock = _held_registry(bolt)
    hiccup = _hiccup_probe(monkeypatch, errno.EPIPE)
    reg.rescan()
    clock.t += HELD_SECONDS - 1
    reg.rescan()
    hiccup["fail"] = False
    reg.rescan()
    hiccup["fail"] = True
    clock.t += 2
    events = reg.rescan()                           # 11 s since the first hold, 0 s since the answer
    assert events["removed"] == [] and events["receiver_removed"] == []
    assert list(reg.devices) == [DEV] and list(reg.receivers) == ["rcv00000000"]


def test_a_dropped_node_that_answers_again_returns_with_the_same_ids(bolt, monkeypatch):
    import errno
    from opl_agent.model import HELD_SECONDS
    reg, clock = _held_registry(bolt)
    hiccup = _hiccup_probe(monkeypatch, errno.ETIMEDOUT)
    reg.rescan()
    clock.t += HELD_SECONDS + 1
    reg.rescan()
    assert reg.devices == {} and reg.receivers == {}
    assert reg._held_since == {}                    # a later hold starts a budget of its own
    reg.rescan()
    hiccup["fail"] = False
    events = reg.rescan()
    assert events["receiver_added"] == ["rcv00000000"]
    assert events["returned"] == [DEV] and events["added"] == []
