#!/bin/bash
# Usage: sign-repo.sh <assets-dir> <package-file> <private-key.pem>
# Turns <assets-dir> (holding the noarch .xbps) into a signed xbps repository for every
# architecture Void supports, and signs the package itself (.sig2). The key file stays the caller's.
set -euo pipefail

DIR=${1:?assets dir} PKG=${2:?package file} KEY=${3:?private key}
SIGNEDBY=${XBPS_SIGNEDBY:-soubarnak <soubarna@live.in>}
ARCHES="x86_64 x86_64-musl i686 aarch64 aarch64-musl armv7l armv7l-musl armv6l armv6l-musl"

[ -f "$KEY" ] || { echo "sign-repo: private key $KEY not found" >&2; exit 1; }
[ -f "$DIR/$PKG" ] || { echo "sign-repo: $DIR/$PKG not found" >&2; exit 1; }
KEY=$(readlink -f "$KEY")

cd "$DIR"
# The package is noarch: index it once per architecture.
for arch in $ARCHES; do
    XBPS_ARCH=$arch xbps-rindex -a "$PKG"
    XBPS_ARCH=$arch xbps-rindex --privkey "$KEY" --signedby "$SIGNEDBY" -s .
done
xbps-rindex --privkey "$KEY" -S "$PKG"
