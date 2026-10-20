"""Per-application profiles (ticket #27): an application can have its own button assignments,
and the active profile follows the focused window.

Seams: the profiles routes (assign with an application's profileId, read profiles back), a
scripted window source standing in for the desktop, the simulated mouse's reports and the
fake virtual input device, and xprop's output for the X11 source."""

import os
import time

import pytest

from opl_agent import focus
from tests.fakes.windows import FakeWindows, FakeXprop
from tests.test_applications import write_desktop
from tests.test_button_remap import (BACK, BACK_DEFAULT, COPY, DEFAULT_PROFILE, FORWARD, KEY_C,
                                     KEY_LEFTCTRL, KEY_V, PASTE, assign, assigned_card_id, keystroke, slot)
from tests.harness import wait_for

TYPE = "type.googleapis.com/"
KEY_F5 = 63
RELOAD = keystroke(62, cid="card_reload")                 # HID usage 62 = F5
FIREFOX, TERMINAL = "firefox", "org.gnome.Terminal"
COPY_KEYS = [(KEY_LEFTCTRL, 1), (KEY_C, 1), (KEY_C, 0), (KEY_LEFTCTRL, 0)]
PASTE_KEYS = [(KEY_LEFTCTRL, 1), (KEY_V, 1), (KEY_V, 0), (KEY_LEFTCTRL, 0)]
RELOAD_KEYS = [(KEY_F5, 1), (KEY_F5, 0)]


def assign_to(client, profile_id, cid, card):
    return client.call("SET", "/v2/profiles/assignment", {
        "@type": TYPE + "logi.protocol.profiles_v2.ProfileAssignment",
        "profileId": profile_id, "assignment": {"slotId": slot(cid), "card": card}})


def profile(client, profile_id):
    profs = client.call("GET", "/v2/profiles")["payload"]["profiles"]
    return next((p for p in profs if p["id"] == profile_id), None)


def card_of(prof, cid):
    return next(a for a in prof["assignments"] if a["slotId"] == slot(cid))["card"]["id"]


@pytest.fixture
def windows():
    return FakeWindows()


def install_apps(workdir):
    """The installed applications a profile can be for; returns their directory."""
    apps = os.path.join(workdir, "applications")
    write_desktop(apps, FIREFOX + ".desktop", "Firefox Web Browser", exec_line="/usr/lib/firefox/firefox %u")
    write_desktop(apps, TERMINAL + ".desktop", "Terminal", exec_line="/usr/bin/gnome-terminal")
    return apps


@pytest.fixture
def tracked(agent_factory, windows, workdir):
    install_apps(workdir)
    return agent_factory(window_source=windows, focus_seconds=0.02)


@pytest.fixture
def client(tracked):
    c = tracked.client()
    yield c
    c.close()


def press(bolt, uinput, cid, count):
    """Press and release a button, return the `count` key events it caused."""
    before = len(uinput.keys())
    bolt.press_button(cid)
    assert wait_for(lambda: len(uinput.keys()) >= before + count), uinput.keys()
    time.sleep(0.05)
    bolt.release_button(cid)
    return uinput.keys()[before:]


def settled(windows):
    """Wait until the tracker has polled a few more times, so a scripted change was seen. A tracker
    that idles (no application has a profile) does not poll; there is nothing to see then."""
    n = windows.polls
    wait_for(lambda: windows.polls >= n + 3, timeout=1)
    time.sleep(0.1)


# ---- the store: one profile per application -----------------------------------------------

def test_an_application_gets_its_own_profile_and_the_default_is_untouched(client, bolt):
    assert assign_to(client, FIREFOX, BACK, RELOAD)["result"]["code"] == "SUCCESS"
    prof = profile(client, FIREFOX)
    assert prof["applicationId"] == FIREFOX and prof["baseProfileId"] == DEFAULT_PROFILE
    assert card_of(prof, BACK) == "card_reload"
    assert card_of(prof, FORWARD) == card_of(profile(client, DEFAULT_PROFILE), FORWARD)
    assert assigned_card_id(client, BACK) == "card-back"          # the default profile
    assert bolt.mouse.diverted == set()                            # not focused: nothing diverted


def test_applications_do_not_share_assignments(client):
    assign_to(client, FIREFOX, BACK, RELOAD)
    assign_to(client, TERMINAL, BACK, PASTE)
    assert card_of(profile(client, FIREFOX), BACK) == "card_reload"
    assert card_of(profile(client, TERMINAL), BACK) == "card_paste"


def test_an_unsupported_card_for_an_application_is_refused_and_not_stored(client):
    nope = {"id": "card_dpi", "attribute": "MACRO_PLAYBACK",
            "macro": {"type": "MOUSE", "mouse": {"action": "DPI_NEXT"}}}
    assert assign_to(client, FIREFOX, BACK, nope)["result"]["code"] == "NOT_SUPPORTED"
    assert profile(client, FIREFOX) is None


