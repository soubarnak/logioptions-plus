"""Which application a window belongs to: StartupWMClass, launcher programs, snaps, and which
sessions follow the focus at all."""

import os

import pytest

from opl_agent import applications, focus
from tests.fakes.windows import FakeXprop
from tests.test_applications import write_desktop


def scanned(workdir, name, display, exec_line, extra=""):
    apps = os.path.join(workdir, "scan-apps")
    write_desktop(apps, name, display, exec_line=exec_line, extra=extra)
    return applications.scan([apps])


def test_scan_reads_startup_wm_class_without_putting_it_in_the_payload(workdir):
    apps = scanned(workdir, "org.example.Editor.desktop", "Editor", "/usr/bin/true", "StartupWMClass=ExEd\n")
    assert apps[0]["startupWMClass"] == "ExEd"
    assert "startupWMClass" not in applications.payload(apps)["applications"][0]


def test_startup_wm_class_matches_a_window_whose_program_says_nothing(workdir):
    apps = scanned(workdir, "editor.desktop", "Editor", "env GDK_BACKEND=x11 sh -c run", "StartupWMClass=ExEd\n")
    window = focus.Window(("exed-main", "ExEd"))
    assert focus.match_profile(window, ["editor"], apps) == "editor"


def test_startup_wm_class_wins_over_a_looser_match(workdir):
    apps = scanned(workdir, "viewer.desktop", "Viewer", "/usr/bin/viewer", "StartupWMClass=Special\n")
    window = focus.Window(("Special", "Special"), "/usr/bin/other")
    assert focus.match_profile(window, ["other", "viewer"], apps + [
        {"applicationId": "other", "name": "Other", "applicationPath": "/usr/bin/other"}]) == "viewer"


@pytest.mark.parametrize("launcher", ["env", "sh", "bash", "python3", "python3.12", "java", "electron", "snap", "flatpak"])
def test_a_launcher_program_does_not_make_every_window_match(workdir, launcher):
    path = "/usr/bin/" + launcher
    apps = [{"applicationId": "tool", "name": "Tool", "applicationPath": path}]
    assert focus.match_profile(focus.Window((), path), ["tool"], apps) is None


def test_env_in_front_of_the_real_program_is_looked_through(workdir):
    apps = scanned(workdir, "firefox_firefox.desktop", "Firefox", "env BAMF_DESKTOP_FILE_HINT=/x.desktop /opt/ff/ffbin %u")
    assert apps[0]["applicationPath"] == "/opt/ff/ffbin"


def test_a_snap_matches_by_either_half_of_its_id(workdir):
    apps = scanned(workdir, "firefox_firefox.desktop", "Web", "env BAMF=1 /snap/bin/firefox %u")
    assert focus.match_profile(focus.Window(("Navigator", "firefox")), ["firefox_firefox"], apps) == "firefox_firefox"
    apps = scanned(workdir, "chromium_chromium.desktop", "Chromium", "/snap/bin/chromium")
    assert focus.match_profile(focus.Window(("chromium", "Chromium")), ["chromium_chromium"], apps) == "chromium_chromium"


def test_an_underscore_id_that_is_not_a_snap_is_not_split(workdir):
    apps = scanned(workdir, "my_app.desktop", "My App", "/usr/bin/true")
    assert focus.match_profile(focus.Window(("app", "App")), ["my_app"], apps) is None


def test_x11_source_ignores_the_program_of_a_window_on_another_machine():
    xprop = FakeXprop(active="0x42", props={"0x42": ('"a", "A"', 77)}, machine="remote.example")
    src = focus.X11Source(run=xprop, exe_of=lambda pid: "/usr/bin/local-thing", hostname=lambda: "mybox")
    assert src.active().exe == ""


def test_x11_source_keeps_the_program_of_a_local_window():
    xprop = FakeXprop(active="0x42", props={"0x42": ('"a", "A"', 77)}, machine="mybox")
    src = focus.X11Source(run=xprop, exe_of=lambda pid: "/usr/bin/local-thing", hostname=lambda: "mybox")
    assert src.active().exe == "/usr/bin/local-thing"


def test_wayland_session_without_wayland_display_gets_no_x11_source():
    env = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
    assert focus.default_source(environ=env, which=lambda n: "/usr/bin/xprop") is None
