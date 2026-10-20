"""Update flow edge cases: restart with a UI attached, install state, honest routes."""

import asyncio
import concurrent.futures
import threading

import pytest

from opl_agent import updates
from tests.fakes.runner import FakeRunner
from tests.harness import wait_for
from tests.test_updates import check


def test_restart_now_tells_the_ui_to_quit_and_the_agent_leaves_despite_the_open_client(agent_factory):
    runner = FakeRunner(installed=("optionsplus-linux",))
    running = agent_factory(runner=runner)
    listener, c = running.client(), running.client()
    listener.subscribe("/updates/restart_incoming")
    serving = asyncio.run_coroutine_threadsafe(running.agent.serve_forever(), running._loop)
    assert c.call("SET", "/updates/restart_now")["result"]["code"] == "SUCCESS"
    m = listener.recv()
    assert (m["verb"], m["path"]) == ("BROADCAST", "/updates/restart_incoming")
    try:
        serving.result(5)               # the attached UI must not hold the old agent alive
    except concurrent.futures.TimeoutError:
        pytest.fail("serve_forever hangs while a client is connected")
    c.close()
    listener.close()


def test_stop_does_not_wait_for_connected_clients(agent_factory):
    running = agent_factory()
    c = running.client()
    try:
        asyncio.run_coroutine_threadsafe(running.agent.stop(), running._loop).result(5)
    except concurrent.futures.TimeoutError:
        pytest.fail("stop() hangs while a client is connected")
    c.close()
    running._stopped = True


def _status(c):
    return c.call("GET", "/updates/status")["payload"]["state"]


def test_a_check_during_install_leaves_the_install_state_alone(agent_factory, releases):
    gate = threading.Event()
    runner = FakeRunner(installed=("pkexec",), effects={"pkexec": lambda: gate.wait(5)})
    releases.publish("9.0.0")
    running = agent_factory(runner=runner)
    c = running.client()
    assert check(c)[-1]["state"] == "UPDATE_AVAILABLE"
    c.call("SET", "/updates/install")
    wait_for(lambda: _status(c) == "UPDATE_INSTALLING")
    asyncio.run_coroutine_threadsafe(running.agent.router.updater.check(), running._loop).result(5)
    assert _status(c) == "UPDATE_INSTALLING"
    gate.set()
    wait_for(lambda: _status(c) == "RESTARTING")
    asyncio.run_coroutine_threadsafe(running.agent.router.updater.check(), running._loop).result(5)
    assert _status(c) == "RESTARTING"
    c.close()


def test_install_uses_an_absolute_xbps_path_under_pkexec():
    assert updates.INSTALL_ARGV[:2] == ["pkexec", "/usr/bin/xbps-install"]


@pytest.mark.parametrize("path", ["/updates/payload", "/updates/payload/download"])
def test_invented_payload_routes_are_gone(client, path):
    for verb in ("GET", "SET"):
        assert client.call(verb, path)["result"]["code"] != "SUCCESS"
