"""Fake command runner: records what the agent would launch or execute.

`launched` holds fire-and-forget programs (`run`); `executed` holds programs the agent
waited for (`run_wait`), whose exit status is scripted with `exit_codes` (by program
name, default 0). `effects` maps a program name to a callable run as a side effect, so a
test can imitate what the real program leaves behind. Nothing here runs anything.
"""


class FakeRunner:
    def __init__(self, installed=(), exit_codes=None, effects=None, outputs=None):
        self.installed = set(installed)
        self.exit_codes = dict(exit_codes or {})
        self.effects = dict(effects or {})
        self.outputs = dict(outputs or {})           # program name -> stdout `run_output` returns
        self.launched = []
        self.executed = []

    def which(self, name):
        return name if name in self.installed else None

    def run(self, argv):
        self.launched.append(list(argv))

    def run_wait(self, argv):
        self.executed.append(list(argv))
        effect = self.effects.get(argv[0])
        if effect:
            effect()
        return self.exit_codes.get(argv[0], 0)

    def run_output(self, argv):
        """Like `run_wait`, but also returns what the program printed: (exit status, stdout)."""
        self.executed.append(list(argv))
        return self.exit_codes.get(argv[0], 0), self.outputs.get(argv[0], "")
