"""Version, About and the update check against GitHub releases.

No Logitech pipeline, no telemetry: the only network traffic is one
unauthenticated request to the project's own GitHub releases, carrying no user
data. Everything environment-specific (feed base URL, clock, os-release path,
payload record) is injectable so tests run against a local HTTP server.
"""

import asyncio
import http.client
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import __version__, fetch, paths, responses
from .runner import SubprocessRunner

log = logging.getLogger("opl.updates")
TYPE = responses.TYPE
APP_ID = "optionsplus-linux"
DEFAULT_RELEASES_URL = "https://github.com/soubarnak/logioptions-plus/releases"
OS_RELEASE = "/etc/os-release"
CHANNELS = ("stable", "testing")
DEFAULT_SETTINGS = {"checkPeriodically": True, "intervalInSeconds": 86400, "channel": "stable"}
MAX_FEED_BYTES = 2 * 1024 * 1024
PACKAGE = "optionsplus-linux"
INSTALL_ARGV = ["pkexec", "xbps-install", "-Syu", PACKAGE]
SUDO_COMMAND = "sudo xbps-install -Su " + PACKAGE
FETCH_COMMAND = "optionsplus-fetch-payload"
LAUNCHER = "optionsplus-linux"
EXIT_DECLINED = fetch.EXIT_DECLINED
POLKIT_NOT_AUTHORIZED = (126, 127)          # pkexec: dialog dismissed / not authorized


def default_payload_path():
    """The record the fetcher writes into the payload directory the launcher uses."""
    return os.path.join(paths.payload_dir(), paths.RECORD)


def read_os_release(path=OS_RELEASE):
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                key, sep, val = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    out[key] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def system_info(os_release_path=OS_RELEASE):
    """`api.system_info` with the OS name and version the About pages show."""
    rel = read_os_release(os_release_path)
    return {
        "@type": TYPE + "logi.protocol.api.system_info",
        "isCpuAppleSilicon": False,
        "osName": rel.get("PRETTY_NAME") or rel.get("NAME") or "Linux",
        "osVersion": rel.get("VERSION_ID") or rel.get("VERSION") or rel.get("BUILD_ID") or "",
    }


class UpdateError(Exception):
    pass


TAG_RE = re.compile(r"^%s-v(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?$" % re.escape(APP_ID))


def parse_tag(tag):
    """Sortable key for `optionsplus-linux-v<ver>[-pre]`, or None for other tags.

    A pre-release sorts below the release it precedes."""
    m = TAG_RE.match(tag)
    if not m:
        return None
    nums = [int(n) for n in m.group(1).split(".")]
    while nums and nums[-1] == 0:
        nums.pop()
    return (tuple(nums), 0 if m.group(2) else 1, m.group(2) or "")


