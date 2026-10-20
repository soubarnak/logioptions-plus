"""Scroll wheel panel (ticket #10): SmartShift, hi-res and invert -> mouse, profile store, reapply."""

import pytest

from tests.fakes import depot as fx

TYPE = "type.googleapis.com/"
DEFAULT_PROFILE = "420fd454-0c36-499d-bde4-146823b16147"
SLOT = fx.SLOT_PREFIX + "_mouse_scroll_wheel_settings"
RATCHET, FREESPIN = 2, 1
HIRES_BIT, INVERT_BIT, TARGET_BIT = 0x02, 0x04, 0x01


def get_profile(client):
    reply = client.call("GET", "/v2/profiles")
    assert reply["result"]["code"] == "SUCCESS"
    return reply["payload"]["profiles"][0]


def assignment(profile):
    for a in profile["assignments"]:
        if a["slotId"] == SLOT:
            return a
    return None


def wheel(asg):
    return asg["card"]["mouseScrollWheelSettings"]


def current(client):
    return wheel(assignment(get_profile(client)))


def change(client, **fields):
    """Set the wheel settings the way the panel does: a partial ScrollWheelSettings."""
    return client.call("SET", "/v2/profiles/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE,
        "assignment": {"slotId": SLOT, "card": {
            "attribute": "MOUSE_SCROLL_WHEEL_SETTINGS", "mouseScrollWheelSettings": fields}},
    })


def first_device(client):
    return client.call("GET", "/devices/list")["payload"]["deviceInfos"][0]


# ---- seeding and capabilities ---------------------------------------------

def test_scroll_slot_is_seeded_from_live_reads(client):
    a = assignment(get_profile(client))
    assert a is not None, "hotspot would say MISSING ASSIGNMENT"
    assert a["card"]["attribute"] == "MOUSE_SCROLL_WHEEL_SETTINGS"
    w = wheel(a)
    assert w["smartshift"]["isEnabled"] is True          # 0x2110 mode 2 = ratchet with auto-disengage
    assert w["smartshift"]["sensitivity"] == 30          # 0x2110 threshold
    assert w["isSmooth"] is True                         # 0x2121: kernel left high resolution on
    assert w["dir"] == "NORMAL"


def test_kernel_set_wheel_state_is_shown_not_assumed(bolt, agent_factory):
    bolt.mouse.wheel_mode = INVERT_BIT                   # hi-res off, inverted
    bolt.mouse.smartshift.update(mode=FREESPIN, threshold=12)
    client = agent_factory().client()
    try:
        w = current(client)
        assert w["isSmooth"] is False
        assert w["dir"] == "INVERTED"
        assert w["smartshift"]["isEnabled"] is False
        assert w["smartshift"]["sensitivity"] == 12
    finally:
        client.close()


def test_scroll_capabilities_come_from_the_features_the_mouse_has(client):
    caps = first_device(client)["capabilities"]
    sw = caps["scrollWheelCapabilities"]
    assert sw["highResolution"] is True
    assert sw["smartshift"] is True
    assert sw["adjustableSpeed"] is True                 # the wheel relay can run (see test_wheel_relay.py)
    assert caps["hasBatteryStatus"] is True              # merged with the battery capabilities


def test_mouse_without_the_features_reports_them_off(bolt, agent_factory):
    bolt.mouse.features = [f for f in bolt.mouse.features if f not in (0x2110, 0x2121)]
    client = agent_factory().client()
    try:
        sw = first_device(client)["capabilities"]["scrollWheelCapabilities"]
        assert sw["highResolution"] is False and sw["smartshift"] is False
        assert assignment(get_profile(client)) is None
    finally:
        client.close()


# ---- SmartShift through 0x2110 --------------------------------------------

def test_smartshift_off_writes_free_spin_and_reads_back(client, bolt):
    reply = change(client, smartshift={"isEnabled": False})
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.smartshift["mode"] == FREESPIN
    assert bolt.mouse.smartshift["threshold"] == 30      # untouched
    assert wheel(reply["payload"]["assignment"])["smartshift"]["isEnabled"] is False
    assert current(client)["smartshift"]["isEnabled"] is False


def test_smartshift_on_writes_ratchet_mode(client, bolt):
    change(client, smartshift={"isEnabled": False})
    change(client, smartshift={"isEnabled": True})
    assert bolt.mouse.smartshift["mode"] == RATCHET


def test_threshold_writes_through_and_replies_with_read_back(client, bolt):
    reply = change(client, smartshift={"sensitivity": 42})
    assert bolt.mouse.smartshift["threshold"] == 42
    assert bolt.mouse.smartshift["mode"] == RATCHET      # untouched
    assert wheel(reply["payload"]["assignment"])["smartshift"]["sensitivity"] == 42


