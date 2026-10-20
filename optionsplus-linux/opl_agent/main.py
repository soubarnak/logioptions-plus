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
import sys

from . import resources, updates
from .ipc import Server, socket_alive, socket_path
from .model import Registry
from .router import Router

log = logging.getLogger("opl.main")

TYPE = "type.googleapis.com/"

POLL_SECONDS = 30
RESCAN_SECONDS = 2.0     # also the longest a switched-off mouse can stay on screen


def _battery_key(reg, dev_id):
    b = reg.devices[dev_id].battery_cache or {}
    return (b.get("percent"), bool(b.get("charging")))


def _device_info(reg, dev_id):
    info = reg.devices[dev_id].info()
    bs = reg.battery_status_for_info(dev_id)
    if bs:
        info["batteryStatus"] = bs
    info["@type"] = TYPE + "logi.protocol.devices.Device.Info"
    return info


async def _watch(server, reg, interval=RESCAN_SECONDS):
    """Rescan on hidraw node add/remove and every `interval` (a mouse switching
    on or off leaves the receiver's node alone); announce what changed."""
    loop = asyncio.get_event_loop()
    wait = getattr(reg.transport, "wait_change", None)
    while True:
        if wait is not None:
            await loop.run_in_executor(None, wait, interval)
        else:
            await asyncio.sleep(interval)
        try:
            events = await loop.run_in_executor(None, reg.rescan)
        except Exception:
            log.exception("rescan failed")
            continue
        for d in events["removed"]:
            info = d.info()
            info["@type"] = TYPE + "logi.protocol.devices.Device.Info"
            server.broadcast("/devices/state/deactivated", info)
        for dev_id in events["added"] + events["returned"]:
            server.broadcast("/devices/state/changed", _device_info(reg, dev_id))


def _broadcast_battery(server, reg, dev_id):
    pl, _ = reg.battery(dev_id)
    server.broadcast("/battery/state/changed", pl)
    server.broadcast("/battery/%s/state/changed" % dev_id, pl)
    server.broadcast("/devices/state/changed", _device_info(reg, dev_id))


async def _poll(server, reg, interval=POLL_SECONDS):
    """Periodically re-read battery; broadcast changes to subscribers."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(interval)
        for dev_id in list(reg.devices):
            prev = _battery_key(reg, dev_id)
            try:
                await loop.run_in_executor(None, reg.refresh_battery, dev_id)
            except Exception:
                continue
            if _battery_key(reg, dev_id) != prev:
                _broadcast_battery(server, reg, dev_id)


class Agent:
    """One agent instance: registry, router, IPC server and battery poller.

    Everything environment-specific is injectable so tests can run it in
    process: `transport` (HID++ node discovery and I/O, default real hidraw),
    `socket_path`, `state_dir`, `depot_dir`, `data_dir`, `runner` (launches
    desktop tools) and `app_dirs` (.desktop directories). With no arguments it
    is the production wiring."""

    def __init__(self, transport=None, socket_path=None, state_dir=None,
                 depot_dir=None, data_dir=None, poll_seconds=POLL_SECONDS,
                 rescan_seconds=RESCAN_SECONDS,
                 runner=None, app_dirs=None,
                 build_id=None, payload_path=None, os_release_path=None,
                 release_base_url=None, clock=None):
        if depot_dir is not None or data_dir is not None:
            resources.configure(depot_dir, data_dir)
        self.reg = Registry(transport)
        updater = updates.Updater(build_id=build_id or os.environ.get("OPL_BUILD_ID", "dev"),
                                  payload_path=payload_path, release_base_url=release_base_url,
                                  clock=clock, state_dir=state_dir)
        self.router = Router(self.reg, state_dir=state_dir, runner=runner, app_dirs=app_dirs,
                             updater=updater, os_release_path=os_release_path)
        self.server = Server(self.router, path=socket_path)
        self.router.server = self.server
        updater.broadcast = self.server.broadcast
        self.poll_seconds = poll_seconds
        self.rescan_seconds = rescan_seconds
        self._watcher = None
        self._srv = None
        self._poller = None
        self._updates = None

    async def start(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.reg.scan)
        log.info("%d device(s) discovered", len(self.reg.devices))
        self._loop = loop
        self.reg.on_resumed.append(self._battery_after_resume)
        self._srv = await self.server.start()
        self._poller = asyncio.ensure_future(_poll(self.server, self.reg, self.poll_seconds))
        self._updates = asyncio.ensure_future(self.router.updater.run())
        self._watcher = asyncio.ensure_future(_watch(self.server, self.reg, self.rescan_seconds))

    def _battery_after_resume(self, dev_id):
        """Registry hook (reader thread): push the battery read taken after wake."""
        def push():
            if dev_id in self.reg.devices:
                _broadcast_battery(self.server, self.reg, dev_id)
        self._loop.call_soon_threadsafe(push)

    async def serve_forever(self):
        async with self._srv:
            await self._srv.serve_forever()

    async def stop(self):
        if self._poller:
            self._poller.cancel()
        if self._updates:
            self._updates.cancel()
        if self._watcher:
            self._watcher.cancel()
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
    if socket_alive(socket_path()):
        log.info("agent already running on %s; exiting", socket_path())
        return
    agent = Agent()
    await agent.start()
    try:
        await agent.serve_forever()
    finally:
        await agent.stop()


if __name__ == "__main__":
    if "--is-running" in sys.argv[1:]:
        sys.exit(0 if socket_alive(socket_path()) else 1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
