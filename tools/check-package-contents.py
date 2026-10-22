#!/usr/bin/env python3
"""Fail if a package tree (or .xbps file) holds anything outside the allow-list
or anything that looks like Logitech payload. Usage: check-package-contents.py <dir|file.xbps>"""
import os
import re
import subprocess
import sys
import tempfile

LIB = r"usr/lib/optionsplus-linux"
ALLOW = [re.compile(p + "$") for p in (
    r"usr/bin/optionsplus-(linux|agent|fetch-payload)",
    LIB + r"/opl_agent/[A-Za-z0-9_]+\.py",
    LIB + r"/bootstrap/(main|links|guard)\.js",
    LIB + r"/bootstrap/package\.json",
    LIB + r"/depots\.json",
    r"usr/lib/udev/rules\.d/70-optionsplus-linux\.rules",
    r"usr/share/polkit-1/actions/io\.github\.soubarnak\.optionsplus-linux\.policy",
    r"etc/xdg/autostart/optionsplus-linux-agent\.desktop",
    r"usr/share/applications/optionsplus-linux(-x11)?\.desktop",
    r"usr/share/icons/hicolor/scalable/apps/optionsplus-linux\.svg",
    r"usr/share/licenses/optionsplus-linux/LICENSE",
    r"(INSTALL|REMOVE)",            # hooks in a destdir
    r"(props|files)\.plist",        # xbps metadata inside a .xbps
)]
DENY_EXT = {".exe", ".dll", ".asar", ".depot", ".pdb", ".msi", ".cab", ".sys", ".so", ".dylib"}
DENY_NAME = re.compile(r"logitech|logi_?options|logioptions", re.I)
DENY_MAGIC = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"PK\x03\x04")  # PE, ELF, Mach-O, zip


def problems(root):
    bad = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                bad.append((rel, "symlink not allowed"))
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in DENY_EXT or DENY_NAME.search(rel):
                bad.append((rel, "matches Logitech payload pattern"))
                continue
            if not any(p.match(rel) for p in ALLOW):
                bad.append((rel, "outside allow-list"))
                continue
            with open(path, "rb") as f:
                head = f.read(4)
            if head.startswith(DENY_MAGIC):
                bad.append((rel, "binary content (PE/ELF/Mach-O/zip)"))
    return bad


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    target = argv[1]
    if os.path.isdir(target):
        bad = problems(target)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["tar", "--zstd", "-xf", target, "-C", tmp], check=True)
            bad = problems(tmp)
    for rel, why in bad:
        print("package contents check: %s: %s" % (rel, why), file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
