"""Stand-ins for /dev/uinput: a recording event sink and a recording ioctl backend."""

import struct

EV_KEY = 1


class FakeInput:
    """The injection sink the engine writes to; records what the virtual device would emit."""

    def __init__(self):
        self.created = 0              # virtual devices created
        self.events = []              # every (ev_type, code, value) emitted, in order
        self.fail_create = False      # /dev/uinput cannot be opened
        self.fail_emit = False        # writing an event fails
        self.fail_emit_on = None      # fail only the n-th emit from now (1-based), once

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
        self.events += list(events)

    def keys(self):
        """[(key code, 1 down / 0 up)] of the key events emitted."""
        return [(c, v) for t, c, v in self.events if t == EV_KEY]

    def close(self):
        pass


class FakeUinputBackend:
    """Records the open / ioctl / write / close calls UinputDevice makes."""

    def __init__(self):
        self.calls = []
        self.fail_open = False

    def open(self, path):
        if self.fail_open:
            raise PermissionError(path)
        self.calls.append(("open", path))
        return 77

    def ioctl(self, fd, request, arg=0):
        self.calls.append(("ioctl", request, arg))

    def write(self, fd, data):
        self.calls.append(("write", bytes(data)))

    def close(self, fd):
        self.calls.append(("close",))

    def ioctls(self, request):
        return [c[2] for c in self.calls if c[0] == "ioctl" and c[1] == request]

    def written(self):
        """[(type, code, value)] decoded from the input_event structs written."""
        size = struct.calcsize("llHHi")
        return [struct.unpack("llHHi", c[1])[2:] for c in self.calls if c[0] == "write" and len(c[1]) == size]
