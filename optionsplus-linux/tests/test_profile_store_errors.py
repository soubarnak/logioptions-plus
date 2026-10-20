"""A choice that cannot be saved is an error, and a batch that fails part way tells the UI what stuck."""

import os

import pytest

from opl_agent import profiles
from tests.test_action_catalogue import BACK, choose, presets
from tests.test_button_remap import COPY, assign, assigned_card_id, slot

TYPE = "type.googleapis.com/"
FORWARD = 83


def test_store_that_cannot_write_raises_and_keeps_its_old_state(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = profiles.ProfileStore(str(blocker / "state"))      # a directory below a file: unwritable
    with pytest.raises(OSError):
        store.set("slot", {"id": "card"})
    assert store.get("slot") is None
    with pytest.raises(OSError):
        store.set("slot", {"id": "card"}, "org.example.App")
    assert store.apps == {}


def test_choosing_an_action_that_cannot_be_saved_is_an_error(client, workdir):
    state = os.path.join(workdir, "state")
    os.makedirs(state, exist_ok=True)
    os.chmod(state, 0o500)
    try:
        if os.access(state, os.W_OK):
            pytest.skip("directory permissions are not enforced here")
        reply = choose(client, presets(client)[0])
    finally:
        os.chmod(state, 0o700)
    assert reply["result"]["code"] != "SUCCESS"
    profile = client.call("GET", "/v2/profiles")["payload"]["profiles"][0]
    assert [a["cardId"] for a in profile["assignments"] if a["slotId"] == BACK] != ["card_play"]


def test_a_batch_that_fails_part_way_still_broadcasts_what_was_stored(client, bolt, agent):
    listener = agent.client()
    listener.subscribe("/v2/profiles/update")
    default_back = {"id": "card-back", "attribute": "MOUSE_BUTTON_ACTION"}
    bolt.mouse.fail_divert_write = True
    ops = [{"set": {"profileId": "420fd454-0c36-499d-bde4-146823b16147",
                    "assignment": {"slotId": slot(82), "card": default_back}}},
           {"set": {"profileId": "420fd454-0c36-499d-bde4-146823b16147",
                    "assignment": {"slotId": slot(FORWARD), "card": COPY}}}]
    reply = client.call("SET", "/v2/profiles/slice", {"@type": TYPE + "logi.protocol.profiles_v2.Batch", "ops": ops})
    assert reply["result"]["code"] == "FAULTED"
    assert assigned_card_id(client, 82) == "card-back"                 # the first op did stick
    m = listener.recv()
    assert (m["verb"], m["path"]) == ("BROADCAST", "/v2/profiles/update")
    listener.close()
