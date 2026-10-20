"""Which application has the focus, for per-application profiles.

A window source is any object with ``active()`` returning the focused `Window`, or
None when nothing is focused or the desktop cannot say. It blocks and may raise; the
agent polls it off the event loop. ``X11Source`` reads the X server with ``xprop`` (no
Python dependencies); on Wayland or without ``xprop`` there is no source and the
default profile stays active.
"""

import logging
import os
import re
import shutil
import socket
import subprocess
import time
from collections import namedtuple

log = logging.getLogger("opl.focus")

Window = namedtuple("Window", "wm_class exe", defaults=("",))   # WM_CLASS parts, path of the program


LAUNCHER = re.compile(r"^(flatpak|env|sh|bash|dash|zsh|fish|snap|electron\d*|java|node|nodejs|"
                      r"(python|ruby|perl|php)[\d.]*|xdg-open|gtk-launch|gio|wine|wine64)$")


def _is_launcher(path):
    """True when `path` names a program that runs other programs, so it says nothing about
    which application a window is."""
    return bool(LAUNCHER.match(os.path.basename(path or "")))


def _xprop(argv):
    return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=2,
                          check=True).stdout


def _exe_of(pid):
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except OSError:
        return ""


class X11Source:
    """The window named by the root window's _NET_ACTIVE_WINDOW. `run(argv)` returns the
    output of a command line and `exe_of(pid)` the program a process runs; both are
    injectable. Asks about a window's class and process only when the window changes, and again
    every RECHECK seconds for the same id, since X reuses the ids of windows that are gone."""

    RECHECK = 5.0

    def __init__(self, run=None, exe_of=None, hostname=None, clock=None):
        self._run = run or _xprop
        self._exe_of = exe_of or _exe_of
        self._hostname = hostname or socket.gethostname
        self._clock = clock or time.monotonic
        self._seen = (None, None)               # window id, its Window
        self._read_at = 0.0

    def active(self):
        out = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        m = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
        if not m or int(m.group(1), 16) == 0:
            self._seen = (None, None)
            return None
        wid = m.group(1)
        if self._seen[0] == wid and self._clock() - self._read_at < self.RECHECK:
            return self._seen[1]
        out = self._run(["xprop", "-id", wid, "WM_CLASS", "_NET_WM_PID", "WM_CLIENT_MACHINE"])
        cls = re.search(r"WM_CLASS\(\w+\) = (.*)", out)
        pid = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", out)
        machine = re.search(r'WM_CLIENT_MACHINE\(\w+\) = "([^"]*)"', out)
        remote = bool(machine) and machine.group(1).split(".")[0].lower() != self._hostname().split(".")[0].lower()
        window = Window(tuple(re.findall(r'"([^"]*)"', cls.group(1))) if cls else (),
                        self._exe_of(int(pid.group(1))) if pid and not remote else "")   # a remote pid is not ours
        self._seen = (wid, window)
        self._read_at = self._clock()
        return window


def default_source(environ=None, which=None, wayland=None):
    """The source for this session, else None (the default profile then stays active).

    A Wayland session (WAYLAND_DISPLAY) uses the wlroots foreign-toplevel source. Nothing is
    connected here: the source connects on its first poll (off the event loop) and again after
    any failure, so an agent started before the compositor is up still follows the focus later.
    GNOME (Mutter) and KWin, which lack the protocol, make it report that and the tracker stops,
    since X11 properties would only describe XWayland windows (a session that says it is Wayland
    gets None even when DISPLAY is set). `wayland(environ)` returns a transport and is
    injectable. An X11 session uses xprop when there is a display and xprop."""
    env = os.environ if environ is None else environ
    if not env.get("WAYLAND_DISPLAY") and env.get("XDG_SESSION_TYPE") == "wayland":
        return None             # XWayland would only describe the X11 windows and stick on the last one
    if env.get("WAYLAND_DISPLAY"):
        from . import wayland_focus
        return wayland_focus.WaylandSource(lambda: (wayland or wayland_focus.connect_from_environment)(env))
    if not env.get("DISPLAY") or not (which or shutil.which)("xprop"):
        return None
    return X11Source()


def _names(*values):
    out = set()
    for v in values:
        v = (v or "").strip().lower()
        if v:
            out.add(v)
            out.add(os.path.basename(v))
    return out


def _snap_names(app):
    """Both halves of a snap's desktop id (<snap>_<app>), else nothing."""
    pid = app.get("applicationId") or ""
    if "_" in pid and "/snap/" in (app.get("commandLine") or "") + (app.get("applicationPath") or ""):
        return _names(*pid.split("_"))
    return set()


def match_profile(window, profile_ids, apps):
    """The first of `profile_ids` (application ids) that `window` belongs to, else None.
    A window whose WM_CLASS is the application's StartupWMClass belongs to it before any other
    reading; otherwise its WM_CLASS or program must equal the application's id (or the last part
    of a reverse-DNS id, or either half of a snap id), program or name. Launcher programs
    (flatpak, env, sh, python3, java ...) are never taken as an application's program.
    `apps` are the scanned applications, which add their program and name as candidates."""
    if window is None:
        return None
    classes = {c.strip().lower() for c in window.wm_class if c and c.strip()}
    have = _names(*window.wm_class, "" if _is_launcher(window.exe) else window.exe)
    if not have:
        return None
    by_id = {a.get("applicationId"): a for a in apps}
    for pid in profile_ids:
        declared = ((by_id.get(pid) or {}).get("startupWMClass") or "").strip().lower()
        if declared and declared in classes:
            return pid
    for pid in profile_ids:
        want = _names(pid, pid.rpartition(".")[2])
        app = by_id.get(pid)
        if app:
            if not _is_launcher(app.get("applicationPath")):
                want |= _names(app.get("applicationPath"))
            want |= _names(app.get("name")) | _snap_names(app)
        if have & want:
            return pid
    return None
