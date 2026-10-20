"""Periodic checks (driven by the injected clock) and the release channel."""

import time

from opl_agent import __version__
from tests.harness import wait_for
from tests.test_updates import TYPE, check


def settle(client):
    """Wait until the check the agent runs at start has finished."""
    wait_for(lambda: client.call("GET", "/updates/status")["payload"]["state"]
             in ("NO_UPDATES", "UPDATE_AVAILABLE", "UPDATER_ERROR"))


def periodic(client):
    return client.call("GET", "/updates/periodic_check")["payload"]


def set_periodic(client, **fields):
    return client.call("SET", "/updates/periodic_check", dict(fields))


def quiet(releases, seconds=0.4):
    """True if no request reaches the release server for `seconds`."""
    n = len(releases.requests)
    time.sleep(seconds)
    return len(releases.requests) == n


# ---- periodic check --------------------------------------------------------

def test_periodic_check_defaults_to_on_daily(client):
    p = periodic(client)
    assert p["@type"] == TYPE + "logi.protocol.updates.PeriodicCheck"
    assert p["checkPeriodically"] is True
    assert p["intervalInSeconds"] == 86400


def test_agent_checks_at_start(client, releases):
    wait_for(lambda: len(releases.requests) >= 1)
    settle(client)


def test_clock_drives_the_periodic_check(client, releases, clock):
    settle(client)
    wait_for(lambda: clock.sleeping >= 1)
    del releases.requests[:]
    clock.advance(86400)
    wait_for(lambda: len(releases.requests) >= 1)


def test_clock_not_yet_due_does_nothing(client, releases, clock):
    settle(client)
    wait_for(lambda: clock.sleeping >= 1)
    del releases.requests[:]
    clock.advance(86399)
    assert quiet(releases)


def test_interval_setting_changes_the_schedule(client, releases, clock):
    settle(client)
    assert set_periodic(client, intervalInSeconds=600)["result"]["code"] == "SUCCESS"
    assert periodic(client)["intervalInSeconds"] == 600
    wait_for(lambda: clock.sleeping >= 1)
    del releases.requests[:]
    clock.advance(600)
    wait_for(lambda: len(releases.requests) >= 1)


def test_turning_it_off_stops_the_scheduled_checks(client, releases, clock):
    settle(client)
    set_periodic(client, checkPeriodically=False)
    assert periodic(client)["checkPeriodically"] is False
    del releases.requests[:]
    clock.advance(10 * 86400)
    assert quiet(releases)


def test_turning_it_back_on_resumes_checks(client, releases, clock):
    settle(client)
    set_periodic(client, checkPeriodically=False)
    set_periodic(client, checkPeriodically=True)
    wait_for(lambda: clock.sleeping >= 1)
    del releases.requests[:]
    clock.advance(86400)
    wait_for(lambda: len(releases.requests) >= 1)


def test_invalid_periodic_values_are_rejected(client):
    for bad in ({"intervalInSeconds": 0}, {"intervalInSeconds": -5},
                {"intervalInSeconds": "soon"}, {"checkPeriodically": "yes"}):
        assert set_periodic(client, **bad)["result"]["code"] == "INVALID_ARG"
    assert periodic(client)["intervalInSeconds"] == 86400


def test_periodic_setting_persists_across_agent_restarts(agent_factory, releases, clock):
    first = agent_factory()
    c = first.client()
    set_periodic(c, checkPeriodically=False, intervalInSeconds=7200)
    c.close()
    first.stop()
    del releases.requests[:]
    second = agent_factory()
    c = second.client()
    assert periodic(c)["checkPeriodically"] is False
    assert periodic(c)["intervalInSeconds"] == 7200
    assert quiet(releases)                      # off means no check at start either
    c.close()


# ---- channel ---------------------------------------------------------------

def test_channel_defaults_to_stable_on_our_releases(client, releases):
    ch = client.call("GET", "/updates/channel")["payload"]
    assert ch["@type"] == TYPE + "logi.protocol.updates.Channel"
    assert ch["name"] == "stable"
    assert ch["pipelineHost"] == releases.base_url


def test_testing_channel_is_unsupported_and_says_so(client):
    reply = client.call("SET", "/updates/channel", {"name": "testing"})
    assert reply["result"]["code"] == "NOT_SUPPORTED" and reply["result"]["what"]
    assert client.call("GET", "/updates/channel")["payload"]["name"] == "stable"
    for bad in ("nightly", "", None, 3):
        assert client.call("SET", "/updates/channel", {"name": bad})["result"]["code"] == "INVALID_ARG"
    assert client.call("SET", "/updates/channel", {"name": "stable"})["result"]["code"] == "SUCCESS"


def test_channel_cannot_redirect_the_check_to_another_host(client, releases):
    client.call("SET", "/updates/channel", {"name": "stable", "pipelineHost": "http://evil.invalid"})
    assert client.call("GET", "/updates/channel")["payload"]["pipelineHost"] == releases.base_url


def test_a_pre_release_is_never_offered(client, releases):
    settle(client)
    releases.publish("9.0.0-rc1", prerelease=True)
    assert check(client)[-1]["state"] == "NO_UPDATES"


def test_a_saved_testing_channel_from_an_older_build_reads_as_stable(agent_factory, workdir):
    import json, os
    os.makedirs(workdir + "/state", exist_ok=True)
    with open(workdir + "/state/updates.json", "w") as fh:
        json.dump({"channel": "testing"}, fh)
    c = agent_factory().client()
    assert c.call("GET", "/updates/channel")["payload"]["name"] == "stable"
    c.close()
