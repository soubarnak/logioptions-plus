import os
import shutil
import socket

import pytest

from opl_agent import hidpp
from tests.fakes.depot import build_depot
from tests.fakes.hidpp_device import SimulatedBolt
from tests.harness import RunningAgent, short_tmpdir


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    """Tests need no network, hidraw, uinput or display: fail loudly if touched."""
    real_socket = socket.socket

    class GuardedSocket(real_socket):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError("test attempted network access: %r" % (address,))
            return super().connect(address)

    monkeypatch.setattr(socket, "socket", GuardedSocket)

    def no_hardware(*_a, **_k):
        raise AssertionError("test touched the real hidraw transport")
    monkeypatch.setattr(hidpp.HIDRAW, "list_nodes", no_hardware)
    monkeypatch.setattr(hidpp.HIDRAW, "open", no_hardware)
    for var in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("OPL_REPLAY", raising=False)


@pytest.fixture
def workdir():
    d = short_tmpdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def bolt():
    return SimulatedBolt()


@pytest.fixture
def agent_factory(workdir, bolt):
    """Start agents on the temp socket/state/depot; every agent is stopped at teardown."""
    depot = build_depot(os.path.join(workdir, "depots"))
    started = []

    def make(**overrides):
        kw = dict(transport=bolt, socket_path=os.path.join(workdir, "s"),
                  state_dir=os.path.join(workdir, "state"), depot_dir=depot)
        kw.update(overrides)
        running = RunningAgent(**kw).start()
        started.append(running)
        return running

    yield make
    for r in started:
        try:
            r.stop()
        except Exception:
            pass


@pytest.fixture
def agent(agent_factory):
    return agent_factory()


@pytest.fixture
def client(agent):
    c = agent.client()
    yield c
    c.close()
