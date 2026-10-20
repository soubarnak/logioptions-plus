"""HID keyboard usage -> Linux key code (the kernel's hid-input table)."""

import pytest

from opl_agent import keymap

# (HID usage, Linux KEY_* value) pairs from linux/input-event-codes.h and the HID usage tables
KNOWN = [(4, 30), (6, 46), (29, 44), (30, 2), (39, 11), (40, 28), (41, 1), (43, 15), (44, 57),
         (58, 59), (69, 88), (74, 102), (79, 106), (82, 103), (76, 111), (101, 127),
         (224, 29), (225, 42), (226, 56), (227, 125), (228, 97), (229, 54), (230, 100), (231, 126)]


@pytest.mark.parametrize("usage,key", KNOWN)
def test_known_usages(usage, key):
    assert keymap.linux_key(usage) == key


@pytest.mark.parametrize("usage", [0, 1, 3, 0xF0, 232, 1000, -1])
def test_unmapped_usages_have_no_key(usage):
    assert keymap.linux_key(usage) is None


def test_no_two_usages_share_a_key_except_the_non_us_hash():
    keys = [keymap.linux_key(u) for u in range(256) if keymap.linux_key(u)]
    assert len(keys) - len(set(keys)) <= 1


@pytest.mark.parametrize("usage,key", [(133, 121), (135, 89), (136, 93), (137, 124), (138, 92), (139, 94),
                                       (140, 95), (144, 122), (145, 123), (146, 90), (147, 91),
                                       (148, 85), (156, 355)])
def test_comma_international_language_and_clear_keys(usage, key):
    assert keymap.linux_key(usage) == key
