"""Version, About and the GitHub update check, as the UI sees them."""

import json
import socket
import time

import pytest

from tests.harness import wait_for
from opl_agent import __version__

TYPE = "type.googleapis.com/"
TERMINAL = ("NO_UPDATES", "UPDATE_AVAILABLE", "UPDATER_ERROR")


def check(client):
    """Subscribe to status, run a check, return every state pushed until a terminal one."""
    wait_for(lambda: client.call("GET", "/updates/status")["payload"]["state"] in TERMINAL)
    client.subscribe("/updates/status")
    assert client.call("SET", "/updates/check_now")["result"]["code"] == "SUCCESS"
    return collect_until_terminal(client)


def collect_until_terminal(client):
    states = []
    while True:
        m = client.recv()
        if m["path"] == "/updates/status" and m["verb"] == "BROADCAST":
            states.append(m["payload"])
            if m["payload"]["state"] in TERMINAL:
                return states


# ---- version and About -----------------------------------------------------

def test_updates_info_reports_our_version_build_id_and_data_version(agent_factory, workdir):
    with open(workdir + "/payload.json", "w") as fh:
        json.dump({"version": "2.8.0-test"}, fh)
    running = agent_factory(build_id="abc1234")
    c = running.client()
    info = c.call("GET", "/updates/info")["payload"]
    assert info["@type"] == TYPE + "logi.protocol.updates.DepositoryInfo"
    assert info["version"] == "%s (Logitech data 2.8.0-test)" % __version__
    assert info["buildId"] == "abc1234"
    assert info["appId"] == "optionsplus-linux"
    assert info["branch"] == "stable"
    c.close()


def test_updates_info_without_installed_data_is_just_our_version(client):
    info = client.call("GET", "/updates/info")["payload"]
    assert info["version"] == __version__


def test_system_info_reports_os_name_and_version_from_os_release(client):
    info = client.call("GET", "/system/info")["payload"]
    assert info["@type"] == TYPE + "logi.protocol.api.system_info"
    assert info["osName"] == "Void Linux"
    assert info["osVersion"] == "rolling"


def test_system_info_prefers_version_id_when_the_distro_has_one(agent_factory, workdir):
    path = workdir + "/os-release-2"
    with open(path, "w") as fh:
        fh.write('PRETTY_NAME="Test OS 12"\nVERSION_ID="12"\nBUILD_ID="rolling"\n')
    running = agent_factory(os_release_path=path)
    c = running.client()
    info = c.call("GET", "/system/info")["payload"]
    assert (info["osName"], info["osVersion"]) == ("Test OS 12", "12")
    c.close()


def test_system_info_survives_a_missing_os_release(agent_factory, workdir):
    running = agent_factory(os_release_path=workdir + "/nope")
    c = running.client()
    info = c.call("GET", "/system/info")
    assert info["result"]["code"] == "SUCCESS"
    assert info["payload"]["osName"] == "Linux"
    c.close()
