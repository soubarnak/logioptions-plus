#!/usr/bin/env python3
"""Check that every pinned depot URL answers 200 unauthenticated with the pinned size.

Sends a plain HEAD request: no credentials, cookies or access-group headers. Run it before
every release that changes optionsplus-linux/data/depots.json; a gated depot (401/403)
must be removed from the manifest. It downloads nothing.

usage: verify-depot-urls.py [depots.json]   exit 0 only if every depot passes
"""
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "optionsplus-linux"))
from opl_agent import fetch  # noqa: E402

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "optionsplus-linux", "data", "depots.json")


def check(depot, timeout=30):
    """Return None when the depot passes, else a reason string."""
    try:
        fetch.check_url(depot["url"])
        req = urllib.request.Request(depot["url"], method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return "HTTP %d" % r.status
            length = r.headers.get("Content-Length")
    except urllib.error.HTTPError as e:
        return "HTTP %d%s" % (e.code, " (gated: remove from manifest)" if e.code in (401, 403) else "")
    except (fetch.FetchError, OSError) as e:
        return str(e)
    if length is None or int(length) != int(depot["size"]):
        return "size %s does not match pinned %s" % (length, depot["size"])
    return None


def main(argv):
    manifest = fetch.load_manifest(argv[1] if len(argv) > 1 else DEFAULT)
    bad = 0
    for d in manifest["depots"]:
        why = check(d)
        print("%-4s %s%s" % ("OK" if why is None else "FAIL", d["name"], "" if why is None else ": " + why))
        bad += why is not None
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
