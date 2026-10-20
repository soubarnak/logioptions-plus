"""The virtual input device, driven through a recording backend (never the real /dev/uinput)."""

import struct

import pytest

from opl_agent import uinput
from tests.fakes.uinput import FakeUinputBackend

UI_DEV_CREATE, UI_DEV_SETUP, UI_SET_EVBIT, UI_SET_KEYBIT = 0x5501, 0x405C5503, 0x40045564, 0x40045565


def device(backend=None):
    backend = backend or FakeUinputBackend()
    return uinput.UinputDevice(backend), backend


def test_creates_one_device_with_the_agreed_name():
    dev, be = device()
    setup = be.ioctls(UI_DEV_SETUP)
    assert len(setup) == 1 and len(be.ioctls(UI_DEV_CREATE)) == 1
    assert setup[0][8:88].rstrip(b"\0") == b"optionsplus-linux virtual input"
    assert uinput.DEVICE_NAME == "optionsplus-linux virtual input"
    assert be.calls[0] == ("open", "/dev/uinput")


def test_declares_keys_before_creating():
    dev, be = device()
    order = [c[1] for c in be.calls if c[0] == "ioctl"]
    declared = [i for i, r in enumerate(order) if r in (UI_SET_EVBIT, UI_SET_KEYBIT)]
    assert order.index(UI_DEV_CREATE) > max(declared)
    assert {0, 1, 2} <= set(be.ioctls(UI_SET_EVBIT))              # EV_SYN, EV_KEY, EV_REL
    keys = set(be.ioctls(UI_SET_KEYBIT))
    assert {29, 46, 0x110, 0x113, 0x114} <= keys                  # Ctrl, C, BTN_LEFT, BTN_SIDE, BTN_EXTRA


def test_emit_writes_events_then_a_sync_report():
    dev, be = device()
    dev.emit([(uinput.EV_KEY, 29, 1), (uinput.EV_KEY, 46, 1)])
    assert be.written() == [(1, 29, 1), (1, 46, 1), (0, 0, 0)]


def test_a_failed_write_raises():
    class Failing(FakeUinputBackend):
        def write(self, fd, data):
            raise OSError("gone")
    dev, _ = device(Failing())
    with pytest.raises(OSError):
        dev.emit([(uinput.EV_KEY, 29, 1)])


def test_unopenable_uinput_raises():
    be = FakeUinputBackend()
    be.fail_open = True
    with pytest.raises(OSError):
        uinput.UinputDevice(be)


def test_close_destroys_the_device():
    dev, be = device()
    dev.close()
    assert ("ioctl", 0x5502, 0) in be.calls and be.calls[-1] == ("close",)


def test_event_struct_matches_the_kernel_layout():
    assert struct.calcsize(uinput.EVENT_FORMAT) == (24 if struct.calcsize("P") == 8 else 16)
