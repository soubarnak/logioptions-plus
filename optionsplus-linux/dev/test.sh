#!/bin/sh
# Run the whole test suite: agent (pytest) and bootstrap (node:test).
# No hardware, network, uinput, display or Logitech data needed.
cd "$(dirname "$0")/.." || exit 1
python3 -m pytest -q "$@" || exit 1
node --test bootstrap/*.test.js
