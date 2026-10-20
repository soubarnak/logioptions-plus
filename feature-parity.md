# Feature-parity plan: Logi Options+ on Void Linux

Goal: make `optionsplus-linux` (the clean-room native agent plus the unmodified Options+
Electron UI) behave on Void Linux the way Logi Options+ behaves on Windows for the devices it
can drive, and ship it as a signed xbps package from GitHub releases with in-app updates.

This is a plan. No code changes are made by it. Every item names the UI symptom, the root
cause found during the 2026-10-20 verification run, the IPC routes and protobuf messages
involved (from the recovered schema), the HID++ 2.0 feature on the device, the Linux
mechanism that replaces the Windows one, and the acceptance test.

Conventions used below:

- "agent" = `optionsplus-linux/opl_agent/` (Python 3, dependency-free apart from PyYAML).
- "UI" = Logitech's `app.asar` running on system `electron35` through `bootstrap/main.js`.
- Routes are the UI's IPC paths (`GET`/`SET`/`SUBSCRIBE`), payload types are
  `logi.protocol.*` messages from `re/proto` (regenerate with `tools/protodump.py`).
- Test device: MX Master 3S (`046d:b034`) on a Bolt receiver (`046d:c548`). Feature indices
  observed on it: `0x1b04` reprogrammable controls v4, `0x2201` adjustable DPI,
  `0x2110` SmartShift, `0x2121` high-resolution wheel, `0x2150` thumb wheel,
  `0x1814` change host, `0x1815` hosts info, `0x1004` unified battery,
  `0x0003` firmware info, `0x0005` name/type, `0x0007` friendly name, `0x0021` unique id,
  `0x1d4b` wireless status, `0x00c3` DFU control.

---

## 0. Where we are (verification run, 2026-10-20)

| # | Feature | Windows behaviour | Linux today | Root cause | Phase |
|---|---|---|---|---|---|
| 1 | Home card, device page, BUTTONS tab art and hotspots | works | works | — | done |
| 2 | Theme, language, app settings persistence | works | works (`configuration.json`) | — | done |
| 3 | POINT AND SCROLL tab | pointer speed, scroll wheel, thumb wheel panels | three hotspots say MISSING ASSIGNMENT | agent seeds only `_c<cid>` and `_thumb_wheel_adapter` slots; `_mouse_settings`, `_mouse_scroll_wheel_settings`, `_mouse_thumb_wheel_settings` slots have no card | 1 |
| 4 | Pointer speed slider | changes DPI on the mouse immediately, persists | slider moves, nothing sent, mouse stays at 1000 DPI, resets to 50% on relaunch | same as 3: with no card in the slot there is nothing to `SET`; no `/mouse` DPI route in the agent | 1 |
| 5 | SmartShift / scroll-wheel settings | ratchet threshold, hi-res, direction, speed | not shown | no card, no `0x2110`/`0x2121` route wiring | 1 |
| 6 | Thumb wheel settings | direction, speed, smooth | not shown | no card, no `0x2150` route wiring | 1 |
| 7 | Button remap (Back, Forward, Middle, Top, Gesture) | action picker with hundreds of presets, applied to device | picker shows only "Create Smart Actions"; search returns nothing; 0 HID++ `0x1b04` calls | `/rac`, `/macros/presets`, `/macros/predefined`, `/macro_assignments/all` answer bare `@type`; no divert/inject engine | 2 |
| 8 | Gestures (thumb button) | 4-way gestures, desktop switching | display only | needs `0x1b04` rawXY divert and a Linux action table | 2 |
| 9 | Swap left/right buttons | applies to the system pointer | agent answers SUCCESS and does nothing; reads back off after relaunch | `/mouse/global/swap` is a static echo | 3 |
| 10 | Battery indicator on card and device page | shown | hidden although the store holds `stateOfCharge 85` | `Device.Info` battery capability flags not set; `/battery/state/changed` never pushed | 3 |
| 11 | Firmware version in SETTINGS | "RBM xx.xx.Bxxxx", check for update | "Firmware version unknown" | `eligibleVersion` comes from `/dfu/<id>/info`, which the agent does not serve | 3 |
| 12 | Easy-Switch tab | host slots 1-3, rename, switch | tab absent | `/devices/<id>/easy_switch` returns NO_SUCH_PATH | 3 |
| 13 | ADD DEVICE | Bluetooth and "Logi Bolt" pairing | only Bluetooth listed | receiver is not surfaced as a device; no pairing routes | 3 |
| 14 | Hot-plug | device appears/disappears live | untested | no hidraw monitor; one scan at start | 3 |
| 15 | ADD APPLICATION, per-app profiles | lists installed apps, switches profile with the active window | "Scanning for applications" forever | `/applications/scan/installed` accepted but nothing is ever broadcast; no active-window tracking | 4 |
| 16 | Smart Actions templates | gallery from marketplace | empty | marketplace is Logitech cloud | out of scope (stub) |
| 17 | App Settings → "Logi Options+ version" | shows version | agent serves `/updates/info` and `/system/info` (tested); live UI run pending | field names of `system_info` unverified against the UI | 5 |
| 18 | Check for update / auto update | Logitech pipeline | agent checks GitHub releases (stable redirect, testing Atom feed), periodic setting, channel (tested); live UI run pending | install/download routes not done | 5 |
| 19 | Support, release notes, feedback links | Logitech URLs | Logitech URLs | hard-coded in `app.min.js`; opened through `electronSend.openExternal` | 5 |
| 20 | LOGI SERVICES, account login, cloud backup | works | empty / login dialog only | Logitech cloud | out of scope (stub) |
| 21 | Packaging, udev, launcher, autostart, updates via xbps | MSI installer, updater service | none | not written | 0 |

