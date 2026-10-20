"""Seam: fetcher CLI with fake zenity/kdialog/pkexec/sudo on PATH."""
import json
import os
import stat
import sys

import pytest

from tests.test_fetcher import fetch, world  # noqa: F401  (world is a fixture)


def fake(world, name, body):
    d = world.tmp / "fakebin"
    d.mkdir(exist_ok=True)
    exe = d / name
    exe.write_text("#!%s\n%s" % (sys.executable, body))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    world.env["PATH"] = "%s%s%s" % (d, os.pathsep, world.env["PATH"])
    return d


def dialog_fake(world, name, answer_code):
    log = world.tmp / (name + ".log")
    fake(world, name, """import os, sys
open(%r, "a").write(" ".join(sys.argv[1:]) + "\\n")
if "--progress" in sys.argv:
    open(%r, "a").write("progress:" + sys.stdin.read().replace("\\n", ",") + "\\n")
    sys.exit(0)
sys.exit(%d)
""" % (str(log), str(log), answer_code))
    return log


def test_zenity_asks_instead_of_the_terminal_and_shows_progress(world):
    log = dialog_fake(world, "zenity", 0)
    world.env["DISPLAY"] = ":99"
    r = fetch(world)                     # no stdin answer: the dialog must decide
    assert r.returncode == 0, r.stderr
    text = log.read_text()
    assert "--question" in text
    assert "https://example.invalid/eula" in text
    assert "--progress" in text and "100" in text
    assert (world.payload / "app.asar").exists()


def test_zenity_cancel_declines_without_downloading(world):
    dialog_fake(world, "zenity", 1)
    world.env["DISPLAY"] = ":99"
    r = fetch(world)
    assert r.returncode != 0
    assert world.srv.hits == []
    assert not (world.tmp / "data").exists()


def test_kdialog_is_used_when_zenity_is_absent(world):
    log = dialog_fake(world, "kdialog", 0)
    world.env["WAYLAND_DISPLAY"] = "wayland-9"
    r = fetch(world)
    assert r.returncode == 0, r.stderr
    assert "--yesno" in log.read_text()


def test_terminal_is_used_without_a_display_even_if_zenity_exists(world):
    log = dialog_fake(world, "zenity", 0)
    r = fetch(world, stdin="n\n")
    assert r.returncode != 0
    assert not log.exists()


def test_no_gui_flag_forces_terminal(world):
    log = dialog_fake(world, "zenity", 0)
    world.env["DISPLAY"] = ":99"
    r = fetch(world, "--no-gui", stdin="n\n")
    assert r.returncode != 0 and not log.exists()


# --- system-wide ----------------------------------------------------------

def privilege_fake(world, name):
    log = world.tmp / (name + ".log")
    fake(world, name, 'import os, sys\nopen(%r, "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
                      'os.execvp(sys.argv[1], sys.argv[1:])\n' % str(log))
    return log


def test_system_install_goes_through_pkexec_and_lands_in_the_system_dir(world):
    log = privilege_fake(world, "pkexec")
    sudo = privilege_fake(world, "sudo")
    r = fetch(world, "--system", stdin="y\n")
    assert r.returncode == 0, r.stderr
    assert "opl_agent.fetch" in log.read_text() and "--system" in log.read_text()
    assert not sudo.exists()
    assert (world.tmp / "system" / "app.asar").exists()
    assert (world.tmp / "system" / "payload.json").exists()
    assert not world.payload.exists()


def test_system_install_falls_back_to_sudo(world):
    sudo = privilege_fake(world, "sudo")
    r = fetch(world, "--system", "--accept-eula")
    assert r.returncode == 0, r.stderr
    assert sudo.exists()
    assert (world.tmp / "system" / "app.asar").exists()


def test_system_install_declined_never_elevates(world):
    log = privilege_fake(world, "pkexec")
    r = fetch(world, "--system", stdin="n\n")
    assert r.returncode != 0
    assert not log.exists()


def test_system_install_without_pkexec_or_sudo_fails_cleanly(world):
    r = fetch(world, "--system", "--accept-eula")
    assert r.returncode != 0
    assert "pkexec" in r.stderr
    assert not (world.tmp / "system").exists()
