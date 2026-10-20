"""Restart after an update, and the offer to refresh Logitech's data when the package pins newer."""

import json

import pytest

from tests.fakes.runner import FakeRunner
from tests.test_updates import wait_for


# ---- restart ---------------------------------------------------------------

def test_restart_now_hands_over_to_the_launcher_and_the_agent_leaves(agent_factory):
    runner = FakeRunner(installed=("optionsplus-linux",))
    running = agent_factory(runner=runner)
    c = running.client()
    assert c.call("SET", "/updates/restart_now")["result"]["code"] == "SUCCESS"
    [argv] = runner.launched
    assert argv[0] == "env" and argv[2:] == ["optionsplus-linux"]
    assert argv[1].startswith("OPL_RESTART_WAIT_PID=")
    wait_for(lambda: running.agent.restart_requested.is_set())
    c.close()


def test_restart_without_a_launcher_on_path_fails_and_keeps_the_agent(agent_factory):
    runner = FakeRunner(installed=())
    running = agent_factory(runner=runner)
    c = running.client()
    assert c.call("SET", "/updates/restart_now")["result"]["code"] != "SUCCESS"
    assert runner.launched == [] and not running.agent.restart_requested.is_set()
    c.close()


# ---- Logitech data refresh -------------------------------------------------

def write_manifest(workdir, version, sha="aa"):
    path = workdir + "/depots.json"
    with open(path, "w") as fh:
        json.dump({"dataVersion": version, "eulaUrl": "https://example.invalid/eula",
                   "depots": [{"name": "logioptionsplus", "url": "https://example.invalid/x.depot",
                               "size": 1, "sha256": sha}]}, fh)
    return path


def write_record(workdir, version, sha="aa"):
    with open(workdir + "/payload.json", "w") as fh:
        json.dump({"schema": 1, "dataVersion": version,
                   "depots": {"logioptionsplus": {"sha256": sha}}}, fh)


def payload_status(c):
    return c.call("GET", "/updates/payload")["payload"]


@pytest.mark.parametrize("pinned, recorded, sha, offered", [
    ("2.9.0", "2.8.0", "aa", True),          # package pins newer data
    ("2.10.0", "2.9.0", "aa", True),         # numeric, not lexical
    ("2.8.0", "2.8.0", "aa", False),         # same
    ("2.8.0", "2.9.0", "aa", False),         # user already has newer
    ("2.8.0", "2.8.0", "bb", True),          # same version, republished depot
])
def test_newer_pinned_data_is_offered(agent_factory, workdir, pinned, recorded, sha, offered):
    write_record(workdir, recorded)
    c = agent_factory(manifest_path=write_manifest(workdir, pinned, sha)).client()
    st = payload_status(c)
    assert st["available"] is offered
    assert (st["installedVersion"], st["pinnedVersion"]) == (recorded, pinned)
    c.close()


def test_nothing_is_offered_when_no_data_is_installed_yet(agent_factory, workdir):
    c = agent_factory(manifest_path=write_manifest(workdir, "2.9.0")).client()
    assert payload_status(c)["available"] is False      # first run belongs to the launcher
    c.close()


def test_data_version_in_updates_info_reads_the_fetchers_record(agent_factory, workdir):
    write_record(workdir, "2.8.0")
    c = agent_factory().client()
    assert c.call("GET", "/updates/info")["payload"]["version"].endswith("(Logitech data 2.8.0)")
    c.close()


def test_download_data_runs_the_fetcher_and_clears_the_offer(agent_factory, workdir):
    write_record(workdir, "2.8.0")
    runner = FakeRunner(effects={"optionsplus-fetch-payload": lambda: write_record(workdir, "2.9.0")})
    running = agent_factory(runner=runner, manifest_path=write_manifest(workdir, "2.9.0"))
    c, listener = running.client(), running.client()
    listener.subscribe("/updates/payload")
    assert c.call("SET", "/updates/payload/download")["result"]["code"] == "SUCCESS"
    while True:
        m = listener.recv()
        if m["path"] == "/updates/payload" and not m["payload"]["downloading"]:
            break
    assert m["payload"]["available"] is False and "errorMessage" not in m["payload"]
    assert runner.executed == [["optionsplus-fetch-payload"]]
    c.close()
    listener.close()


def test_declined_eula_keeps_the_offer_without_an_error(agent_factory, workdir):
    write_record(workdir, "2.8.0")
    runner = FakeRunner(exit_codes={"optionsplus-fetch-payload": 2})
    c = agent_factory(runner=runner, manifest_path=write_manifest(workdir, "2.9.0")).client()
    c.call("SET", "/updates/payload/download")
    wait_for(lambda: len(runner.executed) == 1 and not payload_status(c)["downloading"])
    st = payload_status(c)
    assert st["available"] is True and "errorMessage" not in st
    c.close()


def test_failed_fetch_reports_an_error(agent_factory, workdir):
    write_record(workdir, "2.8.0")
    runner = FakeRunner(exit_codes={"optionsplus-fetch-payload": 1})
    c = agent_factory(runner=runner, manifest_path=write_manifest(workdir, "2.9.0")).client()
    c.call("SET", "/updates/payload/download")
    wait_for(lambda: "errorMessage" in payload_status(c))
    assert "exit status 1" in payload_status(c)["errorMessage"]
    c.close()


def test_download_data_with_nothing_newer_runs_nothing(agent_factory, workdir):
    write_record(workdir, "2.9.0")
    runner = FakeRunner()
    c = agent_factory(runner=runner, manifest_path=write_manifest(workdir, "2.9.0")).client()
    assert c.call("SET", "/updates/payload/download")["result"]["code"] != "SUCCESS"
    assert runner.executed == []
    c.close()
