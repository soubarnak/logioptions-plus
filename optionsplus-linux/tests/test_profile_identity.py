"""Which application a profile is for: the UI's own profile ids, unknown ids, creating and removing
profiles through SET /v2/profiles, and a switch that fails not leaving a half-saved choice."""

from tests.test_app_profiles import (FIREFOX, RELOAD, assign_to, card_of, client, install_apps,  # noqa: F401
                                     profile, settled, tracked, windows)
from tests.test_button_remap import BACK
from tests.harness import wait_for

UUID = "9f1c2a54-7a43-4d3e-8b19-0a5f6c3d7e21"
UPDATE = "type.googleapis.com/logi.protocol.profiles_v2.Update"


def update(client, **fields):
    return client.call("SET", "/v2/profiles", dict({"@type": UPDATE}, **fields))


def create(client, profile_id, app_id):
    return update(client, updatedProfiles=[{"id": profile_id, "applicationId": app_id}])


def test_a_profile_the_ui_creates_with_its_own_id_belongs_to_its_application(client, bolt, windows):
    assert create(client, UUID, FIREFOX)["result"]["code"] == "SUCCESS"
    assert assign_to(client, UUID, BACK, RELOAD)["result"]["code"] == "SUCCESS"
    prof = profile(client, UUID)
    assert prof["applicationId"] == FIREFOX and card_of(prof, BACK) == "card_reload"
    assert profile(client, FIREFOX) is None                       # one profile, listed under the UI's id
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})


def test_a_created_profile_survives_a_restart(agent_factory, bolt, windows, workdir):
    apps = install_apps(workdir)
    first = agent_factory(window_source=windows, focus_seconds=0.02, app_dirs=[apps])
    c = first.client()
    create(c, UUID, FIREFOX)
    c.close()
    first.stop()
    second = agent_factory(window_source=windows, focus_seconds=0.02, app_dirs=[apps])
    c2 = second.client()
    try:
        assert profile(c2, UUID)["applicationId"] == FIREFOX
    finally:
        c2.close()


def test_the_profile_is_named_after_the_application(client):
    assign_to(client, FIREFOX, BACK, RELOAD)
    assert profile(client, FIREFOX)["name"] == "Firefox Web Browser"


def test_a_profile_id_that_names_no_application_is_refused_and_nothing_is_stored(client):
    reply = assign_to(client, "not-an-installed-app", BACK, RELOAD)
    assert reply["result"]["code"] == "INVALID_ARG"
    assert profile(client, "not-an-installed-app") is None


def test_creating_a_profile_for_an_application_that_is_not_installed_is_refused(client):
    assert create(client, UUID, "not.installed.App")["result"]["code"] == "INVALID_ARG"
    assert profile(client, UUID) is None


def test_removing_a_profile_drops_its_buttons(client, bolt, windows):
    create(client, UUID, FIREFOX)
    assign_to(client, UUID, BACK, RELOAD)
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    assert update(client, removedProfileIds=[UUID])["result"]["code"] == "SUCCESS"
    assert profile(client, UUID) is None
    assert wait_for(lambda: bolt.mouse.diverted == set())


def test_removing_a_profile_broadcasts_a_profiles_update_that_drops_its_id(tracked, client):
    create(client, UUID, FIREFOX)
    sub = tracked.client()
    try:
        sub.subscribe("/v2/profiles/update")
        assert update(client, removedProfileIds=[UUID])["result"]["code"] == "SUCCESS"
        msg = sub.recv()
        assert msg["path"] == "/v2/profiles/update" and msg["verb"] == "BROADCAST"
        assert msg["payload"]["@type"] == "type.googleapis.com/logi.protocol.profiles_v2.Update"
        assert msg["payload"]["removedProfileIds"] == [UUID]
        assert "updatedProfiles" in msg["payload"] and "profiles" not in msg["payload"]
    finally:
        sub.close()


def test_resetting_a_profile_by_the_ui_id(client):
    create(client, UUID, FIREFOX)
    assign_to(client, UUID, BACK, RELOAD)
    assert client.call("SET", "/v2/profiles/reset", {"profileId": UUID})["result"]["code"] == "SUCCESS"
    assert profile(client, UUID)["applicationId"] == FIREFOX      # still the application's profile
    assert card_of(profile(client, UUID), BACK) != "card_reload"


def test_a_failed_switch_does_not_leave_the_choice_saved(client, bolt, windows):
    windows.focus(("Navigator", "firefox"))
    settled(windows)
    bolt.mouse.fail_divert_write = True
    reply = assign_to(client, FIREFOX, BACK, RELOAD)
    bolt.mouse.fail_divert_write = False
    assert reply["result"]["code"] == "FAULTED"
    assert profile(client, FIREFOX) is None
    assert bolt.mouse.diverted == set()


def test_a_failed_reset_leaves_the_profile_as_it_was(client, bolt, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    bolt.mouse.fail_divert_write = True
    reply = client.call("SET", "/v2/profiles/reset", {"profileId": FIREFOX})
    bolt.mouse.fail_divert_write = False
    assert reply["result"]["code"] == "FAULTED"
    assert card_of(profile(client, FIREFOX), BACK) == "card_reload"
    assert bolt.mouse.diverted == {BACK}
