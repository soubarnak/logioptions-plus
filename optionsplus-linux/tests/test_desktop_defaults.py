"""Per-desktop defaults for the SYSTEM actions and the default gesture card, chosen from
XDG_CURRENT_DESKTOP (ticket #23). GNOME stays the fallback; actions.toml overrides everything."""

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


def test_the_workspace_actions_are_not_offered_in_the_picker():
    from opl_agent import actions
    assert "SWITCH_WORKSPACE_UP" not in repr(vars(actions))
