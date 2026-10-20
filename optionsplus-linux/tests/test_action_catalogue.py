"""Action picker catalogue (ticket #20): served from the fetched depot through the Linux filter.

Seam: the agent socket, against the synthetic depot of tests/fakes/depot.py."""
import json
import os
import shutil

from tests.fakes import depot as fx

TYPE = "type.googleapis.com/"
DEFAULT_PROFILE = "420fd454-0c36-499d-bde4-146823b16147"
STRINGS = {"packages": ["logioptionsplus/data/strings"]}


def ids(cards):
    return [c["id"] for c in cards]


def presets(client):
    reply = client.call("GET", "/macros/presets", {"type": "SYSTEM"})
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["payload"]["@type"] == TYPE + "logi.protocol.rap.CardList"
    return reply["payload"]["cards"]


def strings(client):
    return client.call("GET", "/resources/localized_packages", STRINGS)["payload"]["packages"][0]["strings"]


def test_presets_come_from_the_depot_without_windows_only_entries(client):
    # one copy of card_play although two files list it; the mac file is ignored
    assert ids(presets(client)) == ["card_play", "card_next_desktop", "card_snip"]


def test_card_names_stay_string_keys_so_picker_search_finds_them(client):
    text = strings(client)
    names = {c["id"]: text[c["name"]] for c in presets(client)}
    assert names["card_next_desktop"] == "Next desktop"
    assert "play" in names["card_play"].lower()


def test_windows_names_are_relabelled_where_a_linux_equivalent_exists(client):
    text = strings(client)
    names = {c["id"]: c["name"] for c in presets(client)}
    assert text[names["card_snip"]] == "Screenshot"
    assert text[names["card_play"]] == "Play/Pause"      # untouched


def test_icons_resolve_to_the_system_actions_depot(client, workdir):
    play = next(c for c in presets(client) if c["id"] == "card_play")
    assert play["icons"]["uri"] == "file://" + os.path.join(workdir, "depots", "system_actions") + "/"
    assert play["icons"]["icons"] == ["card_play.png"]


def test_icon_uri_is_blank_when_system_actions_is_not_installed(agent_factory, workdir):
    shutil.rmtree(os.path.join(workdir, "depots", "system_actions"))
    c = agent_factory().client()
    try:
        play = next(x for x in presets(c) if x["id"] == "card_play")
        assert play["icons"]["uri"] == ""
    finally:
        c.close()


def test_recommended_actions_are_served_and_filtered(client):
    payload = client.call("GET", "/rac", {"deviceId": "dev00000000"})["payload"]
    assert payload["@type"] == TYPE + "logi.protocol.rap.RACContainer"
    slot = payload["recommendedCards"]["racList"][0]["slotCardList"][0]
    assert slot["slotId"] == "046d_4242_c83"
    assert ids(slot["cards"]) == ["card_play"]


def test_a_macro_with_any_hidden_step_is_dropped_whole(client):
    # macros_preset_test holds a hidden step: half a macro would do the wrong thing
    infos = client.call("GET", "/macros/predefined")["payload"]["macroInfos"]
    assert [i["id"] for i in infos] == ["macros_preset_ok"]
    assert ids(infos[0]["cards"]) == ["card_play", "card_snip"]


def test_macro_assignments_only_name_macros_that_are_served(client):
    vals = client.call("GET", "/macro_assignments/all")["payload"]["macroAssignmentVals"]
    assert [(v["macroId"], v["slotId"]) for v in vals] == [("macros_preset_ok", "046d_4242_c86")]


def test_a_hidden_step_nested_at_any_depth_drops_the_macro(agent_factory, workdir):
    ok, bad = fx.keystroke_card("ok", "N"), fx.keystroke_card("card_cortana", "N")
    step = fx.keystroke_card("step", "N", nestedCards={"d": ok, "x": bad}, nestedCardsOrder=["d", "x"])
    path = os.path.join(workdir, "depots", "logioptionsplus", "data", "macros", "more_win.json")
    with open(path, "w") as fh:
        json.dump({"macroInfos": [{"id": "macros_nested", "name": "N", "cards": [step]}]}, fh)
    c = agent_factory().client()
    try:
        infos = c.call("GET", "/macros/predefined")["payload"]["macroInfos"]
        assert "macros_nested" not in [i["id"] for i in infos]
    finally:
        c.close()


def test_macros_named_for_windows_only_features_are_hidden(agent_factory, workdir):
    path = os.path.join(workdir, "depots", "logioptionsplus", "data", "macros", "more_win.json")
    with open(path, "w") as fh:
        json.dump({"macroInfos": [{"id": "macros_preset_browser_copilot", "name": "N",
                                   "cards": [fx.keystroke_card("plain", "N")]}]}, fh)
    c = agent_factory().client()
    try:
        infos = c.call("GET", "/macros/predefined")["payload"]["macroInfos"]
        assert "macros_preset_browser_copilot" not in [i["id"] for i in infos]
    finally:
        c.close()


def card_with(macro, cid="card_x"):
    return fx.keystroke_card(cid, "N", macro=macro)


