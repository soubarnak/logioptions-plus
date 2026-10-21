"""Wheel speed moved from 0..1 (0.5 neutral, the agent's own default) to the UI's -1..1 (0 neutral).
profiles.json written before the move holds 0.5 in the cards the agent seeded."""

import json

import pytest

from opl_agent import profiles

SCROLL = "slot_mouse_scroll_wheel_settings"
THUMB = "slot_mouse_thumb_wheel_settings"


def scroll_card(speed):
    return {"id": "MOUSE_SCROLL_WHEEL_SETTINGS", "mouseScrollWheelSettings": {"isSmooth": False, "speed": speed}}


def thumb_card(speed):
    return {"id": "MOUSE_THUMB_WHEEL_SETTINGS", "mouseThumbWheelSettings": {"isSmooth": False, "speed": speed}}


def write(tmp_path, data):
    (tmp_path / "profiles.json").write_text(json.dumps(data))


def speeds(store):
    return (store.get(SCROLL)["mouseScrollWheelSettings"]["speed"],
            store.get(THUMB)["mouseThumbWheelSettings"]["speed"])


def test_an_old_profile_with_the_old_neutral_becomes_neutral(tmp_path):
    write(tmp_path, {"assignments": {SCROLL: scroll_card(0.5), THUMB: thumb_card(0.5)}})
    assert speeds(profiles.ProfileStore(str(tmp_path))) == (0, 0)


def test_other_old_values_stay(tmp_path):
    write(tmp_path, {"assignments": {SCROLL: scroll_card(0.98), THUMB: thumb_card(0)}})
    assert speeds(profiles.ProfileStore(str(tmp_path))) == (0.98, 0)


def test_an_applications_wheel_cards_are_migrated_too(tmp_path):
    write(tmp_path, {"assignments": {}, "applications": {"app": {SCROLL: scroll_card(0.5)}}})
    store = profiles.ProfileStore(str(tmp_path))
    assert store.apps["app"][SCROLL]["mouseScrollWheelSettings"]["speed"] == 0


def test_the_migration_runs_once(tmp_path):
    write(tmp_path, {"assignments": {SCROLL: scroll_card(0.5)}})
    profiles.ProfileStore(str(tmp_path))
    assert json.loads((tmp_path / "profiles.json").read_text())["speedScale"] == profiles.SPEED_SCALE
    store = profiles.ProfileStore(str(tmp_path))
    store.set(SCROLL, scroll_card(0.5))                 # 75% chosen in the UI afterwards
    assert profiles.ProfileStore(str(tmp_path)).get(SCROLL)["mouseScrollWheelSettings"]["speed"] == 0.5


def test_a_card_that_is_not_a_wheel_card_is_left_alone(tmp_path):
    other = {"id": "x", "mouseScrollWheelSettings": {"speed": 0.5}}
    write(tmp_path, {"assignments": {"slot_other": other}})
    assert profiles.ProfileStore(str(tmp_path)).get("slot_other") == other


@pytest.mark.parametrize("scale", [None, "2", [], True])
def test_a_marker_that_is_no_number_counts_as_unmigrated(tmp_path, caplog, scale):
    write(tmp_path, {"speedScale": scale, "assignments": {SCROLL: scroll_card(0.5)}})
    store = profiles.ProfileStore(str(tmp_path))
    assert store.get(SCROLL)["mouseScrollWheelSettings"]["speed"] == 0
    assert "unreadable" not in caplog.text


@pytest.mark.parametrize("scale", [2, 2.0, 3])
def test_a_marker_that_is_a_real_number_at_the_scale_counts_as_migrated(tmp_path, scale):
    write(tmp_path, {"speedScale": scale, "assignments": {SCROLL: scroll_card(0.5)}})
    assert profiles.ProfileStore(str(tmp_path)).get(SCROLL)["mouseScrollWheelSettings"]["speed"] == 0.5


def test_a_new_store_writes_the_current_scale(tmp_path):
    store = profiles.ProfileStore(str(tmp_path))
    store.set(SCROLL, scroll_card(0.25))
    assert json.loads((tmp_path / "profiles.json").read_text())["speedScale"] == profiles.SPEED_SCALE