def test_only_buttons_can_differ_per_application(client):
    settings = {"id": "MOUSE_SETTINGS", "attribute": "MOUSE_SETTINGS",
                "mouseSettings": {"pointerSpeed": {"active": {"value": 0.9, "dpiLevel": 6000}}}}
    reply = client.call("SET", "/v2/profiles/assignment", {
        "profileId": FIREFOX, "assignment": {"slotId": "046d_4242_mouse_settings", "card": settings}})
    assert reply["result"]["code"] == "NOT_SUPPORTED"
    assert profile(client, FIREFOX) is None


def test_application_profiles_persist_across_restart(agent_factory, bolt, windows, workdir):
    install_apps(workdir)
    first = agent_factory(window_source=windows, focus_seconds=0.02)
    c = first.client()
    assign_to(c, FIREFOX, BACK, RELOAD)
    c.close()
    first.stop()
    second = agent_factory(window_source=windows, focus_seconds=0.02)
    c2 = second.client()
    try:
        assert card_of(profile(c2, FIREFOX), BACK) == "card_reload"
    finally:
        c2.close()


def test_resetting_an_application_profile_clears_its_buttons_only(client):
    assign(client, FORWARD, PASTE)
    assign_to(client, FIREFOX, BACK, RELOAD)
    reply = client.call("SET", "/v2/profiles/reset", {"profileId": FIREFOX})
    assert reply["result"]["code"] == "SUCCESS"
    assert profile(client, FIREFOX) is None
    assert assigned_card_id(client, FORWARD) == "card_paste"


# ---- focus switches what the button does ---------------------------------------------------

def test_focus_switches_the_injected_action(client, bolt, uinput, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    assert bolt.mouse.diverted == set()
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    assert press(bolt, uinput, BACK, 2) == RELOAD_KEYS
    windows.focus(("gnome-terminal-server", "Gnome-terminal"))
    assert wait_for(lambda: bolt.mouse.diverted == set())         # Back is the mouse's own again
    windows.focus(None)
    settled(windows)
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})


def test_an_unknown_window_runs_the_default_profile(client, bolt, uinput, windows):
    assign(client, BACK, COPY)                                    # default profile: Back is Copy
    assign_to(client, FIREFOX, BACK, RELOAD)
    windows.focus(("Navigator", "firefox"))
    settled(windows)
    assert press(bolt, uinput, BACK, 2) == RELOAD_KEYS
    windows.focus(("xterm", "XTerm"))
    settled(windows)
    assert press(bolt, uinput, BACK, 4) == COPY_KEYS


def test_rediverting_happens_only_when_the_diverted_set_differs(client, bolt, uinput, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    assign_to(client, TERMINAL, BACK, PASTE)                      # same button, other card
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    writes = list(bolt.mouse.divert_writes)
    windows.focus(("gnome-terminal-server", TERMINAL))
    settled(windows)
    assert press(bolt, uinput, BACK, 4) == PASTE_KEYS
    assert bolt.mouse.divert_writes == writes                     # no device writes: same set


def test_only_the_differing_buttons_are_written(client, bolt, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    assign_to(client, TERMINAL, FORWARD, PASTE)
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    windows.focus(("x", TERMINAL))
    assert wait_for(lambda: bolt.mouse.diverted == {FORWARD})
    assert sorted(bolt.mouse.divert_writes[-2:]) == [BACK, FORWARD]


def test_an_application_falls_back_to_the_default_profile_per_button(client, bolt, windows):
    assign(client, FORWARD, PASTE)                                # default profile: Forward is Paste
    assign_to(client, FIREFOX, BACK, RELOAD)
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK, FORWARD})


def test_an_application_can_hand_a_button_back_to_the_mouse(client, bolt, windows):
    assign(client, BACK, COPY)
    assign_to(client, FIREFOX, BACK, BACK_DEFAULT)
    assert bolt.mouse.diverted == {BACK}
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == set())


def test_assigning_for_the_focused_application_takes_effect_at_once(client, bolt, uinput, windows):
    windows.focus(("Navigator", "firefox"))
    settled(windows)
    assert assign_to(client, FIREFOX, BACK, RELOAD)["result"]["code"] == "SUCCESS"
    assert bolt.mouse.diverted == {BACK}
    assert press(bolt, uinput, BACK, 2) == RELOAD_KEYS


