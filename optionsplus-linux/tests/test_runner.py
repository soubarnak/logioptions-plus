"""SubprocessRunner.run_wait: bounded, and its output reaches the log."""

import logging
import sys

from opl_agent.runner import SubprocessRunner


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
