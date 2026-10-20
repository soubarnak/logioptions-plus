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


def rac(client):
    reply = client.call("GET", "/rac", {"deviceId": "dev00000000"})
    assert reply["result"]["code"] == "SUCCESS"
    assert reply["payload"]["@type"] == TYPE + "logi.protocol.rap.RACContainer"
    return reply["payload"]


def slots(container, key, app=fx.DEFAULT_APP):
    """{slotId: [card ids]} of one app in availableCards or recommendedCards."""
    racs = {r["appId"]: r for r in container[key]["racList"]}
    return {s["slotId"]: ids(s["cards"]) for s in racs[app]["slotCardList"]} if app in racs else None


def test_available_actions_are_composed_from_the_slot_tags_and_hide_windows_only_cards(client):
    # c83 takes KEY cards (task_view is hidden on Linux); c86 takes DESKTOP cards; the rows of
    # another device and the cards of a mac-only file are not offered
    got = slots(rac(client), "availableCards")
    assert got["046d_4242_c83"] == ["card_play", "card_snip"]
    assert got["046d_4242_c86"] == ["card_next_desktop"]
    assert not any(s.startswith("other-") for s in got)


def test_a_slot_without_a_tag_row_uses_the_control_tag_row(client):
    assert slots(rac(client), "availableCards")["046d_4242_c195"] == ["card_next_desktop"]


def test_default_app_comes_first_and_app_rows_follow(client):
    racs = rac(client)["availableCards"]["racList"]
    assert [r["appId"] for r in racs] == [fx.DEFAULT_APP, fx.OTHER_APP]
    assert slots(rac(client), "availableCards", fx.OTHER_APP) == {"046d_4242_c83": ["card_next_desktop"]}


def test_recommended_actions_resolve_to_served_cards(client):
    # unknown ids and hidden cards are dropped; c86 has no slot row so the control row applies
    got = slots(rac(client), "recommendedCards")
    assert got["046d_4242_c83"] == ["card_play"]
    assert got["046d_4242_c195"] == ["card_snip"]
    assert got["046d_4242_c86"] == ["card_next_desktop"]


def test_served_actions_carry_the_resolved_icon_location(client, workdir):
    card = rac(client)["availableCards"]["racList"][0]["slotCardList"][0]["cards"][0]
    assert card["icons"]["uri"] == "file://" + os.path.join(workdir, "depots", "system_actions") + "/"


def test_a_device_without_rows_gets_no_slots_not_an_error(client):
    from opl_agent import actions
    assert actions.rac({"slotPrefix": "nothing-0000"})["availableCards"] == {"racList": []}


CONTROL_ONLY = fx.CONTROL_ONLY_PREFIX


def prefixes_in(container):
    return {s["slotId"].rsplit("_", 1)[0] for key in ("availableCards", "recommendedCards")
            for r in container[key]["racList"] for s in r["slotCardList"]}


def test_a_device_with_no_slot_rows_gets_its_default_controls_and_the_control_tables(agent_factory, workdir):
    fx.add_control_only_device(os.path.join(workdir, "depots"))
    c = agent_factory().client()
    try:
        from opl_agent import actions
        box = actions.rac({"slotPrefix": CONTROL_ONLY})
        assert slots(box, "availableCards") == {
            CONTROL_ONLY + "_c83": ["card_play", "card_next_desktop", "card_snip"],
            CONTROL_ONLY + "_c195": ["card_next_desktop"]}
        assert slots(box, "recommendedCards") == {CONTROL_ONLY + "_c195": ["card_next_desktop"]}
        # another app has no control rows of its own: it must not repeat the default app's lists
        assert slots(box, "availableCards", fx.OTHER_APP) is None
    finally:
        c.close()


