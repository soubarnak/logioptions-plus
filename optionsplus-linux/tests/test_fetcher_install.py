"""Seam: fetcher CLI, verification, atomic install, recorded versions, offline installer."""
import io
import json
import struct
import zipfile

import pytest

from opl_agent import fetch as fetchmod

from tests.fakes import payloadkit as kit
from tests.test_fetcher import fetch, tree, world  # noqa: F401  (world is a fixture)


def test_checksum_mismatch_aborts_before_unpacking_and_leaves_nothing(world):
    good = world.srv.routes["/depots/test_mouse.depot"]
    world.srv.routes["/depots/test_mouse.depot"] = b"tampered" + good[8:]     # same length: size check passes
    r = fetch(world, "--accept-eula")
    assert r.returncode != 0
    assert "checksum" in r.stderr
    data = world.tmp / "data" / "optionsplus-linux"
    assert not world.payload.exists()
    assert [p for p in data.rglob("*") if p.is_file()] == []


def test_failed_update_keeps_the_previous_payload_in_use(world):
    assert fetch(world, "--accept-eula").returncode == 0
    before = tree(world.payload)
    world.manifest["dataVersion"] = "9.9.9"
    world.mpath.write_text(json.dumps(world.manifest))
    world.srv.routes["/depots/logioptionsplus.depot"] = b"bad"
    r = fetch(world, "--accept-eula", "--force")
    assert r.returncode != 0
    assert tree(world.payload) == before
    assert json.loads((world.payload / "payload.json").read_text())["dataVersion"] == "9.9.1"


def test_records_installed_data_versions(world):
    fetch(world, "--accept-eula")
    rec = json.loads((world.payload / "payload.json").read_text())
    assert rec["dataVersion"] == "9.9.1"
    assert rec["source"] == "download"
    assert rec["depots"]["logioptionsplus"]["sha256"] == world.manifest["depots"][0]["sha256"]
    assert set(rec["depots"]) == {"logioptionsplus", "test_mouse"}


def test_second_run_is_a_no_op_when_up_to_date(world):
    fetch(world, "--accept-eula")
    hits = len(world.srv.hits)
    r = fetch(world, "--accept-eula")
    assert r.returncode == 0
    assert len(world.srv.hits) == hits


def test_manifest_with_newer_data_version_triggers_a_refetch(world):
    fetch(world, "--accept-eula")
    hits = len(world.srv.hits)
    world.manifest["dataVersion"] = "9.9.3"
    world.mpath.write_text(json.dumps(world.manifest))
    assert fetch(world, "--accept-eula").returncode == 0
    assert len(world.srv.hits) > hits
    assert json.loads((world.payload / "payload.json").read_text())["dataVersion"] == "9.9.3"


def test_depot_with_path_traversal_is_rejected(world):
    evil = kit.depot_bytes({"../../escape.txt": b"x", "resources/app.asar": b"a"})
    world.srv.routes["/depots/logioptionsplus.depot"] = evil
    world.manifest["depots"][0].update(size=len(evil), sha256=kit.sha(evil))
    world.mpath.write_text(json.dumps(world.manifest))
    r = fetch(world, "--accept-eula")
    assert r.returncode != 0
    assert not (world.tmp / "data" / "escape.txt").exists()
    assert not world.payload.exists()


def test_plain_http_to_a_remote_host_is_refused(world):
    world.manifest["depots"][0]["url"] = "http://example.invalid/x.depot"
    world.mpath.write_text(json.dumps(world.manifest))
    r = fetch(world, "--accept-eula")
    assert r.returncode != 0
    assert not world.payload.exists()


# --- offline installer fallback ------------------------------------------

def test_installer_fallback_produces_the_same_layout_without_network(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots)
    world.srv.close()
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode == 0, r.stderr
    p = world.payload
    assert (p / "app.asar").read_bytes() == b"fake-asar"
    assert (p / "depots" / "logioptionsplus" / "data" / "strings" / "en-US.yaml").read_bytes() == b"HELLO: Hello\n"
    assert (p / "depots" / "test_mouse" / "thumb.png").read_bytes() == b"fake-png"
    rec = json.loads((p / "payload.json").read_text())
    assert rec["source"] == "installer"
    assert rec["dataVersion"] == "9.9.2"
    assert set(rec["depots"]) == {"logioptionsplus", "test_mouse"}


def test_installer_fallback_gives_same_tree_as_download(world, tmp_path):
    fetch(world, "--accept-eula")
    downloaded = tree(world.payload)
    import shutil
    shutil.rmtree(world.tmp / "data")
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots)
    fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert tree(world.payload) == downloaded


def test_installer_fallback_rejects_corrupt_depot_and_installs_nothing(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots)
    raw = bytearray(exe.read_bytes())
    raw[raw.index(b"fake-asar")] ^= 0xFF          # corrupt a stored depot byte
    exe.write_bytes(bytes(raw))
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode != 0
    assert not world.payload.exists()


