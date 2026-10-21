"""Seam: the restricted local template srcpkgs/optionsplus-linux-payload against the pinned
depots.json, the fetcher's payload layout and the public package's allow-list. No network."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from opl_agent import fetch as fetchmod

from tests.fakes import payloadkit as kit

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
TEMPLATE = REPO / "srcpkgs" / "optionsplus-linux-payload" / "template"
PUBLIC = REPO / "srcpkgs" / "optionsplus-linux" / "template"
DEPOTS = json.loads((ROOT / "data" / "depots.json").read_text())["depots"]
DATA_VERSION = json.loads((ROOT / "data" / "depots.json").read_text())["dataVersion"]
CHECK = REPO / "tools" / "check-package-contents.py"


def var(text, name):
    m = re.search(r'^%s="([^"]*)"' % name, text, re.M)
    assert m, name
    return m.group(1)


def test_template_is_restricted_and_never_the_public_package():
    t = TEMPLATE.read_text()
    assert re.search(r"^pkgname=optionsplus-linux-payload$", t, re.M)
    assert re.search(r"^restricted=yes$", t, re.M)
    assert "XBPS_ALLOW_RESTRICTED=yes" in t
    assert "depends=\"optionsplus-linux" in t
    assert not re.search(r"^restricted=", PUBLIC.read_text(), re.M)


def test_template_version_is_the_pinned_data_version():
    assert re.search(r"^version=%s$" % re.escape(DATA_VERSION), TEMPLATE.read_text(), re.M)


def test_template_distfiles_and_checksums_match_depots_json():
    t = TEMPLATE.read_text()
    urls, sums = var(t, "distfiles").split(), var(t, "checksum").split()
    assert urls == [d["url"] for d in DEPOTS]
    assert sums == [d["sha256"] for d in DEPOTS]
    assert all(re.fullmatch(r"[0-9a-f]{64}", s) for s in sums)
    names = [d["name"] + ".depot" for d in DEPOTS]
    assert [u.rsplit("/", 1)[1] for u in urls] == names           # do_install derives names from urls
    assert sorted(var(t, "skip_extraction").split()) == sorted(names)
    assert all(d["size"] > 0 for d in DEPOTS)                       # sizes live only in depots.json


def test_public_package_and_release_never_touch_the_payload_template():
    for p in (REPO / "tools" / "stage.sh", REPO / "tools" / "build-xbps.sh",
              REPO / ".github" / "workflows" / "release.yml", PUBLIC):
        assert "optionsplus-linux-payload" not in p.read_text(), p
        assert "updates.optionsplus.logitechg.com" not in p.read_text(), p


def test_payload_layout_is_rejected_by_the_public_allow_list(tmp_path):
    lib = tmp_path / "usr/lib/optionsplus-linux/logitech"
    (lib / "depots/x").mkdir(parents=True)
    (lib / "app.asar").write_text("x")
    (lib / "payload.json").write_text("{}")
    r = subprocess.run([sys.executable, str(CHECK), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode != 0
    assert "app.asar" in r.stderr


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


def test_do_install_lays_out_the_same_payload_as_the_fetcher(tmp_path):
    main = kit.depot_bytes({"resources/app.asar": b"ASAR", "resources/x/y.txt": b"y", "dir/": b""})
    others = {d["name"]: kit.depot_bytes({"data/%s.json" % d["name"]: b"{}"}, magic=kit.DEVICE_MAGIC)
              for d in DEPOTS[1:]}
    blobs = {DEPOTS[0]["name"]: main, **others}
    distdir = tmp_path / "dist" / ("optionsplus-linux-payload-" + DATA_VERSION)
    distdir.mkdir(parents=True)
    files = {}
    for name, data in blobs.items():
        (distdir / (name + ".depot")).write_bytes(data)
        files[name] = str(distdir / (name + ".depot"))

    dest = tmp_path / "destdir"
    script = ('pkgname=optionsplus-linux-payload; XBPS_SRCDISTDIR=%s; DESTDIR=%s; . %s; do_install'
              % (tmp_path / "dist", dest, TEMPLATE))
    r = subprocess.run(["bash", "-eu", "-c", script], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = dest / "usr/lib/optionsplus-linux/logitech"

    expect = tmp_path / "expect"
    expect.mkdir()
    manifest = {"dataVersion": DATA_VERSION, "depots": [
        {"name": d["name"], "url": d["url"], "size": len(blobs[d["name"]]),
         "sha256": kit.sha(blobs[d["name"]])} for d in DEPOTS]}
    fetchmod.assemble(str(expect), files, fetchmod.make_record(manifest, "package"))

    a, b = tree(got), tree(expect)
    rec_a, rec_b = json.loads(a.pop("payload.json")), json.loads(b.pop("payload.json"))
    assert a == b
    assert rec_a.pop("installedAt") and rec_b.pop("installedAt")
    assert rec_a == rec_b
    # the fetcher treats the packaged payload as current, so it never re-downloads over it
    assert fetchmod.up_to_date(manifest, str(got))
    assert (got / "app.asar").stat().st_mode & 0o777 == 0o644
    assert (got / "depots").stat().st_mode & 0o777 == 0o755
