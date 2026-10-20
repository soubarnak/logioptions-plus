"""Seam: the `optionsplus-linux` launcher script, run with fake Electron and fake agent."""
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "optionsplus-linux"
AGENT_CMD = ROOT / "bin" / "optionsplus-agent"

FAKE_ELECTRON = """#!{py}
import json, os, sys
json.dump({{"argv": sys.argv, "env": dict(os.environ)}}, open(os.environ["FAKE_OUT"], "w"))
"""

FAKE_AGENT = """#!{py}
import os, socket, sys, time
path = os.environ["OPL_SOCKET"]
def alive():
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(path); return True
    except OSError:
        return False
    finally:
        s.close()
if "--is-running" in sys.argv:
    sys.exit(0 if alive() else 1)
open(os.environ["FAKE_AGENT_STARTS"], "a").write("start\\n")
if os.path.exists(path):
    os.unlink(path)
srv = socket.socket(socket.AF_UNIX)
srv.bind(path)
srv.listen(4)
time.sleep(30)
"""


def make_exe(path, body):
    path.write_text(body.format(py=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("electron35", "electron36", "electron9", "electron-bin"):
        make_exe(bindir / name, FAKE_ELECTRON)
    agent = tmp_path / "fake-agent"
    make_exe(agent, FAKE_AGENT)
    e = dict(os.environ,
             HOME=str(tmp_path / "home"),
             XDG_DATA_HOME=str(tmp_path / "data"),
             XDG_STATE_HOME=str(tmp_path / "state"),
             OPL_BIN_DIR=str(bindir),
             OPL_LIB_DIR=str(ROOT),
             OPL_AGENT_CMD=str(agent),
             OPL_SOCKET=str(tmp_path / "agent.sock"),
             FAKE_OUT=str(tmp_path / "out.json"),
             FAKE_AGENT_STARTS=str(tmp_path / "starts"))
    e.pop("OPL_NO_SANDBOX", None)
    for k in ("OPL_ASAR", "OPL_DATA_DIR", "OPL_DEPOT_DIR", "OPL_PAYLOAD_DIR"):
        e.pop(k, None)
    e["OPL_SYSTEM_PAYLOAD_DIR"] = str(tmp_path / "system")
    payload = tmp_path / "data" / "optionsplus-linux" / "payload"      # an installed payload
    payload.mkdir(parents=True)
    (payload / "app.asar").write_text("x")
    (payload / "payload.json").write_text("{}")
    yield e
    for pid in subprocess.run(["pgrep", "-f", str(agent)], capture_output=True, text=True).stdout.split():
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass


def launch(env, *args):
    r = subprocess.run([str(LAUNCHER), *args], env=env, capture_output=True, text=True, timeout=30)
    out = Path(env["FAKE_OUT"])
    return r, (json.loads(out.read_text()) if out.exists() else None)


def starts(env):
    p = Path(env["FAKE_AGENT_STARTS"])
    return p.read_text().count("start") if p.exists() else 0


def test_runs_newest_electron_3x_with_bootstrap(env):
    r, out = launch(env)
    assert r.returncode == 0, r.stderr
    assert out["argv"][0].endswith("/electron36")
    assert out["argv"][-1] == str(ROOT / "bootstrap")


def test_sandbox_is_on_by_default(env):
    _, out = launch(env)
    assert "--no-sandbox" not in out["argv"]


def test_documented_fallback_flag_disables_sandbox(env):
    env["OPL_NO_SANDBOX"] = "1"
    _, out = launch(env)
    assert "--no-sandbox" in out["argv"]


def test_exports_payload_paths_for_the_agent_and_ui(env):
    _, out = launch(env)
    payload = Path(env["XDG_DATA_HOME"]) / "optionsplus-linux" / "payload"
    assert out["env"]["OPL_ASAR"] == str(payload / "app.asar")
    assert out["env"]["OPL_DATA_DIR"] == str(payload / "depots" / "logioptionsplus" / "data")
    assert out["env"]["OPL_DEPOT_DIR"] == str(payload / "depots")


def test_explicit_payload_variables_win(env):
    env["OPL_ASAR"] = "/elsewhere/app.asar"
    _, out = launch(env)
    assert out["env"]["OPL_ASAR"] == "/elsewhere/app.asar"


def test_starts_agent_when_socket_absent(env):
    r, out = launch(env)
    assert out is not None, r.stderr
    assert starts(env) == 1
    assert Path(env["OPL_SOCKET"]).exists()


def test_replaces_stale_socket(env):
    sock = Path(env["OPL_SOCKET"])
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(sock))
    dead.close()
    r, out = launch(env)
    assert out is not None, r.stderr
    assert starts(env) == 1


def test_does_not_start_second_agent(env):
    launch(env)
    Path(env["FAKE_OUT"]).unlink()
    launch(env)
    assert starts(env) == 1


def test_passes_arguments_to_electron(env):
    _, out = launch(env, "--some-flag")
    assert out["argv"][-1] == "--some-flag"


def test_fails_clearly_without_electron(env, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    env["OPL_BIN_DIR"] = str(empty)
    r, out = launch(env)
    assert r.returncode != 0 and out is None
    assert "electron" in r.stderr.lower()


def test_agent_command_runs_module_from_lib_dir(tmp_path):
    e = dict(os.environ, OPL_LIB_DIR=str(ROOT), OPL_SOCKET=str(tmp_path / "none.sock"))
    r = subprocess.run([str(AGENT_CMD), "--is-running"], env=e, capture_output=True, timeout=20)
    assert r.returncode == 1
