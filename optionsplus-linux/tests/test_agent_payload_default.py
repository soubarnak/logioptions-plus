"""Seam: the agent finds the fetched payload with no OPL_* variables set (autostart case)."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = "from opl_agent import resources; print(resources.DEPOT_DIR); print(resources.DATA_DIR)"


def depot_dirs(env):
    clean = dict(env)
    clean["PYTHONPATH"] = str(ROOT)
    r = subprocess.run([sys.executable, "-c", PROBE], env=clean, capture_output=True, text=True,
                       cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def install(payload):
    (payload / "depots" / "logioptionsplus" / "data").mkdir(parents=True)
    (payload / "app.asar").write_text("x")
    (payload / "payload.json").write_text("{}")


def test_agent_uses_the_users_fetched_payload(tmp_path):
    payload = tmp_path / "xdg" / "optionsplus-linux" / "payload"
    install(payload)
    depot, data = depot_dirs({"XDG_DATA_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "h"),
                              "PATH": os.environ["PATH"]})
    assert depot == str(payload / "depots")
    assert data == str(payload / "depots" / "logioptionsplus" / "data")


def test_agent_falls_back_to_the_system_wide_payload(tmp_path):
    system = tmp_path / "sys"
    install(system)
    depot, _ = depot_dirs({"XDG_DATA_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "h"),
                           "OPL_SYSTEM_PAYLOAD_DIR": str(system)})
    assert depot == str(system / "depots")