def test_rac_serves_the_asked_device_only_and_every_installed_one_when_it_is_unknown(agent_factory, workdir):
    fx.add_control_only_device(os.path.join(workdir, "depots"))
    c = agent_factory().client()
    try:
        asked = c.call("GET", "/rac", {"deviceId": "dev00000000"})["payload"]
        assert prefixes_in(asked) == {fx.SLOT_PREFIX}
        unknown = c.call("GET", "/rac", {"deviceId": "dev99999999"})["payload"]
        assert prefixes_in(unknown) == {fx.SLOT_PREFIX, CONTROL_ONLY}
        assert prefixes_in(c.call("GET", "/rac")["payload"]) == {fx.SLOT_PREFIX, CONTROL_ONLY}
    finally:
        c.close()


def rewrite(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    stamp = os.stat(path).st_mtime + 5
    os.utime(path, (stamp, stamp))


def test_a_changed_table_is_picked_up_without_a_restart(client):
    from opl_agent import actions, resources
    first = slots(actions.rac({"slotPrefix": fx.SLOT_PREFIX}), "recommendedCards")
    assert first["046d_4242_c83"] == ["card_play"]
    rewrite(os.path.join(resources.DATA_DIR, "rap", "recommendations_slot_win.json"),
            fx.info_table({fx.DEFAULT_APP: [("046d_4242_c83", ["card_snip"])]}))
    assert slots(actions.rac({"slotPrefix": fx.SLOT_PREFIX}), "recommendedCards")["046d_4242_c83"] == ["card_snip"]


def test_one_cache_entry_is_kept_per_device_set(client):
    from opl_agent import actions
    actions._rac_cache.clear()
    actions.rac({"slotPrefix": fx.SLOT_PREFIX})
    actions.rac({"slotPrefix": CONTROL_ONLY})
    assert len(actions._rac_cache) == 2
    actions.rac({"slotPrefix": fx.SLOT_PREFIX})
    assert len(actions._rac_cache) == 2


def test_malformed_tables_and_presets_are_ignored_not_fatal(client):
    from opl_agent import actions, resources
    rap = os.path.join(resources.DATA_DIR, "rap")
    rewrite(os.path.join(rap, "available_card_tags_slot.json"), json.dumps({"data": "nope"}))
    rewrite(os.path.join(rap, "available_card_tags_control.json"), json.dumps({"data": {fx.DEFAULT_APP: {
        "infoList": [{"infos": [fx.KEY]}, {"id": "83", "infos": "PRESET_TAG_KEY_OR_BUTTON"}, "junk",
                     {"id": "86", "infos": [fx.DESKTOP]}]}}}))
    rewrite(os.path.join(resources.DATA_DIR, "card_presets", "card_presets_win.json"),
            json.dumps({"cards": [{"name": "no id", "tags": [fx.DESKTOP]}, fx.keystroke_card("card_ok", "N", tags=[fx.DESKTOP])]}))
    box = actions.rac({"slotPrefix": fx.SLOT_PREFIX})
    assert box["availableCards"] == {"racList": []}      # no usable tag row is left
    assert "no id" not in json.dumps(box)


def test_a_non_json_file_is_remembered_so_it_is_not_reopened(client):
    from opl_agent import actions, resources
    path = os.path.join(resources.DATA_DIR, "card_presets", "card_presets_001.json")
    actions._read(path)
    assert actions._files[path][1] is None


def test_a_preset_file_with_a_byte_order_mark_is_read(agent_factory, workdir):
    path = os.path.join(workdir, "depots", "logioptionsplus", "data", "card_presets", "card_presets_bom_win.json")
    with open(path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + json.dumps({"cards": [fx.keystroke_card("card_bom", "N")]}).encode())
    c = agent_factory().client()
    try:
        assert "card_bom" in ids(presets(c))
    finally:
        c.close()


def test_the_encrypted_preset_file_is_skipped_without_a_warning(client, caplog):
    from opl_agent import actions, resources
    path = os.path.join(resources.DATA_DIR, "card_presets", "card_presets_001.json")
    with caplog.at_level("WARNING"):
        assert actions._read(path) is None
    assert caplog.records == []


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
        assert rac(c)["availableCards"] == {"racList": []}
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
