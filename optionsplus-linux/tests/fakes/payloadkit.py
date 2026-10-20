"""Builds synthetic Logitech-shaped depots, a pinned manifest and a local HTTP server.

Everything here is invented: no Logitech bytes, no network beyond 127.0.0.1.
"""
import hashlib
import http.server
import json
import struct
import threading
import zipfile

MAIN_MAGIC = bytes.fromhex("10011720")
DEVICE_MAGIC = bytes.fromhex("06052120")


def depot_bytes(files, magic=MAIN_MAGIC, extra_header=None):
    """Container: magic, u32 LE index length, JSON index, then (u32 size, bytes) per file."""
    index = {"files": [{"name": n, "mode": 420} for n in files]}
    index.update(extra_header or {})
    j = json.dumps(index).encode()
    out = magic + struct.pack("<I", len(j)) + j
    for data in files.values():
        out += struct.pack("<I", len(data)) + data
    return out


def sha(data):
    return hashlib.sha256(data).hexdigest()


MAIN_FILES = {
    "resources/app.asar": b"fake-asar",
    "data/strings/en-US.yaml": b"HELLO: Hello\n",
    "data/devices/devices_initial.json": b'{"devices": []}',
}
DEVICE_FILES = {"thumb.png": b"fake-png", "sub\\layout.json": b"{}"}


class Server:
    """Serves {path: bytes} on 127.0.0.1; records every request path."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.hits = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                body = outer.routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_HEAD = do_GET

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_world(corrupt=False):
    """Returns (server, manifest dict, depot byte strings) for a main and a device depot."""
    main = depot_bytes(MAIN_FILES)
    dev = depot_bytes(DEVICE_FILES, DEVICE_MAGIC, {"header-sha": "x"})
    served_main = (b"X" + main[1:]) if corrupt else main
    srv = Server({"/depots/logioptionsplus.depot": served_main,
                  "/depots/test_mouse.depot": dev})
    manifest = {
        "dataVersion": "9.9.1",
        "eulaUrl": "https://example.invalid/eula",
        "depots": [
            {"name": "logioptionsplus", "url": srv.base + "/depots/logioptionsplus.depot",
             "size": len(main), "sha256": sha(main)},
            {"name": "test_mouse", "url": srv.base + "/depots/test_mouse.depot",
             "size": len(dev), "sha256": sha(dev)},
        ]}
    return srv, manifest, {"logioptionsplus": main, "test_mouse": dev}


def offline_installer(path, depots, version="9.9.2", trailer=b"", depot_macs=None):
    """A fake offline installer: junk PE-like prefix, then a ZIP holding offline.json and depots."""
    entries = []
    with zipfile.ZipFile(path, "w") as z:
        for name, data in depots.items():
            fname = "%s.depot" % name
            z.writestr(fname, data)
            entries.append({"name": name, "url": "https://example.invalid/depots/uuid-%s/%s" % (name, fname),
                            "mac": (depot_macs or {}).get(name, sha(data)), "size": len(data)})
        z.writestr("offline.json", json.dumps({"version": version, "depots": entries}))
    raw = open(path, "rb").read()
    with open(path, "wb") as f:      # prefix shifts offsets like a PE overlay
        f.write(b"MZ" + b"\0" * 1000 + raw + trailer)   # trailer: Authenticode signature after the EOCD