def _request(method, url, timeout=15):
    """One bare request that never follows a redirect. Returns (status, location, body)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UpdateError("unsupported release URL %r" % url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.netloc, timeout=timeout)
    try:
        target = parts.path + ("?" + parts.query if parts.query else "")
        conn.request(method, target, headers={"User-Agent": "%s/%s" % (APP_ID, __version__)})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location"), resp.read()
    except (OSError, http.client.HTTPException) as exc:
        raise UpdateError("cannot reach the release server: %s" % exc)
    finally:
        conn.close()


def latest_stable_tag(base_url):
    status, location, _ = _request("HEAD", base_url + "/latest")
    if status not in (301, 302, 303, 307, 308) or not location:
        raise UpdateError("no release found (server answered %d)" % status)
    return urllib.parse.unquote(urllib.parse.urlsplit(location).path.rsplit("/", 1)[-1])


def newest_feed_tag(base_url):
    """Newest of our tags in the releases Atom feed, pre-releases included."""
    status, _, body = _request("GET", base_url + ".atom")
    if status != 200:
        raise UpdateError("release feed unavailable (server answered %d)" % status)
    try:
        root = ET.fromstring(body[:MAX_FEED_BYTES])
    except ET.ParseError as exc:
        raise UpdateError("release feed is not valid Atom: %s" % exc)
    tags = []
    for link in root.iter("{http://www.w3.org/2005/Atom}link"):
        href = link.get("href") or ""
        if "/releases/tag/" in href:
            tag = urllib.parse.unquote(href.rsplit("/", 1)[-1])
            if parse_tag(tag) is not None:
                tags.append(tag)
    if not tags:
        raise UpdateError("no %s releases in the release feed" % APP_ID)
    return max(tags, key=parse_tag)


class RealClock:
    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def state_dir_for(explicit=None):
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return explicit or os.environ.get("OPL_STATE_DIR") or os.path.join(base, "optionsplus-linux")


class Updater:
    def __init__(self, build_id="dev", payload_path=None, release_base_url=None,
                 clock=None, state_dir=None, runner=None, manifest_path=None):
        self.build_id = build_id
        self.base_url = (release_base_url or DEFAULT_RELEASES_URL).rstrip("/")
        self.clock = clock or RealClock()
        self.settings_path = os.path.join(state_dir_for(state_dir), "updates.json")
        self.settings = dict(DEFAULT_SETTINGS)
        self._load_settings()
        self.state = "INVALID"
        self.error_message = ""
        self.broadcast = lambda path, payload: None   # wired to the IPC server by the agent
        self._checking = False
        self._recheck = False
        self._wake = asyncio.Event()
        self.payload_path = payload_path or default_payload_path()
        self.runner = runner or SubprocessRunner()
        self.manifest_path = manifest_path
        self.on_restart = lambda: None         # wired to the agent: leave so the launcher can start anew
        self._installing = False
        self._fetching = False
        self._payload_error = ""

    # ---- persisted settings ---------------------------------------------
    def _load_settings(self):
        try:
            with open(self.settings_path) as fh:
                saved = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", self.settings_path, exc)
            return
        if isinstance(saved, dict):
            if isinstance(saved.get("checkPeriodically"), bool):
                self.settings["checkPeriodically"] = saved["checkPeriodically"]
            iv = saved.get("intervalInSeconds")
            if isinstance(iv, int) and not isinstance(iv, bool) and iv > 0:
                self.settings["intervalInSeconds"] = iv
            if saved.get("channel") in CHANNELS:
                self.settings["channel"] = saved["channel"]

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.settings, fh, indent=1)
            os.replace(tmp, self.settings_path)
        except OSError as exc:
            log.warning("cannot save update settings: %s", exc)

    def periodic_check(self):
        return {"@type": TYPE + "logi.protocol.updates.PeriodicCheck",
                "checkPeriodically": self.settings["checkPeriodically"],
                "downloadAutomatically": False,
                "intervalInSeconds": self.settings["intervalInSeconds"]}

    def set_periodic_check(self, payload):
        """Returns a result code. Nothing is applied unless every field is valid."""
        payload = payload if isinstance(payload, dict) else {}
        on, iv = payload.get("checkPeriodically"), payload.get("intervalInSeconds")
        if on is not None and not isinstance(on, bool):
            return "INVALID_ARG"
        if iv is not None and (not isinstance(iv, int) or isinstance(iv, bool) or iv <= 0):
            return "INVALID_ARG"
        if on is not None:
            self.settings["checkPeriodically"] = on
        if iv is not None:
            self.settings["intervalInSeconds"] = iv
        self._save_settings()
        self._wake.set()
        self.broadcast("/updates/periodic_check", self.periodic_check())
        return "SUCCESS"

    def channel(self):
        return {"@type": TYPE + "logi.protocol.updates.Channel",
                "name": self.settings["channel"], "pipelineHost": self.base_url}

    def set_channel(self, payload):
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or name not in CHANNELS:
            return "INVALID_ARG"
        self.settings["channel"] = name
        self._save_settings()
        self.broadcast("/updates/channel", self.channel())
        return "SUCCESS"

    async def run(self):
        """Check at start, then on the user's schedule; settings changes reschedule."""
        if self.settings["checkPeriodically"]:
            await self.check()
        while True:
            if not self.settings["checkPeriodically"]:
                await self._wake.wait()
                self._wake.clear()
                continue
            self._wake.clear()
            sleeper = asyncio.ensure_future(self.clock.sleep(self.settings["intervalInSeconds"]))
            waker = asyncio.ensure_future(self._wake.wait())
            try:
                done, _ = await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                sleeper.cancel()
                waker.cancel()
            if sleeper in done:
                await self.check()

    def data_version(self):
        """Version of the Logitech data the fetcher installed, or None."""
        try:
            with open(self.payload_path) as fh:
                rec = json.load(fh)
            return rec.get("dataVersion") or rec.get("version") or None
        except (OSError, ValueError, AttributeError):
            return None

    def status(self):
        st = {"@type": TYPE + "logi.protocol.updates.Status", "state": self.state}
        if self.error_message:
            st["errorMessage"] = self.error_message
        return st

    def _set_state(self, state, error=""):
        self.state, self.error_message = state, error
        self.broadcast("/updates/status", self.status())

    def _newest_tag(self):
        if self.settings["channel"] == "testing":
            return newest_feed_tag(self.base_url)
        return latest_stable_tag(self.base_url)

    def _check_blocking(self):
        tag = self._newest_tag()
        remote, local = parse_tag(tag), parse_tag("%s-v%s" % (APP_ID, __version__))
        if remote is None:
            raise UpdateError("unrecognised release tag %r" % tag)
        return "UPDATE_AVAILABLE" if remote > local else "NO_UPDATES"

    async def check(self):
        """One update check; every outcome ends in a terminal state."""
        if self._checking:
            self._recheck = True            # run once more so a late request is not lost
            return
        self._checking = True
        try:
            while True:
                self._recheck = False
                self._set_state("CHECKING_FOR_UPDATES")
                try:
                    state = await asyncio.get_event_loop().run_in_executor(None, self._check_blocking)
                except UpdateError as exc:
                    self._set_state("UPDATER_ERROR", str(exc))
                except Exception as exc:    # never leave the UI on a spinner
                    log.exception("update check failed")
                    self._set_state("UPDATER_ERROR", "update check failed: %s" % exc)
                else:
                    self._set_state(state)
                if not self._recheck:
                    break
        finally:
            self._checking = False
        self.broadcast("/updates/payload", self.payload_status())

    def request_check(self):
        asyncio.ensure_future(self.check())

    def info(self):
        version = __version__
        data = self.data_version()
        if data:
            version += " (Logitech data %s)" % data
        return {"@type": TYPE + "logi.protocol.updates.DepositoryInfo",
                "version": version, "buildId": self.build_id,
                "appId": APP_ID, "branch": "stable"}

    # ---- install ---------------------------------------------------------
    def download(self):
        """xbps fetches the package while installing, so "download" only acknowledges."""
        if self.state != "UPDATE_AVAILABLE":
            return "INVALID_ARG", "no update is available to download"
        self._set_state("UPDATE_READY")
        return "SUCCESS", ""

    def install(self):
        """Start the privileged xbps update; progress arrives as status broadcasts."""
        if self._installing or self.state not in ("UPDATE_AVAILABLE", "UPDATE_READY"):
            return "INVALID_ARG", "no update is ready to install"
        if not self.runner.which("pkexec"):
            self._set_state("UPDATER_ERROR", "pkexec (polkit) is not installed. "
                            "Run this in a terminal: " + SUDO_COMMAND)
            return "SUCCESS", ""
        self._installing = True
        self._set_state("UPDATE_INSTALLING")
        asyncio.ensure_future(self._install())
        return "SUCCESS", ""

    async def _install(self):
        try:
            code = await asyncio.get_event_loop().run_in_executor(None, self.runner.run_wait, INSTALL_ARGV)
        except Exception as exc:
            log.exception("update install failed")
            code, why = None, "the update could not be started: %s" % exc
        finally:
            self._installing = False
        if code == 0:
            self._set_state("RESTARTING")
            return
        if code in POLKIT_NOT_AUTHORIZED:
            why = "the update was not authorized (password prompt dismissed or refused)"
        elif code is not None:
            why = "xbps-install failed (exit status %d)" % code
        self._set_state("UPDATER_ERROR", "%s. To update by hand run: %s" % (why, SUDO_COMMAND))

    def restart_now(self):
        """Relaunch UI and agent through the launcher once this agent has exited."""
        launcher = self.runner.which(LAUNCHER)
        if not launcher:
            return "FAILED", "the %s launcher is not on PATH" % LAUNCHER
        self.runner.run(["env", "OPL_RESTART_WAIT_PID=%d" % os.getpid(), launcher])
        self.on_restart()
        return "SUCCESS", ""

    # ---- Logitech data refresh ------------------------------------------
    def _pinned(self):
        try:
            return fetch.load_manifest(self.manifest_path or fetch.default_manifest_path())
        except fetch.FetchError as exc:
            log.warning("%s", exc)
            return None

    def payload_status(self):
        """Whether the installed package pins newer Logitech data than the user has."""
        manifest = self._pinned()
        have = paths.installed_versions(os.path.dirname(self.payload_path))
        pinned = (manifest or {}).get("dataVersion", "")
        installed = (have or {}).get("dataVersion", "")
        offer = bool(manifest and have and _data_newer(manifest, have))
        st = {"@type": TYPE + "logi.protocol.updates.PayloadStatus", "available": offer,
              "installedVersion": installed, "pinnedVersion": pinned,
              "downloading": self._fetching}
        if self._payload_error:
            st["errorMessage"] = self._payload_error
        return st

    def download_payload(self):
        if self._fetching or not self.payload_status()["available"]:
            return "INVALID_ARG", "no newer Logitech data is pinned"
        self._fetching = True
        self._payload_error = ""
        self.broadcast("/updates/payload", self.payload_status())
        asyncio.ensure_future(self._fetch_payload())
        return "SUCCESS", ""

    async def _fetch_payload(self):
        try:
            code = await asyncio.get_event_loop().run_in_executor(None, self.runner.run_wait, [FETCH_COMMAND])
        except Exception as exc:
            log.exception("payload fetch failed")
            code = -1
        finally:
            self._fetching = False
        if code not in (0, EXIT_DECLINED):
            self._payload_error = "downloading the Logitech data failed (exit status %d)" % code
        self.broadcast("/updates/payload", self.payload_status())


def _version_key(text):
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums)


def _data_newer(manifest, have):
    """Newer pinned data: a higher data version, or the same version with different depots."""
    pin, cur = _version_key(manifest.get("dataVersion")), _version_key(have.get("dataVersion"))
    if pin != cur:
        return pin > cur
    recorded = have.get("depots") or {}
    return any((recorded.get(d["name"]) or {}).get("sha256") != d["sha256"]
               for d in manifest["depots"])
