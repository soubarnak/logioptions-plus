"""What a SYSTEM card sends on Linux: a default keystroke per action, and the user's overrides.

Desktop switching, show-desktop and the like have no standard key: each compositor binds its
own. The defaults below suit GNOME (and most others); a per-user file changes any of them, and
is read again whenever it changes, so an edit applies to the next press without a restart.

    ~/.config/optionsplus-linux/actions.toml

    [system]
    SWITCH_BETWEEN_DESKTOPS_RIGHT = "super+page_down"
    LOCK = "XF86ScreenSaver"

A value is one chord: keys joined by "+", pressed in order and released in reverse. An entry
that does not parse is ignored (the default stays), so a typo never disables a button.
"""

import logging
import os

from . import keymap

log = logging.getLogger("opl.sysactions")

# linux/input-event-codes.h
KEY_ESC, KEY_ENTER, KEY_TAB, KEY_SPACE, KEY_BACKSPACE = 1, 28, 15, 57, 14
KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT, KEY_LEFTMETA = 29, 42, 56, 125
KEY_MINUS, KEY_EQUAL = 12, 13
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 103, 108, 105, 106
KEY_HOME, KEY_END, KEY_PAGEUP, KEY_PAGEDOWN, KEY_INSERT, KEY_DELETE = 102, 107, 104, 109, 110, 111
KEY_SYSRQ, KEY_COMPOSE, KEY_F4, KEY_F11 = 99, 127, 62, 87

# Keys with no HID usage on a keyboard page: media, power, brightness, launchers.
XF86 = {
    "xf86audiomute": 113, "xf86audiolowervolume": 114, "xf86audioraisevolume": 115,
    "xf86poweroff": 116, "xf86sleep": 142, "xf86screensaver": 152, "xf86homepage": 172,
    "xf86eject": 161, "xf86audionext": 163, "xf86audioplay": 164, "xf86audioprev": 165,
    "xf86audiostop": 166, "xf86monbrightnessdown": 224, "xf86monbrightnessup": 225,
    "xf86audiomicmute": 248, "xf86calculator": 140, "xf86explorer": 144, "xf86www": 150,
    "xf86mail": 155, "xf86audiomedia": 226,
}

_NAMED = {
    "ctrl": KEY_LEFTCTRL, "control": KEY_LEFTCTRL, "shift": KEY_LEFTSHIFT, "alt": KEY_LEFTALT,
    "meta": KEY_LEFTMETA, "super": KEY_LEFTMETA, "win": KEY_LEFTMETA,
    "enter": KEY_ENTER, "return": KEY_ENTER, "esc": KEY_ESC, "escape": KEY_ESC, "tab": KEY_TAB,
    "space": KEY_SPACE, "backspace": KEY_BACKSPACE, "delete": KEY_DELETE, "insert": KEY_INSERT,
    "home": KEY_HOME, "end": KEY_END, "page_up": KEY_PAGEUP, "pageup": KEY_PAGEUP,
    "page_down": KEY_PAGEDOWN, "pagedown": KEY_PAGEDOWN, "up": KEY_UP, "down": KEY_DOWN,
    "left": KEY_LEFT, "right": KEY_RIGHT, "minus": KEY_MINUS, "equal": KEY_EQUAL, "plus": KEY_EQUAL,
    "print": KEY_SYSRQ, "menu": KEY_COMPOSE,
}


def _name_table():
    names = dict(_NAMED)
    names.update(XF86)
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        names[ch] = keymap.linux_key(4 + i)
    for i, ch in enumerate("1234567890"):
        names[ch] = keymap.linux_key(30 + i)
    for i in range(24):
        names["f%d" % (i + 1)] = keymap.linux_key(58 + i if i < 12 else 104 + i - 12)
    return names


NAMES = _name_table()


