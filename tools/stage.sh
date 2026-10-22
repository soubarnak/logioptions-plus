#!/bin/sh
# Stage the optionsplus-linux package tree into DESTDIR. This is the ONE list of installed
# files: tools/build-xbps.sh (the release build) and srcpkgs/optionsplus-linux/template
# (void-packages) both call it, so the two packages cannot drift apart.
#
# Usage: tools/stage.sh DESTDIR
# INSTALL and REMOVE land in the DESTDIR root, where xbps-create (and xbps-src) pick up
# package hooks.
set -eu

[ $# -eq 1 ] || { echo "usage: stage.sh DESTDIR" >&2; exit 2; }
DEST=$1
REPO=$(cd "$(dirname "$0")/.." && pwd)
SRC=$REPO/optionsplus-linux
LIB=$DEST/usr/lib/optionsplus-linux

install -d "$DEST/usr/bin" "$LIB/bootstrap" "$DEST/usr/lib/udev/rules.d" \
    "$DEST/etc/xdg/autostart" "$DEST/usr/share/applications" "$DEST/usr/share/polkit-1/actions" \
    "$DEST/usr/share/icons/hicolor/scalable/apps" "$DEST/usr/share/licenses/optionsplus-linux"
install -m755 "$SRC/bin/optionsplus-linux" "$SRC/bin/optionsplus-agent" \
    "$SRC/bin/optionsplus-fetch-payload" "$DEST/usr/bin/"
cp -r "$SRC/opl_agent" "$LIB/opl_agent"
find "$LIB/opl_agent" -name __pycache__ -prune -exec rm -rf {} +
install -m644 "$SRC/bootstrap/main.js" "$SRC/bootstrap/links.js" "$SRC/bootstrap/guard.js" \
    "$SRC/bootstrap/package.json" "$LIB/bootstrap/"
install -m644 "$SRC/data/depots.json" "$LIB/depots.json"
install -m644 "$SRC/data/70-optionsplus-linux.rules" "$DEST/usr/lib/udev/rules.d/"
install -m644 "$SRC/data/io.github.soubarnak.optionsplus-linux.policy" "$DEST/usr/share/polkit-1/actions/"
install -m644 "$SRC/data/optionsplus-linux-agent.desktop" "$DEST/etc/xdg/autostart/"
install -m644 "$SRC/data/optionsplus-linux.desktop" "$SRC/data/optionsplus-linux-x11.desktop" "$DEST/usr/share/applications/"
install -m644 "$SRC/data/optionsplus-linux.svg" "$DEST/usr/share/icons/hicolor/scalable/apps/"
install -m644 "$REPO/LICENSE" "$DEST/usr/share/licenses/optionsplus-linux/LICENSE"
install -m755 "$SRC/packaging/INSTALL" "$SRC/packaging/REMOVE" "$DEST/"
