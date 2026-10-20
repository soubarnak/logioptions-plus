"""Receiver as a device and Logi Bolt pairing (ticket #18), driven through the simulated Bolt."""

import time

import pytest

from tests.test_battery import next_push

RCV = "rcv00000000"
MOUSE = "dev00000000"
NEW_MOUSE = "dev00000001"


@pytest.fixture
def live(bolt, agent_factory):
    """A client subscribed to the device-state paths on an agent that rescans quickly."""
    c = agent_factory(rescan_seconds=0.1).client()
    c.subscribe("/devices/state/changed")
    c.subscribe("/devices/state/deactivated")
    yield c
    c.close()


def infos(client):
    return client.call("GET", "/devices/list")["payload"]["deviceInfos"]


# ---- the receiver is a device ----------------------------------------------

def test_receiver_is_listed_as_a_receiver_device_after_the_mouse(client):
    listed = infos(client)
    assert [d["id"] for d in listed] == [MOUSE, RCV]
    rx = listed[1]
    assert rx["deviceType"] == "RECEIVER"
    assert rx["state"] == "ACTIVE" and rx["connected"] is True
    assert rx["displayName"] and rx["pid"] == 0xC548


def test_receiver_info_route_answers(client):
    reply = client.call("GET", "/devices/%s/info" % RCV)
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["payload"]["deviceType"] == "RECEIVER"


def test_receiver_is_not_an_ever_connected_mouse(client):
    kinds = [d["deviceType"] for d in client.call("GET", "/devices/ever_connected/list")["payload"]["devices"]]
    assert kinds == ["MOUSE"]


def test_receiver_is_pushed_on_subscription(client):
    _reply, pushes = client.subscribe("/devices/state/changed")
    assert [p["payload"]["id"] for p in pushes] == [MOUSE, RCV]


def test_unplugging_the_receiver_deactivates_it_and_replugging_returns_it(bolt, live):
    bolt.unplug()
    gone = [next_push(live, "/devices/state/deactivated")["id"] for _ in range(2)]
    assert gone == [MOUSE, RCV]
    bolt.plug()
    back = []
    while RCV not in back:
        back.append(next_push(live, "/devices/state/changed")["id"])
    assert infos(live)[-1]["id"] == RCV


# ---- pairing ---------------------------------------------------------------
# Bolt pairs on HID++ 1.0 registers: discovery (0xC0), then pairing (0x2C1) of the advertised device.

PAIR = "/devices/%s/pair" % RCV
CHANGED = PAIR + "/changed"
PASSKEY = PAIR + "/passkey/displayed"
DISCOVERY = "/devices/%s/discovery" % RCV
ADVERTISED = DISCOVERY + "/advertised"
ADDRESS = b"\xaa\xbb\xcc\xdd\xee\x01"


@pytest.fixture
def pairing(bolt, agent_factory):
    """A client subscribed to the pairing broadcasts and device state."""
    c = agent_factory(rescan_seconds=0.1).client()
    for path in (CHANGED, PASSKEY, ADVERTISED, DISCOVERY + "/changed", "/devices/state/changed"):
        c.subscribe(path)
    yield c
    c.close()


def request(client, verb, path, payload=None):
    """(reply, broadcasts that arrived before it): a broadcast may precede the reply."""
    mid = client.send(verb, path, payload)
    pushes = []
    while True:
        m = client.recv()
        if m.get("msgId") == mid and "result" in m:
            return m, [p for p in pushes if p.get("verb") == "BROADCAST"]
        pushes.append(m)


def until_state(client, state):
    seen = next_push(client, CHANGED)
    while seen["state"] != state:
        seen = next_push(client, CHANGED)
    return seen


def status(client):
    return client.call("GET", PAIR)["payload"]


