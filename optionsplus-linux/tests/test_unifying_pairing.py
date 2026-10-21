"""Unifying receiver pairing (ticket #19): register 0xB2 behind the same routes and
broadcasts as Bolt, driven through the simulated Unifying receiver."""

import threading
import time

import pytest

from opl_agent import model
from tests.harness import wait_for
from tests.fakes.hidpp_device import (F_WAKE, LOCK_DEVICE_TIMEOUT, LOCK_NOT_SUPPORTED, LOCK_SEQUENCE_TIMEOUT,
                                      LOCK_TOO_MANY_DEVICES, SimulatedUnifying)
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


@pytest.mark.parametrize("code,error", [
    (LOCK_DEVICE_TIMEOUT, "TIMEOUT"), (LOCK_NOT_SUPPORTED, "FAILED"),
    (LOCK_TOO_MANY_DEVICES, "FAILED"), (LOCK_SEQUENCE_TIMEOUT, "TIMEOUT")])
def test_the_receivers_error_code_decides_how_pairing_stops(unifying, pairing, code, error):
    pairing.call("SET", PAIR)
    unifying.emit_lock_status(open_=False, error=code)
    assert until_state(pairing, "STOP")["error"] == error
    assert [d["id"] for d in infos(pairing)] == [MOUSE, RCV]


def test_the_lock_closing_without_a_new_device_times_pairing_out(unifying, pairing):
    pairing.call("SET", PAIR)
    unifying.emit_lock_status(open_=False)
    assert until_state(pairing, "STOP")["error"] == "TIMEOUT"
    assert [d["id"] for d in infos(pairing)] == [MOUSE, RCV]


def test_opening_the_lock_does_not_announce_start_twice(unifying, pairing):
    _, seen = send(pairing, "SET", PAIR)              # the receiver also confirms the open itself
    unifying.emit_lock_status(open_=False, error=LOCK_DEVICE_TIMEOUT)
    states = seen
    while "STOP" not in states:
        states.append(next_push(pairing, CHANGED)["state"])
    assert states == ["START", "STOP"]


def test_cancelling_is_not_followed_by_a_timeout(unifying, pairing):
    pairing.call("SET", PAIR)
    _, seen = send(pairing, "SET", PAIR + "/cancel")
    assert "CANCEL" in seen or until_state(pairing, "CANCEL")
    pairing.subscribe("/battery/dev00000000/state/changed")
    unifying.emit_lock_status(open_=False)            # the receiver confirming the close
    unifying.emit_event(F_WAKE)                       # handled after it, on the same thread, in order
    while True:
        m = pairing.recv()
        if m.get("path") == "/battery/dev00000000/state/changed" and m.get("verb") == "BROADCAST":
            break
    assert status(pairing)["state"] == "CANCEL"


def wait_for_wake(unifying, client):
    """Return once every notification queued so far has been handled: a wake event queued
    after them is handled after them, on the same thread, and ends in a battery broadcast."""
    client.subscribe("/battery/dev00000000/state/changed")
    unifying.emit_event(F_WAKE)
    next_push(client, "/battery/dev00000000/state/changed")


def test_a_late_open_notification_after_cancel_does_not_restart_pairing(unifying, pairing):
    unifying.hold_lock_status = True                  # the receiver is slow to report the open
    pairing.call("SET", PAIR)
    _, seen = send(pairing, "SET", PAIR + "/cancel")
    assert "CANCEL" in seen or until_state(pairing, "CANCEL")
    unifying.release_lock_status()                    # "open", then "closed", both after the cancel
    wait_for_wake(unifying, pairing)
    assert status(pairing)["state"] == "CANCEL"


def test_another_tool_opening_the_lock_after_a_cancel_is_still_reported(unifying, pairing):
    pairing.call("SET", PAIR)
    pairing.call("SET", PAIR + "/cancel")
    wait_for_wake(unifying, pairing)                  # our own "closed" echo has been handled
    unifying.emit_lock_status(open_=True)
    assert until_state(pairing, "START")["state"] == "START"


def test_the_close_notification_may_beat_the_cancel_reply_without_changing_the_outcome(unifying, pairing):
    unifying.notify_before_reply = True               # the receiver says "closed", then answers the write
    for _ in range(15):
        assert pairing.call("SET", PAIR)["result"]["code"] == "SUCCESS"
        assert pairing.call("SET", PAIR + "/cancel")["result"]["code"] == "SUCCESS"
        assert status(pairing)["state"] == "CANCEL"


def test_cancel_closes_the_lock_without_a_timeout_of_its_own(unifying, pairing):
    pairing.call("SET", PAIR)
    pairing.call("SET", PAIR + "/cancel")
    assert pairing_writes(unifying)[-1][4:7] == bytes([0x02, 0x00, 0x00])


def test_the_locking_routes_follow_the_lock(unifying, pairing):
    locking = "/devices/%s/locking" % RCV
    pairing.subscribe(locking + "/changed")
    assert pairing.call("GET", locking + "/status")["payload"]["state"] == "CLOSED"
    _, pushes = request(pairing, "SET", PAIR)         # the open may be announced before the reply
    seen = [p["payload"] for p in pushes if p["path"] == locking + "/changed"]
    seen = seen[-1] if seen else next_push(pairing, locking + "/changed")
    while seen["state"] != "OPENED":
        seen = next_push(pairing, locking + "/changed")
    assert pairing.call("GET", locking + "/status")["payload"]["state"] == "OPENED"
    assert seen["receiverId"] == RCV


# ---- registry level: races that tests used to hide behind sleeps -------------------------

