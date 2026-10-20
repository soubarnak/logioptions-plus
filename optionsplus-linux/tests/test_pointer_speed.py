"""Pointer speed (ticket #9): slider -> DPI on the mouse, profile store, reapply."""

import json
import os

import pytest

from tests.fakes import depot as fx

TYPE = "type.googleapis.com/"
DEFAULT_PROFILE = "420fd454-0c36-499d-bde4-146823b16147"
SLOT = fx.SLOT_PREFIX + "_mouse_settings"
MIN_DPI, MAX_DPI = 200, 8000          # simulated sensor range, step 50


def _assignment(profile):
    for a in profile["assignments"]:
        if a["slotId"] == SLOT:
            return a
    return None


def get_profile(client):
    reply = client.call("GET", "/v2/profiles")
    assert reply["result"]["code"] == "SUCCESS"
    return reply["payload"]["profiles"][0]


def pointer_value(assignment):
    return assignment["card"]["mouseSettings"]["pointerSpeed"]["active"]["value"]


def slider(client, value):
    return client.call("SET", "/v2/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE,
        "assignment": {"slotId": SLOT, "card": {
            "attribute": "MOUSE_SETTINGS",
            "mouseSettings": {"pointerSpeed": {"active": {"value": value}}}}},
    })


def test_mouse_settings_slot_is_seeded_from_the_live_dpi(client):
    a = _assignment(get_profile(client))
    assert a is not None, "hotspot would say MISSING ASSIGNMENT"
    assert a["card"]["attribute"] == "MOUSE_SETTINGS"
    active = a["card"]["mouseSettings"]["pointerSpeed"]["active"]
    assert active["value"] == pytest.approx(0.0)          # the sensor default sits mid-slider (50%)
    assert active["dpiLevel"] == 1000


@pytest.mark.parametrize("value, dpi", [(-1.0, 200), (-0.46, 650), (0.0, 1000), (0.25, 2750), (0.5, 4500), (0.8, 6600), (1.0, 8000)])
def test_slider_set_writes_dpi_through_2201_and_replies_with_read_back(client, bolt, value, dpi):
    reply = slider(client, value)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.dpi == dpi
    asg = reply["payload"]["assignment"]
    assert asg["card"]["mouseSettings"]["pointerSpeed"]["active"]["dpiLevel"] == dpi
    assert pointer_value(asg) == pytest.approx(value, abs=0.03)


def test_slider_beyond_the_ends_is_clamped_to_the_sensor_range(client, bolt):
    slider(client, 1.7)
    assert bolt.mouse.dpi == MAX_DPI
    slider(client, -3)
    assert bolt.mouse.dpi == MIN_DPI


def test_failed_device_write_is_a_failure_not_success(client, bolt):
    bolt.mouse.fail_dpi_write = True
    reply = slider(client, 0.9)
    assert reply["result"]["code"] != "SUCCESS"
    assert bolt.mouse.dpi == 1000
    value = pointer_value(_assignment(get_profile(client)))
    assert value == pytest.approx(0.0)


def test_profile_persists_across_agent_restart(agent_factory, bolt):
    first = agent_factory()
    c = first.client()
    slider(c, 0.8)
    c.close()
    first.stop()
    bolt.mouse.dpi = 1000                      # mouse forgot, e.g. power cycled
    second = agent_factory()
    c2 = second.client()
    try:
        assert pointer_value(_assignment(get_profile(c2))) == pytest.approx(0.8, abs=0.01)
        assert bolt.mouse.dpi == 6600          # and was reapplied at discovery
    finally:
        c2.close()


