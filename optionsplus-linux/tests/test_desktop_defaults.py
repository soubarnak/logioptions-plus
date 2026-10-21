"""Per-desktop defaults for the SYSTEM actions and the default gesture card, chosen from
XDG_CURRENT_DESKTOP (ticket #23). GNOME stays the fallback; actions.toml overrides everything."""

import json
import os

import pytest

from opl_agent import engine, sysactions
from tests.test_gestures import default_gesture_card

M, KEY_O, KEY_LEFT, KEY_RIGHT, KEY_PAGEUP, KEY_PAGEDOWN = 125, 24, 105, 106, 104, 109


def chord(desktop, action, path=None):
    return sysactions.ActionTable(path, desktop=desktop).chord(action)


def test_the_desktop_is_read_from_a_colon_separated_list_in_any_case():
    assert sysactions.desktop_names("Niri") == ["niri"]
    assert sysactions.desktop_names("ubuntu:GNOME") == ["ubuntu", "gnome"]
    assert sysactions.desktop_names(None) == [] and sysactions.desktop_names("") == []


def test_the_table_reads_the_environment_when_no_desktop_is_given(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "niri")
    assert sysactions.ActionTable().chord("TASK_VIEW") == (M, KEY_O)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP")
    assert sysactions.ActionTable().chord("TASK_VIEW") == (M,)


@pytest.mark.parametrize("desktop", [None, "", "GNOME", "KDE", "unknown"])
def test_other_desktops_keep_the_gnome_defaults(desktop):
    assert chord(desktop, "TASK_VIEW") == (M,) and chord(desktop, "APP_EXPOSE") == (M,)
    assert chord(desktop, "SWITCH_BETWEEN_DESKTOPS_LEFT") == (M, KEY_PAGEUP)
    assert chord(desktop, "SWITCH_BETWEEN_DESKTOPS_RIGHT") == (M, KEY_PAGEDOWN)


@pytest.mark.parametrize("desktop", ["niri", "NIRI", "niri:GNOME"])
def test_niri_defaults(desktop):
    assert chord(desktop, "TASK_VIEW") == (M, KEY_O) and chord(desktop, "APP_EXPOSE") == (M, KEY_O)
    assert chord(desktop, "SWITCH_BETWEEN_DESKTOPS_LEFT") == (M, KEY_LEFT)
    assert chord(desktop, "SWITCH_BETWEEN_DESKTOPS_RIGHT") == (M, KEY_RIGHT)


@pytest.mark.parametrize("desktop", [None, "niri", "GNOME"])
def test_the_workspace_actions_default_to_super_page_up_and_down_everywhere(desktop):
    assert chord(desktop, "SWITCH_WORKSPACE_UP") == (M, KEY_PAGEUP)
    assert chord(desktop, "SWITCH_WORKSPACE_DOWN") == (M, KEY_PAGEDOWN)


def test_actions_toml_overrides_the_desktop_defaults(tmp_path):
    path = tmp_path / "actions.toml"
    path.write_text('[system]\nTASK_VIEW = "super+tab"\nSWITCH_WORKSPACE_UP = "ctrl+alt+up"\n')
    assert chord("niri", "TASK_VIEW", str(path)) == sysactions.parse_chord("super+tab")
    assert chord("niri", "SWITCH_WORKSPACE_UP", str(path)) == sysactions.parse_chord("ctrl+alt+up")
    assert chord("niri", "APP_EXPOSE", str(path)) == (M, KEY_O)       # not overridden: the desktop default


def gesture_actions(desktop):
    table = sysactions.ActionTable(desktop=desktop)
    return {word: steps for word, steps in engine.compile_binding(default_gesture_card(), table).actions.items()}


def test_the_default_gesture_card_on_gnome_is_unchanged():
    assert gesture_actions("GNOME") == {
        "click": [("system", "TASK_VIEW")], "up": [("system", "TASK_VIEW")],
        "down": [("system", "SHOW_DESKTOP")],
        "left": [("system", "SWITCH_BETWEEN_DESKTOPS_LEFT")], "right": [("system", "SWITCH_BETWEEN_DESKTOPS_RIGHT")]}


def test_the_default_gesture_card_on_niri_moves_through_columns_and_workspaces():
    assert gesture_actions("niri") == {
        "click": [("system", "TASK_VIEW")], "up": [("system", "SWITCH_WORKSPACE_UP")],
        "down": [("system", "SWITCH_WORKSPACE_DOWN")],
        "left": [("system", "SWITCH_BETWEEN_DESKTOPS_LEFT")], "right": [("system", "SWITCH_BETWEEN_DESKTOPS_RIGHT")]}


def test_a_niri_card_swap_applies_only_to_a_card_still_holding_the_depot_chord():
    table = sysactions.ActionTable(desktop="niri")
    own = {"id": "inner_card_vd_start_menu", "attribute": "MACRO_PLAYBACK",
           "macro": {"type": "KEYSTROKE", "keystroke": {"code": 4, "modifiers": [224]}}}      # Ctrl+A
    assert engine.compile_card(own, table) == [("emit", [(1, 29, 1), (1, 30, 1), (1, 30, 0), (1, 29, 0)])]


