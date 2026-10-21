"""A spawned agent (or any child of the test run) must not reach the real mouse: with
OPL_NO_HARDWARE=1 it sees no hidraw node, cannot open one, and cannot open /dev/uinput."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
from opl_agent import hidpp, uinput
print("nodes", len(hidpp.HIDRAW.list_nodes()))
for name, fn in (("hidraw", lambda: hidpp.HIDRAW.open("/dev/hidraw0")),
                 ("uinput", lambda: uinput.OsBackend().open(uinput.UINPUT_PATH))):
    try:
        fn()
        print(name, "opened")
    except OSError as exc:
        print(name, "refused", exc.errno)
"""


def test_the_test_run_exports_the_guard_so_every_child_inherits_it():
    assert os.environ.get("OPL_NO_HARDWARE") == "1"


def test_a_child_sees_no_hidraw_and_opens_neither_a_node_nor_uinput():
    env = dict(os.environ, PYTHONPATH=str(ROOT), OPL_NO_HARDWARE="1")
    r = subprocess.run([sys.executable, "-c", PROBE], env=env, cwd=str(ROOT), capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n")[:-1] == ["nodes 0", "hidraw refused 19", "uinput refused 19"]
