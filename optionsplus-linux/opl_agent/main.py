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

from .ipc import Server, socket_alive, socket_path
from .model import Registry
from .router import Router

log = logging.getLogger("opl.main")

POLL_SECONDS = 30


async def _poll(server, reg):
    """Periodically re-read battery; broadcast changes to subscribers."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(POLL_SECONDS)
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


async def main():
    logging.basicConfig(
        level=os.environ.get("OPL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if socket_alive(socket_path()):
        log.info("agent already running on %s; exiting", socket_path())
        return
    reg = Registry()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, reg.scan)
    log.info("%d device(s) discovered", len(reg.devices))

    router = Router(reg)
    server = Server(router)
    router.server = server
    srv = await server.start()
    poller = asyncio.ensure_future(_poll(server, reg))
    try:
        async with srv:
            await srv.serve_forever()
    finally:
        poller.cancel()
        reg.close()


if __name__ == "__main__":
    if "--is-running" in sys.argv[1:]:
        sys.exit(0 if socket_alive(socket_path()) else 1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
