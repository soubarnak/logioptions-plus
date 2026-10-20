#!/bin/sh
# Run the whole agent test suite: no hardware, network, uinput, display or Logitech data needed.
cd "$(dirname "$0")/.." && exec python3 -m pytest "$@"
