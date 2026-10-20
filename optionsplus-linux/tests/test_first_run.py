"""Seam: first launch. The launcher, its fetcher and the agent's payload defaults, together."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from opl_agent import paths
from tests.fakes import payloadkit as kit
from tests.test_launcher import ROOT, env, launch, make_exe, starts  # noqa: F401  (env is a fixture)

FETCHER = ROOT / "bin" / "optionsplus-fetch-payload"
FAKE_FETCH = """#!{py}
import os, sys
open(os.environ["FAKE_FETCH_LOG"], "a").write("fetch\\n")
if os.environ.get("FAKE_FETCH_PAYLOAD"):
    d = os.environ["FAKE_FETCH_PAYLOAD"]
    os.makedirs(d)
    open(d + "/app.asar", "w").write("x")
    open(d + "/payload.json", "w").write("{{}}")
sys.exit(int(os.environ.get("FAKE_FETCH_CODE", "0")))
"""


def payload_of(env):
    return Path(env["XDG_DATA_HOME"]) / "optionsplus-linux" / "payload"


@pytest.fixture
def bare(env, tmp_path):
    """Environment with no payload at all and a fetcher that logs and exits as told."""
    shutil.rmtree(payload_of(env))
    fake = tmp_path / "fake-fetch"
    make_exe(fake, FAKE_FETCH)
    env.update(OPL_FETCH_CMD=str(fake), FAKE_FETCH_LOG=str(tmp_path / "fetch.log"))
    return env


def fetched(env):
    p = Path(env["FAKE_FETCH_LOG"])
    return p.read_text().count("fetch") if p.exists() else 0


def test_missing_payload_runs_the_fetcher_before_anything_starts(bare):
    p = payload_of(bare)
    bare["FAKE_FETCH_PAYLOAD"] = str(p)
    r, out = launch(bare)
    assert r.returncode == 0, r.stderr
    assert fetched(bare) == 1
    assert out["env"]["OPL_ASAR"] == str(p / "app.asar")


def test_declining_the_eula_starts_neither_agent_nor_ui(bare):
    bare["FAKE_FETCH_CODE"] = "2"
    r, out = launch(bare)
    assert r.returncode == 2
    assert out is None
    assert starts(bare) == 0


def test_installed_payload_does_not_run_the_fetcher(env, tmp_path):
    fake = tmp_path / "fake-fetch"
    make_exe(fake, FAKE_FETCH)
    env.update(OPL_FETCH_CMD=str(fake), FAKE_FETCH_LOG=str(tmp_path / "fetch.log"))
    launch(env)
    assert fetched(env) == 0


def test_user_supplied_asar_skips_the_fetcher(bare):
    bare["OPL_ASAR"] = "/elsewhere/app.asar"
    launch(bare)
    assert fetched(bare) == 0


def test_system_wide_payload_is_the_fallback_when_user_has_none(bare, tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    (system / "app.asar").write_text("x")
    (system / "payload.json").write_text("{}")
    _, out = launch(bare)
    assert fetched(bare) == 0
    assert out["env"]["OPL_ASAR"] == str(system / "app.asar")
    assert out["env"]["OPL_DEPOT_DIR"] == str(system / "depots")


def test_user_payload_wins_over_system_wide(env, tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    (system / "app.asar").write_text("x")
    (system / "payload.json").write_text("{}")
    _, out = launch(env)
    assert out["env"]["OPL_ASAR"] == str(payload_of(env) / "app.asar")


def test_real_fetch_then_launch_needs_no_opl_variables(env, tmp_path):
    """Fresh user, menu launch, confirm, download, UI started on the downloaded payload."""
    shutil.rmtree(payload_of(env))
    srv, manifest, _ = kit.make_world()
    try:
        mpath = tmp_path / "depots.json"
        mpath.write_text(json.dumps(manifest))
        pybin = tmp_path / "pybin"
        pybin.mkdir()
        (pybin / "python3").symlink_to(sys.executable)
        env.update(OPL_FETCH_CMD=str(FETCHER), OPL_MANIFEST=str(mpath), no_proxy="127.0.0.1",
                   NO_PROXY="127.0.0.1", PATH="/bin:/usr/bin:" + str(pybin))
        for var in ("DISPLAY", "WAYLAND_DISPLAY"):
            env.pop(var, None)
        import subprocess
        r = subprocess.run([str(ROOT / "bin" / "optionsplus-linux")], env=env, input="y\n",
                           capture_output=True, text=True, timeout=60)
    finally:
        srv.close()
    assert r.returncode == 0, r.stderr
    out = json.loads(Path(env["FAKE_OUT"]).read_text())
    assert not any(k in os.environ for k in ("OPL_ASAR", "OPL_DATA_DIR", "OPL_DEPOT_DIR", "OPL_PAYLOAD_DIR"))
    asar = Path(out["env"]["OPL_ASAR"])
    assert asar.read_bytes() == b"fake-asar"
    assert (Path(out["env"]["OPL_DATA_DIR"]) / "strings" / "en-US.yaml").exists()
    assert (Path(out["env"]["OPL_DEPOT_DIR"]) / "test_mouse" / "thumb.png").exists()
    assert starts(env) == 1


# --- the agent and update check read the same place ----------------------

def test_payload_dir_prefers_user_then_system_then_user_default(tmp_path):
    e = {"XDG_DATA_HOME": str(tmp_path / "d"), "OPL_SYSTEM_PAYLOAD_DIR": str(tmp_path / "s")}
    user = paths.user_payload_dir(e)
    assert paths.payload_dir(e) == user                    # nothing installed: where a fetch writes
    system = tmp_path / "s"
    system.mkdir()
    (system / "app.asar").write_text("x")
    (system / "payload.json").write_text("{}")
    assert paths.payload_dir(e) == str(system)
    os.makedirs(user)
    open(os.path.join(user, "app.asar"), "w").write("x")
    open(os.path.join(user, "payload.json"), "w").write("{}")
    assert paths.payload_dir(e) == user
    assert paths.payload_dir(dict(e, OPL_PAYLOAD_DIR="/x")) == "/x"


def test_installed_versions_reads_what_the_fetcher_recorded(tmp_path):
    (tmp_path / "payload.json").write_text(json.dumps({"dataVersion": "1.2.3", "depots": {}}))
    assert paths.installed_versions(str(tmp_path))["dataVersion"] == "1.2.3"
    assert paths.installed_versions(str(tmp_path / "nope")) is None
    (tmp_path / "payload.json").write_text("{broken")
    assert paths.installed_versions(str(tmp_path)) is None
