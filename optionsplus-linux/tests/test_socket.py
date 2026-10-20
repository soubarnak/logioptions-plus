"""Socket-level behaviour of the agent, exactly as the UI sees it."""

import json
import os
import time
import struct

from opl_agent import hidpp, ipc, main
from tests.fakes import depot as fx
from tests.fakes.hidpp_device import MOUSE_NAME, MOUSE_WPID

TYPE = "type.googleapis.com/"


def device_id(client):
    reply = client.call("GET", "/devices/list")
    return reply["payload"]["deviceInfos"][0]["id"]


# ---- framing --------------------------------------------------------------

def test_agent_greets_then_echoes_msg_id_as_msgId(client):
    proto, body = client.greeting
    assert proto == b"protobuf"
    assert body.endswith(b"backend")
    reply = client.call("GET", "/permissions")
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["verb"] == "GET" and reply["path"] == "/permissions"
    assert reply["origin"] == "backend"
    assert reply["payload"]["@type"] == TYPE + "logi.protocol.app_permissions.Permissions"


def test_replies_are_matched_to_their_own_request(client):
    first = client.send("GET", "/permissions")
    second = client.send("GET", "/lps/status")
    seen = {}
    while len(seen) < 2:
        m = client.recv()
        seen[m["msgId"]] = m["path"]
    assert seen == {first: "/permissions", second: "/lps/status"}


# ---- SUBSCRIBE ------------------------------------------------------------

def test_subscribe_configuration_pushes_current_configuration(client):
    reply, pushes = client.subscribe("/configuration")
    assert reply["result"]["code"] == "SUCCESS"
    assert [p["path"] for p in pushes] == ["/configuration"]
    assert pushes[0]["verb"] == "BROADCAST" and "result" not in pushes[0]
    assert pushes[0]["payload"]["language"] == {"value": "en-US"}


def test_subscribe_boot_paths_push_the_state_the_ui_waits_on(client):
    _, perms = client.subscribe("/permissions")
    assert [p["path"] for p in perms] == ["/permissions"]
    _, lps = client.subscribe("/lps/endpoint/info_changed")
    assert lps[0]["payload"]["state"] == "LOADED"


def test_subscribe_devices_pushes_each_device_with_battery(client):
    _, pushes = client.subscribe("/devices/state/changed")
    pushes = [p for p in pushes if p["payload"]["deviceType"] != "RECEIVER"]     # see test_pairing
    assert len(pushes) == 1
    info = pushes[0]["payload"]
    assert pushes[0]["path"] == "/devices/state/changed"
    assert info["@type"] == TYPE + "logi.protocol.devices.Device.Info"
    assert info["displayName"] == fx.DISPLAY_NAME
    assert info["batteryStatus"]["stateOfCharge"] == 73
    assert info["batteryStatus"]["chargingStatus"] == "DISCHARGING"


def test_subscribe_to_unknown_path_succeeds_without_pushes(client):
    reply, pushes = client.subscribe("/nothing/here")
    assert reply["result"]["code"] == "SUCCESS" and pushes == []


# ---- devices, served from the simulated mouse -----------------------------

def test_device_list_comes_from_the_simulated_mouse(client):
    reply = client.call("GET", "/devices/list")
    infos = [i for i in reply["payload"]["deviceInfos"] if i["deviceType"] != "RECEIVER"]
    assert len(infos) == 1
    info = infos[0]
    assert info["id"] == "dev00000000"
    assert info["pid"] == MOUSE_WPID
    assert info["state"] == "ACTIVE" and info["connected"] is True
    assert info["displayName"] == fx.DISPLAY_NAME            # catalogue name
    assert info["modelId"] == fx.MODEL_ID
    assert info["slotPrefix"] == fx.SLOT_PREFIX
    assert info["resourcesAvailable"] is True
    assert info["thumbnail"].startswith("file://") and info["thumbnail"].endswith("thumb.png")


def test_ever_connected_list_has_the_mouse(client):
    reply = client.call("GET", "/devices/ever_connected/list")
    (dev,) = reply["payload"]["devices"]
    assert dev["deviceType"] == "MOUSE" and dev["deviceModel"] == fx.MODEL_ID


def test_device_info_route_reports_battery_and_firmware(client):
    reply = client.call("GET", "/devices/%s/info" % device_id(client))
    info = reply["payload"]
    assert reply["result"]["code"] == "SUCCESS"
    assert info["id"] == "dev00000000"
    assert info["firmwareVersion"] == [{"type": 0, "name": "RBM", "version": "12.34", "build": 0x567}]
    assert info["batteryStatus"] == {"stateOfCharge": 73, "chargingStatus": "DISCHARGING"}


