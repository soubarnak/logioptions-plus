#!/usr/bin/env python3
"""Runner for manual hardware parity flows.

Walks a person through a flow step by step against a real mouse. Steps that
need the device marked `check` read it over hidraw (no writes) and compare.

    python3 dev/parity.py list
    python3 dev/parity.py pointer-speed [--percent 80]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from opl_agent import hidpp                     # noqa: E402
from opl_agent.profiles import dpi_from_value   # noqa: E402


def read_mouse():
    """-> (dpi, range) of the first HID++ device with AdjustableDPI."""
    for node in hidpp.HIDRAW.list_nodes():
        fd = hidpp.HIDRAW.open(node.path)
        if node.is_receiver:
            devices = hidpp.get_paired_devices(fd, bolt=getattr(node, "is_bolt", False))
            indexes = [d["index"] for d in devices]
        else:
            indexes = [0xFF]
        for idx in indexes:
            try:
                return hidpp.get_dpi(fd, idx)["dpi"], hidpp.get_dpi_list(fd, idx)
            except hidpp.HidppError:
                continue
    raise SystemExit("no mouse with AdjustableDPI found")


def pointer_speed(args):
    dpi, rng = read_mouse()
    want = dpi_from_value(args.percent / 100.0, rng)
    print("Mouse reports %d DPI (range %d..%d step %s)." % (dpi, rng["min"], rng["max"], rng["step"]))
    input("1. Start the agent and the UI. Open the POINT AND SCROLL tab. Enter to continue. ")
    input("2. Move the pointer speed slider to %d percent. Enter to continue. " % args.percent)
    dpi, _ = read_mouse()
    print("   expected %d DPI, hardware read-back %d: %s" % (want, dpi, "PASS" if dpi == want else "FAIL"))
    input("3. Quit and relaunch the UI (and the agent). Enter to continue. ")
    dpi2, _ = read_mouse()
    print("   hardware still %d DPI: %s" % (dpi2, "PASS" if dpi2 == want else "FAIL"))
    print("4. Confirm by eye the slider shows %d percent." % args.percent)


def read_thumb():
    """-> {mode, invert, smooth} of the first HID++ device with a 0x2150 thumb wheel."""
    for node in hidpp.HIDRAW.list_nodes():
        fd = hidpp.HIDRAW.open(node.path)
        if node.is_receiver:
            devices = hidpp.get_paired_devices(fd, bolt=getattr(node, "is_bolt", False))
            indexes = [d["index"] for d in devices]
        else:
            indexes = [0xFF]
        for idx in indexes:
            try:
                return hidpp.get_thumbwheel(fd, idx)
            except hidpp.HidppError:
                continue
    raise SystemExit("no mouse with a thumb wheel (0x2150) found")


def thumb_wheel(args):
    print("Mouse thumb wheel state: %s" % read_thumb())
    input("1. Start the agent and the UI. Open POINT AND SCROLL, thumb wheel panel. Enter. ")
    input("2. Switch the direction to inverted (turn smooth on too). Enter. ")
    st = read_thumb()
    print("   hardware read-back %s: %s" % (st, "PASS" if st["invert"] and st["smooth"] else "FAIL"))
    input("3. In a browser, scroll sideways with the thumb wheel: it must go the other way. Enter. ")
    input("4. Quit and relaunch the UI and agent. Enter. ")
    st = read_thumb()
    print("   still inverted after relaunch: %s" % ("PASS" if st["invert"] else "FAIL"))
    print("5. Confirm by eye the panel shows inverted, and the speed control is disabled.")


FLOWS = {"pointer-speed": pointer_speed, "thumb-wheel": thumb_wheel}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("flow", choices=sorted(FLOWS) + ["list"])
    ap.add_argument("--percent", type=int, default=80)
    args = ap.parse_args()
    if args.flow == "list":
        print("\n".join(sorted(FLOWS)))
    else:
        FLOWS[args.flow](args)


if __name__ == "__main__":
    main()