def parse_chord(text):
    """Key codes of a chord such as "ctrl+alt+right"; raises ValueError for an unknown key."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty keystroke")
    codes = []
    for part in text.split("+"):
        code = NAMES.get(part.strip().lower())
        if code is None:
            raise ValueError("unknown key %r" % part.strip())
        codes.append(code)
    return tuple(codes)


C, S, A, M = KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT, KEY_LEFTMETA

# SYSTEM macro action -> chord. GNOME-style defaults; override per user.
DEFAULTS = {
    "SWITCH_APPS": (A, KEY_TAB), "SHIFT_SWITCH_APPS": (A, S, KEY_TAB),
    "DESKTOP": (M, 32), "SHOW_DESKTOP": (M, 32),                     # Super+D
    "SWITCH_BETWEEN_DESKTOPS": (M,),                                 # the overview
    "SWITCH_BETWEEN_DESKTOPS_LEFT": (C, A, KEY_LEFT), "SWITCH_BETWEEN_DESKTOPS_RIGHT": (C, A, KEY_RIGHT),
    "CLOSE": (A, KEY_F4), "MINIMIZE": (M, KEY_DOWN), "MAXIMIZE": (M, KEY_UP), "FULLSCREEN": (KEY_F11,),
    "SNAP_LEFT": (M, KEY_LEFT), "SNAP_RIGHT": (M, KEY_RIGHT),
    "COPY": (C, 46), "PASTE": (C, 47), "UNDO": (C, 44), "REDO": (C, S, 44),
    "ZOOM_IN": (C, KEY_EQUAL), "ZOOM_OUT": (C, KEY_MINUS),
    "LOCK": (M, 38),                                                  # Super+L
    "SHUT_DOWN": (XF86["xf86poweroff"],), "SLEEP": (XF86["xf86sleep"],),
    "HOME": (XF86["xf86homepage"],), "EJECT": (XF86["xf86eject"],),
    "BRIGHTNESS_UP": (XF86["xf86monbrightnessup"],), "BRIGHTNESS_DOWN": (XF86["xf86monbrightnessdown"],),
    "WINDOWS_SCREEN_CAPTURE": (KEY_SYSRQ,), "WINDOWS_PRINT_SCREEN": (KEY_SYSRQ,),
    "APP_MENU": (KEY_COMPOSE,),
    "FILE_CLIENT": (XF86["xf86explorer"],), "INTERNET_CLIENT": (XF86["xf86www"],),
    "MAIL_CLIENT": (XF86["xf86mail"],), "MEDIA_CLIENT": (XF86["xf86audiomedia"],),
    "CONSOLE_CLIENT": (C, A, 20),                                    # Ctrl+Alt+T
}

# MEDIA macro usage -> key
MEDIA_KEYS = {
    "MUTE": XF86["xf86audiomute"], "VOLUME_UP": XF86["xf86audioraisevolume"],
    "VOLUME_DOWN": XF86["xf86audiolowervolume"], "PLAY_PAUSE": XF86["xf86audioplay"],
    "NEXT_TRACK": XF86["xf86audionext"], "PREVIOUS_TRACK": XF86["xf86audioprev"],
    "STOP": XF86["xf86audiostop"],
}


def all_codes():
    """Every key code an action or override can send (the virtual device must declare them)."""
    return set(NAMES.values()) | {c for chord in DEFAULTS.values() for c in chord}


class ActionTable:
    """The chord of each system action: the defaults plus the user's file, re-read on change."""

    def __init__(self, path=None):
        self.path = path
        self._stamp = None
        self._overrides = {}

    def _reload(self):
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except (OSError, TypeError):
            self._stamp, self._overrides = None, {}
            return
        if stamp == self._stamp:
            return
        self._stamp, self._overrides = stamp, {}
        try:
            import tomllib
            with open(self.path, "rb") as fh:
                section = tomllib.load(fh).get("system", {})
            if not isinstance(section, dict):
                raise ValueError("[system] is not a table")
        except Exception as exc:
            log.warning("ignoring %s: %s", self.path, exc)
            return
        for action, text in section.items():
            try:
                self._overrides[action] = parse_chord(text)
            except ValueError as exc:
                log.warning("%s: %s: %s (default kept)", self.path, action, exc)

    def chord(self, action):
        """The key codes to send for `action`, or None when it has no Linux keystroke."""
        self._reload()
        return self._overrides.get(action) or DEFAULTS.get(action)