def test_the_top_level_desktop_presets_follow_niri_columns():
    table = sysactions.ActionTable(desktop="niri")
    card = {"id": "card_global_presets_desktop_left", "attribute": "MACRO_PLAYBACK",
            "macro": {"type": "KEYSTROKE", "keystroke": {"code": 80, "modifiers": [224, 227]}}}
    assert engine.compile_card(card, table) == [("system", "SWITCH_BETWEEN_DESKTOPS_LEFT")]
    assert table.chord("SWITCH_BETWEEN_DESKTOPS_LEFT") == (M, KEY_LEFT)


def test_the_first_listed_name_with_an_entry_wins_and_an_unknown_one_falls_through():
    assert chord("GNOME:niri", "TASK_VIEW") == (M, KEY_O)           # GNOME has no entry: niri's applies
    assert chord("someotherdesktop:niri", "TASK_VIEW") == (M, KEY_O)
    assert chord("niri:GNOME", "TASK_VIEW") == (M, KEY_O)
    assert chord("GNOME:KDE", "TASK_VIEW") == (M,)


def test_an_edit_of_actions_toml_applies_to_a_niri_table_that_already_read_it(tmp_path):
    path = tmp_path / "actions.toml"
    path.write_text('[system]\nTASK_VIEW = "super+tab"\n')
    table = sysactions.ActionTable(str(path), desktop="niri")
    assert table.chord("TASK_VIEW") == sysactions.parse_chord("super+tab")
    path.write_text('[system]\nSWITCH_WORKSPACE_DOWN = "ctrl+alt+down"\n')
    os.utime(path, ns=(1, 2_000_000_000))                    # a different stamp even on a coarse clock
    assert table.chord("TASK_VIEW") == (M, KEY_O)            # the niri default is back
    assert table.chord("SWITCH_WORKSPACE_DOWN") == sysactions.parse_chord("ctrl+alt+down")


def test_the_table_logs_the_profile_it_chose(caplog):
    with caplog.at_level("INFO", logger="opl.sysactions"):
        sysactions.ActionTable(desktop="niri")
        sysactions.ActionTable(desktop=None)
    assert "niri defaults (XDG_CURRENT_DESKTOP=niri)" in caplog.text
    assert "GNOME defaults (XDG_CURRENT_DESKTOP unset)" in caplog.text


def test_the_workspace_actions_are_not_in_what_the_ui_is_served(client):
    from tests.test_action_catalogue import presets, rac
    served = json.dumps([presets(client), rac(client)])
    assert "SWITCH_WORKSPACE" not in served


# Issue #37: niri's own default config (resources/default-config.kdl) binds these; the GNOME chords
# for the same actions run something else there (Super+Up is focus-window-up, Super+D the launcher).
KEY_Q, KEY_L, KEY_F, KEY_LEFTALT = 16, 38, 33, 56
KEY_LEFTSHIFT = 42


@pytest.mark.parametrize("action, expected", [
    ("CLOSE", (M, KEY_Q)),                          # Mod+Q: close-window
    ("LOCK", (M, KEY_LEFTALT, KEY_L)),              # Super+Alt+L: spawn swaylock
    ("MAXIMIZE", (M, KEY_F)),                       # Mod+F: maximize-column
    ("FULLSCREEN", (M, KEY_LEFTSHIFT, KEY_F)),      # Mod+Shift+F: fullscreen-window
])
def test_niri_uses_its_own_default_binds(action, expected):
    assert chord("niri", action) == expected


@pytest.mark.parametrize("action", ["SHOW_DESKTOP", "DESKTOP", "MINIMIZE"])
def test_niri_leaves_actions_it_has_no_default_bind_for_unassigned(action):
    assert chord("niri", action) is None
    assert chord("GNOME", action) is not None       # the other desktops keep theirs


def test_an_unassigned_niri_action_is_refused_until_actions_toml_sets_it(tmp_path):
    card = {"id": "x", "attribute": "MACRO_PLAYBACK",
            "macro": {"type": "SYSTEM", "system": {"action": "MINIMIZE"}}}
    with pytest.raises(engine.Unsupported, match="MINIMIZE"):
        engine.compile_card(card, sysactions.ActionTable(desktop="niri"))
    path = tmp_path / "actions.toml"
    path.write_text('[system]\nMINIMIZE = "super+h"\n')
    table = sysactions.ActionTable(str(path), desktop="niri")
    assert engine.compile_card(card, table) == [("system", "MINIMIZE")]
    assert table.chord("MINIMIZE") == sysactions.parse_chord("super+h")


def test_other_desktops_keep_the_gnome_window_actions():
    assert chord("GNOME", "CLOSE") == (KEY_LEFTALT, 62) and chord("GNOME", "LOCK") == (M, KEY_L)
    assert chord("GNOME", "MAXIMIZE") == (M, 103) and chord("GNOME", "FULLSCREEN") == (87,)
