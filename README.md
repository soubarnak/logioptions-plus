# Options+ for Linux (`optionsplus-linux`)

An unofficial port of Logi Options+ to Void Linux: a clean-room native agent that speaks
the Options+ UI protocol over HID++ 2.0 on `/dev/hidraw*`, with Logitech's own Options+
user interface running on system Electron.

This project is not affiliated with, endorsed by, or supported by Logitech. "Logi",
"Logitech" and "Logi Options+" are trademarks of Logitech; the repository name uses them
descriptively. No Logitech files are stored in this repository or in any release: the
Options+ interface and device data are downloaded from Logitech's own servers on the
user's machine at first launch (Logitech's EULA applies to them).

## Status

Working today with an MX Master 3S on a Bolt receiver: device discovery, battery read,
home page, device page with the BUTTONS layout, theme and language settings. Settings
that change the mouse (pointer speed, SmartShift, button remapping), per-application
profiles, Easy-Switch, pairing, updates and packaging are not done yet. The full parity
matrix, the root causes and the phased plan are in [feature-parity.md](feature-parity.md).
Reverse-engineering notes are in [FINDINGS.md](FINDINGS.md).

## Layout

- `optionsplus-linux/opl_agent/` — the agent (Python 3, needs only `python3-yaml`).
- `optionsplus-linux/bootstrap/` — Electron entry that runs the unmodified `app.asar`.
- `optionsplus-linux/data/` — udev rule.
- `optionsplus-linux/dev/verify/` — runtime verification harness (CDP drivers, screenshots).
- `tools/` — payload unpackers (`undepot.py`, `unasar.py`) and schema tools.
- `re/` — route inventories from the analysis. The recovered protobuf schema and traffic
  captures are intentionally not committed; `tools/protodump.py` regenerates the schema.

## Running from a checkout (development)

1. Install `electron35`, `python3`, `python3-yaml`; make sure your user is in the `input`
   group and the udev rule from `optionsplus-linux/data/` is installed.
2. Obtain the Options+ payload yourself and unpack it with `tools/undepot.py` and
   `tools/unasar.py` (see FINDINGS.md §2 for the pinned depot URL).
3. Start the agent and the UI:

```bash
OPL_ASAR=/path/to/app.asar OPL_DATA_DIR=/path/to/logioptionsplus/data OPL_DEPOT_DIR=/path/to/depots optionsplus-linux/dev/verify/live.sh
```

`optionsplus-linux/.claude/skills/verify/SKILL.md` describes how the harness drives the UI.

## Local package

`tools/build-xbps.sh` builds `build/optionsplus-linux-<version>_1.noarch.xbps` from a checkout
(needs only `xbps-create`; no void-packages). The version lives in
`optionsplus-linux/opl_agent/__init__.py`. The build fails if the package holds any file outside
the allow-list in `tools/check-package-contents.py` or anything that looks like Logitech payload.

```bash
tools/build-xbps.sh
cd build && xbps-rindex -a optionsplus-linux-*.noarch.xbps
sudo xbps-install -R "$PWD" optionsplus-linux
```

The package installs the agent and bootstrap under `/usr/lib/optionsplus-linux`, the
`optionsplus-linux` launcher and `optionsplus-agent` command, a udev rule (access through the
`input` group; `uaccess` where elogind is present), an XDG autostart entry for the agent and a menu
entry. Join the `input` group on systems without elogind: `sudo usermod -aG input $USER`.

The launcher uses the newest installed `electron3x`, starts the agent when its socket is not
answering (a stale socket is replaced), and runs the UI with the Chromium sandbox on. Only if
Void's Electron cannot sandbox, set `OPL_NO_SANDBOX=1` to add `--no-sandbox`. Removing the package
removes the rule, autostart and menu entries; settings and downloaded data in your home stay.

### First launch: Logitech's data

The first launch finds no Options+ data and runs `optionsplus-fetch-payload`. It says that
the data (about 550 MB) comes from Logitech's servers, shows the size and the link to
Logitech's EULA, and waits for you to confirm (a zenity or kdialog box when a display and
the tool exist, otherwise the terminal). Declining exits without downloading or writing
anything. After a confirmed download the SHA-256 is checked against the value pinned in
`depots.json` before anything is unpacked; on a mismatch nothing is installed.

