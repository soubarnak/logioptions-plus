"""ADD APPLICATION: the installed-application scan, as the UI sees it."""

import os

import pytest


def write_desktop(directory, name, display, exec_line="/usr/bin/true", icon=None, extra=""):
    os.makedirs(directory, exist_ok=True)
    lines = ["[Desktop Entry]", "Type=Application", "Name=" + display, "Exec=" + exec_line]
    if icon:
        lines.append("Icon=" + icon)
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n" + extra)
    return path


@pytest.fixture
def appdirs(workdir):
    """System, per-user and Flatpak export application directories (temp). The
    agent gets them in default_dirs() priority order: user, Flatpak, system."""
    return {
        "system": os.path.join(workdir, "usr", "share", "applications"),
        "user": os.path.join(workdir, "home", ".local", "share", "applications"),
        "flatpak": os.path.join(workdir, "fp", "exports", "share", "applications"),
    }


@pytest.fixture
def scan_client(agent_factory, appdirs):
    def make():
        running = agent_factory(app_dirs=[appdirs["user"], appdirs["flatpak"], appdirs["system"]])
        return running.client()
    return make


def scan(client):
    """Subscribe the way the UI does, trigger a scan, return the completion push."""
    client.subscribe("/applications/installed")
    assert client.call("SET", "/applications/scan/installed", {})["result"]["code"] == "SUCCESS"
    while True:
        m = client.recv()
        if m["verb"] == "BROADCAST" and m["path"] == "/applications/installed":
            return m


def names(payload):
    return sorted(a["name"] for a in payload["applications"])


def test_scan_reads_system_user_and_flatpak_locations(scan_client, appdirs):
    write_desktop(appdirs["system"], "editor.desktop", "Text Editor")
    write_desktop(appdirs["user"], "mine.desktop", "My Tool")
    write_desktop(appdirs["flatpak"], "org.x.Player.desktop", "Player")
    client = scan_client()
    done = scan(client)
    assert "result" not in done
    assert names(done["payload"]) == ["My Tool", "Player", "Text Editor"]
    client.close()


def test_application_carries_id_name_executable_and_icon_url(scan_client, appdirs):
    icons = os.path.join(os.path.dirname(appdirs["system"]), "icons", "hicolor")
    for size in ("48x48", "128x128"):
        os.makedirs(os.path.join(icons, size, "apps"))
        open(os.path.join(icons, size, "apps", "fancy.png"), "wb").close()
    write_desktop(appdirs["system"], "fancy.desktop", "Fancy",
                  exec_line="/opt/fancy/bin/fancy --open %U", icon="fancy")
    (app,) = scan(scan_client())["payload"]["applications"]
    assert app["applicationId"] == "fancy" and app["name"] == "Fancy"
    assert app["applicationPath"] == "/opt/fancy/bin/fancy"
    assert app["commandLine"] == "/opt/fancy/bin/fancy --open"
    assert app["posterUrl"] == "file://" + os.path.join(icons, "128x128", "apps", "fancy.png")
    assert app["isInstalled"] is True


def test_absolute_icon_path_is_used_and_missing_icon_is_empty(scan_client, appdirs, workdir):
    icon = os.path.join(workdir, "abs.png")
    open(icon, "wb").close()
    write_desktop(appdirs["system"], "a.desktop", "A", icon=icon)
    write_desktop(appdirs["system"], "b.desktop", "B", icon="not-installed")
    apps = {a["name"]: a for a in scan(scan_client())["payload"]["applications"]}
    assert apps["A"]["posterUrl"] == "file://" + icon
    assert apps["B"]["posterUrl"] == ""


def test_hidden_and_non_application_entries_are_skipped(scan_client, appdirs):
    write_desktop(appdirs["system"], "ok.desktop", "Visible")
    write_desktop(appdirs["system"], "nd.desktop", "NoDisplay", extra="NoDisplay=true\n")
    write_desktop(appdirs["system"], "h.desktop", "Hidden", extra="Hidden=true\n")
    with open(os.path.join(appdirs["system"], "link.desktop"), "w") as fh:
        fh.write("[Desktop Entry]\nType=Link\nName=Link\nURL=http://x\n")
    with open(os.path.join(appdirs["system"], "junk.desktop"), "wb") as fh:
        fh.write(b"\xff\xfe not a desktop file")
    assert names(scan(scan_client())["payload"]) == ["Visible"]


def test_user_entry_overrides_system_entry_with_same_id(scan_client, appdirs):
    write_desktop(appdirs["system"], "tool.desktop", "System Tool")
    write_desktop(appdirs["user"], "tool.desktop", "User Tool")
    assert names(scan(scan_client())["payload"]) == ["User Tool"]


def test_installed_and_all_answer_from_the_scan(scan_client, appdirs):
    write_desktop(appdirs["system"], "one.desktop", "One")
    client = scan_client()
    scan(client)
    write_desktop(appdirs["system"], "two.desktop", "Two")     # not scanned yet
    for path in ("/applications/installed", "/applications/all"):
        reply = client.call("GET", path)
        assert reply["result"]["code"] == "SUCCESS"
        assert reply["payload"]["@type"].endswith("logi.protocol.applications.Applications")
        assert names(reply["payload"]) == ["One"]
    client.close()


def test_get_before_any_scan_scans_on_demand(scan_client, appdirs):
    write_desktop(appdirs["flatpak"], "x.desktop", "Flat")
    reply = scan_client().call("GET", "/applications/installed")
    assert names(reply["payload"]) == ["Flat"]


def test_empty_result_still_stops_the_spinner(scan_client):
    done = scan(scan_client())                 # none of the directories exist
    assert done["payload"]["applications"] == []


def test_completion_also_reaches_subscribers_of_applications(scan_client, appdirs):
    write_desktop(appdirs["system"], "one.desktop", "One")
    client = scan_client()
    client.subscribe("/applications")
    client.call("SET", "/applications/scan/installed", {})
    while True:
        m = client.recv()
        if m["verb"] == "BROADCAST" and m["path"] == "/applications":
            break
    assert names(m["payload"]) == ["One"]


def test_failing_scan_still_broadcasts_completion(scan_client, monkeypatch):
    from opl_agent import applications

    def boom(_dirs):
        raise RuntimeError("scan blew up")
    monkeypatch.setattr(applications, "scan", boom)
    assert scan(scan_client())["payload"]["applications"] == []


def test_default_locations_cover_user_flatpak_and_system(monkeypatch):
    from opl_agent import applications
    monkeypatch.setenv("HOME", "/h")
    dirs = applications.default_dirs({"XDG_DATA_HOME": "/h/.local/share",
                                      "XDG_DATA_DIRS": "/usr/local/share:/usr/share"})
    assert dirs == ["/h/.local/share/applications",
                    "/h/.local/share/flatpak/exports/share/applications",
                    "/var/lib/flatpak/exports/share/applications",
                    "/usr/local/share/applications", "/usr/share/applications"]


def test_subscribing_after_a_scan_pushes_the_current_list(scan_client, appdirs):
    write_desktop(appdirs["system"], "one.desktop", "One")
    client = scan_client()
    scan(client)
    _, pushes = client.subscribe("/applications/installed")
    assert [names(p["payload"]) for p in pushes] == [["One"]]
