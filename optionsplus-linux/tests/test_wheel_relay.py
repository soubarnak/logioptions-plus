"""Scroll and thumb wheel speed (ticket #25): the agent diverts a wheel (0x2121 / 0x2150) whose
speed is not neutral and re-emits it, scaled, through the virtual input device.

Seams: the profile IPC (SET/GET /v2/profiles, /devices/list), the simulated HID++ mouse
(`turn_wheel` / `turn_thumb` report only while diverted, as the real wheel would) and the
recording input sink."""

import pytest

from tests.fakes import depot as fx
from tests.harness import wait_for

TYPE = "type.googleapis.com/"
DEFAULT_PROFILE = "420fd454-0c36-499d-bde4-146823b16147"
SCROLL_SLOT = fx.SLOT_PREFIX + "_mouse_scroll_wheel_settings"
THUMB_SLOT = fx.SLOT_PREFIX + "_mouse_thumb_wheel_settings"
TARGET_BIT, HIRES_BIT, INVERT_BIT = 0x01, 0x02, 0x04
# Linux evdev relative axes, from linux/input-event-codes.h
REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 6, 8, 11, 12

NEUTRAL, FAST, SLOW, SLOWEST = 0.0, 1.0, -0.5, -1.0   # the UI scale: 0 = unchanged, 1 = 4x, -0.5 = 0.5x, -1 = 0.25x
MULTIPLIER = 8                            # the simulated wheel reports 8 hi-res steps per notch
DIVERTED_RESOLUTION = 7                   # the simulated thumb wheel: 7 diverted units per notch (126 / 18)


def assignment(client, slot):
    profile = client.call("GET", "/v2/profiles")["payload"]["profiles"][0]
    return next((a for a in profile["assignments"] if a["slotId"] == slot), None)


def put(client, slot, attribute, key, **fields):
    return client.call("SET", "/v2/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE,
        "assignment": {"slotId": slot, "card": {"attribute": attribute, key: fields}}})


def scroll(client, **fields):
    return put(client, SCROLL_SLOT, "MOUSE_SCROLL_WHEEL_SETTINGS", "mouseScrollWheelSettings", **fields)


def thumb(client, **fields):
    return put(client, THUMB_SLOT, "MOUSE_THUMB_WHEEL_SETTINGS", "mouseThumbWheelSettings", **fields)


def scroll_settings(client):
    return assignment(client, SCROLL_SLOT)["card"]["mouseScrollWheelSettings"]


def thumb_settings(client):
    return assignment(client, THUMB_SLOT)["card"]["mouseThumbWheelSettings"]


def capabilities(client):
    return client.call("GET", "/devices/list")["payload"]["deviceInfos"][0]["capabilities"]


def reset(client):
    return client.call("SET", "/v2/profiles/reset", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileResetInfo", "profileId": DEFAULT_PROFILE})


def turn(bolt, delta, **kw):
    assert bolt.turn_wheel(delta, **kw), "the wheel was not diverted"


# ---- capabilities ----------------------------------------------------------------

def test_speed_is_adjustable_when_the_virtual_device_can_be_created(client):
    caps = capabilities(client)
    assert caps["scrollWheelCapabilities"]["adjustableSpeed"] is True
    assert caps["mouseThumbWheelOverride"]["speed"] > 0


def test_speed_is_not_adjustable_without_a_virtual_device(agent_factory, uinput):
    uinput.fail_create = True
    c = agent_factory().client()
    try:
        caps = capabilities(c)
        assert caps["scrollWheelCapabilities"]["adjustableSpeed"] is False
        assert caps["mouseThumbWheelOverride"]["speed"] == 0
        assert scroll_settings(c)["speed"] == 0
    finally:
        c.close()


def test_a_mouse_without_the_wheel_features_offers_no_speed(bolt, agent_factory):
    bolt.mouse.features = [f for f in bolt.mouse.features if f not in (0x2121, 0x2150)]
    c = agent_factory().client()
    try:
        caps = capabilities(c)
        assert caps["scrollWheelCapabilities"]["adjustableSpeed"] is False
        assert "mouseThumbWheelOverride" not in caps
    finally:
        c.close()


def test_the_seeded_cards_carry_the_neutral_speed(client):
    assert scroll_settings(client)["speed"] == NEUTRAL
    assert thumb_settings(client)["speed"] == NEUTRAL


def test_wheels_stay_native_until_a_speed_is_set(client, bolt, uinput):
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0 and bolt.mouse.thumb["mode"] == 0
    assert not bolt.turn_wheel(8) and not bolt.turn_thumb(7)
    assert uinput.events == []


# ---- scroll wheel -------------------------------------------------------------------

def test_a_scroll_speed_diverts_the_wheel_and_replies_with_the_speed(client, bolt):
    reply = scroll(client, speed=FAST)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.wheel_mode & TARGET_BIT
    assert reply["payload"]["assignment"]["card"]["mouseScrollWheelSettings"]["speed"] == FAST
    assert scroll_settings(client)["speed"] == FAST


def test_diverted_wheel_motion_is_scaled_by_the_speed(client, bolt, uinput):
    scroll(client, speed=FAST)
    turn(bolt, MULTIPLIER)                           # one notch
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES))
    assert uinput.rels(REL_WHEEL_HI_RES) == [480]    # 4x of 120
    assert uinput.rels(REL_WHEEL) == [4]


