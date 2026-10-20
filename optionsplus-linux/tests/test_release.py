"""Seams: the release helper scripts (tag check, repository signing, template bump) and the shape
of the release workflow, template and update file. Nothing here needs the network or the real key."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
TOOLS = REPO / "tools"
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
TEMPLATE = REPO / "srcpkgs" / "optionsplus-linux" / "template"
UPDATE = REPO / "srcpkgs" / "optionsplus-linux" / "update"
README = REPO / "README.md"

ARCHES = ["x86_64", "x86_64-musl", "i686", "aarch64", "aarch64-musl",
          "armv7l", "armv7l-musl", "armv6l", "armv6l-musl"]
FINGERPRINT = "fa:d1:00:32:ce:b1:b9:fd:73:05:05:76:75:54:37:25"
SUM = "a" * 64


def run(*args, **kw):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, **kw)


def version():
    m = re.search(r'__version__ = "([^"]+)"', (ROOT / "opl_agent" / "__init__.py").read_text())
    return m.group(1)


# --- tag check ---------------------------------------------------------------

def test_tag_matching_source_version_passes():
    r = run(TOOLS / "release-check-tag.sh", "optionsplus-linux-v" + version())
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == version()


@pytest.mark.parametrize("tag", ["optionsplus-linux-v99.0.0", "v0.1.0", "optionsplus-linux-v",
                                 "taema-v0.1.0", ""])
def test_tag_not_matching_version_fails(tag):
    r = run(TOOLS / "release-check-tag.sh", tag)
    assert r.returncode != 0
    assert "does not match" in r.stderr


# --- template bump -----------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    t = tmp_path / "srcpkgs" / "optionsplus-linux"
    t.mkdir(parents=True)
    shutil.copy(TEMPLATE, t / "template")
    shutil.copy(README, tmp_path / "README.md")
    return tmp_path


def test_bump_sets_version_revision_checksum_and_readme(tree):
    r = run(TOOLS / "bump-template.sh", "1.2.3", SUM, tree)
    assert r.returncode == 0, r.stderr
    t = (tree / "srcpkgs/optionsplus-linux/template").read_text()
    assert re.search(r"^version=1\.2\.3$", t, re.M)
    assert re.search(r"^revision=1$", t, re.M)
    assert re.search(r"^checksum=%s$" % SUM, t, re.M)
    assert "optionsplus-linux-1.2.3_1.noarch" in (tree / "README.md").read_text()


def test_bump_is_idempotent(tree):
    run(TOOLS / "bump-template.sh", "1.2.3", SUM, tree)
    before = (tree / "srcpkgs/optionsplus-linux/template").read_text()
    r = run(TOOLS / "bump-template.sh", "1.2.3", SUM, tree)
    assert r.returncode == 0
    assert (tree / "srcpkgs/optionsplus-linux/template").read_text() == before


def test_bump_rejects_bad_input(tree):
    assert run(TOOLS / "bump-template.sh", "1.2.3", "nothex", tree).returncode != 0
    assert run(TOOLS / "bump-template.sh", "not a version", SUM, tree).returncode != 0


# --- signing ------------------------------------------------------------------

@pytest.fixture(scope="module")
def signed(tmp_path_factory):
    """A throwaway package, indexed and signed with a throwaway key generated here."""
    if not (shutil.which("xbps-create") and shutil.which("xbps-rindex") and shutil.which("openssl")):
        pytest.skip("xbps tools or openssl missing")
    w = tmp_path_factory.mktemp("sign")
    dest = w / "dest"
    (dest / "usr/share").mkdir(parents=True)
    (dest / "usr/share/x").write_text("x\n")
    out = w / "assets"
    out.mkdir()
    r = run("xbps-create", "-q", "-A", "noarch", "-n", "optionsplus-linux-9.9.9_1", "-s", "t",
            "-l", "Apache-2.0", "-m", "t <t@example.com>", dest, cwd=out)
    assert r.returncode == 0, r.stderr
    key = w / "k.pem"
    r = run("openssl", "genrsa", "-out", key, "2048")
    assert r.returncode == 0, r.stderr
    r = run(TOOLS / "sign-repo.sh", out, "optionsplus-linux-9.9.9_1.noarch.xbps", key)
    return r, out, key


def test_signing_succeeds(signed):
    r, _out, _key = signed
    assert r.returncode == 0, r.stdout + r.stderr


def test_signing_yields_repodata_for_every_architecture(signed):
    _r, out, _key = signed
    for arch in ARCHES:
        assert (out / (arch + "-repodata")).is_file(), arch


def test_signing_signs_package_and_leaves_no_key_in_assets(signed):
    _r, out, key = signed
    assert (out / "optionsplus-linux-9.9.9_1.noarch.xbps.sig2").is_file()
    assert not any(p.suffix == ".pem" for p in out.iterdir())
    assert key.exists()  # the caller owns the key file


def test_signing_requires_key():
    r = run(TOOLS / "sign-repo.sh", "/nonexistent", "p.xbps", "/nonexistent/key.pem")
    assert r.returncode != 0


# --- template and update file --------------------------------------------------

def test_template_describes_this_package():
    t = TEMPLATE.read_text()
    assert re.search(r"^pkgname=optionsplus-linux$", t, re.M)
    assert re.search(r"^version=%s$" % re.escape(version()), t, re.M)
    assert re.search(r"^checksum=[0-9a-f]{64}$", t, re.M)
    assert "soubarnak/logioptions-plus" in t
    assert "optionsplus-linux-v${version}" in t
    assert re.search(r'^license="?Apache-2.0', t, re.M)


def test_update_file_matches_tag_pattern():
    t = UPDATE.read_text()
    site = re.search(r'^site="?([^"\n]+)', t, re.M).group(1).rstrip('"')
    pattern = re.search(r'^pattern="([^"\n]+)"', t, re.M).group(1)
    assert site == "https://github.com/soubarnak/logioptions-plus/releases/latest"
    m = re.search(pattern, "releases/tag/optionsplus-linux-v1.4.2")
    assert m and m.group(0) == "1.4.2"


# --- workflow shape --------------------------------------------------------------

@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


def step_index(job, needle):
    for i, s in enumerate(job["steps"]):
        if needle in s.get("name", "") + s.get("run", ""):
            return i
    raise AssertionError(needle)


def test_workflow_triggers_only_on_release_tags(wf):
    assert wf.get("on", wf.get(True)) == {"push": {"tags": ["optionsplus-linux-v*"]}}


def test_workflow_runs_in_void_glibc_container(wf):
    assert wf["jobs"]["release"]["container"] == "ghcr.io/void-linux/void-glibc:latest"


def test_workflow_checks_tag_tests_and_builds_before_signing_and_publishing(wf):
    job = wf["jobs"]["release"]
    tag = step_index(job, "release-check-tag.sh")
    test = step_index(job, "dev/test.sh")
    build = step_index(job, "build-xbps.sh")
    sign = step_index(job, "sign-repo.sh")
    publish = step_index(job, "gh release create")
    assert tag < test < build < sign < publish


def test_workflow_signs_with_secret_and_fails_without_it(wf):
    job = wf["jobs"]["release"]
    sign = job["steps"][step_index(job, "sign-repo.sh")]
    assert sign["env"]["XBPS_PRIVKEY"] == "${{ secrets.XBPS_PRIVKEY }}"
    # Unlike taema's package-only fallback, a missing key must not publish an unsigned release.
    assert "exit 1" in sign["run"] and "exit 0" not in sign["run"]


def test_workflow_publishes_everything_in_assets_dir(wf):
    job = wf["jobs"]["release"]
    run_ = job["steps"][step_index(job, "gh release create")]["run"]
    assert "/assets/*" in run_ and "releases/latest/download" in run_


def test_workflow_never_contacts_logitech():
    hosts = re.findall(r"https?://([^/\s'\"]+)", WORKFLOW.read_text())
    assert hosts and all(h.endswith(("github.com", "github.com'")) for h in hosts), hosts


def test_template_job_follows_release_and_updates_main(wf):
    job = wf["jobs"]["template"]
    assert job["needs"] == "release"
    text = "\n".join(s.get("run", "") for s in job["steps"])
    assert "bump-template.sh" in text and "HEAD:main" in text
    assert job["steps"][0]["with"]["ref"] == "main"


def test_workflow_permissions_are_contents_write_only(wf):
    assert wf["permissions"] == {"contents": "write"}


# --- README -----------------------------------------------------------------------

def test_readme_documents_repo_install_and_fingerprint():
    t = README.read_text()
    assert FINGERPRINT in t
    assert "/etc/xbps.d/20-optionsplus-linux.conf" in t
    assert "releases/latest/download" in t
    assert re.search(r"optionsplus-linux-[0-9][0-9.]*_1\.noarch", t)
