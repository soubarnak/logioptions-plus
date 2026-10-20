#!/bin/bash
# Build optionsplus-linux-<version>_1.noarch.xbps from this checkout.
# Needs only xbps-create (package xbps) and tar with zstd; no void-packages checkout.
#
# Version comes from optionsplus-linux/opl_agent/__init__.py (__version__).
# Environment: OPL_BUILD_DIR  output directory (default: <repo>/build)
#              ELECTRON_PKG   Electron 3x package to depend on (default: electron35)
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SRC=$REPO/optionsplus-linux
OUT=${OPL_BUILD_DIR:-$REPO/build}
ELECTRON_PKG=${ELECTRON_PKG:-electron35}

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$SRC/opl_agent/__init__.py")
[ -n "$VERSION" ] || { echo "build-xbps: no __version__ in opl_agent/__init__.py" >&2; exit 1; }
PKGVER=optionsplus-linux-${VERSION}_1

mkdir -p "$OUT"
DEST=$(mktemp -d "$OUT/destdir.XXXXXX")
trap 'rm -rf "$DEST"' EXIT

LIB=$DEST/usr/lib/optionsplus-linux
install -d "$DEST/usr/bin" "$LIB/bootstrap" "$DEST/usr/lib/udev/rules.d" \
    "$DEST/etc/xdg/autostart" "$DEST/usr/share/applications" \
    "$DEST/usr/share/icons/hicolor/scalable/apps" "$DEST/usr/share/licenses/optionsplus-linux"
install -m755 "$SRC/bin/optionsplus-linux" "$SRC/bin/optionsplus-agent" "$DEST/usr/bin/"
cp -r "$SRC/opl_agent" "$LIB/opl_agent"
find "$LIB/opl_agent" -name __pycache__ -prune -exec rm -rf {} +
install -m644 "$SRC/bootstrap/main.js" "$SRC/bootstrap/package.json" "$LIB/bootstrap/"
install -m644 "$SRC/data/70-optionsplus-linux.rules" "$DEST/usr/lib/udev/rules.d/"
install -m644 "$SRC/data/optionsplus-linux-agent.desktop" "$DEST/etc/xdg/autostart/"
install -m644 "$SRC/data/optionsplus-linux.desktop" "$DEST/usr/share/applications/"
install -m644 "$SRC/data/optionsplus-linux.svg" "$DEST/usr/share/icons/hicolor/scalable/apps/"
install -m644 "$REPO/LICENSE" "$DEST/usr/share/licenses/optionsplus-linux/LICENSE"
install -m755 "$SRC/packaging/INSTALL" "$SRC/packaging/REMOVE" "$DEST/"

# Fail the build on anything outside the allow-list or Logitech-looking.
python3 "$REPO/tools/check-package-contents.py" "$DEST"

(cd "$OUT" && xbps-create -q -A noarch -n "$PKGVER" \
    -s "Logitech Options+ for Linux: native agent and launcher" \
    -D "python3>=3.11 python3-yaml ${ELECTRON_PKG} eudev" \
    -l "Apache-2.0" -m "soubarnak <soubarna@live.in>" \
    -H "https://github.com/soubarnak/logioptions-plus" "$DEST")

PKG=$OUT/$PKGVER.noarch.xbps
[ -f "$PKG" ] || { echo "build-xbps: expected $PKG" >&2; exit 1; }
python3 "$REPO/tools/check-package-contents.py" "$PKG"
echo "$PKG"
