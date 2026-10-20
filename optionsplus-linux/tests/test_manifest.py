"""Seams: the shipped depot manifest and tools/verify-depot-urls.py (against a local server)."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from opl_agent import fetch
from tests.fakes import payloadkit as kit

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "depots.json"
VERIFY = ROOT.parent / "tools" / "verify-depot-urls.py"


def test_shipped_manifest_is_well_formed_and_pins_https_urls():
    m = fetch.load_manifest(str(MANIFEST))
    assert m["depots"][0]["name"] == "logioptionsplus"
    names = [d["name"] for d in m["depots"]]
    assert len(names) == len(set(names))
    for d in m["depots"]:
        assert d["url"].startswith("https://")
        assert re.fullmatch(r"[0-9a-f]{64}", d["sha256"])
        assert int(d["size"]) > 0
    assert "logitech" in m["eulaUrl"]


def run_verify(manifest, tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(manifest))
    env = dict(os.environ, no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
    return subprocess.run([sys.executable, "-I", str(VERIFY), str(p)], env=env,
                          capture_output=True, text=True, timeout=30)


def test_verify_passes_when_every_url_answers_with_the_pinned_size(tmp_path):
    srv, manifest, _ = kit.make_world()
    try:
        r = run_verify(manifest, tmp_path)
    finally:
        srv.close()
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("OK") == 2


def test_verify_sends_no_credentials(tmp_path):
    seen = []
    srv, manifest, _ = kit.make_world()
    orig = srv.httpd.RequestHandlerClass.do_GET

    def spy(self):
        seen.append({k.lower() for k in self.headers.keys()})
        return orig(self)
    srv.httpd.RequestHandlerClass.do_HEAD = spy
    try:
        run_verify(manifest, tmp_path)
    finally:
        srv.close()
    assert seen and not any({"authorization", "cookie"} & h for h in seen)


def test_verify_fails_for_a_gated_depot(tmp_path):
    srv, manifest, _ = kit.make_world()
    manifest["depots"][1]["url"] = srv.base + "/depots/gated.depot"      # server answers 404
    try:
        r = run_verify(manifest, tmp_path)
    finally:
        srv.close()
    assert r.returncode == 1
    assert "FAIL test_mouse" in r.stdout


def test_verify_fails_on_size_drift(tmp_path):
    srv, manifest, _ = kit.make_world()
    manifest["depots"][0]["size"] += 1
    try:
        r = run_verify(manifest, tmp_path)
    finally:
        srv.close()
    assert r.returncode == 1
    assert "size" in r.stdout
