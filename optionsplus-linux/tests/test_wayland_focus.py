"""Per-application profiles on Wayland through wlroots (ticket #28): the foreign-toplevel
source reads which toplevel is activated and hands it to the same router as the X11 source.

Seams: the window source interface (`active()`), the wire bytes the source sends, and a
scripted compositor speaking the Wayland protocol over the transport interface."""

import struct

import pytest

from opl_agent import focus, wayland_focus
from opl_agent.wayland_focus import WaylandError, WaylandSource
from tests.fakes.wayland import FakeCompositor, MANAGER


def source_for(compositor):
    src = WaylandSource(lambda: compositor)
    src.start()
    return src


def test_first_request_is_get_registry_on_the_wire():
    comp = FakeCompositor()
    source_for(comp)
    assert comp.requests[0] == (1, 1, struct.pack("=I", 2))      # display.get_registry(new id 2)
    iface, version, _ = comp.bound
    assert (iface, version) == (MANAGER, 3)


def test_reports_the_activated_toplevel():
    comp = FakeCompositor()
    terminal = comp.open_window("org.gnome.Terminal")
    comp.open_window("firefox")
    src = source_for(comp)
    assert src.active() is None                              # nothing activated yet
    comp.activate(terminal)
    window = src.active()
    assert "org.gnome.Terminal" in window.wm_class
    assert "Terminal" in window.wm_class                     # last part of a reverse-DNS id


def test_follows_focus_changes_and_loss_of_focus():
    comp = FakeCompositor()
    a, b = comp.open_window("firefox", active=True), comp.open_window("kitty")
    src = source_for(comp)
    assert src.active().wm_class[0] == "firefox"
    comp.activate(b)
    assert src.active().wm_class[0] == "kitty"
    comp.set_states(b, [])
    assert src.active() is None
    comp.activate(a)
    assert src.active().wm_class[0] == "firefox"


def test_closing_the_focused_window_clears_it():
    comp = FakeCompositor()
    h = comp.open_window("firefox", active=True)
    src = source_for(comp)
    assert src.active() is not None
    comp.close_window(h)
    assert src.active() is None


def test_state_applies_only_when_the_compositor_says_done():
    comp = FakeCompositor()
    a, b = comp.open_window("firefox", active=True), comp.open_window("kitty")
    src = source_for(comp)
    comp.set_states(a, [], commit=False)
    assert src.active().wm_class[0] == "firefox"             # not committed yet
    comp.set_states(b, [2])
    assert src.active().wm_class[0] == "kitty"


def test_app_id_change_is_picked_up():
    comp = FakeCompositor()
    h = comp.open_window("unknown", active=True)
    src = source_for(comp)
    comp.rename(h, "firefox")
    assert src.active().wm_class[0] == "firefox"


def test_messages_split_across_reads_are_reassembled():
    comp = FakeCompositor(chunk=3)
    comp.open_window("firefox", active=True)
    assert source_for(comp).active().wm_class[0] == "firefox"


def test_old_manager_version_is_bound_at_its_own_version():
    comp = FakeCompositor(version=1)
    comp.open_window("firefox", active=True)
    assert source_for(comp).active().wm_class[0] == "firefox"
    assert comp.bound[1] == 1


def test_without_the_protocol_start_says_so():
    comp = FakeCompositor(protocol=False)
    with pytest.raises(WaylandError, match="foreign-toplevel"):
        WaylandSource(lambda: comp).start()


def test_a_compositor_that_never_answers_times_out():
    comp = FakeCompositor(answers=False)
    with pytest.raises(WaylandError, match="answer"):
        WaylandSource(lambda: comp, timeout=0.05).start()


def test_protocol_error_from_the_compositor_surfaces():
    comp = FakeCompositor()
    src = source_for(comp)
    comp.protocol_error("boom")
    with pytest.raises(WaylandError, match="boom"):
        src.active()


def test_manager_finished_surfaces():
    comp = FakeCompositor()
    src = source_for(comp)
    comp.finish()
    with pytest.raises(WaylandError):
        src.active()


def test_reconnects_after_the_connection_drops():
    first, second = FakeCompositor(), FakeCompositor()
    second.open_window("kitty", active=True)
    sessions = iter([first, second])
    src = WaylandSource(lambda: next(sessions))
    assert src.active() is None
    first.hang_up()
    with pytest.raises(ConnectionError):
        src.active()
    assert src.active().wm_class[0] == "kitty"


# -- selection --------------------------------------------------------------------------

def test_default_source_is_wayland_on_a_wlroots_session():
    comp = FakeCompositor()
    src = focus.default_source(environ={"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"},
                               which=lambda n: "/usr/bin/xprop", wayland=lambda env: comp)
    assert isinstance(src, WaylandSource)


def test_default_source_gives_none_without_the_protocol_and_logs_why(caplog):
    comp = FakeCompositor(protocol=False)
    with caplog.at_level("INFO", logger="opl.focus"):
        src = focus.default_source(environ={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
                                   which=lambda n: "/usr/bin/xprop", wayland=lambda env: comp)
    assert src is None                                       # not X11 either: its answer would be wrong
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "wlr-foreign-toplevel-management" in text and "default profile" in text


def test_default_source_gives_none_when_the_socket_is_missing(caplog):
    def refuse(env):
        raise FileNotFoundError("no socket")
    with caplog.at_level("INFO", logger="opl.focus"):
        assert focus.default_source(environ={"WAYLAND_DISPLAY": "wayland-0"}, which=lambda n: None,
                                    wayland=refuse) is None
    assert any("default profile" in r.getMessage() for r in caplog.records)


def test_wayland_socket_path():
    assert wayland_focus.socket_path({"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/user/7"}) \
        == "/run/user/7/wayland-1"
    assert wayland_focus.socket_path({"WAYLAND_DISPLAY": "/tmp/sock"}) == "/tmp/sock"
    with pytest.raises(WaylandError):
        wayland_focus.socket_path({"WAYLAND_DISPLAY": "wayland-1"})


def test_matches_a_profile_by_app_id():
    comp = FakeCompositor()
    comp.open_window("org.mozilla.firefox", active=True)
    window = source_for(comp).active()
    assert focus.match_profile(window, ["org.mozilla.firefox"], []) == "org.mozilla.firefox"
    assert focus.match_profile(window, ["firefox"], []) == "firefox"
    assert focus.match_profile(window, ["kitty"], []) is None
