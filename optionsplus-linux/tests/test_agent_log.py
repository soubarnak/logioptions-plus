"""The agent keeps its own rotating log; the launcher no longer has to redirect into it."""

import logging
import logging.handlers

import pytest

from opl_agent import main


@pytest.fixture
def clean_root_logger():
    root = logging.getLogger()
    saved = list(root.handlers)
    yield root
    for h in list(root.handlers):
        if h not in saved:
            root.removeHandler(h)
            h.close()


def test_log_path_follows_xdg_state_home(tmp_path):
    assert main.log_file_path({"XDG_STATE_HOME": str(tmp_path)}) == str(tmp_path / "optionsplus-linux" / "agent.log")
    assert main.log_file_path({"HOME": "/home/u"}) == "/home/u/.local/state/optionsplus-linux/agent.log"


def test_setup_logging_writes_a_rotating_file(tmp_path, clean_root_logger):
    path = main.setup_logging({"XDG_STATE_HOME": str(tmp_path)})
    logging.getLogger("opl.test").warning("hello from the agent")
    for h in clean_root_logger.handlers:
        h.flush()
    assert "hello from the agent" in open(path).read()
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) and h.maxBytes
               for h in clean_root_logger.handlers)


def test_unwritable_state_dir_does_not_stop_the_agent(tmp_path, clean_root_logger):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert main.setup_logging({"XDG_STATE_HOME": str(blocker)}) is None
