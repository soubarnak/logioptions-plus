"""SubprocessRunner.run_wait: bounded, and its output reaches the log."""

import logging
import sys

import pytest

from opl_agent.runner import SubprocessRunner


@pytest.fixture(autouse=True)
def tools_allowed(monkeypatch):
    monkeypatch.setenv("OPL_NO_HARDWARE", "0")   # these tests run harmless python children


def test_no_hardware_starts_no_tool_and_logs_it(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("OPL_NO_HARDWARE", "1")
    marker = tmp_path / "ran"
    argv = [sys.executable, "-c", "open(%r, 'w')" % str(marker)]
    runner = SubprocessRunner()
    with caplog.at_level(logging.INFO, logger="opl.runner"):
        runner.run(argv)
        assert runner.run_wait(argv) == -1
        assert runner.run_output(argv) == (-1, "")
        assert runner.which("sh") is None
    assert "not running" in caplog.text
    assert not marker.exists()


def test_run_wait_gives_up_on_a_program_that_never_ends(caplog):
    runner = SubprocessRunner()
    with caplog.at_level(logging.WARNING, logger="opl.runner"):
        code = runner.run_wait([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)
    assert code == -1
    assert "timed out" in caplog.text


def test_run_wait_logs_what_the_program_printed(caplog):
    runner = SubprocessRunner()
    with caplog.at_level(logging.INFO, logger="opl.runner"):
        code = runner.run_wait([sys.executable, "-c", "import sys; print('out-line'); print('err-line', file=sys.stderr); sys.exit(3)"])
    assert code == 3
    assert "out-line" in caplog.text and "err-line" in caplog.text
