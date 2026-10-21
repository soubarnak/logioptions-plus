"""The virtual keyboard+mouse device the input engine injects through (/dev/uinput).

One device, named DEVICE_NAME, created over ioctls (stdlib only). The OS calls
sit behind a small backend so tests can substitute a recorder: nothing here
needs the real /dev/uinput unless `OsBackend` is used. Works on X11 and Wayland
because the kernel, not the display server, owns the device.
"""

import fcntl
import os
import struct
import threading

from . import hidpp

DEVICE_NAME = "optionsplus-linux virtual input"
UINPUT_PATH = "/dev/uinput"

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
SYN_REPORT = 0
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0, 1, 6, 8, 11, 12
BUS_VIRTUAL = 6

# linux/uinput.h
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503           # _IOW('U', 3, struct uinput_setup[92 bytes])
UI_SET_EVBIT = 0x40045564           # _IOW('U', 100, int)
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566

EVENT_FORMAT = "llHHi"              # struct input_event: timeval, type, code, value

# Keys the device can send: everything the HID keyboard table maps, plus the mouse buttons.
_BUTTONS = (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA)
_RELS = (REL_X, REL_Y, REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES)


class OsBackend:
    """The real thing: open, ioctl, write and close on a file descriptor."""

    def open(self, path):
        if hidpp.no_hardware():
            hidpp.raise_no_hardware(path)
        return os.open(path, os.O_WRONLY | os.O_NONBLOCK)

    def ioctl(self, fd, request, arg=0):
        fcntl.ioctl(fd, request, arg)

    def write(self, fd, data):
        return os.write(fd, data)

    def close(self, fd):
        os.close(fd)


def _keys():
    from . import keymap
    from . import sysactions
    codes = {keymap.linux_key(u) for u in range(256)} - {None}
    return sorted(codes | sysactions.all_codes()) + list(_BUTTONS)


class UinputDevice:
    """Creates the virtual device on construction; raises OSError if it cannot."""

    def __init__(self, backend=None):
        self.backend = backend or OsBackend()
        self.fd = self.backend.open(UINPUT_PATH)
        self._lock = threading.Lock()
        self._pressed = set()           # key codes this device has sent down and not up
        try:
            for ev in (EV_SYN, EV_KEY, EV_REL):
                self.backend.ioctl(self.fd, UI_SET_EVBIT, ev)
            for key in _keys():
                self.backend.ioctl(self.fd, UI_SET_KEYBIT, key)
            for rel in _RELS:
                self.backend.ioctl(self.fd, UI_SET_RELBIT, rel)
            setup = struct.pack("HHHH80sI", BUS_VIRTUAL, 0, 0, 1, DEVICE_NAME.encode(), 0)
            self.backend.ioctl(self.fd, UI_DEV_SETUP, setup)
            self.backend.ioctl(self.fd, UI_DEV_CREATE, 0)
        except BaseException:
            self.backend.close(self.fd)
            raise

    def emit(self, events):
        """Send [(ev_type, code, value)] followed by one sync report, as one write under a lock
        (the relay and the player threads share the device, and their frames must not mix).
        On a failed write every key this device holds down is released again, so a frame cut
        short never leaves a modifier stuck; the error is raised all the same."""
        events = list(events)
        data = b"".join(struct.pack(EVENT_FORMAT, 0, 0, t, c, v) for t, c, v in events)
        data += struct.pack(EVENT_FORMAT, 0, 0, EV_SYN, SYN_REPORT, 0)
        with self._lock:
            try:
                done = self.backend.write(self.fd, data)
                if done is not None and done != len(data):
                    raise OSError("short write to %s (%d of %d bytes)" % (UINPUT_PATH, done, len(data)))
            except BaseException:
                self._release_all(events)
                raise
            for t, c, v in events:
                if t == EV_KEY:
                    (self._pressed.add if v else self._pressed.discard)(c)

    def _release_all(self, events):
        """Best effort after a failed write: lift every key that may be down, then sync."""
        down = self._pressed | {c for t, c, v in events if t == EV_KEY and v}
        self._pressed = set()
        if not down:
            return
        data = b"".join(struct.pack(EVENT_FORMAT, 0, 0, EV_KEY, c, 0) for c in sorted(down))
        data += struct.pack(EVENT_FORMAT, 0, 0, EV_SYN, SYN_REPORT, 0)
        try:
            self.backend.write(self.fd, data)
        except Exception:
            pass                # the device is broken; the kernel lifts the keys when it is destroyed

    def close(self):
        with self._lock:
            try:
                self.backend.ioctl(self.fd, UI_DEV_DESTROY, 0)
            finally:
                self.backend.close(self.fd)


def open_default():
    """The production sink: a UinputDevice on the real /dev/uinput."""
    return UinputDevice()
