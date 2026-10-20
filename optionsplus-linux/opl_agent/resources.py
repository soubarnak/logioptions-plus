"""Runtime resource packages read from the Logitech-supplied data directory.

Options+ ships its UI strings, device catalogue and device images inside the
depots of the installer payload Logitech distributes. This package never
contains those files; the xbps template (or the user) unpacks them from
Logitech's own download, one directory per depot, into DEPOT_DIR, and the
agent serves them from there at runtime.

DEPOT_DIR defaults to /usr/lib/optionsplus-linux/logitech/depots
(override: OPL_DEPOT_DIR). The main "logioptionsplus" depot holds data/ with
the strings and the device catalogue; OPL_DATA_DIR overrides that data/
directory alone, and its parent then stands in for the logioptionsplus depot.
"""

import base64
import json
import logging
import os

log = logging.getLogger("opl.resources")

TYPE = "type.googleapis.com/"
DEPOT_DIR = os.environ.get("OPL_DEPOT_DIR", "/usr/lib/optionsplus-linux/logitech/depots")
DATA_DIR = os.environ.get("OPL_DATA_DIR", os.path.join(DEPOT_DIR, "logioptionsplus", "data"))
MAIN_DEPOT = "logioptionsplus"

CONTENT_TYPES = {".webp": "image/webp", ".png": "image/png", ".svg": "image/svg+xml",
                 ".json": "application/json", ".jpg": "image/jpeg", ".gif": "image/gif"}

# UI package name -> directory below DATA_DIR holding <lang>.yaml files
STRING_PACKAGES = {
    "logioptionsplus/data/strings": "strings",
}

_cache = {}


def configure(depot_dir=None, data_dir=None):
    """Point the module at another depot tree (tests, relocated installs) and
    drop everything read from the previous one. None keeps the env default."""
    global DEPOT_DIR, DATA_DIR, _catalogue
    if depot_dir is not None:
        DEPOT_DIR = depot_dir
        DATA_DIR = os.path.join(depot_dir, "logioptionsplus", "data")
    if data_dir is not None:
        DATA_DIR = data_dir
    _cache.clear()
    _catalogue = None


def _load_yaml(path):
    try:
        import yaml
    except ImportError:            # PyYAML missing: tiny KEY: value reader
        out = {}
        for line in open(path, encoding="utf-8"):
            if line[:1].isalnum() and ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        return out
    # BaseLoader keeps every value a string (no YES -> True coercion)
    data = yaml.load(open(path, encoding="utf-8"), Loader=yaml.BaseLoader) or {}
    return {str(k): str(v) for k, v in data.items()}


def strings(package, lang):
    """Return {key: text} for a string package, or None if not installed."""
    sub = STRING_PACKAGES.get(package)
    if sub is None:
        return None
    for cand in (lang, "en-US"):
        if not cand:
            continue
        path = os.path.join(DATA_DIR, sub, cand + ".yaml")
        if path in _cache:
            return _cache[path]
        if os.path.exists(path):
            try:
                _cache[path] = _load_yaml(path)
                log.info("loaded %d strings from %s", len(_cache[path]), path)
                return _cache[path]
            except Exception as exc:
                log.warning("cannot read %s: %s", path, exc)
    return None


def localized_packages(request, lang):
    """Reply payload for GET /resources/localized_packages."""
    names = (request or {}).get("packages") or []
    pkgs = []
    for name in names:
        s = strings(name, lang)
        pkgs.append({"name": name, "available": s is not None, "strings": s or {}})
    return {"@type": TYPE + "logi.protocol.resources.LocalizedPackages", "packages": pkgs}


# ---- depots and pipeline:// URLs -----------------------------------------

def depot_root(name):
    if name == MAIN_DEPOT:
        return os.path.dirname(os.path.abspath(DATA_DIR))
    return os.path.join(DEPOT_DIR, name)


def _depot_file(depot, rel):
    root = os.path.abspath(depot_root(depot))
    path = os.path.normpath(os.path.join(root, rel))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    return path


def resolve(url):
    """Map pipeline://<depot>/<path> to a file:// URL, or "" when not installed.

    The real agent hands the UI resolved local URLs; the renderer itself has
    no handler for the pipeline:// scheme."""
    if not url or not url.startswith("pipeline://"):
        return url or ""
    depot, _, rel = url[len("pipeline://"):].partition("/")
    path = _depot_file(depot, rel)
    return "file://" + path if path else ""


# ---- device catalogue ------------------------------------------------------

_catalogue = None


def _load_catalogue():
    global _catalogue
    if _catalogue is not None:
        return _catalogue
    _catalogue = {}
    path = os.path.join(DATA_DIR, "devices", "devices_initial.json")
    try:
        entries = json.load(open(path, encoding="utf-8")).get("devices", [])
    except (OSError, ValueError) as exc:
        log.warning("no device catalogue at %s: %s", path, exc)
        return _catalogue
    for e in entries:
        for mode in e.get("modes", []):
            for itf in mode.get("interfaces", []):
                if itf.get("id"):
                    _catalogue.setdefault(itf["id"].lower(), e)
    log.info("device catalogue: %d interface ids from %s", len(_catalogue), path)
    return _catalogue


def catalogue_entry(pid, vid=0x046D):
    """Catalogue entry for a device matched by its "<vid>_<pid>" interface id."""
    if not pid:
        return None
    return _load_catalogue().get("%04x_%04x" % (vid, pid))


def depot_installed(depot):
    return bool(depot) and os.path.isfile(os.path.join(depot_root(depot), "manifest.json"))


def _data_uri(path, ctype):
    with open(path, "rb") as fh:
        return "data:%s;base64,%s" % (ctype, base64.b64encode(fh.read()).decode())


def device_resources(depot, model_id):
    """logi.protocol.devices.Resources for one model, from the depot manifest.

    JSON resources go out as data: URIs (the renderer reads them with fetch and
    a synchronous XHR); images as file:// URLs."""
    out = {"@type": TYPE + "logi.protocol.devices.Resources", "resources": []}
    if not depot_installed(depot):
        return out
    try:
        manifest = json.load(open(os.path.join(depot_root(depot), "manifest.json"), encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("bad manifest for depot %s: %s", depot, exc)
        return out
    models = {d.get("modelId"): d for d in manifest.get("devices", [])}
    entry = models.get(model_id) or models.get((model_id or "").split("_ext")[0])
    for r in (entry or {}).get("resources", []):
        path = _depot_file(depot, r.get("src", ""))
        if not r.get("key") or not path:
            continue
        ctype = CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        as_data = ctype == "application/json"
        out["resources"].append({
            "key": r["key"], "contentType": ctype, "asDataUri": as_data,
            "url": _data_uri(path, ctype) if as_data else "file://" + path,
        })
    return out
