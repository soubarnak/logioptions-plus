"""Cloud and leftover routes answer calm defaults (ticket #29)."""

import logging

import pytest

from tests.fakes.runner import FakeRunner

TYPE = "type.googleapis.com/"


def get(client, path, payload=None):
    return client.call("GET", path, payload)


def test_activities_are_empty(client):
    r = get(client, "/unified_profiles/activities")
    assert r["result"]["code"] == "SUCCESS"
    assert r["payload"]["@type"] == TYPE + "logi.protocol.unified_profiles.ActivityDataMap"
    assert r["payload"]["activities"] == {}


def test_flow_is_disabled_for_any_device(client):
    r = get(client, "/flow/dev00000000/config")
    assert r["result"]["code"] == "SUCCESS"
    assert r["payload"]["@type"] == TYPE + "logi.protocol.flow.Config"
    assert r["payload"]["on"] is False and r["payload"]["hosts"] == []
    loc = get(client, "/flow/dev00000000/device_location")
    assert loc["payload"]["@type"] == TYPE + "logi.protocol.flow.DeviceLocation"
    peers = get(client, "/flow/dev00000000/device_peer_status")
    assert peers["payload"]["peers"] == []


def test_logi_plugin_service_is_loaded_with_no_plugins(client):
    info = get(client, "/lps/endpoint/info")
    assert info["payload"]["state"] == "LOADED"
    assert get(client, "/lps/plugins")["payload"]["plugins"] == []
    assert get(client, "/lps/get_library_package_list")["payload"]["values"] == []
    assert get(client, "/lps/overlay_settings")["payload"]["enabled"] is False


def test_offers_and_recommendations_are_empty(client):
    r = get(client, "/offer/retrieve")
    assert r["result"]["code"] == "SUCCESS"
    assert r["payload"]["@type"] == TYPE + "logi.protocol.offer_redemption.Offers"
    assert r["payload"]["offers"] == []
    assert get(client, "/api/offer/available")["payload"]["offers"] == []
    assert client.call("SET", "/offer/revoke_all")["result"]["code"] == "SUCCESS"
    rec = get(client, "/device_recommendation_enabled")
    assert rec["payload"]["value"] is False


def test_accounts_backups_and_migration_are_not_logged_in_and_empty(client):
    assert get(client, "/accounts/user_info")["result"]["code"] == "NOT_READY"
    assert get(client, "/accounts/email_subscription")["result"]["code"] == "NOT_READY"
    auth = get(client, "/accounts/is_authenticated")
    assert auth["result"]["code"] == "SUCCESS" and auth["payload"]["value"] is False
    assert get(client, "/backups/device/list")["payload"]["backups"] == []
    assert get(client, "/migration/reports")["result"]["code"] == "NOT_READY"
    state = get(client, "/migration/migration_status")
    assert state["payload"]["migrationState"] == "NOT_MIGRATED"


def test_logivoice_beta_features_and_secure_input_are_inert(client):
    assert get(client, "/logivoice/voice_search_enabled")["payload"]["value"] is False
    assert get(client, "/logivoice/get_long_speech_enabled")["payload"]["value"] is False
    assert get(client, "/logivoice/start")["result"]["code"] == "NO_SUCH_PATH"
    assert client.call("SET", "/logivoice/set_microphone", {"id": "x"})["result"]["code"] == "SUCCESS"
    beta = get(client, "/beta_testing/features")
    assert beta["result"]["code"] == "SUCCESS" and beta["payload"]["features"] == {}
    assert get(client, "/get_secure_input_app_name")["result"]["code"] == "NO_SUCH_PATH"


def test_open_settings_launches_gnome_mouse_settings(agent_factory, monkeypatch):
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    runner = FakeRunner(installed={"gnome-control-center"})
    c = agent_factory(runner=runner).client()
    assert c.call("SET", "/system/open_settings")["result"]["code"] == "SUCCESS"
    assert runner.launched == [["gnome-control-center", "mouse"]]


def test_open_settings_launches_kde_mouse_settings(agent_factory, monkeypatch):
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    runner = FakeRunner(installed={"systemsettings"})
    c = agent_factory(runner=runner).client()
    c.call("SET", "/system/open_settings")
    assert runner.launched == [["systemsettings", "kcm_mouse"]]


def test_open_settings_prefers_the_current_desktop(agent_factory, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    runner = FakeRunner(installed={"gnome-control-center", "systemsettings"})
    c = agent_factory(runner=runner).client()
    c.call("SET", "/system/open_settings")
    assert runner.launched == [["systemsettings", "kcm_mouse"]]


def test_open_settings_does_nothing_when_no_tool_is_known(agent_factory):
    runner = FakeRunner()
    c = agent_factory(runner=runner).client()
    assert c.call("SET", "/system/open_settings")["result"]["code"] == "SUCCESS"
    assert runner.launched == []


def test_unhandled_set_is_refused_and_logged_as_warning(client, caplog):
    with caplog.at_level(logging.WARNING, logger="opl.router"):
        r = client.call("SET", "/no/such/feature", {"a": 1})
    assert r["result"]["code"] == "NOT_SUPPORTED"
    assert any(rec.levelno == logging.WARNING and "/no/such/feature" in rec.getMessage()
               for rec in caplog.records)


@pytest.mark.parametrize("path", [
    "/scarif/event", "/system/settings", "/updates/depot/info",
    "/lps/start_library_package_update_check", "/logivoice/set_microphone",
])
def test_boot_saga_sets_stay_accepted_and_echo_the_payload(client, path):
    r = client.call("SET", path, {"a": 1})
    assert r["result"]["code"] == "SUCCESS" and r["payload"]["a"] == 1
