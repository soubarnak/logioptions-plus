"""The input engine under failure and contention (review of tickets #21 and #22): a failed
virtual device, a mouse that stops answering, writes cut short, long macros, shutdown mid-macro."""

import os
import threading
import time

import pytest

from opl_agent import engine as eng_mod, gestures, hidpp, profiles, sysactions
from opl_agent.engine import InputEngine, Unsupported, compile_card
from tests.fakes.hidpp_device import F_WAKE
from tests.fakes.uinput import FakeInput
from tests.test_button_actions import card, delay, kb, sequence
from tests.test_button_remap import BACK, COPY, FORWARD, KEY_C, KEY_LEFTCTRL, assign, assigned_card_id
from tests.test_wake_reapply import wait_for


# ---- an engine with a scripted mouse, for the cases the simulated mouse cannot produce ------------

class StubDev:
    id = "d1"
    thumb_wheel = False
    slot_prefix = "p"
    depot = None


class StubReg:
    def __init__(self, cids=(82, 83, 86)):
        self.on_button_event, self.on_thumb_event = [], []
        self._controls = {c: {"divertable": True, "raw_xy": False} for c in cids}
        self.writes = []
        self.fail = {}                  # cid -> exception to raise on its divert write
        self.gate = None                # callable run inside every divert write

    def controls(self, dev_id):
        return self._controls

    def set_divert(self, dev_id, cid, divert, raw_xy=None):
        if self.gate:
            self.gate(cid, divert)
        if cid in self.fail and divert is not None:
            raise self.fail[cid]
        self.writes.append((cid, divert))


def engine(reg=None, sink=None):
    reg = reg or StubReg()
    sink = sink or FakeInput()
    e = InputEngine(reg, store=None, sink_factory=sink.factory)
    return e, reg, sink


def tap_card():
    return compile_card(COPY)


# ---- #21 items 3, 4, 5 -------------------------------------------------------------------------

def test_a_timeout_keeps_only_the_buttons_that_were_written():
    e, reg, _ = engine()
    reg.fail[83] = hidpp.HidppTimeout()
    e._desired = lambda dev: {82: tap_card(), 83: tap_card(), 86: tap_card()}
    with pytest.raises(hidpp.HidppTimeout):
        e.sync(StubDev())
    assert set(e._active["d1"]) == {82}             # 83 failed, 86 was never reached
    assert reg.writes == [(82, True)]


def test_a_button_not_reached_after_a_timeout_keeps_what_was_known():
    e, reg, _ = engine()
    e._active["d1"] = {86: tap_card()}
    reg.fail[82] = hidpp.HidppTimeout()
    e._desired = lambda dev: {82: tap_card(), 86: tap_card()}
    with pytest.raises(hidpp.HidppTimeout):
        e.sync(StubDev())
    assert set(e._active["d1"]) == {86}


def test_no_virtual_device_at_sync_hands_an_existing_divert_back():
    e, reg, sink = engine()
    sink.fail_create = True
    e._desired = lambda dev: {82: tap_card()}
    with pytest.raises(OSError):
        e.sync(StubDev())
    assert (82, False) in reg.writes                # undiverted, not left diverted and untracked
    assert "d1" not in e._active or 82 not in e._active["d1"]


