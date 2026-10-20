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
    assert window.wm_class == ("org.gnome.Terminal",)        # the full app_id; no last part to widen matches


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


def test_default_source_without_the_protocol_reports_it_when_polled():
    comp = FakeCompositor(protocol=False)
    src = focus.default_source(environ={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
                               which=lambda n: "/usr/bin/xprop", wayland=lambda env: comp)
    assert isinstance(src, WaylandSource)                    # not X11 either: its answer would be wrong
    with pytest.raises(WaylandError, match="wlr-foreign-toplevel-management"):
        src.active()


def test_default_source_with_a_missing_socket_fails_when_polled_and_not_before():
    def refuse(env):
        raise FileNotFoundError("no socket")
    src = focus.default_source(environ={"WAYLAND_DISPLAY": "wayland-0"}, which=lambda n: None, wayland=refuse)
    with pytest.raises(FileNotFoundError):
        src.active()


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
    assert focus.match_profile(window, ["firefox"], []) is None
    assert focus.match_profile(focus.Window(("firefox",)), ["org.mozilla.firefox"], []) == "org.mozilla.firefox"
    assert focus.match_profile(window, ["kitty"], []) is None


# ---- events a compositor sends mid-session that carry nothing this source uses ----------------

def test_events_it_does_not_use_are_skipped_mid_session():
    from tests.fakes.wayland import message, string, u32
    comp = FakeCompositor()
    h = comp.open_window("firefox", active=True)
    other = comp.open_window("kitty")
    src = source_for(comp)
    assert src.active().wm_class[0] == "firefox"
    comp._out += message(h, 0, string("a new title"))                 # title
    comp._out += message(h, 2, u32(77))                               # output_enter
    comp._out += message(h, 3, u32(77))                               # output_leave
    comp._out += message(h, 7, u32(other))                            # parent
    comp._out += message(h, 5)                                        # done
    comp._out += message(1, 1, u32(5))                                # wl_display.delete_id
    comp._out += message(comp._registry, 1, u32(1))                   # wl_registry.global_remove
    assert src.active().wm_class[0] == "firefox"
    comp.activate(other)
    assert src.active().wm_class[0] == "kitty"


def test_a_closed_toplevel_is_destroyed_and_late_events_for_it_are_ignored():
    from tests.fakes.wayland import message, string
    comp = FakeCompositor()
    h = comp.open_window("firefox", active=True)
    src = source_for(comp)
    comp.close_window(h)
    comp._out += message(h, 1, string("ghost"))                       # sent before the compositor saw destroy
    comp._out += message(h, 5)
    assert src.active() is None
    assert comp.requests.count((h, 7, b"")) == 1                      # destroy went out once


def test_a_hang_up_after_a_close_surfaces_and_closing_twice_is_harmless():
    comp = FakeCompositor()
    h = comp.open_window("firefox", active=True)
    src = source_for(comp)
    comp.close_window(h)
    comp.hang_up()
    with pytest.raises(OSError):
        src.active()
    src.close()
    src.close()


# ---- the real transport over a unix socket ----------------------------------------------------

def test_socket_transport_round_trips_over_a_real_unix_socket(tmp_path):
    import socket
    path = str(tmp_path / "wl.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    transport = wayland_focus.SocketTransport(path)
    peer, _ = server.accept()
    try:
        assert transport.recv(0) == b""
        transport.send(b"hello")
        assert peer.recv(5) == b"hello"
        peer.sendall(b"world")
        assert transport.recv(2) == b"world"
        peer.close()
        with pytest.raises(ConnectionError):
            transport.recv(2)
    finally:
        transport.close()
        server.close()


def test_the_source_follows_focus_over_a_real_socket(tmp_path):
    import socket
    import threading
    path = str(tmp_path / "wl.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    comp = FakeCompositor()
    comp.open_window("firefox", active=True)

    def serve():
        conn, _ = server.accept()
        conn.settimeout(0.05)
        try:
            while True:
                try:
                    data = conn.recv(65536)
                    if not data:
                        return
                    comp.send(data)
                except socket.timeout:
                    pass
                out = comp.recv(0)
                if out:
                    conn.sendall(out)
        except OSError:
            return
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    src = WaylandSource(lambda: wayland_focus.SocketTransport(path), timeout=5)
    try:
        src.start()
        assert src.active().wm_class[0] == "firefox"
    finally:
        src.close()
        server.close()
