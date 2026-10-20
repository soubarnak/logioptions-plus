#!/bin/bash
# Usage: release-check-tag.sh <tag>
# Succeeds (printing the version) only when <tag> is optionsplus-linux-v<__version__> of
# optionsplus-linux/opl_agent/__init__.py.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TAG=${1-}
VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO/optionsplus-linux/opl_agent/__init__.py")
[ -n "$VERSION" ] || { echo "release-check-tag: no __version__ in opl_agent/__init__.py" >&2; exit 1; }

if [ "$TAG" != "optionsplus-linux-v$VERSION" ]; then
    echo "release-check-tag: tag '$TAG' does not match __version__ $VERSION (want optionsplus-linux-v$VERSION)" >&2
    exit 1
fi
echo "$VERSION"
