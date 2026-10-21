"""What a SYSTEM card sends on Linux: a default keystroke per action, and the user's overrides.

Desktop switching, show-desktop and the like have no standard key: each compositor binds its
own. The defaults below suit GNOME (and most others). A desktop that binds things differently
gets its own overrides (DESKTOPS), chosen from XDG_CURRENT_DESKTOP (a colon-separated list, any
case; the first name with an entry wins, else GNOME's stay). On niri, whose stock config has no
bare-Super overview, TASK_VIEW and APP_EXPOSE are Super+O (toggle-overview) and the desktop-left
and -right actions are Super+Left and Super+Right (focus-column-left/right); its default
gesture card also swaps its up and down swipes for SWITCH_WORKSPACE_UP and _DOWN (Super+Page_Up
and Super+Page_Down, niri's focus-workspace-up/down; the same keys everywhere else). That is
intended: on niri the swipes play those two actions, so retuning TASK_VIEW or SHOW_DESKTOP does
not change them; retune SWITCH_WORKSPACE_UP and SWITCH_WORKSPACE_DOWN (the click swipe is still
TASK_VIEW, left and right are SWITCH_BETWEEN_DESKTOPS_LEFT and _RIGHT). The table logs which
profile it chose, once, when it is built. A per-user
file changes any of them, and is read again whenever it changes, so an edit applies to the next
press without a restart.

    ~/.config/optionsplus-linux/actions.toml

    [system]
    SWITCH_BETWEEN_DESKTOPS_RIGHT = "super+page_down"
    LOCK = "XF86ScreenSaver"

A value is one chord: keys joined by "+", pressed in order and released in reverse. An entry
that does not parse is ignored (the default stays), so a typo never disables a button.
"""

import logging
import os
import threading

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