@pytest.mark.parametrize("asked, applied", [(0, 1), (-5, 1), (51, 50), (999, 50)])
def test_threshold_is_clamped_to_the_slider_range(client, bolt, asked, applied):
    change(client, smartshift={"sensitivity": asked})
    assert bolt.mouse.smartshift["threshold"] == applied


def test_failed_smartshift_write_is_a_failure_and_not_persisted(client, bolt):
    bolt.mouse.fail_smartshift_write = True
    reply = change(client, smartshift={"isEnabled": False})
    assert reply["result"]["code"] != "SUCCESS"
    assert bolt.mouse.smartshift["mode"] == RATCHET
    assert current(client)["smartshift"]["isEnabled"] is True


# ---- hi-res and invert through 0x2121 -------------------------------------

def test_hires_off_clears_only_the_resolution_bit(client, bolt):
    bolt.mouse.wheel_mode = HIRES_BIT | TARGET_BIT
    reply = change(client, isSmooth=False)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.wheel_mode == TARGET_BIT           # the diversion target bit is left alone
    assert wheel(reply["payload"]["assignment"])["isSmooth"] is False


def test_invert_sets_the_invert_bit_and_keeps_hires(client, bolt):
    reply = change(client, dir="INVERTED")
    assert bolt.mouse.wheel_mode == HIRES_BIT | INVERT_BIT
    assert wheel(reply["payload"]["assignment"])["dir"] == "INVERTED"
    change(client, dir="NORMAL")
    assert bolt.mouse.wheel_mode == HIRES_BIT


def test_failed_wheel_write_is_a_failure_and_not_persisted(client, bolt):
    bolt.mouse.fail_wheel_write = True
    reply = change(client, dir="INVERTED")
    assert reply["result"]["code"] != "SUCCESS"
    assert current(client)["dir"] == "NORMAL"


def test_a_change_to_one_setting_leaves_the_others_as_the_mouse_has_them(client, bolt):
    change(client, dir="INVERTED")
    bolt.mouse.smartshift.update(mode=FREESPIN, threshold=7)   # changed behind our back
    change(client, isSmooth=False)
    assert bolt.mouse.smartshift == {"mode": FREESPIN, "threshold": 7, "torque": 0}
    assert bolt.mouse.wheel_mode == INVERT_BIT


# ---- persistence, reapply, reset, broadcast -------------------------------

def test_settings_persist_across_agent_restart_and_reapply_at_discovery(agent_factory, bolt):
    first = agent_factory()
    c = first.client()
    change(c, smartshift={"isEnabled": False, "sensitivity": 20}, dir="INVERTED", isSmooth=False)
    c.close()
    first.stop()
    bolt.mouse.smartshift.update(mode=RATCHET, threshold=30)    # mouse forgot, e.g. power cycled
    bolt.mouse.wheel_mode = HIRES_BIT
    c2 = agent_factory().client()
    try:
        w = current(c2)
        assert (w["smartshift"]["isEnabled"], w["smartshift"]["sensitivity"]) == (False, 20)
        assert (w["dir"], w["isSmooth"]) == ("INVERTED", False)
        assert bolt.mouse.smartshift["mode"] == FREESPIN and bolt.mouse.smartshift["threshold"] == 20
        assert bolt.mouse.wheel_mode == INVERT_BIT
    finally:
        c2.close()


def test_settings_are_reapplied_when_a_device_is_rediscovered(agent, client, bolt):
    change(client, smartshift={"isEnabled": False})
    bolt.mouse.smartshift["mode"] = RATCHET
    agent.agent.reg.scan()
    assert bolt.mouse.smartshift["mode"] == FREESPIN


def test_reset_to_default_forgets_the_stored_scroll_settings(client, bolt):
    change(client, smartshift={"isEnabled": False}, dir="INVERTED")
    reply = client.call("SET", "/v2/profiles/reset", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileResetInfo", "profileId": DEFAULT_PROFILE})
    assert reply["result"]["code"] == "SUCCESS"
    bolt.mouse.smartshift["mode"] = RATCHET
    bolt.mouse.wheel_mode = HIRES_BIT
    w = current(client)                                         # shown from the mouse again
    assert w["smartshift"]["isEnabled"] is True and w["dir"] == "NORMAL"


def test_second_subscribed_client_receives_profiles_update(agent, client):
    other = agent.client()
    try:
        other.subscribe("/v2/profiles/update")
        change(client, smartshift={"isEnabled": False})
        push = other.recv()
        assert push["path"] == "/v2/profiles/update" and push["verb"] == "BROADCAST"
        assert wheel(assignment(push["payload"]["profiles"][0]))["smartshift"]["isEnabled"] is False
    finally:
        other.close()


def test_pointer_speed_is_unaffected_by_wheel_changes(client, bolt):
    change(client, dir="INVERTED")
    assert bolt.mouse.dpi == 1000
