"""Command runner port: launches desktop tools such as the settings app.

Production wiring is ``SubprocessRunner``; tests pass a fake with the same four
methods (``which``, ``run``, ``run_wait`` and ``run_output``). ``run`` never waits for the program and
never raises: a missing or failing tool must not take the agent down. ``run_wait`` is for
the privileged update steps (pkexec, the payload fetcher): it waits and returns the exit
status, or -1 when the program cannot be started.
"""

import logging
import os
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

    def run_wait(self, argv, timeout=900):
        """Wait up to `timeout` seconds; the program's output goes to the agent log.
        Returns the exit status, or -1 when it cannot be started or runs out of time."""
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace", timeout=timeout)
        except OSError as exc:
            log.warning("cannot run %s: %s", argv[0], exc)
            return -1
        except subprocess.TimeoutExpired:
            log.warning("%s timed out after %s s", argv[0], timeout)
            return -1
        for line in done.stdout.splitlines():
            log.info("%s: %s", os.path.basename(argv[0]), line)
        return done.returncode

    def run_output(self, argv):
        """Wait for a short query tool; returns (exit status, stdout), (-1, "") when it cannot run."""
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=10)
            return done.returncode, done.stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("cannot run %s: %s", argv[0], exc)
            return -1, ""
