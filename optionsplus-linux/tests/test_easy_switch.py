"""Easy-Switch tab (ticket #16): host slots, names and switching through 0x1814 / 0x1815."""

from opl_agent import easy_switch

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
    assert [h["os"]["type"] for h in p["hosts"][:2]] == ["WINDOWS", "LINUX"]     # read with getHostOsVersion
    assert "os" not in p["hosts"][2]                                            # type 0 is unknown: omitted
    assert p["maxNameLength"] == 32
    assert p["capabilities"]["canGetName"] is True and p["capabilities"]["canSetName"] is True
    assert not p["capabilities"].get("canDelete")


def test_a_host_os_the_mouse_reports_is_named_with_the_chosen_spelling(agent_factory, bolt):
    bolt.mouse.hosts[0]["os"] = 7
    c = agent_factory().client()
    try:
        assert hosts(c)["hosts"][0]["os"]["type"] == easy_switch.OS_NAMES[7]
        assert easy_switch.OS_NAMES[7].endswith("IOS")
    finally:
        c.close()


def test_the_os_names_can_be_switched_to_the_prefixed_strings_keys():
    assert easy_switch.os_names("OS_")[3] == "OS_LINUX"
    assert easy_switch.os_names("OS_")[2] == "OS_WINDOWS_EMBEDDED"
    assert easy_switch.os_names("")[3] == "LINUX"


def test_an_unknown_or_out_of_range_host_os_is_omitted(agent_factory, bolt):
    bolt.mouse.hosts[0]["os"] = 200
    c = agent_factory().client()
    try:
        assert "os" not in hosts(c)["hosts"][0]
    finally:
        c.close()


def test_a_host_os_reply_for_another_host_is_ignored(agent_factory, bolt):
    bolt.mouse.host_os_echo = 1
    c = agent_factory().client()
    try:
        assert "os" not in hosts(c)["hosts"][0]
    finally:
        c.close()


def test_the_os_is_only_read_for_paired_hosts(client, bolt):
    hosts(client)
    index = bolt.mouse.features.index(0x1815)
    os_reads = [w for w in bolt.writes if w[2] == index and w[3] >> 4 == 7]
    assert len(os_reads) == 2


def test_a_silent_host_os_read_is_tried_once_then_skipped(agent_factory, bolt):
    bolt.mouse.host_os_silent = True
    c = agent_factory().client()
    try:
        p = hosts(c)
        assert all("os" not in h for h in p["hosts"])
        assert [h["name"] for h in p["hosts"]] == ["Desk PC", "Laptop", ""]
        index = bolt.mouse.features.index(0x1815)
        assert len([w for w in bolt.writes if w[2] == index and w[3] >> 4 == 7]) == 1
    finally:
        c.close()


def test_reading_the_hosts_sends_only_getHostInfo_to_0x1814(client, bolt):
    hosts(client)
    index = bolt.mouse.features.index(0x1814)
    assert {w[3] >> 4 for w in bolt.writes if w[2] == index} <= {0}
    assert not bolt.mouse.host_writes


def test_a_failing_host_os_read_omits_os_and_keeps_the_rest(agent_factory, bolt):
    bolt.mouse.fail_host_os_read = True
    c = agent_factory().client()
    try:
        p = hosts(c)
        assert all("os" not in h for h in p["hosts"])
        assert [h["name"] for h in p["hosts"]] == ["Desk PC", "Laptop", ""]
    finally:
        c.close()


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


# ---- layout and behaviour on a real mouse (review of ticket #16) ---------------

def test_the_whole_name_is_read_with_its_first_character(client, bolt):
    bolt.mouse.hosts[0]["name"] = b"Living room media centre"
    assert hosts(client)["hosts"][0]["name"] == "Living room media centre"
    assert (0, 0) in bolt.mouse.name_reads and (0, 14) in bolt.mouse.name_reads


