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

Tests: `python3 -m pytest optionsplus-linux/tests`.

## Packaging and updates

Releases will be signed xbps packages attached to GitHub releases, installable with
`xbps-install` from `https://github.com/soubarnak/logioptions-plus/releases/latest/download`,
with the in-app "Check for update" wired to the same releases. See feature-parity.md §3
and §8 for the design; nothing is published yet.

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