Robustness already verified: the agent survives malformed frames, unknown verbs, bad
device ids and wrong payload types, and keeps accepting connections.

---

## 1. Constraints that shape every phase

1. **No Logitech bytes in the repository or in any release asset.** Not the installer, the
   depots, `app.asar`, strings, device catalogue, images, fonts, presets, or traffic
   captures that contain them. The released `.xbps` carries only our code. The Logitech
   payload is fetched **on the user's machine** by a first-run helper, from Logitech's own
   URLs, into a user-writable directory. A `restricted=yes` void-packages template stays
   in the repo as an optional *local* build path (`XBPS_ALLOW_RESTRICTED=yes`), never in CI
   and never as a release asset.
2. **The UI stays unmodified.** Everything the UI needs is answered by the agent over the
   socket or intercepted in `bootstrap/main.js` (our file). No patching of `app.min.js`.
3. **Clean room.** HID++ 2.0 feature behaviour is taken from public documentation
   (Logitech's published HID++ 2.0 feature specs, Solaar's and libratbag's protocol notes)
   and from reading our own device, not from the Windows agent's code.
4. **Void specifics.** No systemd: no systemd user services, no `uaccess` from logind
   unless elogind is installed. runit supervises system services only; a per-user daemon
   starts from the session (XDG autostart) or from the launcher. `eudev` handles udev
   rules. Both glibc and musl must work (pure Python plus Electron, so noarch).
5. **Honesty about scope.** Features that need Logitech's cloud (LogiID, backup, Smart
   Actions marketplace, Flow, Logi Plugin Service, firmware DFU payloads) are stubbed so the
   UI stays calm, and documented as unsupported rather than promised.

---

## 2. Drivers: what Linux already provides, what we rebuild

The Windows agent talks to devices through `HID.DLL`/`SETUPAPI`, `WINUSB` and
`BluetoothGATT`, injects input with `SendInput`, and watches the foreground window with
UIAutomation. None of that is ported as a driver. The Linux stack already covers the device
side:

| Windows piece | Linux replacement | Status |
|---|---|---|
| Receiver enumeration (`SetupDi*`) | kernel `hid-logitech-dj`: one `/dev/hidraw*` per receiver interface; paired devices are enumerated over HID++ register `0xB5` (Unifying) / `0x5n` (Bolt) | done in `hidpp.py` |
| HID++ short/long reports (`HidD_SetFeature`, overlapped reads) | `read()`/`write()` on hidraw, report ids `0x10`/`0x11`; `select()` with timeout | done |
| BLE direct devices (`BluetoothGATT`) | `bluez` pairs the device; the kernel exposes it as `hidraw` with HID id `0005:046D:*`; only long reports (`0x11`) exist | rule written; agent must not require short reports (phase 3) |
| Battery to the OS | kernel `hid-logitech-hidpp` already exports a power supply to upower; the agent reads `0x1004` itself | done |
| `SendInput` | `/dev/uinput` virtual keyboard/mouse (ioctl over `ctypes`, no extra package) | phase 2 |
| Foreground window | X11 `_NET_ACTIVE_WINDOW`; Wayland per compositor (wlr-foreign-toplevel, KWin scripting, GNOME shell extension) | phase 4 |
| Updater service, crashpad, appbroker, LPS plugin service | not ported; xbps is the updater | phase 0/5 |

Kernel notes to verify in phase 1 and 3:

- `hid-logitech-hidpp` may itself enable high-resolution scrolling (`0x2121`) on bind. The
  agent must read the current wheel mode before showing it and must coexist with the
  kernel's setting rather than assume a default.
- Solaar and logiops also drive `0x1b04`/`0x2201`. Running them alongside the agent causes
  fights over diverted buttons. README documents "run one of them".
- No out-of-tree or rebuilt driver is needed. `hid-logitech-dj`, `hid-logitech-hidpp`,
  `uinput` are in Void's default kernel (verified: `uinput` loaded on the test host).

---

## 3. Phase 0 — Repository, packaging, release pipeline, first-run payload

Deliverables for the GitHub repository `soubarnak/logioptions-plus` (GitHub does not allow
`+` in a name; the package keeps the name `optionsplus-linux`).

### 3.1 Version source and layout

- Single version source: `optionsplus-linux/opl_agent/__init__.py` with
  `__version__ = "0.1.0"`. The release tag must be `optionsplus-linux-v<version>`.
