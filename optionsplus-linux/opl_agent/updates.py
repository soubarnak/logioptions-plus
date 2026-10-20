"""Version, About and the update check against GitHub releases.

No Logitech pipeline, no telemetry: the only network traffic is one
unauthenticated request to the project's own GitHub releases, carrying no user
data. Everything environment-specific (feed base URL, clock, os-release path,
payload record) is injectable so tests run against a local HTTP server.
"""

import json
import os

from . import __version__, responses

TYPE = responses.TYPE
APP_ID = "optionsplus-linux"
DEFAULT_RELEASES_URL = "https://github.com/soubarnak/logioptions-plus/releases"
OS_RELEASE = "/etc/os-release"


def default_payload_path():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "optionsplus-linux", "payload.json")


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


class Updater:
    def __init__(self, build_id="dev", payload_path=None, release_base_url=None,
                 clock=None, state_dir=None):
        self.build_id = build_id
        self.base_url = (release_base_url or DEFAULT_RELEASES_URL).rstrip("/")
        self.clock = clock
        self.state_dir = state_dir
        self.payload_path = payload_path or default_payload_path()

    def data_version(self):
        """Version of the Logitech data the fetcher installed, or None."""
        try:
            with open(self.payload_path) as fh:
                return json.load(fh).get("version") or None
        except (OSError, ValueError, AttributeError):
            return None

    def info(self):
        version = __version__
        data = self.data_version()
        if data:
            version += " (Logitech data %s)" % data
        return {"@type": TYPE + "logi.protocol.updates.DepositoryInfo",
                "version": version, "buildId": self.build_id,
                "appId": APP_ID, "branch": "stable"}
