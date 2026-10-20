"""Leaving cleanly: whatever the agent diverted goes back to the mouse, on a normal stop, on
SIGTERM / SIGHUP, and nothing is reopened once the registry is closed."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.test_button_remap import BACK, COPY, assign
from tests.test_gestures import assign_thumb
from tests.test_wheel_relay import FAST, TARGET_BIT, scroll, thumb

ROOT = Path(__file__).resolve().parents[1]


def test_a_clean_stop_hands_back_every_button_and_the_thumb_wheel(agent, client, bolt):
    assert assign(client, BACK, COPY)["result"]["code"] == "SUCCESS"
    assert assign_thumb(client)["result"]["code"] == "SUCCESS"
    scroll(client, speed=FAST)
    assert bolt.mouse.diverted == {BACK} and bolt.mouse.thumb["mode"] == 1
    client.close()
    agent.stop()
    assert bolt.mouse.diverted == set()
    assert bolt.mouse.thumb["mode"] == 0
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0


def test_a_stop_with_the_thumb_wheel_on_the_speed_relay_hands_it_back_too(agent, client, bolt):
    thumb(client, speed=FAST)
    assert bolt.mouse.thumb["mode"] == 1
    client.close()
    agent.stop()
    assert bolt.mouse.thumb["mode"] == 0


def test_a_closed_registry_opens_no_hidraw_again(agent, client, bolt):
    reg = agent.agent.reg
    client.close()
    agent.stop()
    opened = []
    real_open = bolt.open
    bolt.open = lambda path: opened.append(path) or real_open(path)
    assert reg.rescan(force=True)["added"] == []
    assert opened == []
    assert reg._fds == {}


_SCRIPT = textwrap.dedent("""
    import asyncio, os, sys, tempfile
    from opl_agent import main
    from tests.fakes.hidpp_device import SimulatedBolt
    from tests.fakes.uinput import FakeInput

    async def go():
        bolt, work = SimulatedBolt(), tempfile.mkdtemp(prefix="opl")
        agent = main.Agent(transport=bolt, socket_path=os.path.join(work, "s"),
                           state_dir=os.path.join(work, "state"), input_factory=FakeInput().factory,
                           window_source=None, depot_dir=sys.argv[1])
        await agent.start()
        dev = next(iter(agent.reg.devices.values()))
        agent.router.relay.set_wheel(dev, speed=1.0)
        assert bolt.mouse.wheel_mode & 1
        await main.run(agent, ready=lambda: print("READY", flush=True))
        print("NATIVE" if not bolt.mouse.wheel_mode & 1 else "DIVERTED", flush=True)

    asyncio.run(go())
""")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_termination_signal_stops_gracefully_and_hands_the_wheels_back(sig, workdir):
    from tests.fakes.depot import build_depot
    depot = build_depot(os.path.join(workdir, "depots"))
    env = dict(os.environ, PYTHONPATH=str(ROOT), OPL_SOCKET=os.path.join(workdir, "x"),
               XDG_STATE_HOME=os.path.join(workdir, "xdg"))
    p = subprocess.Popen([sys.executable, "-c", _SCRIPT, depot], cwd=str(ROOT), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert p.stdout.readline().strip() == "READY"
        p.send_signal(sig)
        out, _ = p.communicate(timeout=20)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, out
    assert "NATIVE" in out


def test_no_profile_push_is_scheduled_once_the_router_is_stopping(caplog, bolt):
    import asyncio
    import logging
    import threading
    from opl_agent import model
    from opl_agent.router import Router

    class Server:
        def broadcast(self, path, payload=None):
            pass

    class Quiet(Router):
        sent = 0

        async def _broadcast_profiles(self, removed=()):
            await asyncio.sleep(30)          # a push still in flight when the agent stops

    async def run():
        router = Quiet(model.Registry(transport=bolt))
        router.loop, router.server = asyncio.get_running_loop(), Server()
        router._broadcast_after_reply()
        threading.Thread(target=router._schedule_broadcast).start()
        await asyncio.sleep(0.2)
        queued, threaded = set(router._pushes), set(router._push_futures)
        assert len(queued) == 1 and len(threaded) == 1
        router.cancel_pushes()
        # nothing is scheduled any more, from the loop or from a worker thread
        router._broadcast_after_reply()
        t = threading.Thread(target=router._schedule_broadcast)
        t.start()
        t.join()
        await asyncio.sleep(0.2)
        assert not router._pushes and not router._push_futures
        assert all(f.cancelled() for f in threaded)
        assert all(f.cancelled() for f in queued)

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert "profile push failed" not in caplog.text
