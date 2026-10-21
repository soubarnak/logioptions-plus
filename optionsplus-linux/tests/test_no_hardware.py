"""A spawned agent (or any child of the test run) must not reach the real mouse: with
OPL_NO_HARDWARE=1 it sees no hidraw node, cannot open one, and cannot open /dev/uinput.
The probes use paths that do not exist, so a regression of the guard can never open a device:
ENODEV means the guard refused, ENOENT means the open was attempted."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from opl_agent import hidpp

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
from opl_agent import hidpp, uinput
hidpp.list_receivers_and_devices = lambda *a, **k: ["a node"]   # stands for the sysfs scan
print("nodes", len(hidpp.HIDRAW.list_nodes()))
for name, fn in (("hidraw", lambda: hidpp.HIDRAW.open("/dev/hidraw-opl-probe")),
                 ("uinput", lambda: uinput.OsBackend().open("/dev/uinput-opl-probe"))):
    try:
        fn()
        print(name, "opened")
    except OSError as exc:
        print(name, "refused", exc.errno)
"""

GUARDED = ["nodes 0", "hidraw refused 19", "uinput refused 19"]


def probe(env):
    r = subprocess.run([sys.executable, "-c", PROBE], env=env, cwd=str(ROOT), capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return r.stdout.split("\n")[:-1]


def test_the_test_run_exports_the_guard_so_every_child_inherits_it():
    assert os.environ.get("OPL_NO_HARDWARE") == "1"


def test_a_child_inheriting_the_environment_sees_no_hidraw_and_opens_nothing():
    assert probe(dict(os.environ, PYTHONPATH=str(ROOT))) == GUARDED


def test_a_child_with_a_hand_built_env_still_gets_the_guard():
    assert probe({"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT)}) == GUARDED


@pytest.mark.parametrize("value", ["", "0"])
def test_by_default_hardware_is_on_and_the_open_is_attempted(value):
    env = dict(os.environ, PYTHONPATH=str(ROOT), OPL_NO_HARDWARE=value)
    assert probe(env) == ["nodes 1", "hidraw refused 2", "uinput refused 2"]


@pytest.mark.parametrize("value,guarded", [(None, False), ("", False), ("0", False), ("no", False),
                                           ("1", True), ("true", True), ("YES", True), ("On", True)])
def test_which_values_turn_the_guard_on(monkeypatch, value, guarded):
    if value is None:
        monkeypatch.delenv("OPL_NO_HARDWARE", raising=False)
    else:
        monkeypatch.setenv("OPL_NO_HARDWARE", value)
    assert hidpp.no_hardware() is guarded
