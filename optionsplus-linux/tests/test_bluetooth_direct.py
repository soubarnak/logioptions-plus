"""Bluetooth-direct devices whose transport carries long HID++ reports only (ticket #17)."""

import os
import re

import pytest

from opl_agent import hidpp
from tests.test_pointer_speed import slider

DEV = "dev00000000"
RULES = os.path.join(os.path.dirname(__file__), "..", "data", "70-optionsplus-linux.rules")


@pytest.fixture
def ble_client(ble, agent_factory):
    c = agent_factory(transport=ble).client()
    yield c
    c.close()


# ---- requests on a transport without a short-report collection -----------------

def test_every_request_is_a_long_report_on_a_long_only_transport(ble):
    reader = hidpp.Reader(ble.open(ble.path), long_only=True)
    try:
        hidpp.root_ping(reader, hidpp.DEV_RECEIVER)             # short-sized by default
        hidpp.get_dpi(reader, hidpp.DEV_RECEIVER)
    finally:
        reader.close()
    assert ble.writes and {w[0] for w in ble.writes} == {hidpp.LONG}
    assert {len(w) for w in ble.writes} == {hidpp.LONG_LEN}


def test_register_reads_are_long_too_on_a_long_only_transport(ble):
    reader = hidpp.Reader(ble.open(ble.path), long_only=True)
    try:
        with pytest.raises(hidpp.HidppError):                   # the mouse answers with an error
            hidpp.read_register(reader, hidpp.DEV_RECEIVER, 0xB5, b"\x00")
    finally:
        reader.close()
    assert {w[0] for w in ble.writes} == {hidpp.LONG}


def test_a_transport_with_short_reports_still_sends_short_requests(bolt):
    reader = hidpp.Reader(bolt.open(bolt.path))
    try:
        hidpp.root_ping(reader, 1)
    finally:
        reader.close()
    assert bolt.writes[0][0] == hidpp.SHORT


# ---- discovery from the report descriptor ---------------------------------------

def _descriptor(*report_ids, page=0xFF43):
    out = bytearray()
    for rid in report_ids:
        out += bytes([0x06, page & 0xFF, page >> 8, 0x85, rid])
    return bytes(out)


def _sysfs(root, name, descriptor, bus, pid=0xB042):
    dev = root / name / "device"
    dev.mkdir(parents=True)
    (dev / "uevent").write_text("HID_ID=%04X:0000046D:0000%04X\nHID_NAME=Test Mouse X1\n" % (bus, pid))
    (dev / "report_descriptor").write_bytes(descriptor)


def test_node_with_only_the_long_collection_is_long_only(tmp_path):
    _sysfs(tmp_path, "hidraw3", _descriptor(hidpp.LONG), bus=5)
    (node,) = hidpp.list_receivers_and_devices(str(tmp_path))
    assert node.long_only and not node.is_receiver
    assert node.pid == 0xB042


def test_node_with_the_short_collection_is_not_long_only(tmp_path):
    _sysfs(tmp_path, "hidraw3", _descriptor(hidpp.SHORT, hidpp.LONG), bus=3)
    (node,) = hidpp.list_receivers_and_devices(str(tmp_path))
    assert not node.long_only


# ---- the agent drives it like any other device ------------------------------------

def test_discovery_finds_the_device_at_its_direct_index(ble, ble_client):
    infos = ble_client.call("GET", "/devices/list")["payload"]["deviceInfos"]
    assert [i["id"] for i in infos] == [DEV]
    assert infos[0]["displayName"] == "Test Mouse X1"
    assert infos[0]["connected"] is True


def test_device_list_battery_and_firmware_over_long_reports(ble_client):
    info = ble_client.call("GET", "/devices/list")["payload"]["deviceInfos"][0]
    assert info["batteryDischargeLevel"] == 73
    assert info["capabilities"]["battery"]["stateOfCharge"] is True
    assert info["firmwareVersion"]


def test_dpi_write_and_read_back_over_long_reports(ble, ble_client):
    reply = slider(ble_client, 0.5)
    assert reply["result"]["code"] == "SUCCESS"
    assert ble.mouse.dpi == 4500
    assert reply["payload"]["assignment"]["card"]["mouseSettings"]["pointerSpeed"]["active"]["dpiLevel"] == 4500


def test_failed_dpi_write_is_a_failure_not_success(ble, ble_client):
    ble.mouse.fail_dpi_write = True
    reply = slider(ble_client, 0.9)
    assert reply["result"]["code"] != "SUCCESS"
    assert ble.mouse.dpi == 1000


def test_no_short_report_is_ever_written(ble, ble_client):
    ble_client.call("GET", "/devices/list")
    slider(ble_client, 0.2)
    assert ble.writes and all(w[0] == hidpp.LONG for w in ble.writes)


# ---- udev ----------------------------------------------------------------------------

def test_udev_rule_covers_bluetooth_logitech_hid_devices():
    rules = open(RULES).read()
    bt = [l for l in rules.splitlines() if l.startswith('KERNEL=="hidraw*"') and "0005:046D" in l.upper()]
    assert len(bt) == 1
    assert 'GROUP="input"' in bt[0] and 'TAG+="uaccess"' in bt[0] and 'MODE="0660"' in bt[0]
    # the HID parent of a Bluetooth device is named 0005:VID:PID.NNNN
    assert re.search(r'KERNELS=="0005:046D:\*"', bt[0])
