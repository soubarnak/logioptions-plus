"""A manually advanced clock for the agent's scheduled work."""

import asyncio
import threading


class FakeClock:
    """`await clock.sleep(s)` returns only after `advance(>= s)` from the test."""

    def __init__(self):
        self.now = 0.0
        self._sleepers = []             # (deadline, loop, future)
        self._lock = threading.Lock()

    @property
    def sleeping(self):
        with self._lock:
            return len([s for s in self._sleepers if not s[2].done()])

    async def sleep(self, seconds):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self._lock:
            self._sleepers.append((self.now + seconds, loop, fut))
        await fut

    def advance(self, seconds):
        with self._lock:
            self.now += seconds
            due = [s for s in self._sleepers if s[0] <= self.now]
            self._sleepers = [s for s in self._sleepers if s[0] > self.now]
        for _, loop, fut in due:
            loop.call_soon_threadsafe(lambda f=fut: f.done() or f.set_result(None))
