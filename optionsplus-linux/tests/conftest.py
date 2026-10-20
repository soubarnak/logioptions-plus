import os
import shutil
import socket

import pytest

from opl_agent import hidpp
from tests.fakes.depot import build_depot
from tests.fakes.clock import FakeClock
from tests.fakes.hidpp_device import SimulatedBluetoothMouse, SimulatedBolt
from tests.fakes.releases import FakeReleaseServer
from tests.harness import RunningAgent, short_tmpdir

# Only loopback is reachable from tests (the fake release server lives there).
LOOPBACK = ("127.0.0.1", "::1", "localhost")


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    """Tests need no network, hidraw, uinput or display: fail loudly if touched."""
    real_socket = socket.socket

    class GuardedSocket(real_socket):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in LOOPBACK:
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
def ble():
    return SimulatedBluetoothMouse()


@pytest.fixture
def releases():
    server = FakeReleaseServer()
    server.publish("0.1.0")
    yield server
    server.close()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def os_release(workdir):
    path = os.path.join(workdir, "os-release")
    with open(path, "w") as fh:
        fh.write('NAME="Void"\nPRETTY_NAME="Void Linux"\nID="void"\nBUILD_ID="rolling"\n')
    return path


@pytest.fixture
def agent_factory(workdir, bolt, releases, clock, os_release):
    """Start agents on the temp socket/state/depot; every agent is stopped at teardown."""
    depot = build_depot(os.path.join(workdir, "depots"))
    started = []

    def make(**overrides):
        kw = dict(transport=bolt, socket_path=os.path.join(workdir, "s"),
                  state_dir=os.path.join(workdir, "state"), depot_dir=depot,
                  app_dirs=[os.path.join(workdir, "applications")],
                  release_base_url=releases.base_url, clock=clock,
                  os_release_path=os_release,
                  payload_path=os.path.join(workdir, "payload.json"))
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
