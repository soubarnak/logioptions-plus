"""Receiver as a device and Logi Bolt pairing (ticket #18), driven through the simulated Bolt."""

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

from tests.fakes.hidpp_device import PAIR_CLOSED, PAIR_FAILED  # noqa: E402

PAIR = "/devices/%s/pair" % RCV
CHANGED = PAIR + "/changed"
PASSKEY = PAIR + "/passkey/displayed"


@pytest.fixture
def pairing(bolt, agent_factory):
    """A client subscribed to the pairing broadcasts and device state."""
    c = agent_factory(rescan_seconds=0.1).client()
    for path in (CHANGED, PASSKEY, "/devices/state/changed"):
        c.subscribe(path)
    yield c
    c.close()


def request(client, verb, path):
    """(reply, broadcasts that arrived before it): a broadcast may precede the reply."""
    mid = client.send(verb, path)
    pushes = []
    while True:
        m = client.recv()
        if m.get("msgId") == mid and "result" in m:
            return m, [p for p in pushes if p.get("verb") == "BROADCAST"]
        pushes.append(m)


def test_opening_pairing_opens_the_receiver_lock_and_announces_it(bolt, pairing):
    reply, pushes = request(pairing, "SET", PAIR)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.pairing_open is True
    (state,) = [p["payload"] for p in pushes if p["path"] == CHANGED] or [next_push(pairing, CHANGED)]
    assert state["state"] == "STARTED" and state["receiverId"] == RCV


def test_a_failed_open_is_reported_and_leaves_pairing_idle(bolt, pairing):
    bolt.fail_pairing_open = True
    reply = pairing.call("SET", PAIR)
    assert reply["result"]["code"] == "FAULTED"
    assert bolt.pairing_open is False
    assert pairing.call("GET", PAIR)["payload"]["state"] == "IDLE"


def test_a_receiver_without_the_pairing_feature_cannot_pair(bolt, agent_factory):
    bolt.receiver_features = [0x0000]
    c = agent_factory().client()
    assert c.call("SET", PAIR)["result"]["code"] == "FAULTED"
    assert bolt.pairing_open is False


def test_pairing_status_can_be_read(pairing):
    assert pairing.call("GET", PAIR)["payload"]["state"] == "IDLE"
    pairing.call("SET", PAIR)
    assert pairing.call("GET", PAIR)["payload"]["state"] == "STARTED"


def test_cancel_closes_the_lock_and_announces_it(bolt, pairing):
    pairing.call("SET", PAIR)
    reply, pushes = request(pairing, "SET", PAIR + "/cancel")
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.pairing_open is False
    states = [p["payload"]["state"] for p in pushes if p["path"] == CHANGED]
    assert (states or [next_push(pairing, CHANGED)["state"]])[-1] == "CANCELLED"


def test_a_failed_cancel_is_reported_and_pairing_stays_started(bolt, pairing):
    pairing.call("SET", PAIR)
    bolt.fail_pairing_cancel = True
    assert pairing.call("SET", PAIR + "/cancel")["result"]["code"] == "FAULTED"
    assert pairing.call("GET", PAIR)["payload"]["state"] == "STARTED"


def test_the_passkey_is_broadcast_for_the_ui_to_show(bolt, pairing):
    pairing.call("SET", PAIR)
    bolt.emit_passkey("274913")
    shown = next_push(pairing, PASSKEY)
    assert shown["passkey"] == "274913" and shown["receiverId"] == RCV
    assert pairing.call("GET", PAIR)["payload"]["state"] == "PASSKEY_DISPLAYED"


def test_a_device_pairing_completes_and_appears(bolt, pairing):
    pairing.call("SET", PAIR)
    bolt.complete_pairing("Second Mouse Z2", 0x4343)
    done = appeared = None
    while done is None or appeared is None:        # either may arrive first
        m = pairing.recv()
        if m.get("path") == CHANGED and m["payload"]["state"] == "PAIRED":
            done = m["payload"]
        if m.get("path") == "/devices/state/changed" and m["payload"]["id"] == NEW_MOUSE:
            appeared = m["payload"]
    assert done["pairingSlot"] == 2 and done["pairedDevice"]["id"] == NEW_MOUSE
    assert appeared["state"] == "ACTIVE"
    assert [d["id"] for d in infos(pairing)] == [MOUSE, NEW_MOUSE, RCV]


def test_a_pairing_failure_is_reported_with_its_error(bolt, pairing):
    pairing.call("SET", PAIR)
    bolt.emit_pairing_status(PAIR_FAILED, error=3)
    failed = next_push(pairing, CHANGED)
    while failed["state"] != "FAILED":
        failed = next_push(pairing, CHANGED)
    assert failed["error"] != "NO_PAIRING_ERROR"
    assert [d["id"] for d in infos(pairing)] == [MOUSE, RCV]


def test_the_receiver_closing_its_lock_ends_pairing(bolt, pairing):
    pairing.call("SET", PAIR)
    bolt.emit_pairing_status(PAIR_CLOSED)
    ended = next_push(pairing, CHANGED)
    while ended["state"] != "CANCELLED":
        ended = next_push(pairing, CHANGED)


def test_pairing_routes_do_not_exist_on_a_mouse(bolt, pairing):
    assert pairing.call("SET", "/devices/%s/pair" % MOUSE)["result"]["code"] == "NO_SUCH_PATH"
    assert bolt.pairing_open is False