def test_a_slow_speed_carries_the_fraction_until_a_notch_is_complete(client, bolt, uinput):
    scroll(client, speed=SLOW)
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [60])
    assert uinput.rels(REL_WHEEL) == []
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [60, 60])
    assert uinput.rels(REL_WHEEL) == [1]


def test_scrolling_the_other_way_emits_negative_values(client, bolt, uinput):
    scroll(client, speed=FAST)
    turn(bolt, -MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES))
    assert uinput.rels(REL_WHEEL_HI_RES) == [-480]
    assert uinput.rels(REL_WHEEL) == [-4]


def test_inverted_direction_flips_the_relayed_sign_and_clears_the_device_invert(client, bolt, uinput):
    scroll(client, speed=FAST, dir="NATURAL")
    assert bolt.mouse.wheel_mode & INVERT_BIT == 0       # software does the inversion
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES))
    assert uinput.rels(REL_WHEEL_HI_RES) == [-480]
    assert scroll_settings(client)["dir"] == "NATURAL"


def test_without_smooth_scrolling_the_motion_is_whole_notches(client, bolt, uinput):
    scroll(client, speed=FAST, isSmooth=False)
    turn(bolt, 2)                                        # a quarter notch, 4x: exactly one notch
    turn(bolt, 1)                                        # 120 / 8 * 4 / 2: half a notch, held back
    turn(bolt, 1)
    assert wait_for(lambda: uinput.rels(REL_WHEEL) == [1, 1])
    assert uinput.rels(REL_WHEEL_HI_RES) == [120, 120]
    assert scroll_settings(client)["isSmooth"] is False


def test_unit_steps_without_hires_flag_count_whole_notches(client, bolt, uinput):
    scroll(client, speed=FAST)
    turn(bolt, 1, hires=False)                           # one low resolution notch
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES))
    assert uinput.rels(REL_WHEEL_HI_RES) == [480]


def test_a_neutral_speed_hands_the_wheel_back_with_its_direction(client, bolt, uinput):
    scroll(client, speed=FAST, dir="NATURAL")
    reply = scroll(client, speed=NEUTRAL)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0
    assert bolt.mouse.wheel_mode & INVERT_BIT                 # the device inverts again
    assert not bolt.turn_wheel(8)
    assert uinput.events == []
    assert scroll_settings(client)["dir"] == "NATURAL"


def test_smartshift_changes_keep_the_wheel_diverted(client, bolt):
    scroll(client, speed=FAST)
    reply = scroll(client, smartshift={"isEnabled": False})
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.wheel_mode & TARGET_BIT
    assert bolt.mouse.smartshift["mode"] == 1
    assert scroll_settings(client)["speed"] == FAST