def test_battery_route_reports_simulated_state(client):
    reply = client.call("GET", "/battery/%s/state" % device_id(client))
    b = reply["payload"]
    assert b["@type"] == TYPE + "logi.protocol.wireless.Battery"
    assert b["percentage"] == 73.0 and b["charging"] is False
    assert b["level"] == "GOOD" and b["criticalLevel"] is False


def test_battery_reflects_mouse_state_at_agent_start(bolt, agent_factory):
    bolt.mouse.battery_percent = 12
    bolt.mouse.charging = True
    running = agent_factory()
    c = running.client()
    b = c.call("GET", "/battery/dev00000000/state")["payload"]
    assert b["percentage"] == 12.0 and b["charging"] is True and b["criticalLevel"] is True


def test_device_resources_come_from_the_depot(client):
    reply = client.call("GET", "/devices/resources", {"id": device_id(client)})
    keys = {r["key"]: r for r in reply["payload"]["resources"]}
    assert set(keys) == {"thumbnail", "layout", "device_easyswitch_image"}
    assert keys["layout"]["asDataUri"] is True and keys["layout"]["url"].startswith("data:application/json")


def test_default_profile_assignments_come_from_the_depot(client):
    reply = client.call("GET", "/v2/profiles")
    slots = {a["slotId"] for a in reply["payload"]["profiles"][0]["assignments"]}
    assert slots == {"046d_4242_c82", "046d_4242_c83", "046d_4242_c195", "046d_4242_thumb_wheel_adapter",
                     "046d_4242_mouse_settings", "046d_4242_mouse_scroll_wheel_settings",
                     "046d_4242_mouse_thumb_wheel_settings"}


def test_localized_strings_come_from_the_depot(client):
    reply = client.call("GET", "/resources/localized_packages",
                        {"packages": ["logioptionsplus/data/strings"]})
    (pkg,) = reply["payload"]["packages"]
    assert pkg["available"] is True
    assert (pkg["strings"]["HELLO_KEY"], pkg["strings"]["BYE_KEY"]) == ("Hello", "Bye")


def test_agent_talked_hidpp_to_the_simulated_device(client, bolt):
    assert bolt.writes, "agent never wrote to the transport"
    assert all(w[0] in (hidpp.SHORT, hidpp.LONG) for w in bolt.writes)
    assert any(w[0] == hidpp.SHORT for w in bolt.writes)


def test_simulated_device_answers_long_requests_and_records_them(bolt):
    h = hidpp.Reader(bolt.open(bolt.PATH))
    reply = hidpp.call(h, 1, 0, 1, bytes([0, 0, 0xA5]), long=True)    # Root ping, long report
    assert reply[:3] == bytes([4, 2, 0xA5])
    assert bolt.writes[-1][0] == hidpp.LONG and len(bolt.writes[-1]) == hidpp.LONG_LEN


def test_simulated_device_emits_events_on_demand_and_they_are_ignored_by_replies(bolt):
    h = hidpp.Reader(bolt.open(bolt.PATH))
    events = []
    h.add_listener(lambda idx, fidx, fn, params: events.append(params[0]))
    bolt.emit(bytes([hidpp.LONG, 1, 0x04, 0x00, 55]).ljust(hidpp.LONG_LEN, b"\0"))
    assert hidpp.root_ping(h, 1) == (4, 2)
    for _ in range(100):
        if events:
            break
        time.sleep(0.02)
    assert events == [55]


def test_simulated_mouse_name_is_the_fallback_without_a_catalogue(bolt, workdir, agent_factory):
    empty = os.path.join(workdir, "empty")
    os.makedirs(empty)
    running = agent_factory(depot_dir=empty, socket_path=os.path.join(workdir, "s2"))
    info = running.client().call("GET", "/devices/list")["payload"]["deviceInfos"][0]
    assert info["displayName"] == MOUSE_NAME and info["resourcesAvailable"] is False


# ---- configuration persistence -------------------------------------------

def test_configuration_set_is_persisted_and_broadcast(agent, client):
    other = agent.client()
    other.subscribe("/configuration")
    reply = client.call("SET", "/configuration", {"theme": {"value": "DARK_MODE"}})
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["payload"]["theme"] == {"value": "DARK_MODE"}
    push = other.recv()
    assert push["verb"] == "BROADCAST" and push["payload"]["theme"] == {"value": "DARK_MODE"}


def test_configuration_survives_agent_restart(agent_factory, workdir):
    first = agent_factory()
    c = first.client()
    c.call("SET", "/configuration", {"language": {"value": "fr-FR"}, "useSystemTheme": {"value": False}})
    c.close()
    first.stop()

    saved = json.load(open(os.path.join(workdir, "state", "configuration.json")))
    assert saved["language"] == {"value": "fr-FR"}

    second = agent_factory()
    cfg = second.client().call("GET", "/configuration")["payload"]
    assert cfg["language"] == {"value": "fr-FR"}
    assert cfg["useSystemTheme"] == {"value": False}
    assert cfg["autolaunch"] == {"value": True}             # untouched default kept


