"""Restart after an update."""

from tests.fakes.runner import FakeRunner
from tests.test_updates import wait_for


# ---- restart ---------------------------------------------------------------

def test_restart_now_hands_over_to_the_launcher_and_the_agent_leaves(agent_factory):
    runner = FakeRunner(installed=("optionsplus-linux",))
    running = agent_factory(runner=runner)
    c = running.client()
    assert c.call("SET", "/updates/restart_now")["result"]["code"] == "SUCCESS"
    [argv] = runner.launched
    assert argv[0] == "env" and argv[2:] == ["optionsplus-linux"]
    assert argv[1].startswith("OPL_RESTART_WAIT_PID=")
    wait_for(lambda: running.agent.restart_requested.is_set())
    c.close()


def test_restart_without_a_launcher_on_path_fails_and_keeps_the_agent(agent_factory):
    runner = FakeRunner(installed=())
    running = agent_factory(runner=runner)
    c = running.client()
    assert c.call("SET", "/updates/restart_now")["result"]["code"] != "SUCCESS"
    assert runner.launched == [] and not running.agent.restart_requested.is_set()
    c.close()