def test_a_direction_change_alone_stays_native(client, bolt):
    reply = scroll(client, dir="NATURAL")
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0 and bolt.mouse.wheel_mode & INVERT_BIT


def test_a_speed_the_virtual_device_cannot_back_is_not_applied(agent_factory, bolt, uinput):
    uinput.fail_create = True
    c = agent_factory().client()
    try:
        reply = scroll(c, speed=FAST)
        assert reply["result"]["code"] == "SUCCESS"            # nothing was promised: speed reads back 0
        assert bolt.mouse.wheel_mode & TARGET_BIT == 0
        assert scroll_settings(c)["speed"] == 0
    finally:
        c.close()


def test_a_failed_device_write_is_a_failure_and_the_wheel_stays_native(client, bolt):
    bolt.mouse.fail_wheel_write = True
    reply = scroll(client, speed=FAST)
    assert reply["result"]["code"] != "SUCCESS"
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0
    assert scroll_settings(client)["speed"] == NEUTRAL


def test_a_failed_handback_is_a_failure_and_the_speed_stays(client, bolt, uinput):
    scroll(client, speed=FAST)
    bolt.mouse.fail_wheel_write = True
    reply = scroll(client, speed=NEUTRAL)
    assert reply["result"]["code"] != "SUCCESS"
    assert scroll_settings(client)["speed"] == FAST
    bolt.mouse.fail_wheel_write = False
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [480])


def test_a_failed_injection_does_not_stop_later_motion(client, bolt, uinput):
    scroll(client, speed=FAST)
    uinput.fail_emit = True
    turn(bolt, MULTIPLIER)                                       # lost: the sink refused it
    uinput.fail_emit = False
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES)[-1:] == [480])


# ---- thumb wheel --------------------------------------------------------------------

def test_a_thumb_speed_diverts_the_thumb_wheel(client, bolt):
    reply = thumb(client, speed=FAST)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 1
    assert reply["payload"]["assignment"]["card"]["mouseThumbWheelSettings"]["speed"] == FAST


def test_diverted_thumb_motion_is_scaled_into_horizontal_wheel_events(client, bolt, uinput):
    thumb(client, speed=FAST, isSmooth=True)
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES))
    assert uinput.rels(REL_HWHEEL_HI_RES) == [480]
    assert uinput.rels(REL_HWHEEL) == [4]
    assert uinput.rels(REL_WHEEL_HI_RES) == []


def test_thumb_direction_flips_the_relayed_sign(client, bolt, uinput):
    thumb(client, speed=FAST, dir="NATURAL")
    assert bolt.mouse.thumb["invert"] == 0
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES))
    assert uinput.rels(REL_HWHEEL_HI_RES) == [-480]
    assert thumb_settings(client)["dir"] == "NATURAL"


def test_thumb_speed_carries_the_fraction(client, bolt, uinput):
    thumb(client, speed=SLOW, isSmooth=True)
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL) == [1])
    assert uinput.rels(REL_HWHEEL_HI_RES) == [60, 60]


def test_a_neutral_thumb_speed_hands_the_wheel_back_with_its_direction(client, bolt, uinput):
    thumb(client, speed=FAST, dir="NATURAL")
    reply = thumb(client, speed=NEUTRAL)
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 0 and bolt.mouse.thumb["invert"] == 1
    assert not bolt.turn_thumb(7)
    assert uinput.events == []


def test_a_failed_thumb_divert_is_a_failure_and_the_wheel_stays_native(client, bolt):
    bolt.mouse.fail_thumb_write = True
    reply = thumb(client, speed=FAST)
    assert reply["result"]["code"] != "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 0
    assert thumb_settings(client)["speed"] == NEUTRAL


# ---- lifecycle ------------------------------------------------------------------------

def test_the_one_virtual_device_serves_buttons_and_wheels(client, bolt, uinput):
    scroll(client, speed=FAST)
    thumb(client, speed=FAST)
    assert uinput.created == 1


