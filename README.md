# Options+ for Linux (`optionsplus-linux`)

## What it is and what it is not

Options+ for Linux lets you configure Logitech mice on Void Linux with the real Logi Options+
user interface. A native agent written in Python talks HID++ 2.0 to the device over
`/dev/hidraw*`, injects remapped buttons and wheel motion through `/dev/uinput`, and answers the
protocol that Logitech's own Options+ UI (an Electron app) speaks to its background service.
The UI runs unmodified on the system `electron35`.

What it is not:

- **Unofficial.** This project is not affiliated with, endorsed by, or supported by Logitech.
  "Logi", "Logitech" and "Logi Options+" are trademarks of Logitech and appear here only to say
  what the software works with. Do not ask Logitech for help with it.
- **Nothing of Logitech's is shipped.** No Logitech file is in this repository or in any release
  asset; the release workflow checks the package contents against an allow-list and never
  contacts Logitech. On first launch your own machine downloads the Options+ data from Logitech's
  servers, after you confirm Logitech's EULA (see [Install](#install)).
- `optionsplus-linux/opl_agent/schema_defaults.py` is a table of message and field names with
  their default values. `tools/protodefaults.py` generates it from the protobuf descriptors
  recovered from Logitech's UI, because the UI expects every field to be present in a reply. It
  holds interface field names only, no Logitech code or data.
- Cloud features are not provided: LogiID login, backups, the Smart Actions marketplace, Flow,
  the Logi Plugin Service, LogiVoice and firmware updates (DFU). Their pages stay quiet or show
  "not available in Options+ for Linux".
- A "testing" update channel (GitHub pre-releases) is **not supported**; only stable releases
  are followed.

## Status

Checked on real hardware and in the real UI (MX Master 3S on a Logi Bolt receiver): device
discovery, the home page, the device page with the BUTTONS layout, theme and language, pointer
speed, scroll and thumb wheel direction, the action picker and assigning an action, the gesture
button's default, battery, the firmware version and its update check, the Easy-Switch host list,
swap left/right being refused on Wayland, and RESET TO DEFAULT SETTINGS. Each write was read
back from the mouse.

