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
import logging.handlers
import os
import sys

from . import focus, resources, updates
from .ipc import Server, socket_alive, socket_path
from .model import Registry
from .router import Router

log = logging.getLogger("opl.main")

TYPE = "type.googleapis.com/"

POLL_SECONDS = 30
FOCUS_SECONDS = 0.5      # how often the focused window is looked at
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


def _receiver_info(receiver):
    info = receiver.info()
    info["@type"] = TYPE + "logi.protocol.devices.Device.Info"
    return info


async def _watch(server, reg, interval=RESCAN_SECONDS):
    """Rescan on hidraw node add/remove and every `interval` (a mouse switching
    on or off leaves the receiver's node alone); announce what changed."""
    loop = asyncio.get_running_loop()
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
        for r in events["receiver_removed"]:
            server.broadcast("/devices/state/deactivated", _receiver_info(r))
        for dev_id in events["added"] + events["returned"]:
            server.broadcast("/devices/state/changed", _device_info(reg, dev_id))
        for rid in events["receiver_added"]:
            server.broadcast("/devices/state/changed", _receiver_info(reg.receivers[rid]))


def _broadcast_battery(server, reg, dev_id):
    pl, _ = reg.battery(dev_id)
    server.broadcast("/battery/state/changed", pl)
    server.broadcast("/battery/%s/state/changed" % dev_id, pl)
    server.broadcast("/devices/state/changed", _device_info(reg, dev_id))


async def _poll(server, reg, interval=POLL_SECONDS):
    """Periodically re-read battery; broadcast changes to subscribers."""
    loop = asyncio.get_running_loop()
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


FOCUS_FAIL_LIMIT = 8            # failed polls in a row before the focus counts as unknown
FOCUS_RETRY_MAX = 30.0          # longest wait between polls of a source that keeps failing


