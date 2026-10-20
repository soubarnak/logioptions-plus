"""Install, restart and data refresh: the update page's buttons, against a fake privileged runner."""

import threading

import pytest

from tests.fakes.runner import FakeRunner
from tests.test_updates import TERMINAL, check, wait_for

SUDO_COMMAND = "sudo xbps-install -Su optionsplus-linux"
INSTALL_ARGV = ["pkexec", "xbps-install", "-Syu", "optionsplus-linux"]
DONE = ("RESTARTING", "UPDATER_ERROR")


@pytest.fixture
def runner():
    return FakeRunner(installed=("pkexec",))


class Pair:
    """A commanding client plus a second one that only listens (replies would eat broadcasts)."""

    def __init__(self, running):
        self.cmd, self.listener = running.client(), running.client()
        self.listener.subscribe("/updates/status")
        self.listener.subscribe("/updates/payload")

    def call(self, *a):
        return self.cmd.call(*a)

    def status(self):
        return self.cmd.call("GET", "/updates/status")["payload"]

    def close(self):
        self.cmd.close()
        self.listener.close()


def available(agent_factory, releases, **kw):
    """A pair on an agent that has found a newer release."""
    releases.publish("9.0.0")
    running = agent_factory(**kw)
    c = running.client()
    assert check(c)[-1]["state"] == "UPDATE_AVAILABLE"
    c.close()
    return Pair(running)


def states_until(pair, stop):
    seen = []
    while True:
        m = pair.listener.recv()
        if m["path"] == "/updates/status" and m["verb"] == "BROADCAST":
            seen.append(m["payload"])
            if m["payload"]["state"] in stop:
                return seen


# ---- download --------------------------------------------------------------

def test_download_is_ready_at_once_and_downloads_nothing(agent_factory, releases, runner):
    c = available(agent_factory, releases, runner=runner)
    assert c.call("SET", "/updates/download")["result"]["code"] == "SUCCESS"
    assert states_until(c, ("UPDATE_READY",))[-1]["state"] == "UPDATE_READY"
    assert runner.executed == [] and runner.launched == []
    c.close()


def test_download_without_an_update_is_refused(client):
    assert client.call("SET", "/updates/download")["result"]["code"] != "SUCCESS"


# ---- install ---------------------------------------------------------------

def test_install_runs_pkexec_xbps_and_ends_in_restarting(agent_factory, releases, runner):
    c = available(agent_factory, releases, runner=runner)
    assert c.call("SET", "/updates/install")["result"]["code"] == "SUCCESS"
    seen = states_until(c, DONE)
    assert [s["state"] for s in seen] == ["UPDATE_INSTALLING", "RESTARTING"]
    assert runner.executed == [INSTALL_ARGV]
    c.close()


def test_install_after_download_works_the_same(agent_factory, releases, runner):
    c = available(agent_factory, releases, runner=runner)
    c.call("SET", "/updates/download")
    states_until(c, ("UPDATE_READY",))
    c.call("SET", "/updates/install")
    assert states_until(c, DONE)[-1]["state"] == "RESTARTING"
    assert runner.executed == [INSTALL_ARGV]
    c.close()


def test_failed_install_reports_the_exit_status_and_the_manual_command(agent_factory, releases):
    runner = FakeRunner(installed=("pkexec",), exit_codes={"pkexec": 1})
    c = available(agent_factory, releases, runner=runner)
    c.call("SET", "/updates/install")
    last = states_until(c, DONE)[-1]
    assert last["state"] == "UPDATER_ERROR"
    assert "exit status 1" in last["errorMessage"]
    assert SUDO_COMMAND in last["errorMessage"]
    c.close()


@pytest.mark.parametrize("code", [126, 127])
def test_dismissed_authentication_is_reported_as_such(agent_factory, releases, code):
    runner = FakeRunner(installed=("pkexec",), exit_codes={"pkexec": code})
    c = available(agent_factory, releases, runner=runner)
    c.call("SET", "/updates/install")
    last = states_until(c, DONE)[-1]
    assert last["state"] == "UPDATER_ERROR"
    assert "not authorized" in last["errorMessage"]
    c.close()


def test_without_pkexec_the_error_carries_the_exact_sudo_command(agent_factory, releases):
    runner = FakeRunner(installed=())
    c = available(agent_factory, releases, runner=runner)
    c.call("SET", "/updates/install")
    last = states_until(c, DONE)[-1]
    assert last["state"] == "UPDATER_ERROR"
    assert SUDO_COMMAND in last["errorMessage"]
    assert runner.executed == []
    assert c.status()["errorMessage"] == last["errorMessage"]
    c.close()


def test_install_with_nothing_to_install_runs_nothing(agent_factory, runner):
    c = agent_factory(runner=runner).client()
    wait_for(lambda: c.call("GET", "/updates/status")["payload"]["state"] in TERMINAL)
    assert c.call("SET", "/updates/install")["result"]["code"] != "SUCCESS"
    assert runner.executed == []
    c.close()


def test_a_second_install_while_one_runs_is_refused(agent_factory, releases):
    gate = threading.Event()
    runner = FakeRunner(installed=("pkexec",), effects={"pkexec": lambda: gate.wait(5)})
    c = available(agent_factory, releases, runner=runner)
    c.call("SET", "/updates/install")
    wait_for(lambda: c.status()["state"] == "UPDATE_INSTALLING")
    assert c.call("SET", "/updates/install")["result"]["code"] != "SUCCESS"
    gate.set()
    wait_for(lambda: c.status()["state"] == "RESTARTING")
    assert runner.executed == [INSTALL_ARGV]
    c.close()
