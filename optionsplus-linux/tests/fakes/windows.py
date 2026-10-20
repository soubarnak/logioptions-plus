"""Stand-in for the desktop's focused window, and for xprop."""

from opl_agent.focus import Window


class FakeWindows:
    """A window source a test drives: focus() changes what the agent sees."""

    def __init__(self):
        self.current = None
        self.fail = False
        self.polls = 0

    def focus(self, wm_class=None, exe=""):
        self.current = None if wm_class is None else Window(
            wm_class=tuple(wm_class) if isinstance(wm_class, (tuple, list)) else (wm_class,), exe=exe)

    def active(self):
        self.polls += 1
        if self.fail:
            raise OSError("no display")
        return self.current


class FakeXprop:
    """Answers `xprop` command lines from a table of canned output, like the real tool would."""

    def __init__(self, active="0x42", props=None, pid_exe=None, machine=None):
        self.machine = machine
        self.active = active
        self.props = props or {}                  # window id -> (WM_CLASS text, pid or None)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[1:3] == ["-root", "_NET_ACTIVE_WINDOW"]:
            if self.active is None:
                return "_NET_ACTIVE_WINDOW:  not found.\n"
            return "_NET_ACTIVE_WINDOW(WINDOW): window id # %s\n" % self.active
        if argv[1] == "-id":
            cls, pid = self.props.get(argv[2], (None, None))
            out = ('WM_CLASS(STRING) = %s\n' % cls) if cls else "WM_CLASS:  not found.\n"
            out += ("_NET_WM_PID(CARDINAL) = %d\n" % pid) if pid else "_NET_WM_PID:  not found.\n"
            if self.machine:
                out += 'WM_CLIENT_MACHINE(STRING) = "%s"\n' % self.machine
            return out
        raise AssertionError(argv)
