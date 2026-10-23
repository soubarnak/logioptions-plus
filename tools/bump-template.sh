#!/bin/bash
# Usage: bump-template.sh <version> <sha256> [repo-root]
# Sets version, revision=1 and checksum in srcpkgs/optionsplus-linux/template and the package
# file name in README.md. Running it twice with the same arguments changes nothing.
# A version older than the template's current one is skipped (an older tag re-run).
set -euo pipefail

VERSION=${1:?version} SUM=${2:?sha256}
ROOT=${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}

[[ $VERSION =~ ^[0-9]+(\.[0-9]+)*$ ]] || { echo "bump-template: bad version '$VERSION'" >&2; exit 1; }
[[ $SUM =~ ^[0-9a-f]{64}$ ]] || { echo "bump-template: bad sha256 '$SUM'" >&2; exit 1; }

T=$ROOT/srcpkgs/optionsplus-linux/template
CURRENT=$(sed -n 's/^version=//p' "$T")
if [[ $VERSION != "$CURRENT" && $(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | head -n1) == "$VERSION" ]]; then
  echo "bump-template: $VERSION is older than template version $CURRENT; leaving it alone" >&2
  exit 0
fi
sed -i "s/^version=.*/version=$VERSION/; s/^revision=.*/revision=1/; s/^checksum=.*/checksum=$SUM/" "$T"
sed -i "s/optionsplus-linux-[0-9][0-9.]*_1\.noarch/optionsplus-linux-${VERSION}_1.noarch/g" "$ROOT/README.md"
