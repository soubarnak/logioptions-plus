"""Installed desktop applications, for ADD APPLICATION.

Scans .desktop files from the system, per-user and Flatpak export application
directories and builds ``logi.protocol.applications.Applications`` payloads
(id, name, executable, icon as ``file://``). Nothing here touches the network
or needs a display.
"""

import glob
import logging
import os
import re
import shlex
import shutil

log = logging.getLogger("opl.applications")

TYPE = "type.googleapis.com/"
ICON_EXTS = (".png", ".svg", ".xpm")
WM_CLASS_KEY = "startupWMClass"
_FIELD_CODE = re.compile(r"%[a-zA-Z%]")


def default_dirs(environ=None):
    """Application directories in priority order: per-user first, so a user's
    own .desktop file overrides the system one with the same id."""
    env = os.environ if environ is None else environ
    home = os.path.expanduser("~")
    data_home = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    data_dirs = (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    roots = [data_home,
             os.path.join(home, ".local", "share", "flatpak", "exports", "share"),
             "/var/lib/flatpak/exports/share"] + [d for d in data_dirs if d]
    seen, out = set(), []
    for root in roots:
        d = os.path.join(root, "applications")
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _parse(path):
    """The [Desktop Entry] group as a dict; Name/Comment keep only the unlocalised key."""
    entry, in_group = {}, False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                in_group = line == "[Desktop Entry]"
                continue
            if in_group and "=" in line:
                key, _, value = line.partition("=")
                entry.setdefault(key.strip(), value.strip())
    return entry


def _resolve_icon(icon, app_dir):
    """file:// URL for a .desktop Icon= value, or ''. A theme name is looked up
    in the icons/ and pixmaps/ directories next to the application directory."""
    if not icon:
        return ""
    if os.path.isabs(icon):
        return "file://" + icon if os.path.isfile(icon) else ""
    share = os.path.dirname(app_dir)
    best, best_size = "", -1
    for ext in ICON_EXTS:
        for path in glob.glob(os.path.join(share, "icons", "*", "*", "apps", icon + ext)):
            m = re.search(r"/(\d+)x\d+/", path)
            size = int(m.group(1)) if m else (10000 if "/scalable/" in path else 0)
            if size > best_size:
                best, best_size = path, size
        pix = os.path.join(share, "pixmaps", icon + ext)
        if best_size < 0 and os.path.isfile(pix):
            best, best_size = pix, 0
    return "file://" + best if best else ""


def _executable(exec_line):
    """(command line without field codes, program path)."""
    cmd = _FIELD_CODE.sub("", exec_line).strip()
    try:
        argv = shlex.split(cmd)
    except ValueError:
        argv = cmd.split()
    if not argv:
        return cmd, ""
    if os.path.basename(argv[0]) == "env":       # env [-opts] [VAR=value ...] program: the program is the app
        rest = [a for a in argv[1:]]
        while rest and (rest[0].startswith("-") or "=" in rest[0]):
            rest.pop(0)
        argv = rest or argv
    prog = argv[0]
    return cmd, prog if os.path.isabs(prog) else (shutil.which(prog) or prog)


def scan(dirs):
    """Applications found in `dirs` (earlier directories win on equal ids),
    sorted by name. Unreadable files and hidden entries are skipped."""
    found = {}
    for app_dir in dirs:
        for path in sorted(glob.glob(os.path.join(app_dir, "**", "*.desktop"), recursive=True)):
            app_id = os.path.relpath(path, app_dir)[:-len(".desktop")].replace(os.sep, "-")
            if app_id in found:
                continue
            try:
                entry = _parse(path)
            except OSError as exc:
                log.debug("skipping %s: %s", path, exc)
                continue
            if entry.get("Type") != "Application" or not entry.get("Name"):
                continue
            if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
                continue
            cmd, prog = _executable(entry.get("Exec", ""))
            icon = _resolve_icon(entry.get("Icon", ""), app_dir)
            found[app_id] = {
                "applicationId": app_id, "name": entry["Name"],
                "applicationPath": prog, "applicationFolder": os.path.dirname(prog),
                "commandLine": cmd, "bundleId": app_id,
                "poster": icon, "posterPath": icon, "posterUrl": icon,
                "isInstalled": True,
            }
            if entry.get("StartupWMClass"):
                found[app_id][WM_CLASS_KEY] = entry["StartupWMClass"]    # for focus matching, not for the UI
    return sorted(found.values(), key=lambda a: (a["name"].lower(), a["applicationId"]))


def payload(apps):
    return {"@type": TYPE + "logi.protocol.applications.Applications",
            "applications": [{k: v for k, v in a.items() if k != WM_CLASS_KEY} for a in apps]}