def test_installer_fallback_still_asks_for_confirmation(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots)
    r = fetch(world, "--from-installer", str(exe), stdin="n\n")
    assert r.returncode != 0
    assert not (world.tmp / "data").exists()


def test_installer_missing_a_depot_fails_cleanly(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, {"logioptionsplus": world.depots["logioptionsplus"]})
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode != 0
    assert "test_mouse" in r.stderr
    assert not world.payload.exists()


def test_installer_with_a_signature_trailer_after_the_zip_installs(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots, trailer=b"\x30\x82" * 51200)     # 100 KiB
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode == 0, r.stderr
    assert (world.payload / "app.asar").read_bytes() == b"fake-asar"
    assert (world.payload / "depots" / "test_mouse" / "thumb.png").exists()


def test_installer_without_a_zip_directory_is_rejected_with_the_usual_message(world):
    exe = world.tmp / "junk.exe"
    exe.write_bytes(b"MZ" + b"\0" * 5000)
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode != 0
    assert "not an Options+ offline installer" in r.stderr
    assert not world.payload.exists()


def test_installer_depot_is_checked_against_the_pinned_sha256_too(world):
    exe = world.tmp / "installer_offline.exe"
    other = b"another depot" + world.depots["test_mouse"]
    # the installer's own mac matches the bytes it carries, the manifest pin does not
    kit.offline_installer(exe, dict(world.depots, test_mouse=other))
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode != 0
    assert "checksum mismatch for test_mouse; nothing was installed" in r.stderr
    assert not world.payload.exists()


def fake_eocd(**kw):
    f = dict(disk=0, cd_disk=0, n_disk=1, n_total=1, cd_size=46, cd_off=0, clen=0)
    f.update(kw)
    return struct.pack("<4s4H2LH", b"PK\x05\x06", f["disk"], f["cd_disk"], f["n_disk"], f["n_total"],
                       f["cd_size"], f["cd_off"], f["clen"])


def test_installer_trailer_holding_a_fake_end_record_is_skipped(world):
    exe = world.tmp / "installer_offline.exe"
    # looks valid by offsets alone, but no central directory entry sits where it points
    kit.offline_installer(exe, world.depots, trailer=b"junk" + fake_eocd(cd_size=40, cd_off=0) + b"more")
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode == 0, r.stderr


def test_installer_trailer_holding_a_multi_disk_end_record_is_skipped(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots, trailer=fake_eocd(n_disk=0, cd_size=0, cd_off=0))
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode == 0, r.stderr


# the 22-byte record (T = chunk - 10) or just its signature (T = chunk - 20) crosses a 1 MiB read boundary
@pytest.mark.parametrize("trailer_len", [(1 << 20) - 10, (1 << 20) - 20, (1 << 20) - 2, (1 << 20) + 5])
def test_installer_end_record_across_a_scan_chunk_boundary_is_found(world, trailer_len):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots, trailer=b"\0" * trailer_len)
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode == 0, r.stderr


def test_installer_archive_with_a_comment_is_found(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots, comment=b"signed by nobody", trailer=b"\x30\x82" * 1000)
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode == 0, r.stderr


def test_installer_wrong_mac_fails_even_when_the_pin_is_right(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots, depot_macs={"test_mouse": "0" * 64})
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode != 0
    assert "checksum mismatch for test_mouse; nothing was installed" in r.stderr
    assert not world.payload.exists()


def zip_bytes():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("offline.json", "{}")
    return b.getvalue()


def test_find_zip_end_gives_up_beyond_the_scan_limit(monkeypatch):
    data = zip_bytes() + b"\0" * 5000
    assert fetchmod.find_zip_end(io.BytesIO(data), len(data)) == len(zip_bytes())
    monkeypatch.setattr(fetchmod, "SCAN_LIMIT", 1000)
    assert fetchmod.find_zip_end(io.BytesIO(data), len(data)) is None


def test_zip64_installer_gets_a_clear_error(world):
    exe = world.tmp / "big.exe"
    exe.write_bytes(b"MZ" + zip_bytes()[:-22] + fake_eocd(n_total=0xFFFF, n_disk=0xFFFF, cd_size=0xFFFFFFFF,
                                                         cd_off=0xFFFFFFFF))
    r = fetch(world, "--accept-eula", "--from-installer", str(exe))
    assert r.returncode != 0
    assert "ZIP64" in r.stderr
    assert not world.payload.exists()


def test_declining_with_an_installer_says_nothing_was_installed(world):
    exe = world.tmp / "installer_offline.exe"
    kit.offline_installer(exe, world.depots)
    r = fetch(world, "--from-installer", str(exe), stdin="n\n")
    assert "Nothing was installed" in r.stderr and "downloaded" not in r.stderr.split("Declined")[1]
