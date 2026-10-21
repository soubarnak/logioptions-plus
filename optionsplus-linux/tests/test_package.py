"""Seams: the build script, the contents check, and the install/remove hooks."""
import html
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
BUILD = REPO / "tools" / "build-xbps.sh"
CHECK = REPO / "tools" / "check-package-contents.py"
INSTALL = ROOT / "packaging" / "INSTALL"
REMOVE = ROOT / "packaging" / "REMOVE"


def version():
    m = re.search(r'__version__ = "([^"]+)"', (ROOT / "opl_agent" / "__init__.py").read_text())
    return m.group(1)


def check(path):
    return subprocess.run([sys.executable, str(CHECK), str(path)], capture_output=True, text=True)


def clean_tree(root):
    """A destdir that holds only allowed files."""
    files = [
        "usr/bin/optionsplus-linux", "usr/bin/optionsplus-agent", "usr/bin/optionsplus-fetch-payload",
        "usr/lib/optionsplus-linux/depots.json",
        "usr/lib/optionsplus-linux/bootstrap/links.js",
        "usr/lib/optionsplus-linux/bootstrap/guard.js",
        "usr/lib/optionsplus-linux/opl_agent/__init__.py",
        "usr/lib/optionsplus-linux/opl_agent/main.py",
        "usr/lib/optionsplus-linux/bootstrap/main.js",
        "usr/lib/optionsplus-linux/bootstrap/package.json",
        "usr/lib/udev/rules.d/70-optionsplus-linux.rules",
        "usr/share/polkit-1/actions/io.github.soubarnak.optionsplus-linux.policy",
        "etc/xdg/autostart/optionsplus-linux-agent.desktop",
        "usr/share/applications/optionsplus-linux.desktop",
        "usr/share/icons/hicolor/scalable/apps/optionsplus-linux.svg",
        "usr/share/licenses/optionsplus-linux/LICENSE",
    ]
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    return root


# --- contents check -------------------------------------------------------

def test_check_accepts_clean_tree(tmp_path):
    r = check(clean_tree(tmp_path))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", [
    "usr/lib/optionsplus-linux/app.asar",
    "usr/lib/optionsplus-linux/LogiOptionsPlus.exe",
    "usr/lib/optionsplus-linux/opl_agent/hid.dll",
    "usr/lib/optionsplus-linux/depots/mx_master_3s/core.depot",
    "usr/lib/optionsplus-linux/opl_agent/data.pdb",
])
def test_check_rejects_logitech_payload_patterns(tmp_path, name):
    clean_tree(tmp_path)
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    r = check(tmp_path)
    assert r.returncode != 0
    assert name in r.stderr


def test_check_rejects_file_outside_allow_list(tmp_path):
    clean_tree(tmp_path)
    (tmp_path / "usr/share/stray.txt").write_text("x")
    r = check(tmp_path)
    assert r.returncode != 0
    assert "usr/share/stray.txt" in r.stderr


def test_check_rejects_windows_binary_with_innocent_name(tmp_path):
    clean_tree(tmp_path)
    (tmp_path / "usr/lib/optionsplus-linux/opl_agent/helper.py").write_bytes(b"MZ\x90\x00" + b"\0" * 60)
    r = check(tmp_path)
    assert r.returncode != 0
    assert "helper.py" in r.stderr


def test_check_rejects_logitech_in_path(tmp_path):
    clean_tree(tmp_path)
    p = tmp_path / "usr/lib/optionsplus-linux/opl_agent/logitech_data.py"
    p.write_text("x")
    assert check(tmp_path).returncode != 0


# --- install / remove hooks ----------------------------------------------

@pytest.fixture
def fake_udevadm(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "udevadm.log"
    exe = bindir / "udevadm"
    exe.write_text('#!/bin/sh\necho "$@" >> "%s"\n' % log)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]))
    return env, log


