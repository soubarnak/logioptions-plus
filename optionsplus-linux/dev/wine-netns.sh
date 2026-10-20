#!/bin/sh
# Dev-only: run a command inside an unprivileged user+network namespace with only loopback,
# so the Wine-hosted Logitech agent can't reach update/DFU/telemetry servers.
# usage: wine-netns.sh <cmd...>
exec unshare -rn sh -c 'ip link set lo up && exec "$@"' sh "$@"