@pytest.fixture
def reg(unifying):
    r = model.Registry(transport=unifying)
    r.scan()
    yield r
    r.close()


def test_a_device_the_periodic_rescan_found_first_still_counts_as_the_one_paired(unifying, reg):
    seen = []
    reg.on_pairing.append(lambda rid, state, passkey, events: seen.append(state))
    reg.open_pairing(RCV)
    index = unifying.add_device("Second Mouse Z2", 0x4343)
    reg.rescan()                                      # the watcher gets there before the lock closes
    assert NEW_MOUSE in reg.devices
    unifying.emit_lock_status(open_=False)
    assert wait_for(lambda: seen and seen[-1]["state"] == "STOP")
    assert seen[-1]["error"] == "NO_PAIRING_ERROR" and seen[-1]["pairingSlot"] == index
    assert seen[-1]["pairedDevice"]["id"] == NEW_MOUSE


def test_a_lock_that_never_reports_back_times_out(unifying, reg, monkeypatch):
    monkeypatch.setattr(model, "PAIRING_MARGIN", 0.1)
    reg.open_pairing(RCV, timeout=1)
    assert wait_for(lambda: reg.receivers[RCV].pairing["state"] == "STOP")
    assert reg.receivers[RCV].pairing["error"] == "TIMEOUT"


def test_unplugging_mid_pairing_stops_it_as_disconnected(unifying, reg):
    reg.open_pairing(RCV, timeout=30)
    unifying.unplug()
    reg.rescan()
    assert reg.absent_receivers[RCV].pairing["state"] == "STOP"
    assert reg.absent_receivers[RCV].pairing["error"] == "DISCONNECTED"


def test_pairing_can_be_started_again_after_it_ended(unifying, reg):
    reg.open_pairing(RCV)
    unifying.emit_lock_status(open_=False, error=LOCK_DEVICE_TIMEOUT)
    assert wait_for(lambda: reg.receivers[RCV].pairing["state"] == "STOP")
    assert reg.open_pairing(RCV)["state"] == "START"


def test_the_finish_does_not_run_on_the_dispatch_thread(unifying, reg, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    real = reg.rescan

    def slow(force=False):
        entered.set()
        gate.wait(10)
        return real(force)

    buttons = []
    reg.on_button_event.append(lambda dev_id, fn, params: buttons.append(dev_id))
    reg.set_divert(MOUSE, 82, True)
    reg.open_pairing(RCV)
    monkeypatch.setattr(reg, "rescan", slow)
    try:
        unifying.complete_pairing()
        assert entered.wait(2)
        unifying.press_button(82)
        assert wait_for(lambda: buttons, timeout=2)
    finally:
        gate.set()


def _locking_seen(reg):
    seen = []
    reg.on_locking.append(lambda rid, payload: seen.append(payload["state"]))
    return seen


def test_pairing_again_before_the_cancels_echoes_arrive_keeps_the_new_pairing(unifying, reg):
    finishes = []
    real = reg._unifying_finish
    reg._unifying_finish = lambda *a: (finishes.append(a), real(*a))
    seen = _locking_seen(reg)
    unifying.hold_lock_status = True                  # the receiver is slow to report
    reg.open_pairing(RCV)
    reg.cancel_pairing(RCV)
    assert reg.open_pairing(RCV)["state"] == "START"  # again, at once
    unifying.release_lock_status()                    # "open", "closed", "open": all ours, all late
    assert wait_for(lambda: seen[-1:] == ["OPENED"] and "CLOSED" in seen)
    assert finishes == []                             # the cancel's "closed" did not end the new pairing
    assert reg.receivers[RCV].pairing["state"] == "START"


def test_a_receiver_that_never_reports_the_close_does_not_hide_another_tools_open(unifying, reg, monkeypatch):
    monkeypatch.setattr(model, "ECHO_SECONDS", 0.2)
    unifying.hold_lock_status = True
    reg.open_pairing(RCV)
    assert reg.cancel_pairing(RCV)["state"] == "CANCEL"
    unifying.drop_lock_status()                       # neither the open nor the close is ever reported
    time.sleep(0.3)                                   # past the wait for them
    unifying.emit_lock_status(open_=True)             # another tool opens the lock
    assert wait_for(lambda: reg.receivers[RCV].pairing["state"] == "START")


def test_cancelling_with_the_lock_already_shut_waits_for_no_close(unifying, reg, monkeypatch):
    seen = []
    reg.on_pairing.append(lambda rid, state, passkey, events: state and seen.append(state))
    monkeypatch.setattr(model, "ECHO_SECONDS", 0.2)
    unifying.hold_lock_status = True
    reg.open_pairing(RCV)
    unifying.drop_lock_status()
    time.sleep(0.3)                                   # the open was never reported: the lock is shut
    monkeypatch.setattr(model, "ECHO_SECONDS", 30)
    unifying.hold_lock_status = True
    assert reg.cancel_pairing(RCV)["state"] == "CANCEL"
    unifying.drop_lock_status()                       # and the close is not reported either
    unifying.emit_lock_status(open_=True)             # another tool opens the lock
    assert wait_for(lambda: reg.receivers[RCV].pairing["state"] == "START")
    index = unifying.add_device("Second Mouse Z2", 0x4343)
    unifying.emit_lock_status(open_=False)            # a device paired: news, not our close
    assert wait_for(lambda: seen and seen[-1]["state"] == "STOP")
    assert seen[-1]["error"] == "NO_PAIRING_ERROR" and seen[-1]["pairingSlot"] == index
