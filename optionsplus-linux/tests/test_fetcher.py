"""Seam: the `optionsplus-fetch-payload` CLI, run as a process against a local HTTP server
serving a synthetic depot built by the test."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fakes import payloadkit as kit

ROOT = Path(__file__).resolve().parents[1]
FETCH = ROOT / "bin" / "optionsplus-fetch-payload"


class World:
    pass


@pytest.fixture
def world(tmp_path):
    srv, manifest, depots = kit.make_world()
    w = World()
    w.srv, w.manifest, w.depots, w.tmp = srv, manifest, depots, tmp_path
    pybin = tmp_path / "pybin"      # only python3 and env: host zenity/pkexec/sudo must stay invisible
    pybin.mkdir()
    (pybin / "python3").symlink_to(sys.executable)
    (pybin / "env").symlink_to(shutil.which("env"))
    w.mpath = tmp_path / "depots.json"
    w.mpath.write_text(json.dumps(manifest))
    w.env = dict(os.environ, HOME=str(tmp_path / "home"), XDG_DATA_HOME=str(tmp_path / "data"),
                 OPL_LIB_DIR=str(ROOT), OPL_MANIFEST=str(w.mpath), no_proxy="127.0.0.1",
                 NO_PROXY="127.0.0.1", OPL_SYSTEM_PAYLOAD_DIR=str(tmp_path / "system"),
                 PATH=str(pybin))
    for k in ("DISPLAY", "WAYLAND_DISPLAY", "OPL_PAYLOAD_DIR", "OPL_ASAR", "OPL_DATA_DIR", "OPL_DEPOT_DIR"):
        w.env.pop(k, None)
    w.payload = tmp_path / "data" / "optionsplus-linux" / "payload"
    yield w
    srv.close()


def fetch(w, *args, stdin="", env=None):
    return subprocess.run([str(FETCH), *args], input=stdin, env=env or w.env,
                          capture_output=True, text=True, timeout=60)


def tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def test_declining_the_eula_downloads_nothing_and_writes_nothing(world):
    r = fetch(world, stdin="n\n")
    assert r.returncode != 0
    assert world.srv.hits == []
    assert not (world.tmp / "data").exists()
    assert "https://example.invalid/eula" in r.stdout + r.stderr


def test_prompt_shows_size_and_eula_link(world):
    r = fetch(world, stdin="n\n")
    total = sum(d["size"] for d in world.manifest["depots"])
    assert "%d bytes" % total in r.stdout + r.stderr
    assert "Logitech" in r.stdout + r.stderr


def test_no_answer_counts_as_decline(world):
    r = fetch(world, stdin="")
    assert r.returncode != 0
    assert world.srv.hits == []


def test_confirmed_run_unpacks_both_depot_formats(world):
    r = fetch(world, stdin="y\n")
    assert r.returncode == 0, r.stderr
    p = world.payload
    assert (p / "app.asar").read_bytes() == b"fake-asar"
    assert (p / "depots" / "logioptionsplus" / "data" / "strings" / "en-US.yaml").read_bytes() == b"HELLO: Hello\n"
    assert (p / "depots" / "test_mouse" / "thumb.png").read_bytes() == b"fake-png"
    assert (p / "depots" / "test_mouse" / "sub" / "layout.json").read_bytes() == b"{}"
