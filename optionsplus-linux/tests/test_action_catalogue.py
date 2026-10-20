"""Action picker catalogue (ticket #20): served from the fetched depot through the Linux filter.

Seam: the agent socket, against the synthetic depot of tests/fakes/depot.py."""
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
    assert slot["slotId"] == "046d_4242_c82"
    assert ids(slot["cards"]) == ["card_play"]


def test_predefined_macros_drop_hidden_cards_and_emptied_macros(client):
    infos = client.call("GET", "/macros/predefined")["payload"]["macroInfos"]
    assert [i["id"] for i in infos] == ["macros_preset_test"]
    assert ids(infos[0]["cards"]) == ["card_play", "card_snip"]


def test_macro_assignments_are_served(client):
    vals = client.call("GET", "/macro_assignments/all")["payload"]["macroAssignmentVals"]
    assert [(v["macroId"], v["slotId"]) for v in vals] == [("macros_preset_test", "046d_4242_c82")]


def test_default_assignments_merge_control_and_slot_defaults(client):
    reply = client.call("GET", "/unified_profiles/assignment/default")
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["payload"]["@type"] == TYPE + "logi.protocol.profiles_v2.Defaults"
    assert [a["slotId"] for a in reply["payload"]["assignments"]] == ["046d_4242_c82", "046d_4242_c83"]


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

BACK = fx.SLOT_PREFIX + "_c82"


def choose(client, card, slot=BACK):
    return client.call("SET", "/v2/profiles/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": DEFAULT_PROFILE, "assignment": {"slotId": slot, "cardId": card["id"], "card": card}})


def assignment(client, slot=BACK):
    profile = client.call("GET", "/v2/profiles")["payload"]["profiles"][0]
    return [a for a in profile["assignments"] if a["slotId"] == slot]


def test_chosen_action_replaces_the_default_in_the_profile(client):
    reply = choose(client, presets(client)[1])
    assert reply["result"]["code"] == "SUCCESS"
    got = assignment(client)
    assert len(got) == 1 and got[0]["cardId"] == "card_next_desktop"
    assert got[0]["card"]["macro"]["system"]["action"] == "SWITCH_BETWEEN_DESKTOPS_RIGHT"


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
    slot = fx.SLOT_PREFIX + "_c83"
    reply = client.call("SET", "/v2/profiles/assignment", {"profileId": DEFAULT_PROFILE, "ops": [
        {"set": {"assignment": {"slotId": slot, "cardId": card["id"], "card": card}}}]})
    assert reply["result"]["code"] == "SUCCESS"
    assert assignment(client, slot)[0]["cardId"] == "card_play"


def test_profile_reset_restores_the_default_assignment(client):
    choose(client, presets(client)[0])
    client.call("SET", "/v2/profiles/reset", {})
    assert assignment(client)[0]["cardId"] == "card-back"
