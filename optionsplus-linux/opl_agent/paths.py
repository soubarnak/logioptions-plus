"""Where the Logitech payload lives, and what is recorded about it.

Layout of one payload directory (written by the fetcher, read by launcher and agent):

    app.asar             Options+ UI bundle (from the main depot's resources/)
    depots/<name>/       one unpacked depot per directory
    payload.json         record of the installed data versions
"""
import json
import os

RECORD = "payload.json"
SYSTEM_DEFAULT = "/usr/lib/optionsplus-linux/logitech"


def user_payload_dir(environ=None):
    env = os.environ if environ is None else environ
    base = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "optionsplus-linux", "payload")


def system_payload_dir(environ=None):
    env = os.environ if environ is None else environ
    return env.get("OPL_SYSTEM_PAYLOAD_DIR") or SYSTEM_DEFAULT


def is_installed(payload_dir):
    return (os.path.isfile(os.path.join(payload_dir, RECORD))
            and os.path.isfile(os.path.join(payload_dir, "app.asar")))


def payload_dir(environ=None):
    """OPL_PAYLOAD_DIR, else the user's directory when installed, else the system-wide
    one when installed, else the user's directory (where a fetch would put it)."""
    env = os.environ if environ is None else environ
    if env.get("OPL_PAYLOAD_DIR"):
        return env["OPL_PAYLOAD_DIR"]
    user = user_payload_dir(env)
    if is_installed(user):
        return user
    system = system_payload_dir(env)
    return system if is_installed(system) else user


def installed_versions(directory):
    """The recorded payload info ({"dataVersion", "depots": {...}}), or None when absent/corrupt."""
    try:
        with open(os.path.join(directory, RECORD), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None