def test_assign_and_sync_never_write_to_one_mouse_at_the_same_time():
    e, reg, _ = engine()
    e._desired = lambda dev: {83: tap_card()}
    inside, worst = [0], [0]
    guard = threading.Lock()

    def gate(cid, divert):
        with guard:
            inside[0] += 1
            worst[0] = max(worst[0], inside[0])
        time.sleep(0.03)
        with guard:
            inside[0] -= 1
    reg.gate = gate
    dev = StubDev()
    threads = [threading.Thread(target=e.assign, args=(dev, 82, COPY)),
               threading.Thread(target=e.sync, args=(dev,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert worst[0] == 1


def test_no_virtual_device_names_the_device_path(agent_factory, bolt, uinput):
    uinput.fail_create = True
    c = agent_factory().client()
    try:
        reply = assign(c, BACK, COPY)
        assert reply["result"]["code"] == "FAULTED"
        assert "/dev/uinput could not be opened" in str(reply["result"])
    finally:
        c.close()


# ---- #21 item 8: a stored card that no longer compiles ------------------------------------------------

def test_a_stored_card_that_cannot_run_any_more_is_dropped_and_the_ui_told(agent, client, bolt):
    router = agent.agent.router
    slot = "%s_c%d" % (next(iter(router.reg.devices.values())).slot_prefix, BACK)
    router.store.set(slot, card({"type": "NO_SUCH_ACTION"}, cid="card_gone"))
    sub = agent.client()
    try:
        sub.subscribe("/v2/profiles/update")
        bolt.emit_event(F_WAKE)
        assert wait_for(lambda: router.store.get(slot) is None)
        deadline = time.time() + 5
        seen = False
        while time.time() < deadline and not seen:
            msg = sub.recv()
            seen = msg.get("path") == "/v2/profiles/update"
        assert seen
    finally:
        sub.close()
    assert assigned_card_id(client, BACK) == "card-back"


# ---- #22 item 1: keys stuck after a write cut short -----------------------------------------------------

def test_a_chord_cut_short_leaves_no_key_down(client, bolt, uinput):
    assign(client, BACK, COPY)
    uinput.fail_after_events = 2                     # Ctrl is pressed, then the write dies
    bolt.press_button(BACK)
    assert wait_for(lambda: uinput.fail_after_events is None)
    time.sleep(0.2)
    down = set()
    for code, value in uinput.keys():
        (down.add if value else down.discard)(code)
    assert down == set()


def test_a_failed_release_closes_the_device_and_the_next_press_makes_a_new_one(client, bolt, uinput):
    closed = []
    uinput.close = lambda: closed.append(1)
    assign(client, BACK, COPY)
    uinput.fail_emit = True                          # the press fails, and so do the releases after it
    bolt.press_button(BACK)
    bolt.release_button(BACK)
    assert wait_for(lambda: closed)
    uinput.fail_emit = False
    bolt.press_button(BACK)
    assert wait_for(lambda: uinput.created == 2 and len(uinput.keys()) >= 4)


# ---- #22 item 2: long delays, queue, cancelling -----------------------------------------------------------

def test_a_delay_beyond_the_cap_is_refused():
    with pytest.raises(Unsupported):
        compile_card(sequence(delay(6000)))


def test_delays_that_add_up_beyond_the_cap_are_refused():
    with pytest.raises(Unsupported):
        compile_card(sequence(delay(3000), delay(3000)))
    assert compile_card(sequence(delay(2000), delay(3000)))


def test_the_play_queue_is_bounded():
    e, _, _ = engine()
    steps = [("wait", 1.0)]
    for _ in range(eng_mod.PLAY_QUEUE * 3):
        e._emit("d1", [steps])
    assert e._plays.qsize() <= eng_mod.PLAY_QUEUE
    e.close()


def test_handing_a_button_back_cancels_the_macro_it_was_playing(client, bolt, uinput):
    seq = sequence(kb(4, True), delay(1000), kb(4, False), kb(5, True), kb(5, False))
    assert assign(client, BACK, seq)["result"]["code"] == "SUCCESS"
    bolt.press_button(BACK)
    assert wait_for(lambda: uinput.keys() == [(30, 1)])
    assign(client, BACK, {"id": "card-back", "attribute": "MOUSE_BUTTON_ACTION"})
    time.sleep(1.3)
    assert uinput.keys() == [(30, 1), (30, 0)]       # released, and the rest never ran


def test_a_wake_resync_drops_the_presses_still_queued(client, bolt, uinput):
    seq = sequence(delay(800), kb(4, True), kb(4, False))
    assign(client, BACK, seq)
    for _ in range(3):
        bolt.press_button(BACK)
        bolt.release_button(BACK)
    time.sleep(0.2)
    bolt.emit_event(F_WAKE)
    time.sleep(1.5)
    assert uinput.keys() == []


# ---- #22 item 3: a delay that is not a number ---------------------------------------------------------------

@pytest.mark.parametrize("value", ["abc", [1], {"x": 1}])
def test_a_delay_that_is_not_a_number_is_unsupported(value):
    with pytest.raises(Unsupported):
        compile_card(sequence({"delay": {"durationMs": value}}))


def test_such_a_card_is_not_supported_rather_than_faulted(client):
    reply = assign(client, BACK, sequence({"delay": {"durationMs": "abc"}}))
    assert reply["result"]["code"] == "NOT_SUPPORTED"


def test_one_bad_stored_card_does_not_break_the_rest_of_the_device(agent, client, bolt):
    router = agent.agent.router
    prefix = next(iter(router.reg.devices.values())).slot_prefix
    router.store.set("%s_c%d" % (prefix, FORWARD), sequence({"delay": {"durationMs": "abc"}}))
    assign(client, BACK, COPY)
    bolt.mouse.diverted.clear()
    bolt.emit_event(F_WAKE)
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})


# ---- #22 item 4: closing the engine during a macro ----------------------------------------------------------

def test_closing_during_a_macro_stops_it_at_once_and_makes_no_new_device():
    e, _, sink = engine()
    seq = compile_card(sequence(kb(4, True), delay(3000), kb(4, False)))
    e._emit("d1", [seq])
    assert wait_for(lambda: sink.keys() == [(30, 1)])
    started = time.time()
    e.close()
    assert time.time() - started < 1.5
    assert sink.created == 1
    assert sink.keys() == [(30, 1), (30, 0)]         # the key it held is let go
    assert e._player is not None and not e._player.is_alive()


def test_nothing_is_played_after_close():
    e, _, sink = engine()
    e.close()
    e._emit("d1", [tap_card()])
    time.sleep(0.1)
    assert sink.created == 0 and sink.keys() == []


# ---- #22 item 5: the actions file reloaded under another thread ---------------------------------------------------

def test_a_chord_read_during_a_reload_is_never_the_default(tmp_path):
    path = tmp_path / "actions.toml"
    path.write_text('[system]\nCOPY = "ctrl+shift+c"\n')
    table = sysactions.ActionTable(str(path))
    want = sysactions.parse_chord("ctrl+shift+c")
    assert table.chord("COPY") == want
    stop, bad = threading.Event(), []

    def reader():
        while not stop.is_set():
            if table.chord("COPY") != want:
                bad.append(1)
    t = threading.Thread(target=reader)
    t.start()
    for i in range(60):
        tmp = tmp_path / "next"
        tmp.write_text('[system]\nCOPY = "ctrl+shift+c"\n# %d\n' % i)
        os.replace(tmp, path)                        # an editor's save: the old file or the new, whole
    stop.set()
    t.join()
    assert bad == []


def test_a_same_size_rewrite_is_noticed(tmp_path):
    import os
    path = tmp_path / "actions.toml"
    path.write_text('[system]\nCOPY = "ctrl+a"\n')
    os.utime(path, ns=(10**18, 10**18))
    table = sysactions.ActionTable(str(path))
    assert table.chord("COPY") == sysactions.parse_chord("ctrl+a")
    tmp = tmp_path / "new"
    tmp.write_text('[system]\nCOPY = "ctrl+b"\n')   # same size
    os.utime(tmp, ns=(10**18, 10**18))               # and the same mtime
    os.replace(tmp, path)
    assert table.chord("COPY") == sysactions.parse_chord("ctrl+b")