def test_names_of_a_whole_number_of_chunks_are_written_without_an_extra_chunk(client, bolt):
    for name in ("a" * 14, "b" * 28):
        bolt.mouse.name_writes.clear()
        assert rename(client, 0, name)["result"]["code"] == "SUCCESS"
        assert sum(n for _h, _o, n in bolt.mouse.name_writes) == len(name)
        assert all(n > 0 for _h, _o, n in bolt.mouse.name_writes)
        assert hosts(client)["hosts"][0]["name"] == name


def test_a_switch_the_mouse_never_answers_is_still_a_success(client, bolt):
    reply = change(client, host=1)       # the fake leaves the way a real mouse does: no reply
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.current_host == 1


def test_the_name_limit_is_the_one_of_the_host_slot(client, bolt):
    bolt.mouse.hosts[1]["max_name"] = 8
    assert hosts(client)["hosts"][1]["name"] == "Laptop"
    assert rename(client, 1, "Much too long")["result"]["code"] != "SUCCESS"
    assert rename(client, 0, "Much too long")["result"]["code"] == "SUCCESS"


def test_capabilities_come_from_the_mouse(agent_factory, bolt):
    bolt.mouse.host_caps = 0x01          # may read names, may not write them
    c = agent_factory().client()
    try:
        caps = hosts(c)["capabilities"]
        assert caps["canGetName"] is True and not caps.get("canSetName")
        assert rename(c, 0, "Other")["result"]["code"] != "SUCCESS"
        assert bolt.mouse.name_writes == []
    finally:
        c.close()


def test_a_mouse_that_cannot_give_names_is_not_asked_for_them(agent_factory, bolt):
    bolt.mouse.host_caps = 0x00
    c = agent_factory().client()
    try:
        p = hosts(c)
        assert not p["capabilities"].get("canGetName") and [h["name"] for h in p["hosts"]] == ["", "", ""]
        assert bolt.mouse.name_reads == []
    finally:
        c.close()


def test_the_change_host_route_switches_too(client, bolt):
    reply = client.call("SET", "/change_host/%s/host" % device_id(client), {"host": 1})
    assert reply["result"]["code"] == "SUCCESS" and bolt.mouse.current_host == 1
    refused = client.call("SET", "/change_host/%s/host" % device_id(client), {"host": 2})
    assert refused["result"]["code"] != "SUCCESS" and bolt.mouse.current_host == 1


def test_a_change_is_announced(agent_factory, bolt):
    c = agent_factory().client()
    paths = ("/devices/easy_switch", "/devices/host_changed", "/devices/state/easy_switch_cache_updated")
    try:
        for path in paths:
            c.subscribe(path)
        dev = "/devices/%s/easy_switch/change" % device_id(c)
        mid = c.send("SET", dev, {"rename": {"index": 1, "name": "Work laptop"}})
        pushes = _until_reply(c, mid)
        by_path = {p["path"]: p["payload"] for p in pushes}
        assert by_path["/devices/easy_switch"]["hosts"][1]["name"] == "Work laptop"
        entry = by_path["/devices/state/easy_switch_cache_updated"]["devices"][0]
        assert entry["device"]["deviceId"] == device_id(c)
        assert "/devices/host_changed" not in by_path
        mid = c.send("SET", dev, {"host": 1})
        by_path = {p["path"]: p["payload"] for p in _until_reply(c, mid)}
        assert by_path["/devices/host_changed"]["host"] == 1
        assert by_path["/devices/easy_switch"]["hosts"][1]["connected"] is True
    finally:
        c.close()


def _until_reply(client, mid):
    pushes = []
    while True:
        m = client.recv()
        if m.get("msgId") == mid and "result" in m:
            return [p for p in pushes if p.get("verb") == "BROADCAST"]
        pushes.append(m)


def test_reading_the_hosts_only_uses_known_0x1815_functions(client, bolt):
    hosts(client)
    index = bolt.mouse.features.index(0x1815)
    used = {w[3] >> 4 for w in bolt.writes if w[2] == index}
    assert not bolt.mouse.host_writes               # never moveHost (5), deleteHost (6), setHostOsVersion (8)
    assert used <= {0, 1, 3, 7}     # an unconfirmed function number could move or delete a host
