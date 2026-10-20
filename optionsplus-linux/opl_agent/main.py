"""Native Options+ agent entry point.

Serves the Options+ frontend over the Unix socket it expects
(``/tmp/logitech_kiros_agent-<md5(user)>``), answering the boot route burst
from a static table and the device/battery routes from live HID++ reads.

A background poller refreshes battery levels and broadcasts
``/battery/state/changed`` and ``/battery/<id>/state/changed`` to subscribed
clients. All blocking HID++ work runs in the default thread executor so the
asyncio IPC loop never stalls.

Run:  python3 -m opl_agent.main   (or the installed ``optionsplus-agent`` script)
"""

import asyncio
import logging
import os

from . import resources
from .ipc import Server
from .model import Registry
from .router import Router

log = logging.getLogger("opl.main")

POLL_SECONDS = 30


async def _poll(server, reg, interval=POLL_SECONDS):
    """Periodically re-read battery; broadcast changes to subscribers."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(interval)
        for dev_id in list(reg.devices):
            prev = (reg.devices[dev_id].battery_cache or {}).get("percent")
            try:
                await loop.run_in_executor(None, reg.refresh_battery, dev_id)
            except Exception:
                continue
            now = (reg.devices[dev_id].battery_cache or {}).get("percent")
            if now != prev:
                pl, _ = reg.battery(dev_id)
                server.broadcast("/battery/state/changed", pl)
                server.broadcast("/battery/%s/state/changed" % dev_id, pl)


class Agent:
    """One agent instance: registry, router, IPC server and battery poller.

    Everything environment-specific is injectable so tests can run it in
    process: `transport` (HID++ node discovery and I/O, default real hidraw),
    `socket_path`, `state_dir`, `depot_dir` and `data_dir`. With no arguments it
    is the production wiring."""

    def __init__(self, transport=None, socket_path=None, state_dir=None,
                 depot_dir=None, data_dir=None, poll_seconds=POLL_SECONDS):
        if depot_dir is not None or data_dir is not None:
            resources.configure(depot_dir, data_dir)
        self.reg = Registry(transport)
        self.router = Router(self.reg, state_dir=state_dir)
        self.server = Server(self.router, path=socket_path)
        self.router.server = self.server
        self.poll_seconds = poll_seconds
        self._srv = None
        self._poller = None

    async def start(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.reg.scan)
        log.info("%d device(s) discovered", len(self.reg.devices))
        self._srv = await self.server.start()
        self._poller = asyncio.ensure_future(_poll(self.server, self.reg, self.poll_seconds))

    async def serve_forever(self):
        async with self._srv:
            await self._srv.serve_forever()

    async def stop(self):
        if self._poller:
            self._poller.cancel()
        if self._srv:
            self._srv.close()
            await self._srv.wait_closed()
        for c in list(self.server.connections):
            c.writer.close()
        self.reg.close()


async def main():
    logging.basicConfig(
        level=os.environ.get("OPL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    agent = Agent()
    await agent.start()
    try:
        await agent.serve_forever()
    finally:
        await agent.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
