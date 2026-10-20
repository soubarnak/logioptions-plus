"""The update check itself: GitHub redirect for stable, Atom feed for testing."""

import socket

from opl_agent import __version__
from tests.test_updates import TYPE, check


def test_newer_stable_tag_gives_update_available(client, releases):
    releases.publish("9.0.0")
    states = check(client)
    assert [s["state"] for s in states] == ["CHECKING_FOR_UPDATES", "UPDATE_AVAILABLE"]
    assert states[-1]["@type"] == TYPE + "logi.protocol.updates.Status"


def test_same_tag_gives_no_updates(client, releases):
    releases.publish(__version__)
    assert [s["state"] for s in check(client)] == ["CHECKING_FOR_UPDATES", "NO_UPDATES"]


def test_older_tag_gives_no_updates(client, releases):
    releases.stable_tag = "optionsplus-linux-v0.0.1"
    assert check(client)[-1]["state"] == "NO_UPDATES"


def test_ten_is_newer_than_nine(client, releases):
    releases.publish("0.10.0")
    assert check(client)[-1]["state"] == "UPDATE_AVAILABLE"


def test_status_is_readable_after_the_check(client, releases):
    releases.publish("9.0.0")
    check(client)
    assert client.call("GET", "/updates/status")["payload"]["state"] == "UPDATE_AVAILABLE"


def test_offline_gives_updater_error_with_a_message(agent_factory):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                   # nothing listens here any more
    c = agent_factory(release_base_url="http://127.0.0.1:%d/releases" % port).client()
    last = check(c)[-1]
    assert last["state"] == "UPDATER_ERROR"
    assert last["errorMessage"]
    c.close()


def test_unrecognised_release_tag_is_an_error_not_a_hang(client, releases):
    releases.stable_tag = "something-else-v1"
    last = check(client)[-1]
    assert last["state"] == "UPDATER_ERROR" and last["errorMessage"]


def test_no_releases_yet_is_an_error_with_a_message(client, releases):
    releases.stable_tag = None
    last = check(client)[-1]
    assert last["state"] == "UPDATER_ERROR" and last["errorMessage"]


def test_check_sends_one_bare_head_request_and_follows_no_redirect(client, releases):
    del releases.requests[:]
    check(client)
    assert [(m, p) for m, p, _ in releases.requests] == [("HEAD", "/releases/latest")]
    for _, _, headers in releases.requests:
        assert not {"Cookie", "Authorization"} & set(headers)