@pytest.mark.parametrize("script", [INSTALL, REMOVE])
def test_hooks_reload_rules_and_trigger_hidraw(script, fake_udevadm):
    env, log = fake_udevadm
    r = subprocess.run(["sh", str(script), "post", "optionsplus-linux", "0.1.0_1", "yes", "x86_64"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = log.read_text().splitlines()
    assert any(l.startswith("control") and "--reload" in l for l in lines)
    assert any(l.startswith("trigger") and "hidraw" in l for l in lines)


@pytest.mark.parametrize("script", [INSTALL, REMOVE])
def test_hooks_do_nothing_in_pre_phase(script, fake_udevadm):
    env, log = fake_udevadm
    subprocess.run(["sh", str(script), "pre", "optionsplus-linux", "0.1.0_1"], env=env, check=True)
    assert not log.exists()


@pytest.mark.parametrize("script", [INSTALL, REMOVE])
def test_hooks_succeed_without_udevadm(script, tmp_path):
    env = dict(os.environ, PATH=str(tmp_path))  # empty PATH dir: no udevadm
    r = subprocess.run(["/bin/sh", str(script), "post", "optionsplus-linux", "0.1.0_1"], env=env)
    assert r.returncode == 0


def test_remove_hook_leaves_user_data_alone():
    text = REMOVE.read_text()
    assert not re.search(r"\brm\b|\bfind\b.*-delete|HOME|XDG_", text)


# --- packaged data files --------------------------------------------------

def test_udev_rule_grants_input_group_and_uaccess():
    rule = (ROOT / "data" / "70-optionsplus-linux.rules").read_text()
    assert 'GROUP="input"' in rule and 'MODE="0660"' in rule
    assert 'TAG+="uaccess"' in rule
    assert 'SUBSYSTEM=="hidraw"' in rule and "046d" in rule


def test_polkit_action_authorizes_exactly_the_xbps_update():
    import xml.etree.ElementTree as ET
    root = ET.parse(ROOT / "data" / "io.github.soubarnak.optionsplus-linux.policy").getroot()
    [action] = root.findall("action")
    assert action.get("id") == "io.github.soubarnak.optionsplus-linux.update"
    annotations = {a.get("key"): a.text for a in action.findall("annotate")}
    assert annotations["org.freedesktop.policykit.exec.path"] == "/usr/bin/xbps-install"
    defaults = {d.tag: d.text for d in action.find("defaults")}
    assert defaults["allow_active"] == "auth_admin_keep"
    assert "yes" not in defaults.values()          # never unauthenticated


def test_menu_entry_launches_ui():
    d = (ROOT / "data" / "optionsplus-linux.desktop").read_text()
    assert "Exec=optionsplus-linux" in d and "Icon=optionsplus-linux" in d
    assert "Type=Application" in d and "NoDisplay=true" not in d


def test_autostart_entry_runs_agent_hidden():
    d = (ROOT / "data" / "optionsplus-linux-agent.desktop").read_text()
    assert "Exec=optionsplus-agent" in d and "NoDisplay=true" in d


@pytest.mark.skipif(not shutil.which("desktop-file-validate"), reason="desktop-file-validate missing")
@pytest.mark.parametrize("name", ["optionsplus-linux.desktop", "optionsplus-linux-agent.desktop"])
def test_desktop_files_validate(name):
    r = subprocess.run(["desktop-file-validate", str(ROOT / "data" / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_icon_is_our_own_svg():
    svg = (ROOT / "data" / "optionsplus-linux.svg").read_text()
    assert svg.lstrip().startswith(("<?xml", "<svg")) and "viewBox" in svg
    assert "<image" not in svg  # no embedded raster, nothing copied from elsewhere


# --- build ----------------------------------------------------------------

@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if not shutil.which("xbps-create"):
        pytest.skip("xbps-create missing")
    out = tmp_path_factory.mktemp("out")
    r = subprocess.run([str(BUILD)], env=dict(os.environ, OPL_BUILD_DIR=str(out)),
                       capture_output=True, text=True, cwd=str(tmp_path_factory.mktemp("elsewhere")))
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def archive_names(pkg):
    out = subprocess.run(["tar", "--zstd", "-tf", str(pkg)], capture_output=True, text=True, check=True).stdout
    return [n.lstrip("./") for n in out.splitlines()]


def archive_member(pkg, name):
    return subprocess.run(["tar", "--zstd", "-xOf", str(pkg), "./" + name], capture_output=True,
                          text=True, check=True).stdout


def test_build_produces_versioned_noarch_package(built):
    pkg = built / ("optionsplus-linux-%s_1.noarch.xbps" % version())
    assert pkg.exists()


def test_built_package_declares_runtime_dependencies(built):
    pkg = built / ("optionsplus-linux-%s_1.noarch.xbps" % version())
    props = html.unescape(archive_member(pkg, "props.plist"))
    # xbps rejects a bare pkgname in a dependency ("can't guess pkgname").
    for dep in ("python3>=3.11", "python3-yaml>=0", "eudev>=0"):
        assert "<string>%s</string>" % dep in props, dep
    assert re.search(r"<string>electron3\d>=0</string>", props)
    assert "<string>noarch</string>" in props


def test_built_package_carries_hooks_and_all_parts(built):
    pkg = built / ("optionsplus-linux-%s_1.noarch.xbps" % version())
    names = set(archive_names(pkg))
    for need in ("INSTALL", "REMOVE","usr/bin/optionsplus-linux", "usr/bin/optionsplus-agent",
                 "usr/lib/udev/rules.d/70-optionsplus-linux.rules",
                 "usr/share/polkit-1/actions/io.github.soubarnak.optionsplus-linux.policy",
                 "etc/xdg/autostart/optionsplus-linux-agent.desktop",
                 "usr/share/applications/optionsplus-linux.desktop",
                 "usr/share/icons/hicolor/scalable/apps/optionsplus-linux.svg",
                 "usr/bin/optionsplus-fetch-payload",
                 "usr/lib/optionsplus-linux/depots.json",
                 "usr/lib/optionsplus-linux/bootstrap/main.js",
                 "usr/lib/optionsplus-linux/bootstrap/links.js",
                 "usr/lib/optionsplus-linux/bootstrap/guard.js",
                 "usr/lib/optionsplus-linux/opl_agent/fetch.py",
                 "usr/lib/optionsplus-linux/opl_agent/main.py"):
        assert need in names, need


def test_built_package_passes_contents_check(built):
    pkg = built / ("optionsplus-linux-%s_1.noarch.xbps" % version())
    assert check(pkg).returncode == 0


def test_build_fails_when_payload_leaks_into_tree(tmp_path):
    if not shutil.which("xbps-create"):
        pytest.skip("xbps-create missing")
    leak = ROOT / "opl_agent" / "leaked.asar"
    leak.write_text("x")
    try:
        r = subprocess.run([str(BUILD)], env=dict(os.environ, OPL_BUILD_DIR=str(tmp_path)),
                           capture_output=True, text=True)
    finally:
        leak.unlink()
    assert r.returncode != 0
    assert not list(tmp_path.glob("*.xbps"))


# --- one staging script for both packages ---------------------------------

STAGE = REPO / "tools" / "stage.sh"
TEMPLATE = REPO / "srcpkgs" / "optionsplus-linux" / "template"


def tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


def test_stage_installs_everything_the_first_run_needs(tmp_path):
    r = subprocess.run(["sh", str(STAGE), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    files = tree(tmp_path)
    for need in ("usr/bin/optionsplus-fetch-payload",
                 "usr/lib/optionsplus-linux/bootstrap/links.js",
                 "usr/lib/optionsplus-linux/bootstrap/guard.js",
                 "usr/lib/optionsplus-linux/depots.json",       # where fetch.py looks for it
                 "INSTALL", "REMOVE"):
        assert need in files
    assert check(tmp_path).returncode == 0


def test_stage_installs_every_module_the_bootstrap_requires(tmp_path):
    assert subprocess.run(["sh", str(STAGE), str(tmp_path)]).returncode == 0
    staged = tmp_path / "usr/lib/optionsplus-linux/bootstrap"
    todo, seen = ["main.js"], set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        assert (staged / name).is_file(), name
        here = Path(name).parent
        for m in re.findall(r"""require\(\s*(['"])\./([\w./-]+?)\1\s*\)""", (staged / name).read_text()):
            rel = (here / m[1]).as_posix()
            todo.append(rel if rel.endswith(".js") else rel + ".js")
    assert {"main.js", "links.js", "guard.js"} <= seen


def test_template_installs_the_same_files_as_stage_and_build_xbps(tmp_path):
    staged, templated = tmp_path / "staged", tmp_path / "templated"
    assert subprocess.run(["sh", str(STAGE), str(staged)]).returncode == 0
    # run the template's do_install the way xbps-src does: DESTDIR set, cwd the source tree
    script = 'wrksrc=%s; DESTDIR=%s; vlicense() { :; }; . %s; do_install' % (REPO, templated, TEMPLATE)
    r = subprocess.run(["bash", "-eu", "-c", script], cwd=str(REPO), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert tree(templated) == tree(staged)
    # neither the template nor the release build may keep a file list of its own
    assert "stage.sh" in BUILD.read_text()
    for text in (BUILD.read_text(), TEMPLATE.read_text()):
        assert not re.search(r"^\s*(install|vbin|vinstall|vcopy)\b", text, re.M)