async def _track_focus(router, source, interval=FOCUS_SECONDS, fail_limit=FOCUS_FAIL_LIMIT):
    """Poll the window source; hand each change of the focused window to the router.

    The source is polled off the event loop, so connecting to a slow compositor never holds up
    the agent. A source that fails is tried again with growing pauses and one warning in the log;
    after `fail_limit` failures in a row the focus is reported as unknown (the default profile
    takes over) until the source answers again. A source that cannot ever work (its error has
    `permanent` set) is given up after saying so."""
    loop = asyncio.get_running_loop()
    last = object()
    fails = 0
    while True:
        await asyncio.sleep(interval * min(2 ** max(0, fails - 3), FOCUS_RETRY_MAX / interval))
        try:
            window = await loop.run_in_executor(None, source.active)
        except Exception as exc:
            fails += 1
            if getattr(exc, "permanent", False):
                log.info("no per-application profiles: %s; the default profile stays active", exc)
                fails = fail_limit
            elif fails == 1:
                log.warning("cannot tell the focused window (trying again): %s", exc)
            else:
                log.debug("cannot tell the focused window: %s", exc)
            if fails == fail_limit:
                last = None
                try:
                    await loop.run_in_executor(None, router.on_focus, None)
                except Exception:
                    log.exception("switching profile failed")
            if getattr(exc, "permanent", False):
                return
            continue
        if fails:
            log.info("the focused window can be told again")
            fails = 0
        if window == last:
            continue
        last = window
        try:
            await loop.run_in_executor(None, router.on_focus, window)
        except Exception:
            log.exception("switching profile failed")


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
                 release_base_url=None, clock=None,
                 input_factory=None, window_source=None, focus_seconds=FOCUS_SECONDS):
        if depot_dir is not None or data_dir is not None:
            resources.configure(depot_dir, data_dir)
        self.reg = Registry(transport)
        updater = updates.Updater(build_id=build_id or os.environ.get("OPL_BUILD_ID", "dev"),
                                  payload_path=payload_path, release_base_url=release_base_url,
                                  clock=clock, state_dir=state_dir, runner=runner)
        self.router = Router(self.reg, state_dir=state_dir, runner=runner, app_dirs=app_dirs,
                             updater=updater, os_release_path=os_release_path,
                             input_factory=input_factory)
        self.server = Server(self.router, path=socket_path)
        self.router.server = self.server
        updater.broadcast = self.server.broadcast
        self.restart_requested = asyncio.Event()
        updater.on_restart = self._request_restart
        self.poll_seconds = poll_seconds
        self.rescan_seconds = rescan_seconds
        self.window_source = window_source if window_source is not None else focus.default_source()
        self.focus_seconds = focus_seconds
        self._tracker = None
        self._watcher = None
        self._srv = None
        self._poller = None
        self._updates = None

    async def start(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.reg.scan)
        log.info("%d device(s) discovered", len(self.reg.devices))
        self._loop = loop
        self.reg.on_resumed.append(self._battery_after_resume)
        self.reg.on_pairing.append(self._announce_pairing)
        self._srv = await self.server.start()
        self._poller = asyncio.ensure_future(_poll(self.server, self.reg, self.poll_seconds))
        self._updates = asyncio.ensure_future(self.router.updater.run())
        self._watcher = asyncio.ensure_future(_watch(self.server, self.reg, self.rescan_seconds))
        if self.window_source is not None:
            self._tracker = asyncio.ensure_future(_track_focus(self.router, self.window_source, self.focus_seconds))

    def _battery_after_resume(self, dev_id):
        """Registry hook (reader thread): push the battery read taken after wake."""
        def push():
            if dev_id in self.reg.devices:
                _broadcast_battery(self.server, self.reg, dev_id)
        self._loop.call_soon_threadsafe(push)

    def _announce_pairing(self, rcv_id, state, passkey, events):
        """Registry hook (any thread): broadcast pairing progress and any device it added."""
        def push():
            base = "/devices/%s/pair" % rcv_id
            self.server.broadcast(base + "/changed", state)
            if passkey is not None:
                self.server.broadcast(base + "/passkey/displayed", {
                    "@type": TYPE + "logi.protocol.devices.Receiver.PassKeyDisplay",
                    "receiverId": rcv_id, "passkey": passkey})
            for dev_id in (events or {}).get("added", []) + (events or {}).get("returned", []):
                if dev_id in self.reg.devices:
                    self.server.broadcast("/devices/state/changed", _device_info(self.reg, dev_id))
        self._loop.call_soon_threadsafe(push)

    def _request_restart(self):
        """Updater hook (event loop): let the launcher's waiting restart take over."""
        self.restart_requested.set()

    async def serve_forever(self):
        """Serve until cancelled or until a restart is requested (the launcher then starts anew)."""
        serving = asyncio.ensure_future(self._srv.serve_forever())
        leaving = asyncio.ensure_future(self.restart_requested.wait())
        try:
            await asyncio.wait({serving, leaving}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            serving.cancel()
            leaving.cancel()
            await self._close_server()

    async def _close_server(self):
        """Stop listening and drop every client first: wait_closed() (Python 3.12+) waits
        for connected clients, so the UI that asked for a restart would keep us alive."""
        if self._srv is None:
            return
        self._srv.close()
        for c in list(self.server.connections):
            c.writer.close()
        try:
            await asyncio.wait_for(self._srv.wait_closed(), 5)
        except asyncio.TimeoutError:
            log.warning("clients did not disconnect in time")
        self._srv = None

    async def stop(self):
        if self._poller:
            self._poller.cancel()
        if self._updates:
            self._updates.cancel()
        if self._watcher:
            self._watcher.cancel()
        if self._tracker:
            self._tracker.cancel()
        close = getattr(self.window_source, "close", None)
        if close is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(None, close)
            except Exception as exc:
                log.debug("closing the window source failed: %s", exc)
        await self._close_server()
        self.router.relay.close()           # hand the wheels back while the transport is still open
        self.reg.close()
        self.router.engine.close()


LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def log_file_path(environ=None):
    env = os.environ if environ is None else environ
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"),
                                                     ".local", "state")
    return os.path.join(base, "optionsplus-linux", "agent.log")


def setup_logging(environ=None):
    """Log to stderr and to a rotating agent.log in the user's state directory (so an
    autostarted agent, which no launcher redirects, still leaves a log). Returns the file
    path, or None when it cannot be written; the agent runs either way."""
    env = os.environ if environ is None else environ
    logging.basicConfig(level=env.get("OPL_LOG_LEVEL", "INFO"), format=LOG_FORMAT)
    path = log_file_path(env)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3,
                                                       encoding="utf-8")
    except OSError as exc:
        log.warning("cannot write %s: %s", path, exc)
        return None
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    return path


async def main():
    setup_logging()
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
