#!/bin/bash
# Usage: release-notes.sh <version>
# Prints the "## [<version>]" section of CHANGELOG.md (without its heading line) and fails when the
# section is missing or empty.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=${1-}
[ -n "$VERSION" ] || { echo "usage: release-notes.sh <version>" >&2; exit 1; }

notes=$(awk -v want="$VERSION" '
    /^## / { if (on) exit; on = (index($0, "## [" want "]") == 1); next }
    on { print }
' "$REPO/CHANGELOG.md")
if [ -z "$(printf '%s' "$notes" | tr -d '[:space:]')" ]; then
    echo "release-notes: no '## [$VERSION]' section in CHANGELOG.md" >&2
    exit 1
fi
printf '%s\n' "$notes" | sed '/./,$!d'