- `bootstrap/package.json` gets our version and `productName "Options+ for Linux"`; keep
  `app.setName('logioptionsplus')` because the renderer reads `~/.config/logioptionsplus/`.
- Record Logitech's payload version the code was tested against (`2.8.981479`, build
  `876310`, main depot `c08bc392-…`) in `optionsplus-linux/payload/depots.json` (see 3.3).

### 3.2 Package contents (`optionsplus-linux-<ver>_1.noarch.xbps`)

| Path | Source | Notes |
|---|---|---|
| `/usr/lib/optionsplus-linux/opl_agent/` | agent package | pure Python |
| `/usr/lib/optionsplus-linux/bootstrap/` | `bootstrap/main.js`, `package.json` | Electron entry |
| `/usr/bin/optionsplus-linux` | launcher script | picks the newest `/usr/lib/electron3*/electron`, exports `OPL_*` paths, starts the agent if its socket is missing, then runs the UI; no `--no-sandbox` (verify Void's `electron35` ships a working `chrome-sandbox`; fall back with a documented flag only if it does not) |
| `/usr/bin/optionsplus-linux-agent` | agent entry | `python3 -m opl_agent.main` |
| `/usr/bin/optionsplus-linux-fetch-payload` | first-run helper | see 3.3 |
| `/usr/lib/udev/rules.d/70-optionsplus-linux.rules` | `data/70-optionsplus-linux.rules` | see 3.4 |
| `/etc/xdg/autostart/optionsplus-linux-agent.desktop` | new | starts the agent at session login (`Hidden=false`, `X-GNOME-Autostart-enabled=true`) |
| `/usr/share/applications/optionsplus-linux.desktop` | new | menu entry, generic icon of our own (no Logitech artwork) |
| `/usr/share/polkit-1/actions/…` | optional | only if phase 5 uses `pkexec` for in-app update |
| `/usr/share/licenses/optionsplus-linux/LICENSE` | `LICENSE` | |
| `INSTALL`/`REMOVE` scripts | `srcpkgs/optionsplus-linux/INSTALL` | run `udevadm control --reload` and `udevadm trigger --subsystem-match=hidraw` after install |

Runtime dependencies for `xbps-create -D`: `python3>=3.11`, `python3-yaml`, `electron35`
(or whichever `electron3x` Void ships at release time), `eudev` (rules directory),
`xdg-utils` (optional, for `xdg-open` fallbacks). No `plugdev`: the rule uses `input`.

### 3.3 First-run payload fetcher (`optionsplus-linux-fetch-payload`)

- Reads `payload/depots.json` (our file): for each depot we support, its name, the pinned
  Logitech URL (`https://updates.optionsplus.logitechg.com/depots/<uuid>/<name>.depot`),
  size and SHA-256. Start with `logioptionsplus` (552,459,832 bytes,
  `baf9ecf0…715869`, verified HTTP 200 without authentication) and `mx_master_3s`.
  Add device depots as devices are supported. Verify every pinned URL answers 200
  unauthenticated before shipping; keymaster-gated depots are excluded.
- Fallback: `--from-installer <logioptionsplus_installer_offline.exe>` unpacks the ZIP
  overlay and takes depots from there (for users behind proxies or when a URL rots).
- Unpacks with `tools/undepot.py` (both the `10 01 17 20` main format and the device-asset
  `06 05 21 20` format; confirm the second one is fully handled, FINDINGS §2 marks it
  "not decoded yet" but the MX Master 3S depot was unpacked in this session) into
  `$XDG_DATA_HOME/optionsplus-linux/logitech/depots/<name>/`, and extracts
  `resources/app.asar` to `$XDG_DATA_HOME/optionsplus-linux/logitech/app.asar` with
  `tools/unasar.py` only when needed (the UI loads the asar directly).
- `OPL_DEPOT_DIR`, `OPL_DATA_DIR` and `OPL_ASAR` defaults move to that per-user directory,
  with `/usr/lib/optionsplus-linux/logitech/…` as a system-wide fallback for the local
  restricted build. `--system` writes there through `pkexec`/`sudo`.
- The launcher runs the fetcher when `app.asar` is missing, showing progress in the
  terminal and a zenity/kdialog box when available. It shows Logitech's EULA URL and asks
  for confirmation before downloading, because the user is accepting Logitech's terms.
- Verifies SHA-256 before unpacking; keeps a `payload.json` with the versions installed so
  the agent can report "data version" and the update check can tell code updates from
  payload updates.

### 3.4 udev rule (Void/eudev nuances)

- Current rule tags `uaccess`. On Void `uaccess` only works with `elogind`; without it the
  tag is inert. Keep `GROUP="input", MODE="0660"` as the working line and document
  `usermod -aG input $USER`. Keep `TAG+="uaccess"` for elogind systems.
- Keep the BLE rule (`KERNELS=="0005:046D:*"`) and the `uinput` rule. Verify that eudev
  evaluates `KERNELS` on hidraw parents the same way systemd-udevd does.
- `udevadm control --reload && udevadm trigger` in `INSTALL`; eudev supports both.

### 3.5 Release pipeline (mirrors `Bongbetic/taema`)

- `.github/workflows/release.yml`, tag-driven (`optionsplus-linux-v*`), job in
  `ghcr.io/void-linux/void-glibc`: check tag equals `__version__`, run the test suite
  (phase 6 adds one), `tools/build-xbps.sh` (uses `xbps-create -A noarch`, no void-packages
  checkout), sign with `XBPS_PRIVKEY` for `x86_64 x86_64-musl i686 aarch64 aarch64-musl
  armv7l armv7l-musl armv6l armv6l-musl` (`xbps-rindex -a`, `--privkey … --signedby
  "Bongbetic <soubarna@live.in>" -s .`, then `-S` on the package), attach package,
  `.sig2` and every `<arch>-repodata` to the release.
- Repository URL for users: `https://github.com/soubarnak/logioptions-plus/releases/latest/download`.
  Every release must carry the full repodata set; no gh-pages.
- Second job bumps `srcpkgs/optionsplus-linux/template` (`version`, `checksum`) and the
  README download name straight on `main`.
- `srcpkgs/optionsplus-linux/update`: `site=…/releases/latest`,
  `pattern="(?<=releases/tag/optionsplus-linux-v)[\d.]+"`.
- Secret `XBPS_PRIVKEY` = the existing release key (fingerprint
  `fa:d1:00:32:ce:b1:b9:fd:73:05:05:76:75:54:37:25`), set with `gh secret set`. Reusing it
  means users accept one fingerprint for all our packages.
- The release `.xbps` contains no Logitech bytes (3.3 guarantees it). The workflow never
  downloads from Logitech.

### 3.6 Local restricted template (optional path)

`srcpkgs/optionsplus-linux-payload/template` with `restricted=yes`,
`distfiles="https://updates.optionsplus.logitechg.com/depots/<uuid>/logioptionsplus.depot"`,
`checksum=baf9ecf0…`, `skip_extraction`, `do_install` running `tools/undepot.py` into
`/usr/lib/optionsplus-linux/logitech/`. Builds only with `XBPS_ALLOW_RESTRICTED=yes` on
the user's machine. Never in CI.

### 3.7 Acceptance

- Fresh Void VM (glibc) and a musl VM: add the repo, `xbps-install -S optionsplus-linux`,
  fingerprint prompt matches README, launch from the menu, payload fetched, home page shows
  the mouse. Agent restarts cleanly on logout/login. `xbps-install -Su` picks up the next tag.

---

## 4. Phase 1 — POINT AND SCROLL: pointer speed, scroll wheel, thumb wheel

Findings 3–6. All three panels are **profile assignments** on the slots listed in
`core_metadata.json` `device_point_scroll_image`: `<prefix>_mouse_settings`,
`<prefix>_mouse_scroll_wheel_settings`, `<prefix>_mouse_thumb_wheel_settings`.

### 4.1 Seed the cards

- `default_configurations.json` for the MX Master 3S has `POINTER_SPEED` (
  `mouseSettings.pointerSpeed.active`), `THUMB_WHEEL_2150_ACTION`, and **empty** settings for
  `HIGH_RESOLUTION_SCROLLING_2121`, `THUMB_WHEEL_2150_SETTINGS`, `SMARTSHIFT`. The real agent
  builds those cards from live device reads. So: `profiles.py` seeds the three slot cards
  (`profiles_v2.Card` with the matching attribute, e.g. `MOUSE_SETTINGS`,
  `MOUSE_SCROLL_WHEEL_SETTINGS`, `MOUSE_THUMB_WHEEL_SETTINGS` — confirm the exact attribute
  names from `profiles_v2.proto` `Card.Attribute` and `data/defaults/defaults_slot_win.json`),
  with values read from the device at connect time.
- Capabilities for the scroll panel come from `mouse.ScrollWheelCapabilities`
  (`high_resolution` ← `0x2121` present, `smartshift` ← `0x2110`/`0x2111`,
  `adjustable_speed` true, `smooth_scroll` as `WinAndOrMacBool`), served on the route the
  UI asks for (observe it once the card exists; candidates `/mouse/<id>/scroll_wheel/capabilities`
  from the agent route list `re/agent_routes.txt`).

### 4.2 Routes and device writes

| UI action | Route (observe exact path with the card seeded) | Payload | HID++ |
|---|---|---|---|
| pointer speed slider | `SET /v2/profiles/slice` with the `_mouse_settings` assignment (`mouse.Settings.pointer_speed.active.value` 0..1, `dpi_level`) | map 0..1 onto the `0x2201` DPI list (`200..8000 step 50` on this mouse) the way Options+ does (`Dpi.Info.Range`/`Levels` from `getSensorDpiList`) | `0x2201` fn3 `setSensorDpi`; read back fn2 and reply with the applied value |
| SmartShift toggle/threshold | `ScrollWheelSettings.smartshift` (`is_enabled`, `sensitivity` 1..50, `mode`) and `/smartshift/<id>/params` | | `0x2110` fn1 `setRatchetControlMode` (mode 1 free-spin / 2 ratchet, threshold); `0x2111` on devices that have it |
| hi-res / smooth scroll, direction, speed | `ScrollWheelSettings.isSmooth`, `dir`, `speed` | direction and speed are **host-side** on Windows (the agent scales events). On Linux: `0x2121` fn2 `setWheelMode` for hi-res/invert (device supports `invert`), speed via libinput is not available per-device → implement speed/direction in the uinput relay only when the wheel is diverted (phase 2), otherwise expose hi-res + invert only and grey out speed | `0x2121` fn0/fn1/fn2 |
| thumb wheel direction/speed/smooth | `ThumbWheelSettings` | `0x2150` fn0 `getThumbwheelInfo`, fn1 `getThumbwheelStatus`, fn2 `setThumbwheelReporting` (invert, divert); speed via relay when diverted | `0x2150` |

### 4.3 Persistence and reapply

- Persist the profile (`profiles.json` in `OPL_STATE_DIR`, atomic write like
  `configuration.json`). `GET /v2/profiles` returns the stored profile, not the depot
  defaults, once it exists. `RESET TO DEFAULT SETTINGS` in the UI (`/v2/profiles/…` reset
  route, observe) deletes it and reapplies depot defaults.
- Reapply device settings on connect, on wake (`0x1d4b` wireless status event), and on
  receiver re-plug, since Logitech mice keep DPI but drop diverts and some wheel modes on
  power cycle. Also honour `0x0020` config-change events.
- Broadcast `/v2/profiles/update` after each change so a second UI window stays in sync.

### 4.4 Acceptance

Slider to 80% → `agent.log` shows `0x2201` tx/rx → `hidpp.get_dpi` reads the new value →
relaunch shows the same percentage. SmartShift off makes the wheel free-spin physically.
`sudo` is never needed.

---

## 5. Phase 2 — Button remapping and gestures (BUTTONS tab)

Finding 7–8. Two halves: the **catalogue** the picker shows, and the **engine** that makes
a remap do something.

### 5.1 Action catalogue

- Serve from the main depot, read at runtime: `data/rap/` (`/rac`, `rap.RACContainer`:
  recommended actions per slot), `data/card_presets/card_presets_win.json` and
  `card_presets_001.json` (`/macros/presets`, `rap.CardList`), `data/defaults/
  defaults_control_win.json` / `defaults_slot_win.json` (`/unified_profiles/assignment/default`,
  `profiles_v2.Defaults`), `data/macros/` (`/macros/predefined`,
  `profiles_v2.MacroInfosCollection`), `/macro_assignments/all`.
- The UI runs with `process.platform === 'linux'`, which is neither `win` nor `mac`. Only
  `*_win.json` and `*_osx.json` exist. Serve the Windows sets but **filter** cards whose
  macro type cannot run on Linux (Windows-only `system.action`s such as Cortana, Windows
  Ink, Task View names) and relabel where needed. Keep a Linux allow-list in our code
  (`opl_agent/actions.py`), not in the depot.
- Icons: cards reference `pipeline://system_actions/<icon>`. Find the depot that holds
  `system_actions` (look in `offline.json` for a depot named `system_actions`; add it to
  `payload/depots.json`) and resolve it with the existing `pipeline://` resolver.
- Search in the picker is client-side once the lists are non-empty.

### 5.2 Engine

- **Divert**: `0x1b04` fn3 `setCidReporting(cid, flags)` with `divert`/`persist` for every
  CID that has a non-default assignment; `rawXY` divert for the gesture button. Un-divert
  on default. Diverted buttons arrive as `0x1b04` event 0 (`divertedButtonsEvent`, up to
  four CIDs) and event 1 (`divertedRawXY`).
- **Event loop**: the agent already reads hidraw; add a reader task per node that routes
  event reports (`0x10/0x11`, feature index of `0x1b04`, software id 0) to the engine and
  everything else to the request matcher.
- **Inject** via `/dev/uinput` (`ctypes` + ioctls `UI_SET_EVBIT`, `UI_SET_KEYBIT`,
  `UI_DEV_SETUP`, `UI_DEV_CREATE`), one virtual keyboard+mouse device named
  `optionsplus-linux virtual input`. Works on X11 and Wayland.
- **Card → events table** (`opl_agent/actions.py`):
  - `KEYSTROKE {code, modifiers}` uses HID usage ids (e.g. `code 7, modifiers [227]` =
    Win+D): HID usage → Linux `KEY_*` table (the kernel's `hid-input` mapping, public).
  - `MOUSE {action: BUTTON, hidUsage}` → `BTN_LEFT/MIDDLE/RIGHT/SIDE/EXTRA`;
    `WIN_BACK`/`WIN_FORWARD` → `BTN_SIDE`/`BTN_EXTRA` (what the kernel sends today, so
    default = unchanged behaviour).
  - `SYSTEM {action}` → Linux equivalents: `SWITCH_APPS` → Alt+Tab; show desktop, virtual
    desktop next/prev, screenshot, lock, media keys, volume, brightness → XF86 keys. Desktop
    switching is compositor-specific; ship a default keystroke per action and a user
    override file `~/.config/optionsplus-linux/actions.toml`.
  - `MACRO_PLAYBACK` sequences, `ADAPTER_4WAYS` gesture cards (direction thresholds from
    `gestureInfo`), `ADAPTER_2WAYS` for the thumb wheel.
  - Unsupported card types answer with an error the UI understands (observe the `what`
    string the Windows agent returns for an unsupported card; else `NOT_SUPPORTED`).
- **Scroll relay** (optional, 5.2 depends on it for wheel speed/direction): divert
  `0x2121` hi-res wheel events and re-emit `REL_WHEEL_HI_RES` with scaling.
- **Swap left/right** (finding 9): implement as a device-level remap through `0x1b04`
  (`remap` CID `0x50` ↔ `0x51`) when the device reports `remap` capability; otherwise fall
  back to `xinput set-button-map` on X11 and document the Wayland limitation. Persist in
  `configuration.json`, reapply on connect.

### 5.3 Acceptance

Assign "Copy" to the Back button → `0x1b04` divert visible in the log → pressing Back in a
text editor pastes nothing but copies (test with `wl-paste`/`xclip`) → unassigning restores
Back. Gesture button + left swipe switches desktop on a known compositor (document which).
Setting survives a receiver re-plug.

---

## 6. Phase 3 — Device page completeness

### 6.1 Battery (finding 10)

- Probe which field gates the indicator: `devices.Device.Info` has
  `battery_discharge_level` (field 54), `battery_status` (57), and capability flags
  `has_battery_status` (40), `unified_battery` (43), `BatteryCapabilities battery` (64).
  Fill them from `0x1004` capabilities. Push `/battery/state/changed` in `initial_state`
  on subscribe and from the poller on change. Expect the card badge and the device-page
  header to show percent and charging.

### 6.2 Firmware version and update check (finding 11)

- Serve `GET /dfu/<id>/info` → `dfu.Info{require_fw_update false, eligible_version
  "<current>", dfu_result SUCCESS}` and `GET /dfu/update/available` → nothing available.
  Build the string from `0x0003` (`RBM 22.01.B0006` style; confirm the format by comparing
  with the Windows screenshot of the same mouse).
- Real DFU is out of scope: it needs Logitech's signed firmware payloads from the pipeline.
  README points receiver firmware updates to `fwupd`/LVFS (Unifying and Bolt receivers are
  on LVFS); mouse firmware stays on Windows/macOS.

### 6.3 Easy-Switch (finding 12)

- `GET /devices/<id>/easy_switch` → `devices.EasySwitch`-type payload (find the exact
  message near `hide_easy_switch_connection_type` in `devices.proto`) built from `0x1815`
  `getHostInfo`/`getHostFriendlyName` and the current host from `0x1814`.
  `SET /devices/<id>/easy_switch/change` → `0x1814` fn1 `setCurrentHost`. Host names via
  `0x1815` `setHostFriendlyName`. Serve `device_easyswitch_image` (already in the depot
  manifest) so the tab renders.

### 6.4 Receiver and pairing (finding 13)

- Surface the Bolt/Unifying receiver as a `Device.Info` of type RECEIVER so "Logi Bolt"
  appears in ADD DEVICE and the "Bolt receiver" tooltip has a device behind it.
- Pairing routes (`/devices/pairing/*`, `/receiver/*` — take names from
  `re/agent_routes.txt`): Bolt pairing uses HID++ 2.0 feature `0x1817` on the receiver
  (open lock, passkey display, status events); Unifying uses register `0xB2`. Both are
  documented by Solaar. Implement open/close/cancel with status broadcasts; the UI shows
  the passkey screen itself.

### 6.5 Hot-plug and reconnect (finding 14)

- Watch `/dev/hidraw*` with inotify on `/dev` (stdlib `ctypes` `inotify_init`) or poll
  `/sys/class/hidraw` every 2 s; rescan on change. Broadcast `/devices/state/changed`
  (ACTIVE) and `/devices/state/deactivated`. Handle `0x1d4b` wake-up events to refresh
  battery and reapply settings (4.3).
- BLE direct: devices with only long reports; `hidpp.py` must send `0x11` frames when the
  descriptor has no `0x10` collection.

### 6.6 Acceptance

Unplug the receiver: card disappears within 3 s; re-plug: card returns with battery and
settings reapplied. Easy-Switch tab lists hosts and switching to host 2 moves the mouse.

---

## 7. Phase 4 — Applications and per-application profiles

Finding 15.

- `SET /applications/scan/installed` → scan `/usr/share/applications`,
  `~/.local/share/applications`, flatpak exports; build `applications.Applications`
  (`id`, `name`, executable, icon path as `file://`). Broadcast on the path the UI
  subscribes to for scan completion (observe; the spinner waits on it) and answer
  `/applications/installed`, `/applications/all`.
- Per-app profile switching needs the active window's executable/app id:
  - X11: `_NET_ACTIVE_WINDOW` + `_NET_WM_PID` through `python-xlib`? No — keep
    dependency-free: read with a tiny `ctypes` Xlib binding or shell out to `xprop`
    (`xprop` is in `xprop` package; optional dep).
  - Wayland: wlroots `wlr-foreign-toplevel-management` (sway, hyprland, river), KWin
    scripting D-Bus, GNOME via an extension. Ship wlroots first; others pluggable.
- `/v2/profiles` grows one profile per application (`applicationId`), the engine
  switches the active assignment set on focus change and re-diverts only when the CID set
  differs.
- Acceptance: add Firefox, assign Back → "Reload", switch to Firefox: Back reloads;
  switch to a terminal: Back goes back.

---

## 8. Phase 5 — Update channel, about section, support links

Finding 17–19. The in-app update section becomes a front end for GitHub releases and xbps.

### 8.1 Version and about

- `GET /updates/info` → `updates.DepositoryInfo{version: __version__, build_id: git short
  sha baked at build time, branch: "stable", app_id: "optionsplus-linux"}`. This fills
  "Logi Options+ version" in App Settings → General.
- `GET /system/info` → fill OS name/version from `/etc/os-release` so the About and
  feedback pages show "Void Linux".
- Payload data version (3.3 `payload.json`) is appended to the version string, e.g.
  `0.1.0 (Logitech data 2.8.981479)`, so support reports carry both.

### 8.2 Check for updates (no Logitech pipeline, no telemetry)

- `SET /updates/check_now` → the agent does one `HEAD https://github.com/soubarnak/
  logioptions-plus/releases/latest` (stdlib `urllib`, follows no redirect, reads
  `Location`), parses `optionsplus-linux-v<ver>` from it, compares with `__version__`.
  No API quota, no token, no user data sent.
- `GET /updates/status` and broadcasts on `/updates/status` →
  `updates.Status{state}`: `CHECKING_FOR_UPDATES` → `NO_UPDATES` or `UPDATE_AVAILABLE`
  (`UPDATER_ERROR` with `error_message` when offline). The UI shows
  "New update available" + "Install update".
- `GET /updates/periodic_check` / `SET` → `updates.PeriodicCheck` persisted in
  `configuration.json`; the agent runs the check on that interval and at start. The
  "Automatically install updates" toggle becomes "check periodically" (install needs root).
- `GET /updates/channel` → `updates.Channel{pipeline_host: "https://github.com/soubarnak/
  logioptions-plus/releases", name: "stable"}`; `SET /updates/channel` accepted only for
  the names `stable` and `testing` (a `testing` channel = GitHub pre-releases; the HEAD
  trick does not see pre-releases, so `testing` uses the releases Atom feed).
- `SET /updates/download` → `UPDATE_READY` immediately (xbps downloads at install).
- `SET /updates/install` → if `pkexec` exists: `pkexec xbps-install -Syu optionsplus-linux`
  with a polkit action file shipped in 3.2; progress mapped to `UPDATE_INSTALLING` then
  `RESTARTING`. Without polkit: `UPDATER_ERROR` whose `error_message` is the exact command
  to run (`sudo xbps-install -Su optionsplus-linux`). `SET /updates/restart_now` restarts
  the UI and agent (the launcher re-executes itself).
- Payload updates: the same check compares `payload/depots.json` in the installed package
  with `payload.json`; when the package pins newer Logitech data, the status page offers
  "Download" which runs the fetcher (3.3).

### 8.3 Support and feedback links

The UI opens every external link with `window.electronSend.openExternal(url)`, which
reaches `shell.openExternal` in the main process. `bootstrap/main.js` (ours) wraps it and
rewrites by host/path, so no asar patching:

| UI link | Target |
|---|---|
| `https://support.logi.com/hc/*`, `https://www.logitech.com/support*` | `https://github.com/soubarnak/logioptions-plus#support` |
| device support page (`support.logi.com/hc/?mID=<supportPageId>`) | `https://github.com/soubarnak/logioptions-plus/wiki/Devices#<model>` (or README device table) |
| `https://marketplace.logi.com/releasenotes/optionsplus/` | `https://github.com/soubarnak/logioptions-plus/releases` |
| `https://feedback.logitech.com/s3/*` | `https://github.com/soubarnak/logioptions-plus/issues/new/choose` |
| EULA / privacy | unchanged (Logitech's terms apply to Logitech's payload) |
| LogiID login, marketplace, recommendation hosts | blocked with a dialog "not available in Options+ for Linux" |

"Report issues" in Feedback & Support collects logs through `/logging/*` routes; point the
archive at `~/.local/state/optionsplus-linux/` and open the issues page with a prefilled
template.

### 8.4 README (About and usage)

Sections, in order: what it is and what it is not (unofficial, not affiliated with or
endorsed by Logitech; "Logi", "Logitech", "Logi Options+" are Logitech trademarks; the repo
name is used descriptively); supported devices table with the parity status from §0;
requirements (Void Linux glibc or musl, `electron35`, user in `input`); install (add the
repo, key fingerprint `fa:d1:00:32:ce:b1:b9:fd:73:05:05:76:75:54:37:25`, `xbps-install -S
optionsplus-linux`, first launch downloads Logitech's payload after EULA confirmation);
updating (`xbps-install -Su`, or the in-app button); usage (launcher, autostart, tray,
per-app profiles, where settings live, coexistence with Solaar/logiops); troubleshooting
(hidraw permissions, Wayland notes, logs); building locally (`tools/build-xbps.sh`,
restricted template); development (`dev/verify/live.sh`, `OPL_*` variables, how to
regenerate `schema_defaults.py`); license and attribution.

---

## 9. Routes left unanswered: disposition

From `agent.log` of the verification run. "stub" means answer a calm default so the UI
hides the feature; "later" means a phase above; "no" means documented unsupported.

| Route | Disposition |
|---|---|
| `/devices/<id>/easy_switch` | done (ticket #16): list, switch, rename; HID++ indexes 0-based, UI indexing and 0x1815 byte layouts unconfirmed on hardware |
| `/dfu/*` | later (6.2) for info; DFU itself: no |
| `/unified_profiles/activities` | stub: empty list (activities are Windows "Logi profiles") |
| `/flow/<id>/config`, `/flow/<id>/device_location` | stub: Flow disabled (needs Logitech's cross-host service); no |
| `/applications`, `/application/additional_data`, `/applications/installed` | later (7) |
| `/updates/*` | later (8) |
| `/lps/*` (Logi Plugin Service: Adobe, Zoom, Teams plugins) | stub: endpoint `LOADED`, no plugins; no |
| `/offer/retrieve`, `/device_recommendation_enabled`, recommendation hosts | stub: nothing; no |
| `/accounts/*`, `/backups/*`, `/migration/*` | stub: not logged in, no backups; no |
| `/logivoice/*` | stub; no |
| `/beta_testing/features`, `/get_secure_input_app_name` | stub |
| `/macros/*`, `/rac`, `/macro_assignments/all` | later (5.1) |
| `/system/open_settings` | open the desktop's mouse settings (`gnome-control-center mouse`, `systemsettings kcm_mouse`, else nothing) |
| `/mouse/global/swap` | later (5.2) |

---

## 10. Phase 6 — Verification and regression protocol

- Keep `dev/verify/` as the harness. Add `dev/verify/flows/` scripts, one per acceptance
  test above, each producing a screenshot and a hardware read-back, so a parity run is one
  command. Every phase closes with the matrix in §0 updated.
- Unit tests (no hardware): frame codec, `fill()` defaults, catalogue lookup, profile
  seeding, action table, release-version parsing. Run in CI before packaging.
- Hardware CI is manual: MX Master 3S on Bolt, plus one Unifying mouse and one BLE device
  when available.
- Windows parity reference: a short screenshot set from the Windows app for the same mouse
  (buttons list, point & scroll values, settings page, firmware string) kept out of the repo
  (Logitech artwork) and referenced in the plan's acceptance tests.

---

## 11. Risks and open questions

- Electron 35 vs the 39 the asar was built for: works today; a Void bump to a newer
  `electron3x` is handled by the launcher picking the newest one. Pin tested versions in
  README.
- Logitech may move or gate depot URLs; the fetcher's installer fallback covers it.
- `process.platform === 'linux'` paths in the obfuscated UI may hit untested branches
  (e.g. shortcut rendering, modifier names). Watch the renderer log (`OPL_UI_LOGGING=1`).
- Wayland active-window tracking has no universal API; per-app profiles will be
  compositor-dependent. Say so in README.
- Kernel `hid-logitech-hidpp` already uses some features (hi-res wheel, battery); the agent
  must read before it writes and never assume exclusive ownership.
- Trademark: the repo name contains Logitech's mark. Keep the non-affiliation notice
  prominent; rename is cheap if asked.
- Legal: Logitech's EULA governs the payload the user downloads; the fetcher must show it
  and require confirmation. Our code is Apache-2.0.

---

## 12. Order of work and rough effort

| Phase | Items | Effort |
|---|---|---|
| 0 | repo, launcher, udev, autostart, fetcher, build script, workflow, README, first tag | 2–3 days |
| 1 | point & scroll cards, DPI/SmartShift/hi-res/thumb wheel, persistence, reapply | 2 days |
| 2 | catalogue, divert engine, uinput, action table, gestures, swap | 4–5 days |
| 3 | battery, firmware string, Easy-Switch, receiver + pairing, hot-plug, BLE | 3 days |
| 4 | application scan, active-window tracking, per-app profiles | 3 days |
| 5 | update channel, about, link rewriting, polkit install | 1–2 days |
| 6 | flows, unit tests, matrix update | ongoing |

Phases 1 and 5 can run in parallel with 0; phase 2 depends on 1 (profile persistence);
phase 4 depends on 2 (engine).