def test_reset_hands_both_wheels_back(client, bolt, uinput):
    scroll(client, speed=FAST, dir="NATURAL")
    thumb(client, speed=FAST, dir="NATURAL")
    assert reset(client)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0 and bolt.mouse.thumb["mode"] == 0
    assert bolt.mouse.thumb["invert"] == 0
    assert scroll_settings(client)["speed"] == NEUTRAL and thumb_settings(client)["speed"] == NEUTRAL
    assert not bolt.turn_wheel(8) and not bolt.turn_thumb(7)


def test_releasing_with_reset_hands_the_wheel_back_without_hi_res(client, agent, bolt):
    scroll(client, speed=FAST, isSmooth=True)
    assert bolt.mouse.wheel_mode & TARGET_BIT and bolt.mouse.wheel_mode & HIRES_BIT
    dev = next(iter(agent.agent.reg.devices.values()))
    agent.agent.router.relay.release(dev, reset=True)
    assert bolt.mouse.wheel_mode & (TARGET_BIT | HIRES_BIT | INVERT_BIT) == 0


def test_speeds_survive_a_restart_and_are_diverted_again(agent_factory, bolt, uinput):
    first = agent_factory()
    c = first.client()
    scroll(c, speed=FAST, dir="NATURAL")
    thumb(c, speed=SLOW)
    c.close()
    first.stop()
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0 and bolt.mouse.thumb["mode"] == 0   # a clean stop hands them back
    c2 = agent_factory().client()
    try:
        assert bolt.mouse.wheel_mode & TARGET_BIT and bolt.mouse.thumb["mode"] == 1
        assert scroll_settings(c2)["speed"] == FAST and scroll_settings(c2)["dir"] == "NATURAL"
        assert thumb_settings(c2)["speed"] == SLOW
        turn(bolt, MULTIPLIER)
        assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [-480])
    finally:
        c2.close()


def test_stopping_the_agent_returns_the_wheels_to_native_reporting(agent, client, bolt):
    scroll(client, speed=FAST, dir="NATURAL")
    thumb(client, speed=FAST, dir="NATURAL")
    client.close()
    agent.stop()
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0 and bolt.mouse.wheel_mode & INVERT_BIT
    assert bolt.mouse.thumb["mode"] == 0 and bolt.mouse.thumb["invert"] == 1


def test_diversions_are_applied_again_when_the_mouse_is_rediscovered(agent, client, bolt, uinput):
    scroll(client, speed=FAST)
    thumb(client, speed=FAST)
    bolt.mouse.wheel_mode &= ~TARGET_BIT                      # the mouse forgot, e.g. power cycled
    bolt.mouse.thumb["mode"] = 0
    agent.agent.reg.scan()
    assert bolt.mouse.wheel_mode & TARGET_BIT and bolt.mouse.thumb["mode"] == 1
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [480])


def test_disabling_the_relay_returns_the_wheels_to_native_reporting(agent, client, bolt, uinput):
    scroll(client, speed=FAST, dir="NATURAL")
    thumb(client, speed=FAST)
    agent.agent.router.set_wheel_relay(False)
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0 and bolt.mouse.wheel_mode & INVERT_BIT
    assert bolt.mouse.thumb["mode"] == 0
    assert not bolt.turn_wheel(8) and not bolt.turn_thumb(7)
    caps = capabilities(client)
    assert caps["scrollWheelCapabilities"]["adjustableSpeed"] is False
    assert caps["mouseThumbWheelOverride"]["speed"] == 0
    agent.agent.router.set_wheel_relay(True)
    assert bolt.mouse.wheel_mode & TARGET_BIT and bolt.mouse.thumb["mode"] == 1
    assert capabilities(client)["scrollWheelCapabilities"]["adjustableSpeed"] is True


def test_neutral_value_never_diverts(client, bolt):
    scroll(client, speed=0.0)
    thumb(client, speed=0.0)
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0 and bolt.mouse.thumb["mode"] == 0


# ---- the UI's scale: -1..1, 0 = 50% = unchanged (live: dragging to 99% sent 0.98) --------------

@pytest.mark.parametrize("speed, factor", [(-1.0, 0.25), (-0.5, 0.5), (0.0, 1.0), (0.5, 2.0), (1.0, 4.0)])
def test_the_multiplier_follows_the_uis_scale(speed, factor):
    from opl_agent import wheel_relay
    assert wheel_relay.multiplier(speed) == pytest.approx(factor)