def test_reset_to_default_restores_and_applies_depot_defaults(client, bolt):
    slider(client, 0.9)
    reply = client.call("SET", "/v2/profiles/reset", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileResetInfo", "profileId": DEFAULT_PROFILE})
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.dpi == 4500              # depot default value 0.5
    assert pointer_value(_assignment(get_profile(client))) == pytest.approx(0.5, abs=0.01)


def test_second_subscribed_client_receives_profiles_update(agent, client):
    other = agent.client()
    try:
        other.subscribe("/v2/profiles/update")
        slider(client, 0.5)
        push = other.recv()
        assert push["path"] == "/v2/profiles/update" and push["verb"] == "BROADCAST"
        assert pointer_value(_assignment(push["payload"]["profiles"][0])) == pytest.approx(0.5, abs=0.01)
    finally:
        other.close()


def test_settings_are_reapplied_when_a_device_is_discovered(agent, client, bolt):
    slider(client, 0.5)
    bolt.mouse.dpi = 1000                      # receiver replug: mouse back at its own value
    agent.agent.reg.scan()
    assert bolt.mouse.dpi == 4500


def test_the_ui_route_set_v2_assignment_writes_the_dpi(client, bolt):
    reply = client.call("SET", "/v2/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE,
        "assignment": {"slotId": SLOT, "cardId": "x", "card": {
            "attribute": "MOUSE_SETTINGS",
            "mouseSettings": {"pointerSpeed": {"active": {"value": 1.0}}}}}})
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.dpi == MAX_DPI


def test_the_old_profiles_assignment_alias_still_works(client, bolt):
    reply = client.call("SET", "/v2/profiles/assignment", {
        "profileId": DEFAULT_PROFILE,
        "assignment": {"slotId": SLOT, "card": {
            "attribute": "MOUSE_SETTINGS",
            "mouseSettings": {"pointerSpeed": {"active": {"value": 1.0}}}}}})
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.dpi == MAX_DPI


def test_an_assignment_for_an_unknown_slot_is_an_invalid_argument(client):
    reply = client.call("SET", "/v2/assignment", {
        "profileId": DEFAULT_PROFILE,
        "assignment": {"slotId": "no-such-device_c999", "card": {"attribute": "KEYSTROKE"}}})
    assert reply["result"]["code"] == "INVALID_ARG"
    assert "no-such-device_c999" in reply["result"]["what"]


def _stored_slots(workdir):
    path = os.path.join(workdir, "state", "profiles.json")
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh).get("assignments") or {}


def test_device_restore_resets_the_device_named_by_the_payload(client, bolt, workdir):
    slider(client, 0.9)
    assert bolt.mouse.dpi != 4500
    assert SLOT in _stored_slots(workdir)
    reply = client.call("SET", "/device/restore", {
        "@type": TYPE + "logi.protocol.device.DeviceInfo", "id": "dev00000000",
        "deviceModel": fx.MODEL_ID, "slotPrefix": fx.SLOT_PREFIX})
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["payload"]["id"] == "dev00000000"
    assert bolt.mouse.dpi == 4500
    assert pointer_value(_assignment(get_profile(client))) == pytest.approx(0.5, abs=0.01)


@pytest.mark.parametrize("value, dpi", [
    (-1.0, 200), (-0.46, 650), (-0.5, 600), (0.0, 1000), (0.5, 4500), (1.0, 8000), (-5, 200), (5, 8000)])
def test_slider_value_maps_around_the_sensor_default(value, dpi):
    from opl_agent import profiles
    rng = {"min": 200, "max": 8000, "step": 50, "default": 1000}
    assert profiles.dpi_from_value(value, rng) == dpi
    assert profiles.value_from_dpi(dpi, rng) == pytest.approx(max(-1.0, min(1.0, value)), abs=0.03)


def test_slider_maps_around_the_range_midpoint_when_the_sensor_reports_no_default():
    from opl_agent import profiles
    rng = {"min": 200, "max": 8000, "step": 50, "default": 0}
    assert profiles.dpi_from_value(0.0, rng) == 4100
    assert profiles.value_from_dpi(4100, rng) == pytest.approx(0.0)


def test_slider_snaps_to_a_dpi_list_without_a_step():
    from opl_agent import profiles
    rng = {"values": [400, 800, 1600, 3200], "min": 400, "max": 3200, "step": None, "default": 800}
    assert profiles.dpi_from_value(0.0, rng) == 800
    assert profiles.dpi_from_value(1.0, rng) == 3200
    assert profiles.dpi_from_value(-1.0, rng) == 400
