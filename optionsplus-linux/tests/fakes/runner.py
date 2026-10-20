"""Fake privileged-command runner: records what the agent would launch."""


class FakeRunner:
    def __init__(self, installed=()):
        self.installed = set(installed)
        self.launched = []

    def which(self, name):
        return name if name in self.installed else None

    def run(self, argv):
        self.launched.append(list(argv))
