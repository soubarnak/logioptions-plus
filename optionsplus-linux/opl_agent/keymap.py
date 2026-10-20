"""HID keyboard usage (usage page 0x07) -> Linux key code.

The same mapping the kernel's hid-input driver applies (hid_keyboard[] in
drivers/hid/hid-input.c); key codes are those of linux/input-event-codes.h.
Cards carry HID usages (a keystroke is {code, modifiers}); the virtual input
device speaks key codes.
"""

# usage 4..29: a..z
_LETTERS = {  # letter -> KEY_*
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35, "i": 23, "j": 36,
    "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20,
    "u": 22, "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
}

_TABLE = {}
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _TABLE[4 + _i] = _LETTERS[_ch]
for _i, _key in enumerate([2, 3, 4, 5, 6, 7, 8, 9, 10, 11]):         # 1..9, 0
    _TABLE[30 + _i] = _key
_TABLE.update({
    40: 28, 41: 1, 42: 14, 43: 15, 44: 57,                           # Enter Esc Backspace Tab Space
    45: 12, 46: 13, 47: 26, 48: 27, 49: 43, 50: 43,                  # - = [ ] \ and non-US #
    51: 39, 52: 40, 53: 41, 54: 51, 55: 52, 56: 53,                  # ; ' ` , . /
    57: 58,                                                          # Caps Lock
    70: 99, 71: 70, 72: 119, 73: 110, 74: 102, 75: 104, 76: 111,     # PrtSc ScrLk Pause Ins Home PgUp Del
    77: 107, 78: 109, 79: 106, 80: 105, 81: 108, 82: 103,           # End PgDn Right Left Down Up
    83: 69, 84: 98, 85: 55, 86: 74, 87: 78, 88: 96,                  # NumLk KP/ KP* KP- KP+ KPEnter
    89: 79, 90: 80, 91: 81, 92: 75, 93: 76, 94: 77, 95: 71, 96: 72, 97: 73, 98: 82, 99: 83,   # KP1..9 KP0 KP.
    100: 86, 101: 127, 102: 116, 103: 117,                           # non-US \ Compose Power KP=
    116: 134, 117: 138, 118: 130, 119: 132, 120: 128, 121: 129,      # Open Help Props Front Stop Again
    122: 131, 123: 137, 124: 133, 125: 135, 126: 136,                # Undo Cut Copy Paste Find
    127: 113, 128: 115, 129: 114,                                    # Mute Volume up / down
    133: 121,                                                        # KP comma
    135: 89, 136: 93, 137: 124, 138: 92, 139: 94, 140: 95,           # International 1..6: Ro Kana Yen Henkan Muhenkan KPJPComma
    144: 122, 145: 123, 146: 90, 147: 91, 148: 85,                   # Language 1..5: Hangeul Hanja Katakana Hiragana Zenkaku
    156: 355,                                                        # Clear
    224: 29, 225: 42, 226: 56, 227: 125, 228: 97, 229: 54, 230: 100, 231: 126,   # Ctrl Shift Alt Meta, L then R
})
for _i, _key in enumerate([59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88]):     # F1..F12
    _TABLE[58 + _i] = _key
for _i in range(12):                                                 # F13..F24
    _TABLE[104 + _i] = 183 + _i

MODIFIER_USAGES = frozenset(range(224, 232))


def linux_key(usage):
    """Linux KEY_* code for a HID keyboard usage, or None when it has none."""
    return _TABLE.get(usage)
