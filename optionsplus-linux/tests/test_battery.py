"""Battery badge and live battery updates (ticket #14), seen from the UI socket."""

import socket

import pytest

TYPE = "type.googleapis.com/"
DEV = "dev00000000"


def first_device(client):
    return client.call("GET", "/devices/list")["payload"]["deviceInfos"][0]


def next_push(client, path):
    """Next BROADCAST on `path`; other traffic is skipped."""
    while True:
        m = client.recv()
        if m.get("path") == path and m.get("verb") == "BROADCAST":
            return m["payload"]


# ---- Device.Info capability flags that gate the indicator ------------------

def test_device_info_declares_battery_capabilities_from_0x1004(client):
    caps = first_device(client)["capabilities"]
    assert caps["hasBatteryStatus"] is True
    assert caps["unifiedBattery"] is True
    assert caps["battery"] == {
        "rechargeable": True, "stateOfCharge": True, "showBatteryStatus": False,
        "supportedLevels": ["critical", "low", "good", "full"],
    }


def test_device_info_carries_discharge_level(client):
    assert first_device(client)["batteryDischargeLevel"] == 73


def test_pushed_device_info_has_the_same_capabilities(client):
    _, pushes = client.subscribe("/devices/state/changed")
    assert pushes[0]["payload"]["capabilities"]["battery"]["stateOfCharge"] is True
    assert pushes[0]["payload"]["capabilities"]["unifiedBattery"] is True


def test_supported_levels_follow_the_device_capability_flags(bolt, agent_factory):
    bolt.mouse.battery_caps = (0x05, 0x03)      # critical + good only
    c = agent_factory().client()
    caps = c.call("GET", "/devices/%s/info" % DEV)["payload"]["capabilities"]
    assert caps["battery"]["supportedLevels"] == ["critical", "good"]
    c.close()


def test_capabilities_without_soc_report_level_only(bolt, agent_factory):
    bolt.mouse.battery_caps = (0x0F, 0x01)      # rechargeable, no state-of-charge percentage
    c = agent_factory().client()
    caps = c.call("GET", "/devices/%s/info" % DEV)["payload"]["capabilities"]
    assert caps["battery"]["stateOfCharge"] is False
    c.close()


# ---- /battery/state/changed on subscribe ----------------------------------

def test_subscribe_battery_state_changed_pushes_each_device(client):
    _, pushes = client.subscribe("/battery/state/changed")
    assert [p["path"] for p in pushes] == ["/battery/state/changed"]
    b = pushes[0]["payload"]
    assert b["@type"] == TYPE + "logi.protocol.wireless.Battery"
    assert b["deviceId"] == DEV
    assert b["percentage"] == 73.0
    assert b["charging"] is False


def test_subscribe_per_device_battery_state_pushes_that_device(client):
    _, pushes = client.subscribe("/battery/%s/state/changed" % DEV)
    assert len(pushes) == 1 and pushes[0]["payload"]["percentage"] == 73.0


# ---- live updates ---------------------------------------------------------

@pytest.fixture
def live(bolt, agent_factory):
    c = agent_factory(poll_seconds=0.05).client()
    yield c
    c.close()


def test_level_change_is_pushed(bolt, live):
    live.subscribe("/battery/state/changed")
    bolt.mouse.battery_percent = 41
    assert next_push(live, "/battery/state/changed")["percentage"] == 41.0


def test_charging_change_is_pushed(bolt, live):
    live.subscribe("/battery/state/changed")
    bolt.mouse.charging = True
    p = next_push(live, "/battery/state/changed")
    assert p["charging"] is True and p["percentage"] == 73.0
    bolt.mouse.charging = False
    assert next_push(live, "/battery/state/changed")["charging"] is False


def test_change_also_updates_the_device_card(bolt, live):
    live.subscribe("/devices/state/changed")
    bolt.mouse.battery_percent = 30
    bolt.mouse.charging = True
    while True:
        info = next_push(live, "/devices/state/changed")
        if info["batteryStatus"]["stateOfCharge"] == 30 and \
                info["batteryStatus"]["chargingStatus"] == "CHARGING":
            break
    assert info["batteryDischargeLevel"] == 30


def test_unchanged_battery_is_not_repushed(bolt, live):
    live.subscribe("/battery/state/changed")
    live.sock.settimeout(0.5)
    with pytest.raises((socket.timeout, TimeoutError)):
        live.recv()


# ---- the poller survives a device going away mid-poll ----------------------

def test_poller_skips_a_device_removed_mid_poll_and_keeps_running():
    import asyncio
    from types import SimpleNamespace

    from opl_agent import main

    reg = SimpleNamespace(devices={
        "gone": SimpleNamespace(battery_cache={"percent": 50}),
        "stays": SimpleNamespace(battery_cache={"percent": 50}),
    })
    refreshed = []

    def refresh(dev_id):
        refreshed.append(dev_id)
        if dev_id == "gone":
            del reg.devices["gone"]      # the rescan thread drops it while the read runs
        else:
            reg.devices[dev_id].battery_cache = {"percent": 40 + len(refreshed)}

    reg.refresh_battery = refresh
    reg.battery = lambda dev_id: ({"percentage": reg.devices[dev_id].battery_cache["percent"]}, None)
    reg.device_info = lambda dev_id: {"id": dev_id}
    sent = []
    server = SimpleNamespace(broadcast=lambda path, pl: sent.append(path))

    async def run():
        task = asyncio.ensure_future(main._poll(server, reg, 0.01))
        await asyncio.sleep(0.15)
        died = task.done()
        task.cancel()
        return died

    assert asyncio.run(run()) is False
    assert refreshed.count("stays") >= 2          # polled again on later ticks
    assert "/battery/stays/state/changed" in sent
