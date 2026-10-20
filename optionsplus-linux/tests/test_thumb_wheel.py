"""Thumb wheel panel (ticket #11): direction and smoothness through 0x2150."""

from tests.fakes import depot as fx
from tests.fakes import hidpp_device

TYPE = "type.googleapis.com/"
DEFAULT_PROFILE = "420fd454-0c36-499d-bde4-146823b16147"
SLOT = fx.SLOT_PREFIX + "_mouse_thumb_wheel_settings"


def get_profile(client):
    reply = client.call("GET", "/v2/profiles")
    assert reply["result"]["code"] == "SUCCESS"
    return reply["payload"]["profiles"][0]


def assignment(profile):
    return next((a for a in profile["assignments"] if a["slotId"] == SLOT), None)


def settings(asg):
    return asg["card"]["mouseThumbWheelSettings"]


def put(client, **fields):
    return client.call("SET", "/v2/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE,
        "assignment": {"slotId": SLOT, "card": {
            "attribute": "MOUSE_THUMB_WHEEL_SETTINGS", "mouseThumbWheelSettings": fields}},
    })


def device_info(client):
    return client.call("GET", "/devices/list")["payload"]["deviceInfos"][0]


def test_thumb_wheel_slot_is_seeded_from_the_live_status(client):
    asg = assignment(get_profile(client))
    assert asg is not None, "hotspot would say MISSING ASSIGNMENT"
    assert asg["card"]["attribute"] == "MOUSE_THUMB_WHEEL_SETTINGS"
    s = settings(asg)
    assert s["dir"] == "STANDARD" and s["isSmooth"] is False
    assert s["speed"] == 0.5                      # neutral: the wheel relay can apply a speed (ticket #25)


def test_seed_reflects_a_mouse_that_is_already_inverted_and_smooth(agent_factory, bolt):
    bolt.mouse.thumb.update(invert=1, smooth=1)
    c = agent_factory().client()
    try:
        s = settings(assignment(get_profile(c)))
        assert s["dir"] == "NATURAL" and s["isSmooth"] is True
    finally:
        c.close()


def test_invert_writes_through_2150_and_replies_with_read_back(client, bolt):
    reply = put(client, dir="NATURAL")
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["invert"] == 1
    assert settings(reply["payload"]["assignment"])["dir"] == "NATURAL"
    assert settings(assignment(get_profile(client)))["dir"] == "NATURAL"
    put(client, dir="STANDARD")
    assert bolt.mouse.thumb["invert"] == 0


def test_smooth_writes_through_2150_and_keeps_direction(client, bolt):
    put(client, dir="NATURAL")
    reply = put(client, isSmooth=True)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["smooth"] == 1 and bolt.mouse.thumb["invert"] == 1
    s = settings(reply["payload"]["assignment"])
    assert s["isSmooth"] is True and s["dir"] == "NATURAL"


def test_write_never_diverts_the_wheel(client, bolt):
    put(client, dir="NATURAL", isSmooth=True)
    assert bolt.mouse.thumb["mode"] == 0          # native HID reporting: no speed asked, nothing relayed


def test_speed_is_not_adjustable_without_the_relay(agent_factory, uinput):
    uinput.fail_create = True                     # no virtual device: no relay
    c = agent_factory().client()
    try:
        reply = put(c, speed=0.8, dir="NATURAL")
        assert settings(reply["payload"]["assignment"])["speed"] == 0
        assert settings(assignment(get_profile(c)))["speed"] == 0
        caps = device_info(c)["capabilities"]
        assert caps["mouseThumbWheelOverride"]["speed"] == 0
    finally:
        c.close()


def test_failed_device_write_is_a_failure_and_keeps_the_old_value(client, bolt):
    bolt.mouse.fail_thumb_write = True
    reply = put(client, dir="NATURAL")
    assert reply["result"]["code"] != "SUCCESS"
    assert bolt.mouse.thumb["invert"] == 0
    assert settings(assignment(get_profile(client)))["dir"] == "STANDARD"


def test_settings_persist_across_restart_and_are_reapplied(agent_factory, bolt):
    first = agent_factory()
    c = first.client()
    put(c, dir="NATURAL", isSmooth=True)
    c.close()
    first.stop()
    bolt.mouse.thumb.update(invert=0, smooth=0)   # mouse forgot, e.g. power cycled
    c2 = agent_factory().client()
    try:
        s = settings(assignment(get_profile(c2)))
        assert s["dir"] == "NATURAL" and s["isSmooth"] is True
        assert bolt.mouse.thumb["invert"] == 1 and bolt.mouse.thumb["smooth"] == 1
    finally:
        c2.close()


def test_settings_are_reapplied_when_a_device_is_discovered(agent, client, bolt):
    put(client, dir="NATURAL")
    bolt.mouse.thumb["invert"] = 0                # receiver replug
    agent.agent.reg.scan()
    assert bolt.mouse.thumb["invert"] == 1


def test_reset_to_default_restores_standard_direction(client, bolt):
    put(client, dir="NATURAL", isSmooth=True)
    reply = client.call("SET", "/v2/profiles/reset", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileResetInfo", "profileId": DEFAULT_PROFILE})
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["invert"] == 0 and bolt.mouse.thumb["smooth"] == 0
    assert settings(assignment(get_profile(client)))["dir"] == "STANDARD"


def test_second_subscribed_client_receives_profiles_update(agent, client):
    other = agent.client()
    try:
        other.subscribe("/v2/profiles/update")
        put(client, dir="NATURAL")
        push = other.recv()
        assert push["path"] == "/v2/profiles/update"
        assert settings(assignment(push["payload"]["updatedProfiles"][0]))["dir"] == "NATURAL"
    finally:
        other.close()


def test_mouse_without_a_thumb_wheel_gets_no_card_and_no_capability(bolt, agent_factory):
    bolt.mouse.features = [f for f in bolt.mouse.features if f != 0x2150]
    c = agent_factory().client()
    try:
        assert assignment(get_profile(c)) is None
        assert "mouseThumbWheelOverride" not in device_info(c).get("capabilities", {})
    finally:
        c.close()
