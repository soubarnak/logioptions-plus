"""Easy-Switch tab (ticket #16): host slots, names and switching through 0x1814 / 0x1815."""

TYPE = "type.googleapis.com/"


def device_id(client):
    return client.call("GET", "/devices/list")["payload"]["deviceInfos"][0]["id"]


def hosts(client):
    reply = client.call("GET", "/devices/%s/easy_switch" % device_id(client))
    assert reply["result"]["code"] == "SUCCESS"
    return reply["payload"]


def change(client, **fields):
    return client.call("SET", "/devices/%s/easy_switch/change" % device_id(client), fields)


def rename(client, index, name):
    return change(client, rename={"index": index, "name": name})


def test_lists_the_host_slots_with_names_and_the_current_host(client):
    p = hosts(client)
    assert p["@type"] == TYPE + "logi.protocol.devices.HostInfo.Hosts"
    assert p["deviceId"] == device_id(client)
    assert [h["index"] for h in p["hosts"]] == [0, 1, 2]
    assert [h["name"] for h in p["hosts"]] == ["Desk PC", "Laptop", ""]
    assert [h["paired"] for h in p["hosts"]] == [True, True, False]
    assert [h["connected"] for h in p["hosts"]] == [True, False, False]
    assert p["hosts"][0]["os"]["type"] == "WINDOWS" and p["hosts"][1]["os"]["type"] == "MACOS"
    assert p["maxNameLength"] == 32
    assert p["capabilities"]["canGetName"] is True and p["capabilities"]["canSetName"] is True
    assert not p["capabilities"].get("canDelete")


def test_the_tab_is_advertised_in_device_capabilities(client):
    info = client.call("GET", "/devices/list")["payload"]["deviceInfos"][0]
    assert info["capabilities"]["hostInfos"] is True


def test_a_mouse_without_easy_switch_has_no_tab(agent_factory, bolt):
    bolt.mouse.features = [f for f in bolt.mouse.features if f not in (0x1814, 0x1815)]
    c = agent_factory().client()
    try:
        info = c.call("GET", "/devices/list")["payload"]["deviceInfos"][0]
        assert not info["capabilities"].get("hostInfos")
        assert c.call("GET", "/devices/%s/easy_switch" % info["id"])["result"]["code"] == "NO_SUCH_PATH"
    finally:
        c.close()


def test_the_current_host_is_read_from_the_mouse(agent_factory, bolt):
    bolt.mouse.current_host = 1
    c = agent_factory().client()
    try:
        assert [h["connected"] for h in hosts(c)["hosts"]] == [False, True, False]
    finally:
        c.close()


def test_switch_writes_set_current_host_and_the_mouse_reports_it(client, bolt):
    reply = change(client, host=1)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.current_host == 1
    assert [h["connected"] for h in hosts(client)["hosts"]] == [False, True, False]


def test_switch_to_an_empty_or_unknown_slot_is_refused_and_nothing_is_written(client, bolt):
    for slot in (2, 7):
        assert change(client, host=slot)["result"]["code"] != "SUCCESS"
    assert bolt.mouse.current_host == 0


def test_failed_switch_is_a_failure_and_keeps_the_current_host(client, bolt):
    bolt.mouse.fail_host_switch = True
    assert change(client, host=1)["result"]["code"] != "SUCCESS"
    assert bolt.mouse.current_host == 0
    assert hosts(client)["hosts"][0]["connected"] is True


def test_rename_writes_the_friendly_name_and_shows_after_reload(agent, agent_factory, client, bolt):
    assert rename(client, 1, "Work laptop")["result"]["code"] == "SUCCESS"
    assert bolt.mouse.hosts[1]["name"] == b"Work laptop"
    assert hosts(client)["hosts"][1]["name"] == "Work laptop"
    client.close()
    agent.stop()
    fresh = agent_factory().client()
    try:
        assert hosts(fresh)["hosts"][1]["name"] == "Work laptop"
    finally:
        fresh.close()


def test_long_names_are_written_in_chunks_and_read_back_whole(client, bolt):
    name = "Living room media centre"          # 24 bytes: more than one HID++ report
    assert rename(client, 0, name)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.hosts[0]["name"] == name.encode()
    assert hosts(client)["hosts"][0]["name"] == name


def test_a_shorter_name_replaces_a_longer_one(client, bolt):
    rename(client, 0, "Living room media centre")
    rename(client, 0, "TV")
    assert bolt.mouse.hosts[0]["name"] == b"TV"
    assert hosts(client)["hosts"][0]["name"] == "TV"


def test_unicode_names_survive(client):
    assert rename(client, 0, "Büro-PC")["result"]["code"] == "SUCCESS"
    assert hosts(client)["hosts"][0]["name"] == "Büro-PC"


def test_names_longer_than_the_mouse_allows_or_empty_are_refused(client, bolt):
    assert rename(client, 0, "x" * 33)["result"]["code"] != "SUCCESS"
    assert rename(client, 0, "")["result"]["code"] != "SUCCESS"
    assert rename(client, 9, "nope")["result"]["code"] != "SUCCESS"
    assert bolt.mouse.hosts[0]["name"] == b"Desk PC"


def test_failed_rename_is_a_failure_and_keeps_the_old_name(client, bolt):
    bolt.mouse.fail_host_name_write = True
    assert rename(client, 0, "Other")["result"]["code"] != "SUCCESS"
    assert hosts(client)["hosts"][0]["name"] == "Desk PC"


def test_unsupported_changes_are_not_reported_as_done(client, bolt):
    for fields in ({"remove": {"index": 1}}, {"move": {"index": 0, "target": 1}}, {}):
        assert change(client, **fields)["result"]["code"] != "SUCCESS"
    assert bolt.mouse.hosts[1]["name"] == b"Laptop"


def test_unknown_device_is_not_fatal(client):
    assert client.call("GET", "/devices/devNOPE/easy_switch")["result"]["code"] == "NO_SUCH_PATH"
    assert client.call("SET", "/devices/devNOPE/easy_switch/change", {"host": 1})["result"]["code"] != "SUCCESS"


def test_easy_switch_image_is_served_so_the_tab_renders(client):
    reply = client.call("GET", "/devices/resources", {"id": device_id(client)})
    res = {r["key"]: r for r in reply["payload"]["resources"]}
    assert res["device_easyswitch_image"]["url"].startswith("file://")
    assert res["device_easyswitch_image"]["url"].endswith("easyswitch.png")
    assert res["device_easyswitch_image"]["contentType"] == "image/png"
