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
import subprocess
from collections import namedtuple

log = logging.getLogger("opl.focus")

Window = namedtuple("Window", "wm_class exe", defaults=("",))   # WM_CLASS parts, path of the program


def _xprop(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=2, check=True).stdout


def _exe_of(pid):
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except OSError:
        return ""


class X11Source:
    """The window named by the root window's _NET_ACTIVE_WINDOW. `run(argv)` returns the
    output of a command line and `exe_of(pid)` the program a process runs; both are
    injectable. Asks about a window's class and process only when the window changes."""

    def __init__(self, run=None, exe_of=None):
        self._run = run or _xprop
        self._exe_of = exe_of or _exe_of
        self._seen = (None, None)               # window id, its Window

    def active(self):
        out = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        m = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
        if not m or int(m.group(1), 16) == 0:
            self._seen = (None, None)
            return None
        wid = m.group(1)
        if self._seen[0] == wid:
            return self._seen[1]
        out = self._run(["xprop", "-id", wid, "WM_CLASS", "_NET_WM_PID"])
        cls = re.search(r"WM_CLASS\(\w+\) = (.*)", out)
        pid = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", out)
        window = Window(tuple(re.findall(r'"([^"]*)"', cls.group(1))) if cls else (),
                        self._exe_of(int(pid.group(1))) if pid else "")
        self._seen = (wid, window)
        return window


def default_source(environ=None, which=None):
    """The X11 source when there is an X display and xprop, else None."""
    env = os.environ if environ is None else environ
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


def match_profile(window, profile_ids, apps):
    """The first of `profile_ids` (application ids) that `window` belongs to, else None.
    A window belongs to an application when its WM_CLASS or program equals the
    application's id (or the last part of a reverse-DNS id), program or name. `apps` are
    the scanned applications, which add their program and name as candidates."""
    if window is None:
        return None
    have = _names(*window.wm_class, window.exe)
    if not have:
        return None
    by_id = {a.get("applicationId"): a for a in apps}
    for pid in profile_ids:
        want = _names(pid, pid.rpartition(".")[2])
        app = by_id.get(pid)
        if app:
            prog = os.path.basename(app.get("applicationPath") or "")
            if prog != "flatpak":             # every Flatpak app launches through it
                want |= _names(app.get("applicationPath"))
            want |= _names(app.get("name"))
        if have & want:
            return pid
    return None