# ---- robustness -----------------------------------------------------------

def assert_still_serving(client):
    reply = client.call("GET", "/permissions")
    assert reply["result"]["code"] == "SUCCESS"


def test_malformed_frames_leave_the_agent_serving(agent, client):
    client.send_raw(client.frame(b"{not json"))
    client.send_raw(client.frame(b"[1, 2, 3]"))
    client.send_raw(client.frame(b'"just a string"'))
    client.send_raw(client.frame(b"{}", proto=b"nope"))
    client.send_raw(client.frame(b""))
    assert_still_serving(client)
    # a fresh connection is accepted too
    other = agent.client()
    assert_still_serving(other)


def test_truncated_frame_then_disconnect_leaves_the_agent_serving(agent):
    c = agent.client()
    c.send_raw(struct.pack("<I", 99) + struct.pack(">I", 4) + b"js")
    c.close()
    other = agent.client()
    assert_still_serving(other)


def test_unknown_verbs_are_answered_not_fatal(client):
    for verb in ("FROBNICATE", "", "BROADCAST", "OPTIONS", "REMOVE"):
        reply = client.call(verb, "/permissions")
        assert reply["result"]["code"]
    reply = client.call("GET", "/no/such/route")
    assert reply["result"]["code"] == "NO_SUCH_PATH"
    assert_still_serving(client)


def test_bad_device_ids_are_rejected_not_fatal(client):
    assert client.call("GET", "/devices/devNOPE/info")["result"]["code"] == "NO_SUCH_PATH"
    assert client.call("GET", "/battery/devNOPE/state")["result"]["code"] == "NO_SUCH_PATH"
    assert client.call("GET", "/devices/resources", {"id": "devNOPE"})["result"]["code"] == "INVALID_DEVICE"
    assert_still_serving(client)


def test_wrong_payload_types_are_not_fatal(client):
    for payload in ("a string", 5, [1, 2], True, {"packages": 7}):
        client.call("GET", "/devices/resources", payload)
        client.call("GET", "/resources/localized_packages", payload)
        client.call("SET", "/configuration", payload)
    assert client.call("SET", "/configuration", "text")["result"]["code"] == "SUCCESS"
    assert_still_serving(client)


def test_missing_or_odd_msg_id_does_not_break_the_connection(client):
    client.send_raw(client.frame(json.dumps({"verb": "GET", "path": "/permissions"}).encode()))
    client.send_raw(client.frame(json.dumps({"msg_id": 7, "verb": "GET", "path": "/permissions"}).encode()))
    ids = [client.recv()["msgId"] for _ in range(2)]
    assert sorted(ids) == ["", "7"]
    assert_still_serving(client)


# ---- wiring ---------------------------------------------------------------

def test_production_wiring_uses_real_hidraw_and_the_real_socket_path():
    agent = main.Agent()
    assert agent.reg.transport is hidpp.HIDRAW
    assert agent.server.path == ipc.socket_path()
    assert agent.server.path.startswith("/tmp/logitech_kiros_agent-")


def test_hermetic_guard_blocks_the_real_transport():
    import pytest
    with pytest.raises(AssertionError):
        hidpp.HIDRAW.open("/dev/hidraw0")


# ---- firmware version and update check -----------------------------------

def test_dfu_info_reports_no_update_and_the_windows_style_version(client):
    reply = client.call("GET", "/dfu/%s/info" % device_id(client))
    info = reply["payload"]
    assert reply["result"]["code"] == "SUCCESS"
    assert info["@type"] == TYPE + "logi.protocol.dfu.Info"
    assert info["requireFwUpdate"] is False
    assert info["dfuResult"] == "SUCCESS"
    # simulated 0x0003 entity: RBM 0x12.0x34 build 0x0567
    assert info["eligibleVersion"] == "RBM 12.34.B0567"


def test_dfu_info_for_unknown_device_is_rejected(client):
    reply = client.call("GET", "/dfu/dev99999999/info")
    assert reply["result"]["code"] == "NO_SUCH_PATH"


def test_dfu_update_available_reports_nothing(client):
    reply = client.call("GET", "/dfu/update/available")
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["payload"]["@type"] == TYPE + "google.protobuf.BoolValue"
    assert reply["payload"]["value"] is False


def test_firmware_string_uses_the_main_application_entity_and_pads_the_build():
    from opl_agent import model
    fw = [{"type": 1, "name": "BL1", "version": "01.00", "build": 1},
          {"type": 0, "name": "RBM", "version": "22.01", "build": 6}]
    assert model.format_firmware(fw) == "RBM 22.01.B0006"
    assert model.format_firmware([]) == ""
    assert model.format_firmware("") == ""
