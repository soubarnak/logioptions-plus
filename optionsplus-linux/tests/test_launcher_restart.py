"""Seam: the launcher re-executed after an update waits for the old agent to exit first."""
import subprocess
import time

from tests.test_launcher import env, launch, starts  # noqa: F401  (env is a fixture)


def test_waits_for_the_old_agent_to_exit_before_starting(env):  # noqa: F811
    old = subprocess.Popen(["sleep", "1.5"])
    env["OPL_RESTART_WAIT_PID"] = str(old.pid)
    t0 = time.monotonic()
    r, out = launch(env)
    elapsed = time.monotonic() - t0
    old.wait()
    assert r.returncode == 0, r.stderr
    assert elapsed >= 1.2
    assert out is not None and starts(env) == 1


def test_a_pid_that_is_already_gone_does_not_delay_the_start(env):  # noqa: F811
    gone = subprocess.Popen(["true"])
    gone.wait()
    env["OPL_RESTART_WAIT_PID"] = str(gone.pid)
    t0 = time.monotonic()
    r, out = launch(env)
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - t0 < 1.0
    assert out is not None


def test_a_wait_pid_that_never_exits_gives_up_instead_of_hanging(env):  # noqa: F811
    old = subprocess.Popen(["sleep", "30"])
    env["OPL_RESTART_WAIT_PID"] = str(old.pid)
    env["OPL_RESTART_WAIT_SECONDS"] = "1"
    try:
        r, out = launch(env)
    finally:
        old.kill()
        old.wait()
    assert r.returncode == 0, r.stderr
    assert out is not None


def test_a_non_numeric_wait_pid_is_ignored(env):  # noqa: F811
    env["OPL_RESTART_WAIT_PID"] = "not-a-pid; touch /nonexistent/x"
    r, out = launch(env)
    assert r.returncode == 0, r.stderr
    assert out is not None