@pytest.mark.parametrize("junk", [None, "fast", True, float("nan")])
def test_a_value_that_is_no_number_is_neutral(junk):
    from opl_agent import wheel_relay
    assert wheel_relay.multiplier(junk) == 1.0 and wheel_relay.is_neutral(junk)


def test_the_multiplier_is_clamped_to_the_scale():
    from opl_agent import wheel_relay
    assert wheel_relay.multiplier(7) == wheel_relay.multiplier(1)
    assert wheel_relay.multiplier(-7) == wheel_relay.multiplier(-1)


def test_the_neutral_speed_the_agent_reports_is_the_uis_middle():
    from opl_agent import wheel_relay
    assert wheel_relay.NEUTRAL_SPEED == 0 and round(50 * (wheel_relay.NEUTRAL_SPEED + 1)) == 50


def test_the_old_neutral_half_is_75_percent_and_speeds_up(client, bolt, uinput):
    scroll(client, speed=0.5)
    assert bolt.mouse.wheel_mode & TARGET_BIT       # no longer neutral: 2x
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [240])


def test_the_slowest_speed_slows_the_wheel(client, bolt, uinput):
    scroll(client, speed=SLOWEST)
    assert bolt.mouse.wheel_mode & TARGET_BIT
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [30])     # a quarter of a notch
    assert uinput.rels(REL_WHEEL) == []


def test_the_slowest_thumb_speed_slows_the_thumb_wheel(client, bolt, uinput):
    thumb(client, speed=SLOWEST, isSmooth=True)
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES) == [30])


def test_the_live_value_is_nearly_the_fastest(client, bolt, uinput):
    scroll(client, speed=0.98)
    turn(bolt, MULTIPLIER)
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES))
    assert 450 <= uinput.rels(REL_WHEEL_HI_RES)[0] <= 480


# ---- thumb wheel: travel per notch and direction as native (live, MX Master 3S) -------------------

def test_a_thumb_wheel_at_neutral_speed_would_travel_a_notch_per_notch(client, bolt, uinput):
    """The mouse answers getThumbwheelInfo with 18 native / 120 diverted units per revolution:
    6.67 diverted units are one notch, not 120 (which made max speed travel less than native)."""
    bolt.mouse.thumb_native_resolution, bolt.mouse.thumb_diverted_resolution = 18, 120
    thumb(client, speed=0.01, isSmooth=True)         # barely off neutral: diverted, ~1x
    assert bolt.turn_thumb(20)                       # 3 notches
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES))
    assert 355 <= uinput.rels(REL_HWHEEL_HI_RES)[0] <= 370


def test_the_fastest_thumb_speed_travels_four_notches_per_notch(client, bolt, uinput):
    bolt.mouse.thumb_native_resolution, bolt.mouse.thumb_diverted_resolution = 18, 120
    thumb(client, speed=FAST, isSmooth=True)
    assert bolt.turn_thumb(20)                       # 3 notches, 4x
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES))
    assert uinput.rels(REL_HWHEEL_HI_RES) == [1440]
    assert uinput.rels(REL_HWHEEL) == [12]


@pytest.mark.parametrize("default_direction", [0, 1])
def test_a_thumb_turn_to_the_right_is_relayed_to_the_right(client, bolt, uinput, default_direction):
    """The real mouse (direction byte 0) reports a turn to the right as a negative rotation."""
    bolt.mouse.thumb_default_direction = default_direction
    thumb(client, speed=FAST, isSmooth=True)
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)      # to the right
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES))
    assert uinput.rels(REL_HWHEEL_HI_RES) == [480]


# ---- the thumb wheel has one owner: a thumb action card outranks thumb speed (#23 vs #25) ------

from tests.fakes.hidpp_device import F_WAKE                                     # noqa: E402
from tests.harness import keys_down                                             # noqa: E402
from tests.test_gestures import THUMB_DEFAULT, assign_thumb, thumb_card         # noqa: E402

