#!/usr/bin/env python3
"""Print the mouse's own settings, read from the hardware (no agent involved).

    readback.py [--node /dev/hidrawN] [--index N] [--save FILE | --restore FILE]

Prints DPI, SmartShift, wheel, thumb wheel, the reporting state of every control and the
Easy-Switch host names. `--save FILE` also writes the values as JSON; `--restore FILE` writes
them back (DPI, SmartShift mode and threshold, wheel hi-res and invert, thumb invert and
smooth, then every control undiverted and unmapped) and prints the result.
Run it with the agent stopped or not: it only talks HID++ over the hidraw node.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from opl_agent import hidpp  # noqa: E402


def find_device(node=None, index=None):
    """-> (open reader, device index) for the first mouse found, or the one named."""
    for n in hidpp.list_receivers_and_devices():
        if node and n.path != node:
            continue
        fd = hidpp.open_node(n.path, long_only=n.long_only)
        if not n.is_receiver:
            return fd, hidpp.DEV_RECEIVER
        for d in hidpp.get_paired_devices(fd, bolt=n.is_bolt):
            if index is None or d["index"] == index:
                return fd, d["index"]
        hidpp.close_node(fd)
    raise SystemExit("no device found")


def _optional(read, fd, idx):
    """What `read` returns, or None for a mouse without that feature."""
    try:
        return read(fd, idx)
    except hidpp.FeatureNotSupported:
        return None


def collect(fd, idx):
    """Every value this tool reads, as a JSON-able dict."""
    out = {"dpi": hidpp.get_dpi(fd, idx), "smartshift": hidpp.get_smartshift(fd, idx),
           "wheel": _optional(hidpp.get_wheel, fd, idx), "thumb": _optional(hidpp.get_thumbwheel, fd, idx),
           "controls": {}, "hosts": []}
    for cid in sorted(hidpp.get_controls(fd, idx)):
        div, raw, remap = hidpp.get_cid_reporting(fd, idx, cid)
        out["controls"][str(cid)] = {"diverted": div, "raw_xy": raw, "remap": remap}
    hosts = hidpp.get_hosts(fd, idx)
    out["hosts"] = [{"index": h["index"], "paired": h["paired"], "name": h["name"]}
                    for h in hosts["hosts"]]
    out["current_host"] = hosts["current"]
    return out


def restore(fd, idx, saved):
    """Write the saved values back. Hosts are never written."""
    hidpp.set_dpi(fd, idx, saved["dpi"]["dpi"])
    ss = saved["smartshift"]
    hidpp.set_smartshift(fd, idx, mode=ss["mode"], threshold=ss["threshold"])
    w = saved["wheel"]
    if w is not None:
        hidpp.set_wheel(fd, idx, hires=w["hires"], invert=w["invert"], target=False)
    t = saved["thumb"]
    if t is not None:
        hidpp.set_thumbwheel(fd, idx, t["invert"], t["smooth"], mode=hidpp.THUMB_NATIVE)
    for cid, state in saved["controls"].items():
        cid = int(cid)
        if state["remap"] != cid:
            hidpp.set_cid_remap(fd, idx, cid, cid)
        if state["diverted"] or state["raw_xy"]:
            hidpp.set_cid_diverted(fd, idx, cid, False, raw_xy=False)


def show(state):
    d = state["dpi"]
    print("dpi        %s (default %s)" % (d["dpi"], d["default"]))
    s = state["smartshift"]
    print("smartshift %s threshold %s" % (s["mode"], s["threshold"]))
    w = state["wheel"]
    if w is not None:
        print("wheel      hires %s invert %s diverted %s" % (w["hires"], w["invert"], w["target"]))
    t = state["thumb"]
    if t is not None:
        print("thumb      mode %s invert %s smooth %s" % (t["mode"], t["invert"], t["smooth"]))
    for cid, c in state["controls"].items():
        print("ctl %-4s   %s" % (cid, (c["diverted"], c["raw_xy"], c["remap"])))
    for h in state["hosts"]:
        print("host %d     paired %s %r%s" % (h["index"], h["paired"], h["name"],
                                              "  (current)" if h["index"] == state["current_host"] else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--node")
    ap.add_argument("--index", type=int)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--save", metavar="FILE")
    g.add_argument("--restore", metavar="FILE")
    args = ap.parse_args(argv)
    fd, idx = find_device(args.node, args.index)
    try:
        if args.restore:
            with open(args.restore) as f:
                restore(fd, idx, json.load(f))
        state = collect(fd, idx)
        show(state)
        if args.save:
            with open(args.save, "w") as f:
                json.dump(state, f, indent=2)
    finally:
        hidpp.close_node(fd)


if __name__ == "__main__":
    main()