def test_changing_the_default_while_an_application_overrides_it_keeps_the_override(client, bolt, uinput, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    assign(client, BACK, COPY)
    assert press(bolt, uinput, BACK, 2) == RELOAD_KEYS
    windows.focus(None)
    settled(windows)
    assert bolt.mouse.diverted == {BACK}                          # Copy now
    assert press(bolt, uinput, BACK, 4) == COPY_KEYS


def test_a_window_source_that_fails_leaves_the_current_profile(client, bolt, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    windows.focus(("Navigator", "firefox"))
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    windows.fail = True
    settled(windows)
    assert bolt.mouse.diverted == {BACK}


def test_a_failed_device_write_on_focus_change_leaves_the_old_state(client, bolt, windows):
    assign_to(client, FIREFOX, BACK, RELOAD)
    bolt.mouse.fail_divert_write = True
    windows.focus(("Navigator", "firefox"))
    settled(windows)
    assert bolt.mouse.diverted == set()
    bolt.mouse.fail_divert_write = False
    windows.focus(None)
    settled(windows)
    windows.focus(("Navigator", "firefox"))                       # focus seen again: retried
    assert wait_for(lambda: bolt.mouse.diverted == {BACK})


def test_a_window_matches_the_application_by_its_desktop_entry(agent_factory, bolt, windows, workdir):
    apps = os.path.join(workdir, "apps")
    write_desktop(apps, "org.mozilla.firefox.desktop", "Firefox Web Browser", exec_line="/opt/ff/bin/ff-bin %u")
    running = agent_factory(window_source=windows, focus_seconds=0.02, app_dirs=[apps])
    c = running.client()
    try:
        assign_to(c, "org.mozilla.firefox", BACK, RELOAD)
        windows.focus(("Navigator", "something"), exe="/opt/ff/bin/ff-bin")
        assert wait_for(lambda: bolt.mouse.diverted == {BACK})
    finally:
        c.close()


# ---- matching and the X11 source ---------------------------------------------------------------

@pytest.mark.parametrize("window,expected", [
    (focus.Window(("Navigator", "firefox")), "org.mozilla.firefox"),
    (focus.Window(("gnome-terminal-server", "Gnome-terminal")), None),
    (focus.Window((), "/usr/lib/firefox/firefox"), "org.mozilla.firefox"),
    (focus.Window(("code", "Code")), "code"),
    (None, None),
])
def test_window_to_profile_matching(window, expected):
    assert focus.match_profile(window, ["org.mozilla.firefox", "code"], []) == expected


def test_x11_source_reads_the_active_window():
    xprop = FakeXprop(active="0x42", props={"0x42": ('"navigator", "Firefox"', 77)})
    src = focus.X11Source(run=xprop, exe_of=lambda pid: "/usr/lib/firefox/firefox" if pid == 77 else "")
    assert src.active() == focus.Window(("navigator", "Firefox"), "/usr/lib/firefox/firefox")


def test_x11_source_without_a_focused_window_returns_none():
    assert focus.X11Source(run=FakeXprop(active="0x0")).active() is None
    assert focus.X11Source(run=FakeXprop(active=None)).active() is None


def test_x11_source_asks_about_the_window_only_when_it_changes():
    xprop = FakeXprop(active="0x42", props={"0x42": ('"a", "A"', None), "0x43": ('"b", "B"', None)})
    src = focus.X11Source(run=xprop, exe_of=lambda pid: "")
    src.active(), src.active()
    assert len(xprop.calls) == 3                 # root, window, root
    xprop.active = "0x43"
    assert src.active().wm_class == ("b", "B")


def test_x11_source_looks_again_when_a_window_id_may_have_been_reused():
    now = [0.0]
    xprop = FakeXprop(active="0x42", props={"0x42": ('"a", "A"', 1)})
    src = focus.X11Source(run=xprop, exe_of=lambda pid: "", clock=lambda: now[0])
    assert src.active().wm_class == ("a", "A")
    xprop.props["0x42"] = ('"b", "B"', 2)             # the window closed and X handed its id to another
    assert src.active().wm_class == ("a", "A")        # not yet: asking every poll costs a process each
    now[0] += focus.X11Source.RECHECK
    assert src.active().wm_class == ("b", "B")


def test_x11_source_survives_a_class_that_is_not_utf8(monkeypatch):
    import subprocess
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, stdout="")
    monkeypatch.setattr(subprocess, "run", run)
    focus._xprop(["xprop", "-root"])
    assert seen["errors"] == "replace"


def test_default_source_needs_a_display_and_xprop():
    assert focus.default_source(environ={}, which=lambda n: "/usr/bin/xprop") is None
    assert focus.default_source(environ={"DISPLAY": ":0"}, which=lambda n: None) is None
    assert isinstance(focus.default_source(environ={"DISPLAY": ":0"}, which=lambda n: "/usr/bin/xprop"),
                      focus.X11Source)
