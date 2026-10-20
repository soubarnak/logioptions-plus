#!/usr/bin/env python3
"""Dev-only: make a Wine prefix look like a finished offline install of Options+.

Unpacks every plain (10 01 17 20) depot from the offline installer into
C:\\ProgramData\\LogiOptionsPlus\\depots\\<name>\\ and writes current.json/next.json
(logi.protocol.updates.Depository in proto-JSON) marking them PRESENT, so the real
updater service answers /updates/depot/info for the capture run. Encrypted depots
(06 05 21 20, unreleased products) are listed as LOCKED.

usage: stage_wine_depots.py <depots-dir-with-offline.json> <wineprefix>
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
from undepot import unpack  # noqa: E402

PLAIN = bytes.fromhex("10011720")


def depot_file(base, url):
    p = os.path.join(base, url.split("/")[-1])
    return p if os.path.exists(p) else os.path.join(base, url.split("/")[2] + ".depot")


def file_list(path):
    d = open(path, "rb").read(8)
    n = struct.unpack_from("<I", d, 4)[0]
    with open(path, "rb") as fh:
        fh.seek(8)
        return [f["name"] for f in json.loads(fh.read(n))["files"]]


def main():
    base, prefix = sys.argv[1], sys.argv[2]
    off = json.load(open(os.path.join(base, "offline.json")))
    pd = os.path.join(prefix, "drive_c", "ProgramData", "LogiOptionsPlus")
    depots = []
    for x in off["depots"]:
        src = depot_file(base, x["url"])
        entry = {k: x[k] for k in ("name", "url", "mac", "size", "signatures") if k in x}
        entry["dependsOn"] = x.get("dependsOn", [])
        entry["required"] = x.get("required", False)
        with open(src, "rb") as fh:
            magic = fh.read(4)
        if x["name"] == "logioptionsplus":
            entry.update(state="PRESENT", localFolder="C:\\Program Files\\LogiOptionsPlus", files=file_list(src))
        elif magic == PLAIN:
            out = os.path.join(pd, "depots", x["name"])
            if not os.path.isdir(out):
                unpack(src, out)
            entry.update(state="PRESENT", localFolder="C:\\ProgramData\\LogiOptionsPlus\\depots\\" + x["name"],
                         files=file_list(src))
        else:
            entry.update(state="LOCKED")
        depots.append(entry)
    repo = {"version": off["version"], "buildId": str(off["buildId"]), "branch": off["branch"],
            "accessGroup": off["keys"]["accessGroup"], "region": "US", "depots": depots,
            "lockdownName": "", "appId": off["appId"]}
    for name in ("current.json", "next.json"):
        json.dump(repo, open(os.path.join(pd, name), "w"), indent=1)
    json.dump(off["keys"], open(os.path.join(pd, "keys.json"), "w"), indent=1)
    print(sum(d["state"] == "PRESENT" for d in depots), "present,", sum(d["state"] == "LOCKED" for d in depots), "locked")


if __name__ == "__main__":
    main()
