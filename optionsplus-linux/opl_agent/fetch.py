"""First-run payload fetcher: downloads Logitech's Options+ depots after EULA confirmation.

Nothing is written (not even a directory) before the user accepts. Downloads go to a
staging directory next to the payload, are verified against the pinned SHA-256, unpacked
there, and only then swapped in, so a failure never leaves a half-installed payload.
"""
import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

from . import paths

EXIT_DECLINED = 2
EXIT_FAILED = 1
MAGICS = (bytes.fromhex("10011720"), bytes.fromhex("06052120"))   # main app, device assets
EOCD = b"PK\x05\x06"
SCAN_LIMIT = (16 << 20) + (64 << 10) + 22     # how far from the end of an installer the ZIP record is sought
HARD_MAX = 4 << 30                            # ceiling for a depot whose manifest entry pins no size
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class FetchError(Exception):
    pass


# --- manifest -------------------------------------------------------------

def default_manifest_path():
    env = os.environ.get("OPL_MANIFEST")
    if env:
        return env
    lib = os.environ.get("OPL_LIB_DIR", "/usr/lib/optionsplus-linux")
    return os.path.join(lib, "depots.json")


def load_manifest(path):
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
        for d in m["depots"]:
            d["name"], d["url"], int(d["size"]), d["sha256"]
        m["eulaUrl"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise FetchError("cannot read depot manifest %s: %s" % (path, e))
    return m


def check_url(url):
    u = urllib.parse.urlparse(url)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in LOOPBACK):
        return url
    raise FetchError("refusing non-https URL: %s" % url)


# --- depot container ------------------------------------------------------

def safe_join(root, name):
    name = name.replace("\\", "/")
    dst = os.path.normpath(os.path.join(root, name))
    if os.path.isabs(name) or not (dst + os.sep).startswith(os.path.abspath(root) + os.sep):
        raise FetchError("unsafe path in depot: %r" % name)
    return dst


