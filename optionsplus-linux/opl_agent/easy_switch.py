"""Easy-Switch tab: HID++ host slots as logi.protocol.devices.HostInfo payloads.

The wire message shapes come from devices.proto (HostInfo.Hosts for the list,
ChangeHost / HostInfo.ChangeInfo for the writes). Indexes are the HID++ ones,
0-based, as they are on the mouse; whether the UI counts the same way is
still to be confirmed on hardware (feature-parity.md, section 6.3)."""

TYPE = "type.googleapis.com/"

# HostInfo.BusType and HostInfo.OSType enum names, by the value the mouse reports.
BUS_TYPES = ["INVALID", "UNIFYING", "USB", "BT", "BLE", "BLEPRO"]
OS_TYPES = ["UNKNOWN", "WINDOWS", "WINDOWS_EMBEDDED", "LINUX", "CHROME",
            "ANDROID", "MACOS", "IOS", "WEBOS", "TIZEN"]


def _enum(names, value):
    return names[value] if 0 <= value < len(names) else names[0]


def _slot(by_index, host):
    if isinstance(host, bool) or not isinstance(host, int):
        return None
    return by_index.get(host)


def hosts_payload(dev, state):
    """HostInfo.Hosts for one device from hidpp.get_hosts()."""
    return {
        "@type": TYPE + "logi.protocol.devices.HostInfo.Hosts",
        "deviceId": dev.id,
        "serialNumber": dev.serial,
        "capabilities": {"canGetName": state["can_get_name"], "canSetName": state["can_set_name"]},
        "maxNameLength": state["max_name"],
        "hosts": [{
            "index": h["index"],
            "paired": h["paired"],
            "connected": h["index"] == state["current"],
            "busType": _enum(BUS_TYPES, h["bus"]),
            "name": h["name"],
            "os": {"type": _enum(OS_TYPES, h["os"])},
        } for h in state["hosts"]],
    }


def cache_payload(dev, state):
    """HostInfo.Cache with this device's Hosts, for /devices/state/easy_switch_cache_updated."""
    hosts = hosts_payload(dev, state)
    del hosts["@type"]
    return {"@type": TYPE + "logi.protocol.devices.HostInfo.Cache", "devices": [{"device": hosts}]}


def host_changed_payload(host):
    """ChangeHost, for /devices/host_changed."""
    return {"@type": TYPE + "logi.protocol.devices.ChangeHost", "host": host}


def apply_change(reg, dev, change):
    """Blocking: carry out a ChangeHost ({host}) or ChangeInfo ({rename}) request.

    Returns (code, message, state): a refused or failed request never reports SUCCESS
    (a device error raises out of the register calls); `state` is the hosts state as the
    change leaves it (None when nothing changed), for the announcements."""
    if not isinstance(change, dict):
        return "FAULTED", "expected a host change", None
    state = reg.read_hosts(dev.id)
    by_index = {h["index"]: h for h in state["hosts"]}
    if "host" in change:
        slot = _slot(by_index, change["host"])
        if slot is None or not slot["paired"]:
            return "FAULTED", "no paired host in that slot", None
        reg.switch_host(dev.id, slot["index"])
        state["current"] = slot["index"]
        return "SUCCESS", "", state
    ren = change.get("rename")
    if isinstance(ren, dict):
        slot, name = _slot(by_index, ren.get("index")), ren.get("name")
        if slot is None or not slot["paired"]:
            return "FAULTED", "no paired host in that slot", None
        if not state["can_set_name"]:
            return "FAULTED", "this mouse does not let host names be changed", None
        if not isinstance(name, str) or not name.strip():
            return "FAULTED", "a host name cannot be empty", None
        if len(name.encode("utf-8")) > slot["max_name"]:
            return "FAULTED", "host name longer than %d bytes" % slot["max_name"], None
        reg.write_host_name(dev.id, slot["index"], name)
        slot["name"] = name
        return "SUCCESS", "", state
    return "FAULTED", "only switching host and renaming are supported", None
