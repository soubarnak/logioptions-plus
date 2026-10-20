#!/usr/bin/env python3
"""Compose /rac from a real depots directory and print per-slot card counts (dev only).

    python3 -I dev/rac_dump.py DEPOTS_DIR [SLOT_PREFIX]

DEPOTS_DIR holds logioptionsplus/ (and optionally the device depots). Nothing is written.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from opl_agent import actions, resources  # noqa: E402


def main(argv):
    if not argv:
        sys.exit(__doc__)
    resources.configure(depot_dir=argv[0])
    payload = {"slotPrefix": argv[1]} if len(argv) > 1 else None
    box = actions.rac(payload)
    print("prefixes:", [argv[1]] if payload else actions._installed_prefixes())
    for key in ("availableCards", "recommendedCards"):
        racs = box[key]["racList"]
        print("%s: %d apps" % (key, len(racs)))
        for rac in racs:
            counts = ", ".join("%s=%d" % (s["slotId"], len(s.get("desktopCards", s.get("cards", ()))))
                               for s in rac["slotCardList"])
            print("  %-45s %s" % (rac["appId"], counts))


main(sys.argv[1:])