def unpack_depot(path, out):
    """Unpack a depot: magic, u32 LE index length, JSON index, then u32 size + bytes per file.
    Both known magics share this layout (the device-asset one adds header keys)."""
    with open(path, "rb") as fh:
        head = fh.read(8)
        if len(head) < 8 or head[:4] not in MAGICS:
            raise FetchError("not a depot file: %s" % os.path.basename(path))
        n = struct.unpack_from("<I", head, 4)[0]
        try:
            index = json.loads(fh.read(n))["files"]
        except (ValueError, KeyError, TypeError):
            raise FetchError("corrupt depot index: %s" % os.path.basename(path))
        os.makedirs(out, exist_ok=True)
        for f in index:
            raw = fh.read(4)
            if len(raw) < 4:
                raise FetchError("truncated depot: %s" % os.path.basename(path))
            size = struct.unpack("<I", raw)[0]
            name = f["name"]
            dst = safe_join(out, name)
            if name.endswith(("/", "\\")):
                os.makedirs(dst, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as o:
                left = size
                while left:
                    chunk = fh.read(min(left, 1 << 20))
                    if not chunk:
                        raise FetchError("truncated depot: %s" % os.path.basename(path))
                    o.write(chunk)
                    left -= len(chunk)


# --- download -------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, progress, size=None):
    """Fetch url to dest. The pinned size is a hard cap: a longer stream is aborted and the partial
    file deleted, and a shorter one is an error. Without a size, HARD_MAX caps the stream."""
    cap = HARD_MAX if size is None else size
    got = 0
    try:
        try:
            with urllib.request.urlopen(check_url(url), timeout=60) as r, open(dest, "wb") as o:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > cap:
                        raise FetchError("download of %s is larger than the pinned %d bytes; aborted"
                                         % (url, cap))
                    o.write(chunk)
                    progress(len(chunk))
            if size is not None and got != size:
                raise FetchError("download of %s is %d bytes, expected the pinned %d" % (url, got, size))
        except FetchError:
            raise
        except Exception as e:
            raise FetchError("download failed for %s: %s" % (url, e))
    except FetchError:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise


class Progress:
    """Terminal progress on stderr (a zenity dialog is added by the GUI layer)."""

    def __init__(self, total, sink=None):
        self.total, self.done, self.sink, self._last = max(total, 1), 0, sink, 0.0

    def __call__(self, n):
        self.done += n
        pct = min(100, self.done * 100 // self.total)
        now = time.monotonic()
        if now - self._last > 0.25 or pct == 100:
            self._last = now
            if self.sink:
                self.sink(pct)
            sys.stderr.write("\rDownloading Options+ data: %3d%%" % pct)
            sys.stderr.flush()

    def finish(self):
        sys.stderr.write("\n")


# --- consent and progress UI ----------------------------------------------

def human(n):
    return "%.0f MB" % (n / 1e6)


def eula_text(manifest, installer=None):
    total = sum(int(d["size"]) for d in manifest["depots"])
    how = ("read from %s" % installer) if installer else "downloaded from Logitech's servers"
    return ("Options+ for Linux needs Logitech's Options+ data. It will be %s: %s (%d bytes).\n"
            "By continuing you accept Logitech's End User License Agreement:\n  %s\n"
            % (how, human(total), total, manifest["eulaUrl"]))


class UI:
    """zenity or kdialog when a display and the tool exist, else the terminal."""

    def __init__(self, use_gui=True):
        self.tool = None
        if use_gui and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            for t in ("zenity", "kdialog"):
                if shutil.which(t):
                    self.tool = t
                    break
        self._bar = None

    def confirm(self, text, assume_yes=False):
        if assume_yes:
            sys.stderr.write(text)
            return True
        if self.tool == "zenity":
            return subprocess.run(["zenity", "--question", "--title", "Options+ for Linux", "--width", "480",
                                   "--no-markup", "--text", text + "\nDownload now?",
                                   "--ok-label", "Download", "--cancel-label", "Cancel"]).returncode == 0
        if self.tool == "kdialog":
            return subprocess.run(["kdialog", "--title", "Options+ for Linux", "--yesno",
                                   text + "\nDownload now?"]).returncode == 0
        sys.stderr.write(text + "Download now? [y/N] ")
        sys.stderr.flush()
        try:
            answer = sys.stdin.readline()
        except OSError:
            answer = ""
        return answer.strip().lower() in ("y", "yes")

    def progress_sink(self):
        if self.tool != "zenity":           # kdialog progress needs D-Bus; terminal bar is used
            return None
        self._bar = subprocess.Popen(["zenity", "--progress", "--title", "Options+ for Linux",
                                      "--text", "Downloading Options+ data from Logitech", "--auto-close",
                                      "--no-cancel"], stdin=subprocess.PIPE, text=True)

        def sink(pct):
            try:
                self._bar.stdin.write("%d\n" % pct)
                self._bar.stdin.flush()
            except (OSError, ValueError):
                pass
        return sink

    def progress_close(self):
        if self._bar:
            try:
                self._bar.stdin.close()
                self._bar.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._bar.kill()
            self._bar = None


def elevate(argv, manifest_path):
    """Re-run ourselves as root through pkexec, else sudo."""
    prog = shutil.which("pkexec") or shutil.which("sudo")
    if not prog:
        raise FetchError("--system needs pkexec or sudo, and neither was found")
    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [prog, "env", "PYTHONPATH=" + lib, "OPL_MANIFEST=" + manifest_path,
           sys.executable, "-m", "opl_agent.fetch"] + argv
    return subprocess.run(cmd).returncode


# --- install --------------------------------------------------------------

def swap_in(staging, dest):
    """Replace dest with staging; the old payload is only removed after the new one is in place."""
    old = None
    if os.path.lexists(dest):
        old = dest + ".old-%d" % os.getpid()
        os.rename(dest, old)
    try:
        os.rename(staging, dest)
    except OSError:
        if old:
            os.rename(old, dest)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)


def assemble(staging, depot_files, record):
    """Unpack depot files ({name: path}) into staging and write the layout and record."""
    for name, path in depot_files.items():
        unpack_depot(path, os.path.join(staging, "depots", name))
    main = os.path.join(staging, "depots", "logioptionsplus")
    asar = os.path.join(main, "resources", "app.asar")
    if not os.path.isfile(asar):
        raise FetchError("main depot has no resources/app.asar")
    os.replace(asar, os.path.join(staging, "app.asar"))
    with open(os.path.join(staging, paths.RECORD), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    for base, dirs, files in os.walk(staging):          # readable by every user for --system
        os.chmod(base, 0o755)
        for f in files:
            if not os.path.islink(os.path.join(base, f)):
                os.chmod(os.path.join(base, f), 0o644)


@contextlib.contextmanager
def fetch_lock(parent):
    """Exclusive, non-blocking lock on the payload's parent directory (flock on the directory
    itself, so no lock file is left behind). Released on every path, including a crash."""
    try:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise FetchError("cannot lock %s: %s" % (parent, e))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise FetchError("another fetch is running for %s; try again when it has finished" % parent)
        yield
    finally:
        os.close(fd)            # closing drops the flock


def stage_and_swap(dest, collect):
    """collect(staging) -> (depot_files, record); then unpack and swap into dest. The lock is held
    for the whole fetch and swap."""
    parent = os.path.dirname(dest.rstrip("/"))
    os.makedirs(parent, exist_ok=True)
    with fetch_lock(parent):
        staging = tempfile.mkdtemp(prefix=".staging-", dir=parent)
        try:
            files, record = collect(staging)
            build = os.path.join(staging, "payload")
            os.mkdir(build)
            assemble(build, files, record)
            swap_in(build, dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def collect_download(manifest, gui):
    def collect(staging):
        progress = Progress(sum(int(d["size"]) for d in manifest["depots"]), gui.progress_sink())
        files = {}
        try:
            for d in manifest["depots"]:
                target = os.path.join(staging, d["name"] + ".depot")
                download(d["url"], target, progress, int(d["size"]))
                if sha256_file(target) != d["sha256"].lower():
                    raise FetchError("checksum mismatch for %s; nothing was installed" % d["name"])
                files[d["name"]] = target
        finally:
            progress.finish()
            gui.progress_close()
        return files, make_record(manifest, "download")
    return collect


def find_in_zip(z, entry):
    names = {os.path.basename(n): n for n in z.namelist()}
    for cand in (entry["url"].split("/")[-1], entry["url"].split("/")[-2] + ".depot"):
        if cand in names:
            return names[cand]
    return None


class _Window(io.RawIOBase):
    """Read-only view of the first `end` bytes of a file (zipfile only looks for the EOCD at EOF)."""

    def __init__(self, fh, end):
        self._fh, self._end, self._pos = fh, end, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=os.SEEK_SET):
        base = {os.SEEK_SET: 0, os.SEEK_CUR: self._pos, os.SEEK_END: self._end}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, b):
        n = max(0, min(len(b), self._end - self._pos))
        self._fh.seek(self._pos)
        data = self._fh.read(n)
        b[:len(data)] = data
        self._pos += len(data)
        return len(data)


def find_zip_end(fh, size, chunk=1 << 20):
    """Offset just past the ZIP end-of-central-directory record, or None. Scans backwards from the
    end of the file (at most SCAN_LIMIT bytes): Logitech's installer carries an Authenticode
    signature (about 96 KB) after the record, beyond the 64 KiB that zipfile searches. A candidate
    counts only if it is a single-disk record whose central directory starts with a file header."""
    lower = max(0, size - SCAN_LIMIT)
    tail, pos = b"", size
    while pos > lower:
        start = max(lower, pos - chunk)
        fh.seek(start)
        buf = fh.read(pos - start) + tail
        i = buf.rfind(EOCD)
        while i >= 0:
            end = _valid_eocd(fh, size, start + i, buf[i:i + 22])
            if end:
                return end
            i = buf.rfind(EOCD, 0, i)
        tail = buf[:len(EOCD) - 1 + 22]       # a record may straddle the chunk boundary
        pos = start
    return None


def _valid_eocd(fh, size, at, rec):
    if len(rec) < 22:
        return None
    disk, cd_disk, n_disk, n_total, cd_size, cd_off, clen = struct.unpack("<4x4H2LH", rec)
    if disk or cd_disk or n_disk != n_total or at + 22 + clen > size:
        return None
    if 0xFFFF in (n_disk, n_total) or 0xFFFFFFFF in (cd_size, cd_off):
        raise FetchError("this installer uses ZIP64, which is not supported")
    if cd_size + cd_off > at:
        return None
    if cd_size:
        fh.seek(at - cd_size)
        if fh.read(4) != b"PK\x01\x02":
            return None
    return at + 22 + clen


def collect_installer(manifest, exe):
    def collect(staging):
        try:
            fh = open(exe, "rb")
        except OSError as e:
            raise FetchError("%s is not an Options+ offline installer: %s" % (exe, e))
        with fh:
            return unpack(fh, staging)

    def unpack(fh, staging):
        try:
            end = find_zip_end(fh, os.fstat(fh.fileno()).st_size)
            if end is None:
                raise zipfile.BadZipFile("File is not a zip file")
            z = zipfile.ZipFile(_Window(fh, end))     # the PE overlay is a plain ZIP
            offline = json.loads(z.read("offline.json"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
            raise FetchError("%s is not an Options+ offline installer: %s" % (exe, e))
        by_name = {d["name"]: d for d in offline.get("depots", [])}
        files, depots = {}, {}
        with z:
            for want in manifest["depots"]:
                entry = by_name.get(want["name"])
                member = find_in_zip(z, entry) if entry else None
                if not member:
                    raise FetchError("installer has no depot named %s" % want["name"])
                target = os.path.join(staging, want["name"] + ".depot")
                try:
                    with z.open(member) as src, open(target, "wb") as o:
                        shutil.copyfileobj(src, o)
                except (zipfile.BadZipFile, OSError) as e:
                    raise FetchError("cannot read %s from installer: %s" % (want["name"], e))
                digest = sha256_file(target)
                if digest != str(entry.get("mac", "")).lower() or digest != want["sha256"].lower():
                    raise FetchError("checksum mismatch for %s; nothing was installed" % want["name"])
                files[want["name"]] = target
                depots[want["name"]] = {"sha256": digest, "size": os.path.getsize(target),
                                        "url": entry.get("url", "")}
        rec = make_record(manifest, "installer")
        rec["dataVersion"] = str(offline.get("version", rec["dataVersion"]))
        rec["depots"] = depots
        return files, rec
    return collect


def up_to_date(manifest, dest):
    rec = paths.installed_versions(dest)
    if not rec or not paths.is_installed(dest):
        return False
    have = rec.get("depots", {})
    return (rec.get("dataVersion") == manifest.get("dataVersion", "") and
            all(have.get(d["name"], {}).get("sha256") == d["sha256"] for d in manifest["depots"]))


def make_record(manifest, source):
    return {"schema": 1, "dataVersion": manifest.get("dataVersion", ""), "source": source,
            "installedAt": int(time.time()),
            "depots": {d["name"]: {"sha256": d["sha256"], "size": int(d["size"]), "url": d["url"]}
                       for d in manifest["depots"]}}


# --- cli ------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="optionsplus-fetch-payload", description=__doc__.split("\n")[0])
    p.add_argument("--manifest", default=None, help="pinned depot manifest (default: installed depots.json)")
    p.add_argument("--accept-eula", action="store_true",
                   help="accept Logitech's EULA without asking (you have read it)")
    p.add_argument("--from-installer", metavar="EXE",
                   help="take depots from an offline installer you already have (proxies, dead URLs)")
    p.add_argument("--system", action="store_true",
                   help="install for every user through pkexec or sudo")
    p.add_argument("--force", action="store_true", help="fetch even when the installed data is current")
    p.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)   # set on the re-run
    p.add_argument("--no-gui", action="store_true", help="never open zenity or kdialog")
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_parser().parse_args(argv)
    try:
        mpath = os.path.abspath(args.manifest or default_manifest_path())
        manifest = load_manifest(mpath)
        system = args.system
        dest = paths.system_payload_dir() if system else paths.user_payload_dir()
        if not args.force and not args.from_installer and up_to_date(manifest, dest):
            sys.stderr.write("Options+ data is already up to date.\n")
            return 0
        if args.from_installer and not os.path.isfile(args.from_installer):
            raise FetchError("no such installer: %s" % args.from_installer)
        ui = UI(not args.no_gui)
        if not ui.confirm(eula_text(manifest, args.from_installer), args.accept_eula):
            sys.stderr.write("Declined. Nothing was %s.\n" % ("installed" if args.from_installer else "downloaded"))
            return EXIT_DECLINED
        if system and os.geteuid() != 0 and not args.elevated:
            rest = [a for a in argv if a not in ("--accept-eula", "--manifest") and a != args.manifest]
            return elevate(rest + ["--accept-eula", "--elevated"], mpath)
        if args.from_installer:
            collect = collect_installer(manifest, os.path.abspath(args.from_installer))
        else:
            collect = collect_download(manifest, ui)
        stage_and_swap(dest, collect)
    except FetchError as e:
        sys.stderr.write("optionsplus-fetch-payload: %s\n" % e)
        return EXIT_FAILED
    sys.stderr.write("Options+ data installed in %s\n" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
