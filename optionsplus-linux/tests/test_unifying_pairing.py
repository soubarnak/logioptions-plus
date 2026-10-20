"""Unifying receiver pairing (ticket #19): register 0xB2 behind the same routes and
broadcasts as Bolt, driven through the simulated Unifying receiver."""

import time

import pytest

from tests.fakes.hidpp_device import SimulatedUnifying
from tests.test_battery import next_push
from tests.test_pairing import request

RCV = "rcv00000000"
MOUSE = "dev00000000"
NEW_MOUSE = "dev00000001"
PAIR = "/devices/%s/pair" % RCV
CHANGED = PAIR + "/changed"
PASSKEY = PAIR + "/passkey/displayed"


@pytest.fixture
def unifying():
    return SimulatedUnifying()


@pytest.fixture
def pairing(unifying, agent_factory):
    c = agent_factory(transport=unifying, rescan_seconds=0.1).client()
    for path in (CHANGED, PASSKEY, "/devices/state/changed"):
        c.subscribe(path)
    yield c
    c.close()


def infos(client):
    return client.call("GET", "/devices/list")["payload"]["deviceInfos"]


def until_state(client, state):
    seen = next_push(client, CHANGED)
    while seen["state"] != state:
        seen = next_push(client, CHANGED)
    return seen


def send(client, verb, path):
    """(reply, PairingState names broadcast before the reply): a broadcast may precede it."""
    reply, pushes = request(client, verb, path)
    return reply, [p["payload"]["state"] for p in pushes if p["path"] == CHANGED]


def status(client):
    return client.call("GET", PAIR)["payload"]


def pairing_writes(unifying):
    return [w for w in unifying.writes if w[1] == 0xFF and w[2:4] == bytes([0x80, 0xB2])]


def test_the_unifying_receiver_is_a_receiver_device_with_its_mouse(pairing):
    listed = infos(pairing)
    assert [d["id"] for d in listed] == [MOUSE, RCV]
    assert listed[1]["deviceType"] == "RECEIVER" and listed[1]["pairingMethod"] == "UNIFYING"


def test_opening_pairing_writes_the_pairing_register(unifying, pairing):
    reply, seen = send(pairing, "SET", PAIR)
    assert reply["result"]["code"] == "SUCCESS"
    assert unifying.pairing_open is True
    (write,) = pairing_writes(unifying)
    assert write[4] == 0x01 and write[6] > 0          # open lock, a non-zero timeout
    assert seen == ["START"] or until_state(pairing, "START")["receiverId"] == RCV
    assert status(pairing)["state"] == "START"


def test_a_rejected_open_is_faulted_and_leaves_pairing_unstarted(unifying, pairing):
    unifying.fail_pairing_open = True
    assert pairing.call("SET", PAIR)["result"]["code"] == "FAULTED"
    assert unifying.pairing_open is False
    assert status(pairing)["state"] == "UNKNOWN_PAIRING_STATE"


def test_an_unanswered_open_is_faulted(unifying, pairing):
    unifying.ignore_pairing_writes = True
    assert pairing.call("SET", PAIR)["result"]["code"] == "FAULTED"
    assert status(pairing)["state"] == "UNKNOWN_PAIRING_STATE"


def test_cancel_closes_the_lock_and_announces_it(unifying, pairing):
    pairing.call("SET", PAIR)
    reply, seen = send(pairing, "SET", PAIR + "/cancel")
    assert reply["result"]["code"] == "SUCCESS"
    assert unifying.pairing_open is False
    assert pairing_writes(unifying)[-1][4] == 0x02
    assert "CANCEL" in seen or until_state(pairing, "CANCEL")
    assert status(pairing)["state"] == "CANCEL"


def test_a_rejected_cancel_is_faulted_and_pairing_stays_started(unifying, pairing):
    pairing.call("SET", PAIR)
    unifying.fail_pairing_cancel = True
    assert pairing.call("SET", PAIR + "/cancel")["result"]["code"] == "FAULTED"
    assert status(pairing)["state"] == "START"


def test_a_device_pairing_completes_and_appears(unifying, pairing):
    pairing.call("SET", PAIR)
    unifying.complete_pairing("Second Mouse Z2", 0x4343)
    done = appeared = None
    while done is None or appeared is None:           # either may arrive first
        m = pairing.recv()
        if m.get("path") == CHANGED and m["payload"]["state"] == "STOP":
            done = m["payload"]
        if m.get("path") == "/devices/state/changed" and m["payload"]["id"] == NEW_MOUSE:
            appeared = m["payload"]
    assert done["error"] == "NO_PAIRING_ERROR" and done["pairingSlot"] == 2
    basic = done["pairedDevice"]
    assert basic["id"] == NEW_MOUSE and basic["deviceType"] == "MOUSE" and basic["pid"] == 0x4343
    assert basic["slotPrefix"] == "046d_4343" and "displayName" not in basic
    assert [d["id"] for d in infos(pairing)] == [MOUSE, NEW_MOUSE, RCV]


def test_too_many_devices_stops_pairing_as_failed(unifying, pairing):
    pairing.call("SET", PAIR)
    unifying.emit_lock_status(open_=False, error=2)
    failed = until_state(pairing, "STOP")
    assert failed["error"] == "FAILED"
    assert [d["id"] for d in infos(pairing)] == [MOUSE, RCV]


def test_a_device_timeout_stops_pairing_as_timed_out(unifying, pairing):
    pairing.call("SET", PAIR)
    unifying.emit_lock_status(open_=False, error=1)
    assert until_state(pairing, "STOP")["error"] == "TIMEOUT"


def test_the_lock_closing_without_a_new_device_times_pairing_out(unifying, pairing):
    pairing.call("SET", PAIR)
    unifying.emit_lock_status(open_=False)
    assert until_state(pairing, "STOP")["error"] == "TIMEOUT"
    assert [d["id"] for d in infos(pairing)] == [MOUSE, RCV]


def test_opening_the_lock_does_not_announce_start_twice(unifying, pairing):
    _, seen = send(pairing, "SET", PAIR)              # the receiver also confirms the open itself
    unifying.emit_lock_status(open_=False, error=1)
    states = seen
    while "STOP" not in states:
        states.append(next_push(pairing, CHANGED)["state"])
    assert states == ["START", "STOP"]


def test_cancelling_is_not_followed_by_a_timeout(unifying, pairing):
    pairing.call("SET", PAIR)
    _, seen = send(pairing, "SET", PAIR + "/cancel")
    assert "CANCEL" in seen or until_state(pairing, "CANCEL")
    unifying.emit_lock_status(open_=False)            # the receiver confirming the close
    time.sleep(0.4)
    assert status(pairing)["state"] == "CANCEL"