- Data lives in `~/.local/share/optionsplus-linux/payload/` (`app.asar`, `depots/`,
  `payload.json` with the installed data version). Delete that directory to reset it.
  The launcher and the agent use it without any `OPL_*` variable; a system-wide copy in
  `/usr/lib/optionsplus-linux/logitech/` is the fallback.
- Behind a proxy or with a dead URL: `optionsplus-fetch-payload --from-installer
  ~/Downloads/logioptionsplus_installer_offline.exe` takes the depots from Logitech's offline
  installer instead. Standard `https_proxy` variables are honoured for downloads.
- For every user on the machine: `optionsplus-fetch-payload --system` (uses `pkexec`, else `sudo`).
- `--accept-eula` skips the question for scripted installs; use it only after reading the EULA.

Tests: `python3 -m pytest optionsplus-linux/tests`. Before a release that changes
`optionsplus-linux/data/depots.json`, run `tools/verify-depot-urls.py` (needs the network): every
pinned URL must answer unauthenticated with the pinned size.

## Packaging and updates

Each release is a signed xbps repository attached to a GitHub release, containing
`optionsplus-linux-0.1.0_1.noarch.xbps`, its `.sig2` and signed repodata for x86_64, i686, aarch64,
armv7l and armv6l (glibc and musl). Add it once, then install and update like any package:

```bash
echo 'repository=https://github.com/soubarnak/logioptions-plus/releases/latest/download' | sudo tee /etc/xbps.d/20-optionsplus-linux.conf
sudo xbps-install -S optionsplus-linux
```

The first sync asks you to accept the repository key; check that the fingerprint is

`fa:d1:00:32:ce:b1:b9:fd:73:05:05:76:75:54:37:25`

before accepting (do not use `-y`). Later updates arrive with `sudo xbps-install -Su`.

Releases are tag-driven: bump `__version__` in `optionsplus-linux/opl_agent/__init__.py`, commit,
then push the tag `optionsplus-linux-v<version>`. The workflow in `.github/workflows/release.yml`
fails if the tag differs from the source version, if tests fail or if the package holds anything
outside the allow-list, and only then signs and publishes (with the `XBPS_PRIVKEY` Actions
secret). Afterwards it commits the new version and checksum to `srcpkgs/optionsplus-linux/template`
and the package name above to main. The workflow never contacts Logitech. The in-app "Check for
update" is wired to the same releases; see feature-parity.md §3 and §8 for the design.

## Button actions

A button assigned a keystroke, mouse-button, system, media or macro card is diverted to the
agent, which replays the card through a virtual input device (`/dev/uinput`, so it works on X11
and Wayland). System actions send a default keystroke that suits GNOME; other compositors bind
their own keys, so any action can be changed in `~/.config/optionsplus-linux/actions.toml`
(`$XDG_CONFIG_HOME` is honoured). The file is read again whenever it changes, no restart needed:

    [system]
    SWITCH_BETWEEN_DESKTOPS_RIGHT = "super+page_down"
    LOCK = "XF86ScreenSaver"

A value is one chord, keys joined by `+` (`ctrl`, `shift`, `alt`, `super`, letters, digits,
`f1`..`f24`, `page_up`, `left`, ..., or an `XF86...` media key). An entry that does not parse is
ignored and the default stays. Defaults: switch apps Alt+Tab, show desktop Super+D, next/previous
desktop Ctrl+Alt+Right/Left, lock Super+L, screenshot Print; brightness and media actions send the
XF86 keys. Holding a button does not auto-repeat (a volume hold fires once).

Manual check (GNOME, hardware needed): assign "Next track" and the "next virtual desktop" system
action to two buttons, press them, and confirm the player skips and the workspace changes. Then put
your compositor's own binding in `actions.toml` and press again.

## Support

Links in the Options+ UI (support, device support, release notes, feedback) open this
project's pages instead of Logitech's; Logitech's EULA and privacy links are left alone, and
Logitech cloud features (LogiID, marketplace, recommendations) show "not available in
Options+ for Linux". Report problems through the
[issue chooser](https://github.com/soubarnak/logioptions-plus/issues/new/choose) (bug report
or device support request). Logs are in `~/.local/state/optionsplus-linux/`. Device notes are
in the [Devices wiki](https://github.com/soubarnak/logioptions-plus/wiki/Devices).

## License

Apache-2.0 for the code in this repository. Logitech's software and data remain under
Logitech's terms.