KEY_RIGHT = 18                                                                  # thumb_card's right action


def test_a_thumb_action_card_wins_over_thumb_speed(client, bolt, uinput):
    thumb(client, speed=FAST)
    assert bolt.mouse.thumb["mode"] == 1
    assert assign_thumb(client)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 1
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: keys_down(uinput) == [KEY_RIGHT])
    bolt.settle()
    assert uinput.rels(REL_HWHEEL) == [] and uinput.rels(REL_HWHEEL_HI_RES) == []   # not scaled as well
    assert keys_down(uinput) == [KEY_RIGHT]                                         # not run twice
    assert thumb_settings(client)["speed"] == FAST                                  # the choice is kept


def test_a_speed_set_while_the_card_is_bound_is_kept_but_not_applied(client, bolt, uinput):
    assign_thumb(client)
    reply = thumb(client, speed=FAST, dir="NATURAL")
    assert reply["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 1 and bolt.mouse.thumb["invert"] == 1
    assert thumb_settings(client)["speed"] == FAST
    bolt.turn_thumb(14)
    assert wait_for(lambda: len(keys_down(uinput)) >= 1)               # one notch, one action
    bolt.settle()
    assert uinput.rels(REL_HWHEEL_HI_RES) == []


def test_removing_the_card_gives_the_wheel_back_to_the_speed_relay(client, bolt, uinput):
    thumb(client, speed=FAST)
    assign_thumb(client)
    assert assign_thumb(client, THUMB_DEFAULT)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 1                                            # relay diverts again
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL) == [4])
    assert keys_down(uinput) == []


def test_removing_the_card_with_neutral_speed_leaves_the_wheel_native(client, bolt):
    assign_thumb(client)
    assert assign_thumb(client, THUMB_DEFAULT)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 0


def test_neutral_speed_does_not_hand_back_a_wheel_the_card_holds(client, bolt):
    thumb(client, speed=FAST)
    assign_thumb(client)
    assert thumb(client, speed=NEUTRAL)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.thumb["mode"] == 1


def test_reapply_after_wake_keeps_the_card_as_owner(client, bolt, uinput):
    thumb(client, speed=FAST)
    assign_thumb(client)
    bolt.mouse.thumb["mode"] = 0
    bolt.emit_event(F_WAKE)
    assert wait_for(lambda: bolt.mouse.thumb["mode"] == 1)
    bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: keys_down(uinput) == [KEY_RIGHT])
    bolt.settle()
    assert uinput.rels(REL_HWHEEL_HI_RES) == []


def test_reapply_after_wake_keeps_the_relay_when_no_card_is_bound(client, bolt, uinput):
    thumb(client, speed=FAST)
    bolt.mouse.thumb["mode"] = 0
    bolt.emit_event(F_WAKE)
    assert wait_for(lambda: bolt.mouse.thumb["mode"] == 1)
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL) == [4])


def test_reset_hands_the_thumb_wheel_back_whoever_held_it(client, bolt):
    thumb(client, speed=FAST)
    assign_thumb(client)
    reset(client)
    assert bolt.mouse.thumb["mode"] == 0


def test_thumb_direction_survives_a_card_being_added_and_removed(client, bolt, uinput):
    thumb(client, speed=FAST, dir="NATURAL")
    assign_thumb(client)
    assign_thumb(client, THUMB_DEFAULT)
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES))
    assert uinput.rels(REL_HWHEEL_HI_RES) == [-480]