def wait_for(cond, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def start_and_pair(bolt, client):
    """Open pairing, let a device advertise, wait for the receiver to be told to pair it."""
    assert client.call("SET", PAIR)["result"]["code"] == "SUCCESS"
    bolt.advertise("New Mouse", ADDRESS)
    assert wait_for(lambda: bolt.pairing_open)


def test_opening_pairing_starts_device_discovery_and_announces_it(bolt, pairing):
    reply, pushes = request(pairing, "SET", PAIR)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.discovering is True
    (state,) = [p["payload"] for p in pushes if p["path"] == CHANGED] or [next_push(pairing, CHANGED)]
    assert state["state"] == "START" and state["receiverId"] == RCV
    assert status(pairing)["state"] == "START"


def test_a_failed_open_is_reported_and_leaves_pairing_unstarted(bolt, pairing):
    bolt.fail_discovery = True
    assert pairing.call("SET", PAIR)["result"]["code"] == "FAULTED"
    assert bolt.discovering is False
    assert status(pairing)["state"] == "UNKNOWN_PAIRING_STATE"


def test_an_advertising_device_is_announced_and_paired(bolt, pairing):
    pairing.call("SET", PAIR)
    bolt.advertise("New Mouse", ADDRESS, kind=2, authentication=1)
    adv = next_push(pairing, ADVERTISED)
    assert adv["name"] == "New Mouse" and adv["deviceAddress"] == ADDRESS.hex() and adv["receiverId"] == RCV
    assert adv["deviceType"] == "MOUSE"
    assert wait_for(lambda: bolt.pairing_open)
    assert bolt.paired_address == ADDRESS
    assert bolt.writes[-1][2:4] == bytes([0x82, 0xC1]) and bolt.writes[-1][0] == 0x11   # a long write


def test_a_device_address_in_the_request_is_paired_without_discovery(bolt, pairing):
    reply = pairing.call("SET", PAIR, {"deviceAddress": ADDRESS.hex()})
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.pairing_open is True and bolt.paired_address == ADDRESS and bolt.discovering is False
    assert status(pairing)["state"] == "START"


def test_a_bad_device_address_is_refused(bolt, pairing):
    assert pairing.call("SET", PAIR, {"deviceAddress": "xyz"})["result"]["code"] == "FAULTED"
    assert bolt.pairing_open is False


def test_cancel_during_discovery_stops_it_and_announces_cancel(bolt, pairing):
    pairing.call("SET", PAIR)
    reply, pushes = request(pairing, "SET", PAIR + "/cancel")
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.discovering is False
    states = [p["payload"]["state"] for p in pushes if p["path"] == CHANGED]
    assert (states or [next_push(pairing, CHANGED)["state"]])[-1] == "CANCEL"
    assert status(pairing)["state"] == "CANCEL"


def test_cancel_during_pairing_closes_the_pairing(bolt, pairing):
    start_and_pair(bolt, pairing)
    assert pairing.call("SET", PAIR + "/cancel")["result"]["code"] == "SUCCESS"
    assert bolt.pairing_open is False
    assert status(pairing)["state"] == "CANCEL"


def test_a_failed_cancel_is_reported_and_pairing_stays_started(bolt, pairing):
    start_and_pair(bolt, pairing)
    bolt.fail_pairing_cancel = True
    assert pairing.call("SET", PAIR + "/cancel")["result"]["code"] == "FAULTED"
    assert status(pairing)["state"] == "START"


def test_cancel_without_pairing_in_progress_changes_nothing(bolt, pairing):
    reply = pairing.call("SET", PAIR + "/cancel")
    assert reply["result"]["code"] == "SUCCESS"
    assert status(pairing)["state"] == "UNKNOWN_PAIRING_STATE"


def test_the_passkey_is_broadcast_for_the_ui_to_show(bolt, pairing):
    bolt.passkey = "274913"
    start_and_pair(bolt, pairing)
    shown = next_push(pairing, PASSKEY)
    assert shown["passkey"] == "274913" and shown["receiverId"] == RCV
    assert shown["deviceAddress"] == ADDRESS.hex()
    assert status(pairing)["state"] == "START"           # showing a passkey is not a pairing state


def test_a_device_pairing_completes_and_appears(bolt, pairing):
    start_and_pair(bolt, pairing)
    bolt.complete_pairing("Second Mouse Z2", 0x4343)
    done = appeared = None
    while done is None or appeared is None:        # either may arrive first
        m = pairing.recv()
        if m.get("path") == CHANGED and m["payload"]["state"] == "STOP":
            done = m["payload"]
        if m.get("path") == "/devices/state/changed" and m["payload"]["id"] == NEW_MOUSE:
            appeared = m["payload"]
    assert done["error"] == "NO_PAIRING_ERROR" and done["pairingSlot"] == 2
    assert done["deviceAddress"] == ADDRESS.hex()
    basic = done["pairedDevice"]
    assert basic["id"] == NEW_MOUSE and basic["deviceType"] == "MOUSE" and basic["pid"] == 0x4343
    assert basic["slotPrefix"] == "046d_4343" and "displayName" not in basic
    assert appeared["state"] == "ACTIVE"
    assert [d["id"] for d in infos(pairing)] == [MOUSE, NEW_MOUSE, RCV]


@pytest.mark.parametrize("code,error", [(1, "TIMEOUT"), (2, "FAILED")])
def test_a_pairing_failure_is_reported_with_its_error(bolt, pairing, code, error):
    start_and_pair(bolt, pairing)
    bolt.emit_pairing_status(1, error=code)
    failed = until_state(pairing, "STOP")
    assert failed["error"] == error
    assert [d["id"] for d in infos(pairing)] == [MOUSE, RCV]


def test_discovery_ending_with_a_timeout_ends_pairing_as_timed_out(bolt, pairing):
    pairing.call("SET", PAIR)
    bolt.emit_discovery_status(False, error=1)
    assert until_state(pairing, "STOP")["error"] == "TIMEOUT"


def test_discovery_closing_without_a_device_ends_as_timed_out(bolt, pairing):
    pairing.call("SET", PAIR)
    bolt.emit_discovery_status(False)             # the receiver gave up looking
    assert until_state(pairing, "STOP")["error"] == "TIMEOUT"


def test_pairing_routes_do_not_exist_on_a_mouse(bolt, pairing):
    assert pairing.call("SET", "/devices/%s/pair" % MOUSE)["result"]["code"] == "NO_SUCH_PATH"
    assert bolt.pairing_open is False and bolt.discovering is False


def test_the_discovery_route_looks_for_devices_without_pairing_them(bolt, pairing):
    reply = pairing.call("SET", DISCOVERY, {"timeoutSeconds": 20})
    assert reply["result"]["code"] == "SUCCESS" and bolt.discovering is True
    assert bolt.writes[-1][4] == 20                # the timeout goes first, then the action
    bolt.advertise("New Mouse", ADDRESS)
    assert next_push(pairing, ADVERTISED)["deviceAddress"] == ADDRESS.hex()
    assert bolt.pairing_open is False


# ---- registry level: watchdog, unplug, several receivers, threads -----------------

import threading  # noqa: E402

from opl_agent import model  # noqa: E402
from tests.fakes.hidpp_device import SimulatedBolt, SimulatedMouse  # noqa: E402


class Both:
    """One transport over several simulated receivers (their nodes list together)."""

    def __init__(self, *transports):
        self.parts = transports

    def list_nodes(self):
        return [n for t in self.parts for n in t.list_nodes()]

    def open(self, path):
        return next(t for t in self.parts if t.path == path).open(path)


@pytest.fixture
def reg_for():
    made = []

    def make(transport):
        reg = model.Registry(transport=transport)
        reg.scan()
        made.append(reg)
        return reg
    yield make
    for reg in made:
        reg.close()


def collect(reg):
    seen = []
    reg.on_pairing.append(lambda rid, state, passkey, events: seen.append((rid, state["state"], state["error"])))
    return seen


def two_receivers():
    one = SimulatedBolt()
    two = SimulatedBolt(path="/dev/hidraw-sim1", unique_id=b"\x0b\x02\x0b\x02",
                        mouse=SimulatedMouse("Other Mouse", 0x4343))
    return one, two


def test_a_pairing_that_hears_nothing_more_times_out(bolt, reg_for, monkeypatch):
    monkeypatch.setattr(model, "PAIRING_MARGIN", 0.1)
    reg = reg_for(bolt)
    seen = collect(reg)
    reg.open_pairing(RCV, timeout=1)
    assert wait_for(lambda: reg.receivers[RCV].pairing["state"] == "STOP", timeout=5)
    assert seen == [(RCV, "START", "NO_PAIRING_ERROR"), (RCV, "STOP", "TIMEOUT")]


def test_a_finished_pairing_is_not_timed_out_later(bolt, reg_for, monkeypatch):
    monkeypatch.setattr(model, "PAIRING_MARGIN", 0.1)
    reg = reg_for(bolt)
    seen = collect(reg)
    reg.open_pairing(RCV, timeout=1, address=ADDRESS)
    bolt.complete_pairing()
    assert wait_for(lambda: reg.receivers[RCV].pairing["state"] == "STOP")
    assert wait_for(lambda: reg.receivers[RCV].watchdog is None, timeout=2)
    assert [s for _r, s, _e in seen] == ["START", "STOP"] and seen[-1][2] == "NO_PAIRING_ERROR"


def test_unplugging_the_receiver_mid_pairing_stops_it_as_disconnected(bolt, reg_for):
    reg = reg_for(bolt)
    seen = collect(reg)
    reg.open_pairing(RCV, timeout=30)
    bolt.unplug()
    reg.rescan()
    assert seen[-1] == (RCV, "STOP", "DISCONNECTED")
    assert reg.absent_receivers[RCV].pairing["state"] == "STOP"
    assert reg.absent_receivers[RCV].watchdog is None


def test_two_receivers_of_the_same_model_are_two_receivers(reg_for):
    one, two = two_receivers()
    reg = reg_for(Both(one, two))
    assert sorted(reg.receivers) == ["rcv00000000", "rcv00000001"]
    assert {r.node.path for r in reg.receivers.values()} == {one.path, two.path}
    reg.open_pairing("rcv00000001", timeout=30, address=ADDRESS)
    assert two.pairing_open is True and one.pairing_open is False
    assert reg.receivers["rcv00000000"].pairing["state"] == "UNKNOWN_PAIRING_STATE"


def test_receivers_replugged_on_other_nodes_keep_their_ids(reg_for):
    one, two = two_receivers()
    reg = reg_for(Both(one, two))
    one.unplug()
    two.unplug()
    reg.rescan()
    assert reg.receivers == {}
    one.plug("/dev/hidraw-sim7")
    two.plug("/dev/hidraw-sim8")
    reg.rescan()
    assert reg.receivers["rcv00000000"].node.path == "/dev/hidraw-sim7"
    assert reg.receivers["rcv00000001"].node.path == "/dev/hidraw-sim8"


def test_the_rescan_after_a_pairing_does_not_hold_up_input_events(bolt, reg_for, monkeypatch):
    reg = reg_for(bolt)
    gate, entered = threading.Event(), threading.Event()
    real_rescan = reg.rescan

    def slow_rescan(force=False):
        entered.set()
        gate.wait(10)
        return real_rescan(force)

    buttons = []
    reg.on_button_event.append(lambda dev_id, fn, params: buttons.append(dev_id))
    reg.set_divert(MOUSE, 82, True)
    reg.open_pairing(RCV, timeout=30, address=ADDRESS)
    monkeypatch.setattr(reg, "rescan", slow_rescan)
    try:
        bolt.complete_pairing()
        assert entered.wait(2)                      # the rescan is running, and stuck
        bolt.press_button(82)
        assert wait_for(lambda: buttons, timeout=2)
    finally:
        gate.set()


def test_the_locking_status_route_reports_the_lock(bolt, pairing):
    reply = pairing.call("GET", "/devices/%s/locking/status" % RCV)
    assert reply["result"]["code"] == "SUCCESS" and reply["payload"]["receiverId"] == RCV
