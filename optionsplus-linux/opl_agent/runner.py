"""Command runner port: launches desktop tools such as the settings app.

Production wiring is ``SubprocessRunner``; tests pass a fake with the same two
methods (``which`` and ``run``). ``run`` never waits for the program and never
raises: a missing or failing tool must not take the agent down.
"""

import logging
import shutil
import subprocess

log = logging.getLogger("opl.runner")


class SubprocessRunner:
    def which(self, name):
        return shutil.which(name)

    def run(self, argv):
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            log.warning("cannot run %s: %s", argv[0], exc)