# SYSTEM macro action -> chord. Defaults follow GNOME 40+ where it binds the action; override per user.
# Two are risky and worth overriding or avoiding: CONSOLE_CLIENT is Ctrl+Alt+T (an Ubuntu binding, not
# stock GNOME; other desktops ignore it) and SHUT_DOWN sends KEY_POWER, which logind turns into an
# immediate power-off when nothing inhibits it (no confirmation dialog on every desktop).
DEFAULTS = {
    "SWITCH_APPS": (A, KEY_TAB), "SHIFT_SWITCH_APPS": (A, S, KEY_TAB),
    # stock GNOME has no show-desktop key; Super+D is Ubuntu's (and KDE's, Xfce's) binding
    "DESKTOP": (M, 32), "SHOW_DESKTOP": (M, 32),
    "SWITCH_BETWEEN_DESKTOPS": (M,),                                 # the overview
    "TASK_VIEW": (M,), "APP_EXPOSE": (M,),                           # the overview is GNOME's nearest
    "SWITCH_BETWEEN_DESKTOPS_LEFT": (M, KEY_PAGEUP), "SWITCH_BETWEEN_DESKTOPS_RIGHT": (M, KEY_PAGEDOWN),
    # agent-side actions, not Logitech's: the default gesture card's up and down swipes on a desktop
    # whose workspaces are stacked (see DESKTOPS); never offered in the picker
    "SWITCH_WORKSPACE_UP": (M, KEY_PAGEUP), "SWITCH_WORKSPACE_DOWN": (M, KEY_PAGEDOWN),
    "SWITCH_LANGUAGE": (M, KEY_SPACE),                               # GNOME's next input source
    "CLOSE": (A, KEY_F4), "MINIMIZE": (M, 35), "MAXIMIZE": (M, KEY_UP), "FULLSCREEN": (KEY_F11,),   # Super+H hides
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

# Per-desktop overrides of DEFAULTS, by lower-case XDG_CURRENT_DESKTOP name. "chords" replace default
# chords (None leaves the action unassigned: the desktop has no bind for it and the GNOME one would
# run something else; the user's actions.toml can still set it); "cards" swap the SYSTEM action a depot card compiles to (card id -> action), only while
# the card still holds its Windows chord (see engine.WINDOWS_CHORD_ACTIONS).
DESKTOPS = {
    "niri": {
        # niri's own default config (resources/default-config.kdl). It has no minimize and no
        # show-desktop, and Mod+D is its launcher, so those stay unassigned.
        "chords": {"TASK_VIEW": (M, 24), "APP_EXPOSE": (M, 24),                  # Mod+O: toggle-overview
                   "CLOSE": (M, 16),                                            # Mod+Q: close-window
                   "LOCK": (M, A, 38),                                          # Super+Alt+L: spawn swaylock
                   "MAXIMIZE": (M, 33),                                         # Mod+F: maximize-column
                   "FULLSCREEN": (M, S, 33),                                    # Mod+Shift+F: fullscreen-window
                   "SHOW_DESKTOP": None, "DESKTOP": None, "MINIMIZE": None,
                   "SWITCH_BETWEEN_DESKTOPS_LEFT": (M, KEY_LEFT),               # Mod+Left: focus-column-left
                   "SWITCH_BETWEEN_DESKTOPS_RIGHT": (M, KEY_RIGHT)},
        "cards": {"inner_card_vd_start_menu": "SWITCH_WORKSPACE_UP",
                  "inner_card_vd_hide_show_desktop": "SWITCH_WORKSPACE_DOWN"},
    },
}


def desktop_names(value):
    """The lower-case names in an XDG_CURRENT_DESKTOP value, in order."""
    return [n.strip().lower() for n in (value or "").split(":") if n.strip()]


def desktop_profile(value):
    """The DESKTOPS entry for an XDG_CURRENT_DESKTOP value, or an empty one."""
    for name in desktop_names(value):
        if name in DESKTOPS:
            return DESKTOPS[name]
    return {}


# SYSTEM actions that move the horizontal wheel rather than press keys: action -> sign
SCROLL_ACTIONS = {"SCROLL_LEFT": -1, "SCROLL_RIGHT": 1}

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
    """The chord of each system action: the defaults (the desktop's over GNOME's) plus the user's
    file, re-read on change. `desktop` is an XDG_CURRENT_DESKTOP value; None reads the environment."""

    def __init__(self, path=None, desktop=None):
        self.path = path
        value = os.environ.get("XDG_CURRENT_DESKTOP") if desktop is None else desktop
        self._desktop = desktop_profile(value)
        chosen = next((n for n in desktop_names(value) if n in DESKTOPS), None)
        log.info("system actions: %s defaults (XDG_CURRENT_DESKTOP%s)", chosen or "GNOME",
                 "=" + value if value else " unset")
        self._stamp = None
        self._overrides = {}
        self._lock = threading.Lock()   # the player, assign and sync threads all ask

    def _reload(self):
        """The current overrides. A new table is built aside and swapped in whole, so a reader
        never sees one half filled."""
        with self._lock:
            try:
                st = os.stat(self.path)
                stamp = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
            except (OSError, TypeError):
                self._stamp, self._overrides = None, {}
                return self._overrides
            if stamp == self._stamp:
                return self._overrides
            overrides = {}
            try:
                import tomllib
                with open(self.path, "rb") as fh:
                    section = tomllib.load(fh).get("system", {})
                if not isinstance(section, dict):
                    raise ValueError("[system] is not a table")
            except Exception as exc:
                log.warning("ignoring %s: %s", self.path, exc)
                section = {}
            for action, text in section.items():
                try:
                    overrides[action] = parse_chord(text)
                except ValueError as exc:
                    log.warning("%s: %s: %s (default kept)", self.path, action, exc)
            self._stamp, self._overrides = stamp, overrides
            return overrides

    def chord(self, action):
        """The key codes to send for `action`, or None when it has no Linux keystroke."""
        user = self._reload().get(action)
        if user:
            return user
        chords = self._desktop.get("chords", {})
        return chords[action] if action in chords else DEFAULTS.get(action)

    def card_action(self, card_id):
        """The action this desktop gives the depot card `card_id` in place of its usual one, or None."""
        return self._desktop.get("cards", {}).get(card_id)
