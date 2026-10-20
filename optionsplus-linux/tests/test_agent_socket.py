"""Seam: the agent process and its Unix socket (`python3 -m opl_agent.main`)."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_agent(sock_dir, *args):
    env = dict(os.environ, OPL_SOCKET=str(sock_dir / "agent.sock"),
               OPL_STATE_DIR=str(sock_dir / "state"), PYTHONPATH=str(ROOT))
    return subprocess.Popen([sys.executable, "-m", "opl_agent.main", *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_alive(path, secs=10):
    end = time.time() + secs
    while time.time() < end:
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(str(path))
            return True
        except OSError:
            time.sleep(0.1)
        finally:
            s.close()
    return False


def stop(p):
    p.terminate()
    p.wait(5)


def test_stale_socket_is_replaced(tmp_path):
    sock = tmp_path / "agent.sock"
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(sock))
    dead.close()  # file stays behind, nobody listens
    assert sock.exists()
    p = run_agent(tmp_path)
    try:
        assert wait_alive(sock)
    finally:
        stop(p)


def test_second_agent_leaves_live_one_alone(tmp_path):
    sock = tmp_path / "agent.sock"
    first = run_agent(tmp_path)
    try:
        assert wait_alive(sock)
        second = run_agent(tmp_path)
        assert second.wait(10) == 0
        assert wait_alive(sock)  # first still serves
        assert first.poll() is None
    finally:
        stop(first)


def test_is_running_flag_reports_state(tmp_path):
    assert run_agent(tmp_path, "--is-running").wait(10) == 1
    first = run_agent(tmp_path)
    try:
        assert wait_alive(tmp_path / "agent.sock")
        assert run_agent(tmp_path, "--is-running").wait(10) == 0
    finally:
        stop(first)