def test_windows_only_cards_are_hidden_by_what_they_launch_not_only_by_id():
    from opl_agent import actions
    hidden = [
        card_with({"type": "APP", "app": {"executablePath": "C:\\Windows\\explorer.exe"}}),
        card_with({"type": "APP", "app": {"executablePath": "%windir%\\system32\\calc"}}),
        card_with({"type": "OPEN_FILE_FOLDER", "openFileFolder": {"appName": "Microsoft OneDrive"}}),
        fx.keystroke_card("win_action_center", "N"),
        fx.keystroke_card("win_dictation", "N"),
        fx.keystroke_card("win_settings", "N"),
        fx.keystroke_card("card_global_presets_this_pc", "N"),
        fx.keystroke_card("presets_search", "N"),
    ]
    for card in hidden:
        assert not actions.is_linux_card(card), card["id"]
    assert actions.is_linux_card(card_with({"type": "OPEN_WEB_PAGE", "openWebPage": {"url": "https://example.org/a.exe.html"}}))


def test_icon_paths_cannot_leave_the_depot(client, workdir):
    from opl_agent import actions
    assert actions._icon_dir("pipeline://system_actions/") != ""
    for uri in ("pipeline://system_actions/../..", "pipeline://system_actions/../logioptionsplus/data",
                "pipeline://../depots/", "pipeline://logioptionsplus/../../../"):
        assert actions._icon_dir(uri) == "", uri


def test_default_assignments_merge_control_and_slot_defaults(client):
    reply = client.call("GET", "/unified_profiles/assignment/default")
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["payload"]["@type"] == TYPE + "logi.protocol.profiles_v2.Defaults"
    assert [a["slotId"] for a in reply["payload"]["assignments"]] == ["046d_4242_c83", "046d_4242_c86"]


def test_nested_cards_are_filtered_and_an_empty_folder_disappears():
    from opl_agent import actions
    ok, bad = fx.keystroke_card("ok", "N"), fx.keystroke_card("card_cortana", "N")
    folder = fx.keystroke_card("folder", "N", attribute="FOLDER", macro=None,
                               nestedCards={"ok": ok, "card_cortana": bad},
                               nestedCardsOrder=["ok", "card_cortana"], selectedNestedCard="card_cortana")
    kept = actions.filter_cards([folder])
    assert ids(kept) == ["folder"]
    assert list(kept[0]["nestedCards"]) == ["ok"]
    assert kept[0]["nestedCardsOrder"] == ["ok"] and kept[0]["selectedNestedCard"] == ""
    empty = dict(folder, nestedCards={"card_cortana": bad}, nestedCardsOrder=["card_cortana"])
    assert actions.filter_cards([empty]) == []


def test_catalogue_is_empty_not_an_error_when_the_depot_has_none(agent_factory, workdir):
    d = os.path.join(workdir, "depots", "logioptionsplus", "data")
    for sub in ("card_presets", "rap", "defaults", "macros"):
        shutil.rmtree(os.path.join(d, sub))
    c = agent_factory().client()
    try:
        assert presets(c) == []
        assert c.call("GET", "/rac", {"deviceId": "x"})["result"]["code"] == "SUCCESS"
        assert c.call("GET", "/unified_profiles/assignment/default")["payload"]["assignments"] == []
    finally:
        c.close()


# ---- choosing an action stores the assignment ----------------------------

BACK = fx.SLOT_PREFIX + "_c83"


def choose(client, card, slot=BACK):
    return client.call("SET", "/v2/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE, "assignment": {"slotId": slot, "cardId": card["id"], "card": card}})


def assignment(client, slot=BACK):
    profile = client.call("GET", "/v2/profiles")["payload"]["profiles"][0]
    return [a for a in profile["assignments"] if a["slotId"] == slot]


def test_chosen_action_replaces_the_default_in_the_profile(client):
    reply = choose(client, presets(client)[0])
    assert reply["result"]["code"] == "SUCCESS"
    got = assignment(client)
    assert len(got) == 1 and got[0]["cardId"] == "card_play"
    assert got[0]["card"]["macro"]["type"] == "KEYSTROKE"


def test_chosen_action_survives_an_agent_restart(agent_factory):
    a = agent_factory()
    c = a.client()
    choose(c, presets(c)[0])
    c.close()
    a.stop()
    c = agent_factory().client()
    try:
        assert assignment(c)[0]["cardId"] == "card_play"
    finally:
        c.close()


def test_choosing_in_an_ops_batch_also_stores(client):
    card = presets(client)[0]
    slot = fx.SLOT_PREFIX + "_c86"
    reply = client.call("SET", "/v2/assignment", {"profileId": DEFAULT_PROFILE, "ops": [
        {"set": {"assignment": {"slotId": slot, "cardId": card["id"], "card": card}}}]})
    assert reply["result"]["code"] == "SUCCESS"
    assert assignment(client, slot)[0]["cardId"] == "card_play"


def test_profile_reset_restores_the_default_assignment(client):
    choose(client, presets(client)[0])
    client.call("SET", "/v2/profiles/reset", {})
    assert assignment(client)[0]["cardId"] == "card-back"
