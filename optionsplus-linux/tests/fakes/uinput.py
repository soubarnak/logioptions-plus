"""Stand-ins for /dev/uinput: a recording event sink and a recording ioctl backend."""

import struct

EV_KEY = 1
EV_REL = 2


class FakeInput:
    """The injection sink the engine writes to; records what the virtual device would emit."""

    def __init__(self):
        self.created = 0              # virtual devices created
        self.events = []              # every (ev_type, code, value) emitted, in order
        self.fail_create = False      # /dev/uinput cannot be opened
        self.fail_emit = False        # writing an event fails
        self.fail_emit_on = None      # fail only the n-th emit from now (1-based), once
        self.fail_after_events = None  # the next emit records this many events, then fails (a write cut short)

    def factory(self):
        if self.fail_create:
            raise PermissionError("/dev/uinput: permission denied")
        self.created += 1
        return self

    def emit(self, events):
        if self.fail_emit:
            raise OSError("write failed")
        if self.fail_emit_on is not None:
            self.fail_emit_on -= 1
            if self.fail_emit_on == 0:
                self.fail_emit_on = None
                raise OSError("write failed")
        events = list(events)
        if self.fail_after_events is not None:
            keep, self.fail_after_events = self.fail_after_events, None
            self.events += events[:keep]
            raise OSError("write failed partway")
        self.events += events

    def keys(self):
        """[(key code, 1 down / 0 up)] of the key events emitted."""
        return [(c, v) for t, c, v in self.events if t == EV_KEY]

    def rels(self, code):
        """The values of the relative events with `code` emitted so far."""
        return [v for t, c, v in self.events if t == EV_REL and c == code]

    def close(self):
        pass


class FakeUinputBackend:
    """Records the open / ioctl / write / close calls UinputDevice makes."""

    def __init__(self):
        self.calls = []
        self.fail_open = False
        self.fail_write_on = None     # fail only the n-th write from now (1-based), once

    def open(self, path):
        if self.fail_open:
            raise PermissionError(path)
        self.calls.append(("open", path))
        return 77

    def ioctl(self, fd, request, arg=0):
        self.calls.append(("ioctl", request, arg))

    def write(self, fd, data):
        if self.fail_write_on is not None:
            self.fail_write_on -= 1
            if self.fail_write_on == 0:
                self.fail_write_on = None
                raise OSError("write failed")
        self.calls.append(("write", bytes(data)))

    def close(self, fd):
        self.calls.append(("close",))

    def ioctls(self, request):
        return [c[2] for c in self.calls if c[0] == "ioctl" and c[1] == request]

    def written(self):
        """[(type, code, value)] decoded from the input_event structs written."""
        size = struct.calcsize("llHHi")
        out = []
        for c in self.calls:
            if c[0] == "write":
                out += [struct.unpack("llHHi", c[1][i:i + size])[2:] for i in range(0, len(c[1]), size)]
        return out

    def writes(self):
        """The number of write calls made (a batch is one)."""
        return sum(1 for c in self.calls if c[0] == "write")