Implemented and covered by automated tests that simulate the device, the compositor and the
UI socket, but **not yet confirmed on hardware or in the live UI**: SmartShift and hi-res
writes, wheel and thumb wheel speed, the key and action injection from a button press, gesture
swipes, swap through the mouse or X11, switching Easy-Switch hosts, Bolt and Unifying pairing,
hot-plug, per-application profiles (X11 and wlroots), and the in-app update. See [Known limitations](#known-limitations) for the
specific unverified assumptions. The plan and root causes are in
[feature-parity.md](feature-parity.md); reverse-engineering notes are in [FINDINGS.md](FINDINGS.md).

## Supported devices

The reference device is the **MX Master 3S on a Logi Bolt receiver**. Other Logitech mice that
speak HID++ 2.0 through a Bolt or Unifying receiver, over Bluetooth, or by cable are discovered
too, but only the features their firmware reports are offered, and none of them has been tried.
Add notes for your model through a
[device support request](https://github.com/soubarnak/logioptions-plus/issues/new/choose); they
are collected in the [device notes](docs/devices.md).

| Feature | MX Master 3S (Bolt) | Notes |
|---|---|---|
| Discovery, home page, device page, BUTTONS layout | verified | |
| Theme, language | verified | |
| Hot-plug (receiver or mouse removed or switched off) | implemented, unverified | watches `hidraw` nodes and rescans every 2 s |
| Pointer speed (DPI) | verified | slider maps onto the range and step the mouse reports |
| SmartShift, hi-res scrolling, scroll direction | scroll direction verified; SmartShift and hi-res writes unverified | |
| Scroll and thumb wheel speed | implemented, unverified | needs `/dev/uinput`; speed controls are hidden without it; `OPL_WHEEL_RELAY=0` turns it off |
| Thumb wheel direction, smoothness | direction verified; smoothness unverified | |
| Button remap: keystroke, mouse button, system, media, macro | implemented, unverified | virtual input device, works on X11 and Wayland |
| Gesture button, thumb wheel actions | default diversion verified; swipes unverified | card layout assumed, see limitations |
| Swap left/right | refusal on Wayland verified; swapping unverified | one method system-wide, see [Usage](#swap-left-and-right) |
| Battery level and charging | level verified; charging unverified | |
| Firmware version | verified | shown only; the firmware check answers "no update" |
| Easy-Switch: list and switch hosts | list verified; switch unverified | this UI build offers no rename |
| Logi Bolt and Unifying pairing | implemented, unverified | |
| Bluetooth-direct devices | implemented, unverified | uses long HID++ reports |
| Per-application profiles | implemented, unverified | X11 and wlroots Wayland only |
| Update check, in-app update | implemented, unverified | |
| Firmware update (DFU), cloud features | not supported | |

"Implemented, unverified" means there are tests, not a hardware run. Please report what you find.

## Requirements

- Void Linux, glibc or musl, on a 64-bit or 32-bit x86 or ARM machine (see the architecture
  list under [Install](#install)).
- Installed automatically with the package: `python3` (3.11 or newer), `python3-yaml`,
  `electron35` and `eudev`. The launcher uses the newest `/usr/bin/electron3x` present.
- Your user needs access to `/dev/hidraw*` and `/dev/uinput`: the package's udev rule grants it
  through `uaccess` (when elogind is running) and through the `input` group. Without elogind run
  `sudo usermod -aG input $USER` and log in again.
- Optional: `xprop` (per-application profiles on X11), `xinput` (swap left/right on X11),
  `zenity` or `kdialog` (download dialog at first launch), `polkit` / `pkexec` (in-app update),
  `fwupd` (receiver firmware).
- About 550 MB of disk space and a network connection for the first launch.

## Install

Add the project's repository once, then install like any package:

```bash
echo 'repository=https://github.com/soubarnak/logioptions-plus/releases/latest/download' | sudo tee /etc/xbps.d/20-optionsplus-linux.conf
sudo xbps-install -S optionsplus-linux
```

`xbps-install -S` syncs the repository and shows the signing key's fingerprint before you
accept it. It must be

`fa:d1:00:32:ce:b1:b9:fd:73:05:05:76:75:54:37:25`

Do not use `-y`, which skips that check. The key is the same for every package signed by
soubarnak, so you accept one fingerprint once.

Each release is a signed xbps repository attached to a GitHub release. It holds the single
`noarch` package `optionsplus-linux-0.1.0_1.noarch.xbps`, its `.sig2`, and signed repodata for
`x86_64`, `x86_64-musl`, `i686`, `aarch64`, `aarch64-musl`, `armv7l`, `armv7l-musl`, `armv6l`
and `armv6l-musl`. Void has no musl i686 flavour. The package installs the agent and launcher
under `/usr/lib/optionsplus-linux`, the `optionsplus-linux`, `optionsplus-agent` and
`optionsplus-fetch-payload` commands, a udev rule, an XDG autostart entry for the agent, a menu
entry, and a polkit action for updates. Removing the package removes those; your settings and
the downloaded Logitech data stay.

### First launch: Logitech's data

Start **Options+ for Linux** from the menu (or run `optionsplus-linux`). The first launch finds
no Options+ data and runs `optionsplus-fetch-payload`. It says that the data (three depots,
about 554 MB in total) comes from Logitech's servers, shows the size and the link to Logitech's
EULA (https://www.logitech.com/legal/eula.html), and waits for you to confirm (a `zenity` or
`kdialog` box when a display and the tool exist, otherwise the terminal). Declining exits without downloading or writing anything. After a confirmed download the SHA-256
is checked against the value pinned in `depots.json` before anything is unpacked; on a mismatch
nothing is installed.

- The data lives in `~/.local/share/optionsplus-linux/payload/` (`app.asar`, `depots/`,
  `payload.json` with the installed data version). Delete that directory to start over. A
  system-wide copy in `/usr/lib/optionsplus-linux/logitech/` is the fallback.
- Behind a proxy or with a dead URL: `optionsplus-fetch-payload --from-installer
  ~/Downloads/logioptionsplus_installer_offline.exe` takes the depots from Logitech's offline
  installer (a 724 MB file with Logitech's signature appended) instead; the prompt then says the
  data is read from that file. The usual `https_proxy` variables are honoured for downloads.
- For every user on the machine: `optionsplus-fetch-payload --system` (uses `pkexec`, else `sudo`).
- `--accept-eula` skips the question for scripted installs; use it only after reading the EULA.
- `--force` fetches again even when the installed data is current.
- Three depots are pinned in `depots.json`: `logioptionsplus` (the app and its data, 552 MB),
  `mx_master_3s` (device data and artwork, 1.6 MB) and `system_actions` (action icons, 160 KB).
  Other mouse models need their own depot, which is not pinned yet.

## Updating

- With the system: `sudo xbps-install -Su` updates `optionsplus-linux` with everything else.
- In the app: App Settings shows the Options+ for Linux version and the Logitech data version.
  "Check for update" asks GitHub's releases page (one unauthenticated request, no telemetry, no
  Logitech server). The app also checks at start and once a day; the periodic check can be
  turned off. "Install update" runs `pkexec /usr/bin/xbps-install -Syu optionsplus-linux`
  after a polkit password prompt and then restarts the app. Without `pkexec` the app shows the
  exact command to run in a terminal: `sudo xbps-install -Su optionsplus-linux`.
- New Logitech data: the app does not offer it. When a release pins newer data, run
  `optionsplus-fetch-payload`; it says if the installed data is already current.
- The "testing" channel is not supported and answers "not supported".

## Usage

**Launcher.** `optionsplus-linux` makes sure the agent runs (it starts it when its socket does
not answer) and opens the UI with the Chromium sandbox on. Only if Void's Electron cannot sandbox,
set `OPL_NO_SANDBOX=1` to add `--no-sandbox`.

**Autostart.** The package installs `/etc/xdg/autostart/optionsplus-linux-agent.desktop`, so a
session that runs XDG autostart starts the agent at login and your settings and button mappings
apply without opening the window. If your window manager or compositor does not process XDG
autostart, start the agent from its own configuration: `exec optionsplus-agent` (sway, river),
`exec-once = optionsplus-agent` (Hyprland), or an entry in your session's startup script. A second
agent exits at once when one is already running. When the agent stops (logout, SIGTERM) it gives
diverted buttons and wheels back to the mouse.

**Where things live.**

| What | Where |
|---|---|
| Agent log | `~/.local/state/optionsplus-linux/agent.log` (rotating, 1 MiB, 3 old files; `$XDG_STATE_HOME` is honoured). `agent.stderr.log` beside it catches what is printed before logging starts. |
| Settings, profiles, swap choice, update settings | `~/.config/optionsplus-linux/` (`$XDG_CONFIG_HOME`, or `OPL_STATE_DIR`), including `configuration.json`, `button_swap.json`, `updates.json` and `actions.toml` |
| Logitech data | `~/.local/share/optionsplus-linux/payload/`, else `/usr/lib/optionsplus-linux/logitech/` |
| Agent socket | `/tmp/logitech_kiros_agent-<md5 of your user name>` (`OPL_SOCKET` overrides) |

**Environment variables.** All are optional. `OPL_LOG_LEVEL` (default `INFO`) sets the agent log
level. `OPL_WHEEL_RELAY=0` turns off the wheel relay (scroll and thumb wheel speed). `OPL_NO_SANDBOX=1`
is described above. `OPL_PAYLOAD_DIR` and `OPL_SYSTEM_PAYLOAD_DIR` point to the Logitech data.
`OPL_STATE_DIR` moves the settings directory. `OPL_UI_LOGGING=1` keeps the UI's console logging
on. The variables for development are listed under [Development](#development).

### Buttons and actions

Assigning a keystroke, mouse-button, system, media or macro card diverts that button to the
agent, which replays the card through a virtual input device (`optionsplus-linux virtual input`,
via `/dev/uinput`), so it works on X11 and Wayland. A button left at its default is not diverted,
and resetting it hands it back to the mouse. Gesture cards (press and swipe) and the thumb wheel's
two-way cards are supported. Windows-only actions are hidden from the picker. Holding a button
does not auto-repeat (a volume hold fires once).

The gesture button's default card is the exception: its swipes need the agent, so that button is
diverted from the start and its Windows chords are replaced by the matching system actions
(next/previous virtual desktop, show desktop, overview). While the agent is not running, the gesture
button does nothing.

System actions send a default keystroke that suits GNOME 40 and later. Other desktops bind their
own keys, so any action can be changed in `~/.config/optionsplus-linux/actions.toml`, which is
read again whenever it changes:

```toml
[system]
SWITCH_BETWEEN_DESKTOPS_RIGHT = "super+page_down"
LOCK = "XF86ScreenSaver"
```

A value is one chord, keys joined by `+` (`ctrl`, `shift`, `alt`, `super`, letters, digits,
`f1` to `f24`, `page_up`, `left`, and so on, or an `XF86...` media key). An entry that does not
parse is ignored and the default stays. Main defaults:

| Action | Default chord |
|---|---|
| Switch applications | `alt+tab` |
| Show desktop | `super+d` |
| Next / previous virtual desktop | `super+page_down` / `super+page_up` |
| Lock | `super+l` |
| Screenshot | `print` |
| Brightness, media, volume | the matching `XF86` keys |

### Swap left and right

One method is used for every mouse. If every connected pointer allows it, the mouse itself swaps
the buttons (so it holds in every program and on Wayland). Otherwise, in an X11 session with
`xinput` installed, the agent runs `xinput set-button-map` on the Logitech pointers only. On
Wayland with a mouse that cannot do it, the app refuses and points you to your desktop's own
mouse settings: swapping needs X11 or device support. The choice is kept and applied again when the
mouse reconnects.

### Per-application profiles

Add an application in Options+ (ADD APPLICATION lists desktop entries from the system, your user
directory and Flatpak) and give it its own button assignments; a button it does not change keeps
the default profile's card. The active profile follows the focused window. Only button assignments
differ per application; pointer speed and wheel settings are global.

| Session | Per-application profiles |
|---|---|
| X11, any window manager (needs `xprop`) | supported: matches WM_CLASS or program |
| sway, Hyprland, river, other wlroots compositors | supported through `wlr-foreign-toplevel-management`; matches the window's app_id |
| GNOME (Mutter) on Wayland | not supported: default profile only |
| KDE Plasma (KWin) on Wayland | not supported: default profile only |

Start the agent from inside the compositor session (its configuration, as above) so it finds
`WAYLAND_DISPLAY`; look for "following the focused window through wlr-foreign-toplevel-management"
in `agent.log`. Where focus cannot be followed the log says so and the default profile stays active.

### Other things the UI offers

Pointer speed, SmartShift, scroll and thumb wheel settings, battery, firmware version, the
Easy-Switch tab (list and switch hosts), and ADD DEVICE with Logi Bolt and Unifying pairing
through the receiver. Devices appear and disappear within a few seconds of plugging or unplugging.
"Open system mouse settings" starts `gnome-control-center mouse` or `systemsettings kcm_mouse`
when installed. Settings are written to the device and applied again after reconnect or wake.

### Coexistence with Solaar and logiops

Do not run Solaar or logiops at the same time. Both divert buttons and write settings on the same
HID++ devices, so they fight the agent over diverted buttons and the last writer wins. Stop and
disable the other tool first. (This is advice; the agent does not detect them.)

## Troubleshooting

- **No device listed or settings do nothing.** Check access: `ls -l /dev/hidraw*` should show a
  group `input` with `rw` for the group (or an ACL for you). Check `id` includes `input` (log in
  again after `usermod`), then replug the receiver. Check `/dev/uinput` is writable by you, too:
  without it remapping and wheel speed do not work. With elogind, `loginctl` should show an active
  local session.
- **A UI window opens but is empty.** Check the agent runs: `optionsplus-agent --is-running; echo $?`
  (0 is running). Read `~/.local/state/optionsplus-linux/agent.log`; raise detail with
  `OPL_LOG_LEVEL=DEBUG optionsplus-agent` in a terminal.
- **Per-application profile never switches.** Check the table above for your session, that `xprop`
  is installed on X11, and the focus line in `agent.log`. On GNOME and KDE Wayland only the default
  profile applies.
- **Swap left/right is refused.** See [Swap left and right](#swap-left-and-right).
- **A system action does the wrong thing.** Override it in `actions.toml`; see the defaults and
  [Known limitations](#known-limitations) for the risky ones.
- **CHECK FOR UPDATE always says the firmware is current.** This is expected: the agent never
  offers a mouse firmware update. The SETTINGS tab shows the version the mouse reports.
- **The device page stays on CONFIGURING.** The device's depot is missing from the Logitech data.
  Run the first-launch download again; `agent.log` names the depot it looked for.
- **A setting looks applied in the UI but the mouse ignores it.** Run the agent with
  `OPL_LOG_LEVEL=DEBUG` and look for the HID++ `tx` lines after the change. If there are none,
  report it with the matching `<- SET` line from `agent.log`.
- **Receiver firmware.** The app does not update firmware. Bolt and Unifying receivers are
  covered by fwupd and LVFS: `fwupdmgr get-devices` and `fwupdmgr update`. Mouse firmware cannot
  be updated on Linux with this project.
- **First launch fails to download.** Retry, use `--from-installer`, or set `https_proxy`.
- **Reset everything.** Quit the app and the agent, then remove `~/.config/optionsplus-linux/`
  (settings) and, if wanted, `~/.local/share/optionsplus-linux/payload/` (Logitech data).

### Window does not appear (hybrid NVIDIA/AMD, Wayland)

Symptom: `optionsplus-linux` starts, the agent runs, but no window ever shows. The UI process logs
that the GPU process crashed with `Cannot create bo with format= RGBA_8888 and usage=SCANOUT`.

Observed, not proven: on a laptop with an NVIDIA GPU driving HDMI and an AMD GPU driving the
internal panel (eDP), under niri, the default native Wayland mode showed a window in 0 of 8 runs
and `--ozone-platform=x11` in 8 of 8. Nothing in this project sets that mode beyond Electron's
`ozone-platform-hint=auto`, so the cause looks like Chromium's buffer allocation across the two GPUs.
A stale `~/.config/logioptionsplus` is not the cause.

Fix: run the UI through X11. This needs Xwayland (on niri, `xwayland-satellite`).

```sh
mkdir -p ~/.config/optionsplus-linux
echo 'OPL_OZONE_PLATFORM=x11' > ~/.config/optionsplus-linux/launcher.conf
```

or set `OPL_OZONE_PLATFORM=x11` in the environment for one run. The variable wins over the file;
`wayland` and `auto` (the default) are the other accepted values. Remove the line to go back.

### Known limitations

These behaviours are guesses from the protocol or from other implementations and have not been
confirmed on hardware or in the live UI:

- The scroll-wheel speed slider is assumed to run from 0 to 1, with 0.5 as unchanged and 1 as four
  times faster; the thumb wheel's unit per notch and the sign of its rotation (positive is taken as
  right) are unconfirmed.
- SmartShift threshold ranges and the exact SmartShift and hi-res byte layouts are unverified.
- Gesture and thumb wheel card layouts (the nested card keys, the swipe threshold) are assumed.
- Swap through the mouse relies on a group-mask rule taken from Solaar; on many mice the buttons
  cannot be remapped and X11 is the working path.
- Easy-Switch indexing and byte layouts, and the Bolt and Unifying pairing registers, are
  unverified; pairing by passkey entry and unpairing from the receiver are not implemented.
- The flow for creating a new profile in the live UI has not been walked through on hardware.
- Per-application profiles do not work on GNOME or KDE Wayland (no focus protocol).
- Default system chords follow GNOME 40 and later, and two are risky. `SHUT_DOWN` sends
  `KEY_POWER`, which logind may turn into an immediate power-off when nothing inhibits it, with no
  confirmation dialog on every desktop. `CONSOLE_CLIENT` sends `Ctrl+Alt+T`, an Ubuntu binding that
  stock GNOME and other desktops ignore. Likewise stock GNOME has no show-desktop key: `Super+D` is
  Ubuntu's, KDE's and Xfce's binding. Override these in `actions.toml`.
- Holding a button does not auto-repeat.
- A keystroke card you pick yourself that holds a Windows chord (for example Win+Ctrl+Left) injects
  that chord unchanged; most Linux desktops do not bind it. Only the gesture button's default card is
  translated.
- No firmware update (DFU) and no cloud features; no "testing" update channel.

## Building locally

`tools/build-xbps.sh` builds `build/optionsplus-linux-<version>_1.noarch.xbps` from a checkout
(it needs only `xbps-create`, `python3` and `tar` with zstd; no void-packages). The version lives
in `optionsplus-linux/opl_agent/__init__.py`. The build fails if the package holds any file
outside the allow-list in `tools/check-package-contents.py` or anything that looks like Logitech
payload. `tools/stage.sh` is the single list of installed files, shared with the void-packages
template in `srcpkgs/optionsplus-linux/`.

```bash
tools/build-xbps.sh
cd build && xbps-rindex -a optionsplus-linux-*.noarch.xbps
sudo xbps-install -R "$PWD" optionsplus-linux
```

### Restricted template with Logitech's data (local only)

`srcpkgs/optionsplus-linux-payload/template` builds a second package, `optionsplus-linux-payload`,
that bundles Logitech's UI bundle and device data into
`/usr/lib/optionsplus-linux/logitech/`, the shared location the launcher and agent already read.
It installs the same layout `optionsplus-fetch-payload --system` writes, so you can use either.
The template is `restricted=yes`: Void never builds, hosts or redistributes it, CI never touches
it, and it is for your own machine only. Its version is the pinned data version
(`dataVersion` in `optionsplus-linux/data/depots.json`), and its distfiles and checksums are that
file's depot URLs and SHA-256 sums (about 550 MB from Logitech's servers). By building it you accept
[Logitech's terms](https://www.logitech.com/legal/eula.html). The public package never contains
any of it.

```bash
cp -r srcpkgs/optionsplus-linux-payload ~/void-packages/srcpkgs/
cd ~/void-packages
XBPS_ALLOW_RESTRICTED=yes ./xbps-src pkg optionsplus-linux-payload
sudo xbps-install -R hostdir/binpkgs/restricted optionsplus-linux-payload
```

The template's file layout is checked against the fetcher by the tests; building it with
`xbps-src` itself has not been tried (the repository tests do not run `xbps-src`). When Logitech
publishes a new depot set, update `depots.json` and the template together;
`tests/test_payload_template.py` fails if they differ.

Releases are tag-driven: bump `__version__` in `optionsplus-linux/opl_agent/__init__.py`, commit,
then push the tag `optionsplus-linux-v<version>`. The workflow in `.github/workflows/release.yml`
fails if the tag differs from the source version, if tests fail or if the package holds anything
outside the allow-list, and only then signs and publishes with the `XBPS_PRIVKEY` Actions secret.
Afterwards it commits the new version and checksum to `srcpkgs/optionsplus-linux/template` and the
package name above to main.

## Development

Run from a checkout with the payload unpacked by hand (`tools/undepot.py`, `tools/unasar.py`;
FINDINGS.md §2 has the pinned depot URL):

```bash
OPL_ASAR=/path/to/app.asar OPL_DATA_DIR=/path/to/logioptionsplus/data OPL_DEPOT_DIR=/path/to/depots \
  optionsplus-linux/dev/verify/live.sh
```

`live.sh` needs `electron35` and starts the agent and the UI with remote debugging;
`optionsplus-linux/.claude/skills/verify/SKILL.md` describes how the harness drives the UI.

Tests need no hardware, network, display or Logitech data: `optionsplus-linux/dev/test.sh` runs
the agent tests (pytest) and the bootstrap tests (`node --test`).

Variables read by the code, besides those under [Usage](#usage): `OPL_ASAR`, `OPL_DATA_DIR`,
`OPL_DEPOT_DIR` (single payload paths), `OPL_LIB_DIR`, `OPL_BIN_DIR`, `OPL_AGENT_CMD`,
`OPL_FETCH_CMD` (where the launcher finds our code, Electron, the agent and the fetcher),
`OPL_MANIFEST` (depot manifest), `OPL_SOCKET`, `OPL_REPLAY` (boot replies from a capture, a
diagnostic), `OPL_LINK_LOG` (log how the UI's links were rewritten), `OPL_BUILD_ID`,
`OPL_BUILD_DIR` (output of `tools/build-xbps.sh`), `OPL_NO_HARDWARE=1` (the agent lists no hidraw node and opens neither hidraw nor `/dev/uinput`; the test run sets it for every child process), and `OPL_RESTART_WAIT_PID` with
`OPL_RESTART_WAIT_SECONDS` (set by the app itself when it restarts after an update).

Regenerate the field-name table with `tools/protodefaults.py <proto-root> <out.py>`; the proto
root comes from `tools/protodump.py`. The recovered schema and traffic captures are not committed.
Before a release that changes `optionsplus-linux/data/depots.json`, run
`tools/verify-depot-urls.py` (needs the network): every pinned URL must answer unauthenticated
with the pinned size.

Layout: `optionsplus-linux/opl_agent/` (the agent), `optionsplus-linux/bootstrap/` (Electron
entry that opens the unmodified `app.asar` and rewrites its links), `optionsplus-linux/data/`
(udev rule, desktop files, polkit action, `depots.json`), `optionsplus-linux/dev/` (test and
verification helpers), `tools/` (payload unpackers, schema and release tools), `re/` (route
inventories).

## Support

- Questions and bugs: open a [new issue](https://github.com/soubarnak/logioptions-plus/issues/new/choose)
  (bug report or device support request). The links in the Options+ UI for support, feedback and
  release notes open this project's pages instead of Logitech's; release notes are the
  [GitHub releases](https://github.com/soubarnak/logioptions-plus/releases). Logitech's EULA and
  privacy links are left pointing at Logitech.
- Include the Options+ for Linux version and the data version (App Settings), your Void flavour and
  desktop or compositor, the device and connection (for example "MX Master 3S on Bolt"), what you
  did and what you expected, and `~/.local/state/optionsplus-linux/agent.log`. Attach the log
  yourself; the app does not attach it. Remove anything you do not want public first.
- Device notes for your model are in [docs/devices.md](docs/devices.md).
- Not supported here: Logitech's cloud services, and anything that needs a Logitech account.

## License

Apache-2.0 for the code in this repository. Logitech's software and data remain under
Logitech's terms.