class _Recorded(dict):
    """A stand-in for the simulated thumb status that remembers every reporting mode written."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.modes = []

    def update(self, *a, **kw):
        super().update(*a, **kw)
        self.modes.append(self["mode"])


def test_reapplying_after_wake_does_not_hand_the_relayed_thumb_wheel_back_and_forth(agent, client, bolt):
    thumb(client, speed=FAST)
    bolt.mouse.thumb = _Recorded(bolt.mouse.thumb)
    bolt.emit_event(F_WAKE)
    agent.settle()
    assert 0 not in bolt.mouse.thumb.modes
    assert bolt.mouse.thumb["mode"] == 1


def test_a_failed_thumb_reapply_does_not_leave_the_relay_believing_it_is_diverted(agent, client, bolt):
    thumb(client, speed=FAST)
    bolt.mouse.thumb["mode"] = 0                       # the mouse forgot while asleep
    bolt.mouse.fail_thumb_write = True
    bolt.emit_event(F_WAKE)
    agent.settle()
    assert not agent.agent.router.relay.axis(agent.agent.reg.devices["dev00000000"], "thumb").diverted
    bolt.mouse.fail_thumb_write = False
    bolt.emit_event(F_WAKE)
    assert wait_for(lambda: bolt.mouse.thumb["mode"] == 1)


# ---- recovery: a diversion nothing asked for, left by a crashed agent or rewritten by the kernel ----

def test_a_wheel_left_diverted_by_a_crashed_agent_is_handed_back_when_the_speed_is_neutral(agent_factory, bolt):
    bolt.mouse.wheel_mode |= TARGET_BIT
    bolt.mouse.thumb["mode"] = 1
    c = agent_factory().client()
    try:
        assert bolt.mouse.wheel_mode & TARGET_BIT == 0
        assert bolt.mouse.thumb["mode"] == 0
    finally:
        c.close()


def test_a_wheel_left_diverted_is_handed_back_when_the_relay_is_switched_off(agent_factory, bolt, monkeypatch):
    first = agent_factory()
    c = first.client()
    scroll(c, speed=FAST)
    c.close()
    first.stop()
    bolt.mouse.wheel_mode |= TARGET_BIT          # as a crash would have left it
    bolt.mouse.thumb["mode"] = 1
    monkeypatch.setenv("OPL_WHEEL_RELAY", "0")
    agent_factory().client().close()
    assert bolt.mouse.wheel_mode & TARGET_BIT == 0 and bolt.mouse.thumb["mode"] == 0


def test_a_wheel_left_diverted_stays_with_the_relay_when_a_speed_is_stored(agent_factory, bolt, uinput):
    first = agent_factory()
    c = first.client()
    scroll(c, speed=FAST)
    c.close()
    first.stop()
    bolt.mouse.wheel_mode |= TARGET_BIT
    c2 = agent_factory().client()
    try:
        assert bolt.mouse.wheel_mode & TARGET_BIT
        turn(bolt, MULTIPLIER)
        assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [480])
    finally:
        c2.close()


def test_a_diversion_the_kernel_driver_rewrote_after_a_reconnect_is_asserted_again(agent, client, bolt, uinput):
    from opl_agent.wheel_relay import WheelRelay
    WheelRelay.REASSERT_DELAY = 0.3
    try:
        scroll(client, speed=FAST)
        thumb(client, speed=FAST)
        bolt.emit_event(F_WAKE)
        agent.settle()
        bolt.mouse.wheel_mode &= ~TARGET_BIT          # hid-logitech-hidpp enabled its own mode afterwards
        bolt.mouse.thumb["mode"] = 0
        assert wait_for(lambda: bolt.mouse.wheel_mode & TARGET_BIT and bolt.mouse.thumb["mode"] == 1)
        turn(bolt, MULTIPLIER)
        assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [480])
    finally:
        WheelRelay.REASSERT_DELAY = 2.0


def test_a_lost_thumb_diversion_is_not_asserted_again_without_a_scale(agent, client, bolt, uinput):
    import time
    from opl_agent.wheel_relay import WheelRelay
    WheelRelay.REASSERT_DELAY = 0.2
    try:
        thumb(client, speed=FAST)
        bolt.emit_event(F_WAKE)
        agent.settle()
        bolt.mouse.thumb["mode"] = 0
        bolt.mouse.thumb_diverted_resolution = 0         # the mouse no longer says how big a notch is
        time.sleep(1.0)
        assert bolt.mouse.thumb["mode"] == 0             # stays native, and nothing divided by zero
        assert not bolt.turn_thumb(7)
    finally:
        WheelRelay.REASSERT_DELAY = 2.0


# ---- thumb direction: the user's invert flips the sign once, whatever the mouse's own byte ---

@pytest.mark.parametrize("default_direction", [0, 1])
def test_thumb_invert_flips_the_relayed_sign_exactly_once(client, bolt, uinput, default_direction):
    bolt.mouse.thumb_default_direction = default_direction
    thumb(client, speed=FAST, isSmooth=True, dir="STANDARD")
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)           # to the right
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES) == [480])
    thumb(client, speed=FAST, isSmooth=True, dir="NATURAL")
    assert bolt.mouse.thumb["invert"] == 0                # applied here, not by the mouse as well
    assert bolt.turn_thumb(DIVERTED_RESOLUTION)
    assert wait_for(lambda: uinput.rels(REL_HWHEEL_HI_RES) == [480, -480])


@pytest.mark.parametrize("default_direction", [0, 1])
def test_a_neutral_thumb_stays_native_with_the_users_invert_and_no_sign(client, bolt, uinput, default_direction):
    bolt.mouse.thumb_default_direction = default_direction
    thumb(client, speed=NEUTRAL, dir="NATURAL")
    assert bolt.mouse.thumb["mode"] == 0 and bolt.mouse.thumb["invert"] == 1
    assert not bolt.turn_thumb(DIVERTED_RESOLUTION)        # nothing is reported to software
    assert uinput.events == []
    thumb(client, speed=NEUTRAL, dir="STANDARD")
    assert bolt.mouse.thumb["mode"] == 0 and bolt.mouse.thumb["invert"] == 0


# ---- accumulation and the speed a relay that cannot run must not forget --------------------

def test_reversing_direction_drops_the_motion_held_back_the_other_way(client, bolt, uinput):
    scroll(client, speed=SLOW, isSmooth=False)       # 0.5x, whole notches only
    turn(bolt, MULTIPLIER)                           # +60 of a notch, held back
    turn(bolt, -MULTIPLIER)                          # reversed: starts from nothing, -60 held back
    turn(bolt, -MULTIPLIER)                          # -120: one notch at once
    assert wait_for(lambda: uinput.rels(REL_WHEEL_HI_RES) == [-120])
    assert uinput.rels(REL_WHEEL) == [-1]


def test_a_speed_stored_before_the_relay_became_unavailable_is_not_forgotten(agent, client, bolt):
    scroll(client, speed=FAST)
    agent.agent.router.set_wheel_relay(False)
    assert scroll(client, dir="NATURAL")["result"]["code"] == "SUCCESS"
    agent.agent.router.set_wheel_relay(True)
    assert scroll_settings(client)["speed"] == FAST
    assert bolt.mouse.wheel_mode & TARGET_BIT


def test_a_diversion_the_mouse_does_not_keep_is_a_failure(client, bolt):
    bolt.mouse.thumb_ignores_mode = True             # takes the write, still reports native
    reply = thumb(client, speed=FAST)
    assert reply["result"]["code"] != "SUCCESS"


# ---- one writer at a time per wheel --------------------------------------------------------

def test_two_settings_changes_of_one_wheel_do_not_interleave():
    import threading
    from opl_agent import wheel_relay

    entered, release, order = threading.Event(), threading.Event(), []

    class Reg:
        on_wheel_event = []

        def read_scroll(self, dev_id):
            order.append("read")
            return {"wheel": {"invert": False, "hires": True}}

        def wheel_units_per_notch(self, dev_id):
            return 8

        def divert_wheel(self, dev_id, divert, invert=None, hires=None):
            order.append("write")
            entered.set()
            assert release.wait(5)
            return {"target": True}

    class Dev:
        id = "d"
        thumb_wheel = False
        scroll_caps = {"highResolution": True}

    relay = wheel_relay.WheelRelay(Reg(), lambda: object())
    first = threading.Thread(target=lambda: relay.set_wheel(Dev, speed=FAST))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=lambda: relay.set_wheel(Dev, speed=SLOW))
    second.start()
    second.join(0.3)
    assert second.is_alive() and order == ["read", "write"]      # waits for the first to commit
    release.set()
    first.join(5)
    second.join(5)
    assert order == ["read", "write", "write"]                   # the second saw the first's result
