# Plan: make every Options+ setting reach the MX Master 3S

Date: 2026-10-20. Scope: the defects found by the live `/verify` run of
`optionsplus-linux` 0.1.0 (main at `7ea6e5e`) against the real Options+ 2.8.981479 UI
and a real MX Master 3S on a Bolt receiver, plus the EULA URL check. Status: plan only,
no code changed. Every claim below carries its evidence tier (section 2.1) so the
implementer knows what is proven and what still needs the live harness.

Tracker: GitHub issues in `soubarnak/logioptions-plus`. Tickets touched: #9, #10, #11,
#15, #16, #20, #21, #23, #4, #6, #24 (see section 7).

## 1. How the software and the hardware are integrated

The chain, left to right, with the file that owns each hop:

```
Options+ UI (Electron, app.asar)
   │  JSON frames over /tmp/logitech_kiros_agent-<md5(user)>   ipc.py
   ▼
Router.handle                                                   router.py:124-246
   │  ordered dispatch: replay → localized strings → actions.ROUTES → applications
   │  → /v2/profiles → swap → updates → /configuration → responses.BOOT → stubs
   │  → /system/open_settings → pairing/discovery/locking → easy_switch → _device_route
   │  → any other SET: NOT_SUPPORTED (router.py:243) → any other verb: NO_SUCH_PATH
   ▼
Feature handlers
   profiles.py      card builders/parsers, ProfileStore (profiles.json)
   engine.py        button diversion (0x1b04) → uinput injection; gestures.py
   wheel_relay.py   wheel/thumb speed by diverting 0x2121 / 0x2150 and re-emitting
   swap.py          left/right swap by 0x1b04 remap, else xinput, else refuse
   easy_switch.py   0x1814 switch host, 0x1815 host names
   updates.py       GitHub release check, xbps install
   actions.py       action picker catalogue from the main depot's data/ files
   resources.py     device catalogue, depot lookup, strings
   ▼
Registry / Device                                               model.py
   │  one hidpp.Reader per hidraw node, two daemon threads each (read + dispatch)
   │  discovery: sysfs scan → ping slots 1..6 → _build (0x0003, 0x0005, 0x1004, …)
   │  every write is followed by a read-back; the device is the source of truth
   ▼
hidpp.py  (HID++ 2.0 over hidraw: 0x10 short / 0x11 long reports, swid 1..15)
   ▼
/dev/hidrawN  ←→  Bolt receiver c548  ←→  MX Master 3S (wpid b034, HID++ 4.5)
```

Facts about this mouse, read on hardware during the verify run (evidence tier H):

- Feature set (0x0001): 0x0003 fw, 0x0005 name, 0x1004 battery, 0x1b04 reprogrammable
  controls v4, 0x1814 change host, 0x1815 hosts info, 0x2201 adjustable DPI
  (200..8000 step 50, default 1000), 0x2110 SmartShift, 0x2121 hi-res wheel,
  0x2150 thumb wheel, 0x1d4b / 0x0020 wake and config-change events, plus 0x1802,
  0x1803, 0x1805, 0x1806, 0x1816, 0x1830, 0x1861, 0x1891, 0x18a1, 0x1e00, 0x1e02,
  0x1e22, 0x1eb0, 0x1602, 0x2250, 0x2251, 0x9001, 0x9205, 0x9300, 0x00c3.
- Controls (0x1b04): 0x50 left and 0x51 right are neither reprogrammable nor
  divertable (group 1, mask 1). 0x52 middle, 0x53 back, 0x56 forward, 0xc3 gesture
  and 0xc4 mode-shift are reprogrammable, divertable and raw-XY capable (group 2,
  mask 3). 0xd7 is divertable only (group 3, mask 0). So:
  - button swap by device remap is impossible on this mouse; `swap.py` is right to
    refuse under Wayland;
  - the gesture button can be diverted with raw XY, which `engine.py` needs for
    swipes.
- Firmware: BL1 69.01 build 6, RBM 22.01 build 6.
- Thumb wheel resolution (0x2150 fn0): native 18, diverted 120 units per turn.
- Hosts (0x1815): 0 "Logitech Bolt receiver" (BLE Pro), 1 and 2 "xserve" (BLE).

Persisted state (all atomic `.tmp` + `os.replace`): `configuration.json`,
`profiles.json`, `button_swap.json`, `updates.json` under `$OPL_STATE_DIR` or
`~/.config/optionsplus-linux`. `_apply_stored` (router.py:416-455) replays
`profiles.json` on every discovery and wake. This works: on the last relaunch the agent
reapplied the 200 DPI and inverted thumb wheel left over from the probe run.

The UI's device page needs the device depot (`mx_master_3s/manifest.json`), otherwise
`resourcesAvailable` is false and the card stays on "CONFIGURING". The depot is not
pinned in `data/depots.json` today.

## 2. Root causes

### 2.1 Evidence tiers

- H: read or written on the real mouse and read back.
- U: observed in the real UI's own socket traffic (`agent.log`).
- B: inferred from the UI bundle or Logitech's data files and `schema_defaults.py`.
- D: a design decision of ours, not evidence.

### 2.2 Defects, in fix order

| # | Symptom | Root cause | Tier |
|---|---|---|---|
| 1 | Pointer speed, scroll wheel, thumb wheel, button writes all answer `NOT_SUPPORTED` | The UI sends `SET /v2/assignment` (payload `{profileId, assignment:{slotId, cardId, card}}`). `router.py:156` only routes `SET /v2/profiles/*`. `/v2/profiles/assignment` was invented in commit `4d50626` and copied into twelve test files, so the tests pass. With a one-line alias the writes landed on the mouse. | U, H |
| 2 | RESET TO DEFAULT SETTINGS does nothing | The UI sends `SET /device/restore` with the device info object as payload (`{"id": "dev00000000", "deviceModel": "2b034", "slotPrefix": …}`). The agent only knows `/v2/profiles/reset`. | U |
| 3 | Scroll direction radio shows neither option; invert never applied | `profiles.py:152` and `:173` use `NORMAL`/`INVERTED`. The UI sends `"dir": "NATURAL"` for Inverted and the proto `logi.protocol.mouse.ScrollWheelSettings.dir` shares the `WheelDirection` enum with the thumb wheel (`UNDEFINED`, `STANDARD`, `NATURAL`), which `profiles.py:179` already uses for the thumb wheel. | U, B |
| 4 | Pointer 27% sets 200 DPI; 1000 DPI shows as 55%; stored card for 200 DPI says value 0.0 | The UI slider value runs from -1 to 1 (27% sent `value: -0.46`; the depot default is `0.0`). `profiles.dpi_from_value`/`value_from_dpi` (`profiles.py:85-98`) assume 0..1, so the left half clamps to the minimum and the default DPI lands at 55%. | U, B |
| 5 | Action picker empty; no button can be remapped | `/rac` is built by `actions._rac_list` (`actions.py:231-244`) from a `RACContainer` shape that the depot does not ship. The depot's `data/rap/*.json` are tag and recommendation tables: `{"data": {<app id>: {"infoList": [{"id": <slot or control id>, "infos": [...]}]}}}`. The container has to be composed from `card_presets_win.json` and those tables. | U, B |
| 6 | Gestures (default thumb-button card) do nothing | Two causes. (a) `engine._desired` (`engine.py:399`) and `router._assign_button` (`router.py:611`) never divert a button on its depot-default card, and the default for 0xc3 is `card_global_presets_one_of_gesture_button` with `selectedNestedCard: virtual_desktops`, a software gesture. (b) Even if diverted, that card fails to compile: its `up` nested card is a modifier-only keystroke (Windows key, `code: None`, `modifiers: [227]`) and `compile_card` (`engine.py:118-122`) refuses a keystroke without a key code. The other four nested cards compile. | H (controls), B (compile tested offline against the real depot) |
| 7 | `--from-installer` rejects Logitech's real offline installer: "File is not a zip file" | 96 345 bytes of Authenticode signature follow the ZIP end-of-central-directory record. Python's `zipfile` searches only the last 64 KiB + 22 bytes for the EOCD. A copy truncated at the EOCD installs all three depots with matching checksums. | H (file), U (fetcher output) |
| 8 | Fresh install leaves the device on "CONFIGURING" | `data/depots.json` pins only `logioptionsplus`. The `mx_master_3s` and `system_actions` depots exist at public URLs (HTTP 200, sizes match `offline.json`). The UI's `SET /updates/depot/info {"name": …, "state": "REQUESTED"}` is echoed by `stubs.py:77` and never acted on. | U, H (HEAD requests) |
| 9 | EULA URL differs from Logitech's | `depots.json` has `https://www.logitech.com/en-us/legal/eula.html`; the installer uses `https://www.logitech.com/legal/eula.html` (302 to the locale page). `fetch.eula_text` also says "downloaded from Logitech's servers" with `--from-installer`. | H (installer strings) |
| 10 | Easy-Switch rows show "Oops. Something went wrong" | `hidpp.get_hosts` hardcodes `os: 0` (`hidpp.py:979`, `:988`); `easy_switch.hosts_payload` sends `os: {"type": "UNKNOWN"}`; the UI renders the string key `UNKNOWN`, which `en-US.yaml:219` maps to that text. The strings file has `OS_WINDOWS`, `OS_MACOS`, `OS_LINUX`, `OS_ANDROID`, `OS_IOS`, `OS_CHROME`, `OS_WEBOS`, `OS_TIZEN`, `OS_WINDOWS_EMBEDDED`. | U, B |
| 11 | SETTINGS tab shows "Firmware version unknown" | `Device.info` sends `firmwareVersion` as the raw list from 0x0003 (`model.py:134`, `:483`). The proto field is a string (`schema_defaults.py:764`). `model.format_firmware` already produces `RBM 22.01.B0006` but is only used for `/dfu/<id>/info`. | U, B |
| 12 | Device CHECK FOR UPDATE refused | `SET /dfu/<id>/check` is unhandled; `SET /dfu/config/update {"autoInstallEnabled": …}` too. The UI keeps saying "latest version" because it never learns otherwise. | U |
| 13 | Swap toggle snaps back with no message | Correct refusal (see hardware facts) but `NOT_SUPPORTED` carries the reason only in the reply; the UI shows nothing. | U, H |
| 14 | `card_presets_001.json` warning on every load | The file is an encrypted wrapper (magic `21 05 21 20`, key id header); `actions._read` tries to parse it as JSON. | B |
| 15 | Silent skip in `_profiles_set` | An entry whose slot has no target `continue`s (`router.py:549-550`); when nothing is handled the function returns None (`:578`) and the generic SET refusal answers. This is why defect 1 looked like "not implemented" instead of "unknown slot". | B |
| 16 | `agent.log` truncates payloads at 400 bytes | `ipc.py:152` and `:177`. Cost an hour of guessing during verify. | B |
| 17 | Reset leaves hardware wheel bits | `wheel_relay.release(reset=True)` (`wheel_relay.py:355-360`) writes the device invert and hi-res bits only when the wheel was diverted. | B |

## 3. Work items

Each item: files, the change, the TDD test, the live check, and the feasibility verdict.
Live checks use `dev/verify/live.sh` with the real depots (section 5). "Read-back" means
`dev/verify/readback.py` (new, item W0) printing the mouse's own state.

### W0. Harness: read-back tool and full IPC log

- Add `dev/verify/readback.py`: opens the hidraw node, prints DPI, SmartShift, wheel,
  thumb, control reporting for 0x50..0xd7 and host names (the script used during
  verify). `--save FILE` writes the values as JSON; `--restore FILE` writes them back
  (DPI, SmartShift mode and threshold, wheel hi-res and invert, thumb invert and
  smooth, undivert and unmap every control). Document it in
  `.claude/skills/verify/SKILL.md`.
- `ipc.py:152,177`: log the full payload when `OPL_IPC_LOG_FULL=1`, else the current
  400 bytes. `live.sh` sets it.
- No test needed beyond `test_socket.py` still passing. Verdict: trivial.

### W1. Routing: the routes the UI really uses

Files: `router.py:150-160`, `router.py:514-580`, twelve test files, `feature-parity.md`
§4.2 and §9.

1. Route `SET /v2/assignment` to `_profiles_set`. Keep `SET /v2/profiles/assignment`
   as an alias for one release so the tests and any script keep working, then migrate
   the tests to `/v2/assignment` (`sed` over `tests/*.py`; the payload shape is the same
   single-assignment form the tests already send).
2. Route `SET /device/restore` to `_reset_device(dev)` for the device named by
   `payload["id"]` (fall back to `deviceId`). Reply `SUCCESS` with the device info, then
   `_broadcast_profiles` and `/devices/state/changed`. Keep `/v2/profiles/reset`.
3. In `_profiles_set`, replace the silent `continue` for an unknown slot with
   `INVALID_ARG, "no such slot <id>"`, and make the "nothing handled" return an explicit
   error instead of None. Keep the None fallthrough only for payloads with no entries.
4. Fix the stale docstring at `router.py:7` (unknown SET is NOT_SUPPORTED, not SUCCESS).
5. `feature-parity.md` §4.2: replace "observe exact path with the card seeded" with the
   observed routes and payloads. §9: add `/device/restore`, and `GET /v2/defaults/slice`
   as "in the bundle, never sent in these runs, no action".

Tests: `test_pointer_speed.py` gains a case that sends the exact captured
`/v2/assignment` payload (scroll card as logged, with `dir: NATURAL`) and asserts the
fake mouse's 0x2121 invert bit. `test_socket.py` gains `SET /device/restore` with the
device info object and asserts DPI back to default and `profiles.json` emptied for that
device.

Live check: pointer slider click → `agent.log` shows `<- SET /v2/assignment`, a
0x2201 fn3 tx, `-> SET /v2/assignment SUCCESS` → read-back DPI changed. RESET TO
DEFAULT SETTINGS → YES → `<- SET /device/restore` → read-back DPI 1000, thumb not
inverted.

Verdict: will work. The alias was tried live and the thumb and DPI writes reached the
mouse (tier H). The reset route is tier U; the handler exists.

### W2. Scroll direction enum

Files: `profiles.py:120-175` (scroll), reuse `DIR_STANDARD`/`DIR_INVERTED` from `:179`;
tests `test_scroll_wheel.py` (lines 59-219) and `test_wheel_relay.py` (146-568).

- `scroll_card`: `settings["dir"] = DIR_INVERTED if wheel["invert"] else DIR_STANDARD`.
- `scroll_request`: accept `NATURAL`/`STANDARD`; keep accepting `INVERTED`/`NORMAL` from
  an old `profiles.json` and rewrite on save.
- Tests: replace the literals; add one case with the captured payload.

Live check: click Inverted → `dir: NATURAL` in the request, 0x2121 fn2 tx, read-back
`invert: True`, radio "Inverted" selected after reload. Click Standard → back.

Verdict: will work. The value the UI sends is captured (tier U) and the thumb wheel
already round-trips the same enum (tier H).

### W3. Pointer speed scale

Files: `profiles.py:85-111`, `router.py:347-351`, `test_pointer_speed.py`.

- The slider value is in [-1, 1]; the UI displays `(value + 1) / 2`. Map it piecewise
  around the sensor default so that 0.0 is the factory DPI:
  - `v < 0`: `dpi = default + v * (default - min)`
  - `v >= 0`: `dpi = default + v * (max - default)`
  - snap to the DPI list / step; inverse in `value_from_dpi`.
  - On this mouse: -0.46 → 632 → 650 DPI; 1000 ↔ 0.0 ↔ 50%; 8000 ↔ 1.0.
- `default` comes from 0x2201 fn2 (`get_dpi()["default"]`, 1000 here); when the device
  reports 0, use the midpoint of the range.
- Seed the stored card from the live DPI with the new inverse so the slider shows 50%
  on a factory mouse.
- No migration: the old scale never reached a user because the route was dead.

Tests: table-driven cases for the five points above with the fake mouse's range.

Live check: slider at 27% → read-back 650; drag to 50% → 1000; relaunch → slider at
the same percentage.

Verdict: will work for the mapping; the exact curve Logitech uses on Windows is not
known (tier D). The 0.0 ↔ default anchor is supported by the depot default
(`POINTER_SPEED value 0.0`) and the mouse's own default. If a user compares percentages
with Windows they may differ at the ends; acceptable and documented.

### W4. Action picker: compose `/rac` from Logitech's tables

Files: `actions.py:231-250`, `tests/fakes/depot.py:59-61`, `test_action_catalogue.py`.

Inputs in `data/rap/` (all `{"data": {app: {"infoList": [...]}}}`):

- `available_card_tags_slot.json`: slot id → allowed preset tags.
  For `mx-master-3s-2b034_c82`: 13 tags; `_thumb_wheel_adapter`: `PRESET_TAG_THUMBWHEEL`.
- `available_card_tags_control.json`: control id ("82") → tags, for devices without a
  slot entry.
- `recommendations_slot_win.json`, `recommendations_control_win.json`: slot or control
  → recommended card ids (8 for c82, 9 for c195, 5 for the thumb wheel).
- `card_presets_win.json`: the 270 preset cards with their `tags`.

Algorithm for `GET /rac {"deviceId"}`:

1. Resolve the device's `slot_prefix`; the default app id is
   `420fd454-0c36-499d-bde4-146823b16147` (`profiles.DEFAULT_PROFILE_ID`); other keys
   are application ids.
2. For each app in the slot-tags table (default first), for each slot that starts with
   the prefix: `cards` = presets whose tags intersect the slot's tags, passed through
   `_serve` (the Linux filter and icon rewrite). With the real data: 52 cards per
   button slot, 11 for the thumb wheel.
3. `recommendedCards` from the recommendations table for the same slots, resolved to
   full cards (all recommended ids exist in the presets).
4. Fall back to the control tables with `"<prefix>_c<cid>"` when a slot is missing.
5. Emit `RACContainer{availableCards:{racList:[{appId, slotCardList:[{slotId, cards}]}]}, recommendedCards:{…}}`.
6. Cache per (device, mtime); drop the dead static `/rac` in `responses.py:81`.
7. `_read` (`actions.py:146`): skip files whose first byte is not `{` or `[` without a
   warning (defect 14).

Tests: build the fake depot with a two-entry tag table and one recommendation; assert
the slot lists and that a Windows-only card is filtered.

Live check: click Forward → picker lists categories and cards; choose "Volume up" →
`<- SET /v2/assignment` with that card → 0x1b04 fn3 tx for 0x56 → read-back
`ctl 86 (True, False, 86)` → press the button → `evtest` on the virtual device shows
`KEY_VOLUMEUP`. Choose "Forward" again → control undiverted.

Verdict: likely to work; the highest-uncertainty item. The container shape is the
proto (tier B) and the UI's `racList → slotCardList → cards` walk is visible in the
bundle (tier B). Whether the picker needs more than `cards` (categories, `appSpecificCards`)
is only provable live, so this item carries a time-box: if the picker stays empty with
a well-formed container, capture the UI's redux `rac` slice (`st.js`,
`__PATH='rac'`) and adjust before touching anything else.

### W5. Gesture button works out of the box

Files: `engine.py:112-153` (compile), `:388-416` (`_desired`), `router.py:604-612`,
`keymap.py:45`, `test_gestures.py`, `test_button_remap.py`.

1. Modifier-only keystroke: when `code` is None and `modifiers` is non-empty, compile
   to a tap of the modifier keys (usage 227 → `KEY_LEFTMETA`). Then the depot's default
   gesture card compiles (checked offline: only `up` failed).
2. Divert policy: a default card is diverted when it resolves (through
   `selectedNestedCard`) to a gesture adapter (`gestures.kind` is `ADAPTER_4WAYS`).
   Plain default cards (middle, back, forward, mode-shift, thumb-wheel horizontal
   scroll) stay native; the mouse does those itself. Implement as a helper
   `profiles.needs_software(card)` used by both `_desired` and `_assign_button`.
3. Translate the Windows chords. The default card's nested actions are keystrokes for
   Windows: `left` = Ctrl+Win+Left, `right` = Ctrl+Win+Right, `down` = Win+D, `up` =
   Win alone; only `click` is a SYSTEM action (`TASK_VIEW`). Injected as-is, no Linux
   desktop binds them. `sysactions.py:92-95` already carries Linux chords for
   `SWITCH_BETWEEN_DESKTOPS_LEFT/RIGHT`, `SHOW_DESKTOP` and `TASK_VIEW`, and
   `actions.toml` lets the user retune them. Add a table keyed by inner card id, the way
   `actions.RELABEL_*` keys by id, that compiles `inner_card_vd_desktop_left/right` to
   `SWITCH_BETWEEN_DESKTOPS_LEFT/RIGHT`, `inner_card_vd_hide_show_desktop` to
   `SHOW_DESKTOP` and `inner_card_vd_start_menu` to the overview action. The general
   case stays open: a user-picked preset whose keystrokes are Windows chords injects
   them unchanged, because `actions._WINDOWS_ONLY` filters names and paths, not key
   chords. Document that in the README and leave a ticket.
4. Consequence to document: while the agent is not running, the gesture button is
   inert (diverted, nobody listening). `engine.sync` on start and `_hand_back` on
   shutdown already restore the device; `_apply_stored` on wake re-diverts.

Tests: compile the real default card shape (copy the structure, not Logitech's text)
and assert a `Gesture` binding whose `left` step is the `sysactions` chord; assert
`_desired` diverts 0xc3 with no stored card and does not divert 0x52.

Live check: fresh state → agent start → read-back `ctl 195 (True, True, 195)`;
hold the gesture button and swipe left → `evtest` on the virtual device shows the
`sysactions` chord for `SWITCH_BETWEEN_DESKTOPS_LEFT` (Super+PageUp by default) and
the desktop switches; tap → overview.

Verdict: will work with the translation in step 3. Divert with raw XY is possible on
0xc3 (tier H), the card compiles after step 1 (tier B), and the engine's gesture path
is exercised by `test_gestures.py`. The desktop reaction depends on the compositor's
shortcuts (tier D); the injected chord is what we verify.

### W6. Fetcher and data pins

Files: `fetch.py:300-341`, `:158-163`, `data/depots.json`, `tests/fakes/payloadkit.py:93`,
`test_fetcher*.py`, `README.md` (First launch section), `tools/verify-depot-urls.py`.

1. `collect_installer`: find the EOCD by scanning backwards from the end of the file
   (signature `PK\x05\x06`, then validate the central-directory offset and size), open
   `zipfile.ZipFile` on a bounded file view ending at `EOCD + 22 + comment length`.
   Reject files without a valid EOCD with the current message.
2. Check each extracted depot against the pinned manifest `sha256` too, not only the
   installer's `mac`. Mismatch: "checksum mismatch for <name>; nothing was installed".
3. `eula_text(manifest, source)`: "It will be read from <installer>" when
   `--from-installer`, sizes from `offline.json`.
4. `data/depots.json`: `eulaUrl` → `https://www.logitech.com/legal/eula.html`; add

   ```json
   {"name": "system_actions",
    "url": "https://updates.optionsplus.logitechg.com/depots/7e89b27a-323f-4026-93a1-3ef5d5387c15/system_actions.depot",
    "size": 159698,
    "sha256": "85bd8bdba43b61bc7e8341863e5d072518224c9f0defe1710cf6b7c3330445c3"},
   {"name": "mx_master_3s",
    "url": "https://updates.optionsplus.logitechg.com/depots/9c81feae-0ae7-4e8d-9e9c-ceddbfda8a81/mx_master_3s.depot",
    "size": 1606141,
    "sha256": "50803b06f23984ef26204c4c8018a274145ab52b94c8bafa9b8ab1a6d88ef521"}
   ```

   (values from `offline.json` of installer build 876310, URLs answered HTTP 200 with
   these sizes on 2026-10-20). Run `tools/verify-depot-urls.py` before the release.
   Update the `_comment`.
5. `SET /updates/depot/info`: answer `Depot.InstallStatus {depotName, state}`; do not
   download. Move it out of `_SET_ECHO`. Only `REQUESTED` has been observed; the
   state names for installed and missing depots are a guess (tier D) and need a live
   check of what the UI does with the reply.
6. README: EULA sentence, the three depots, total download size (554 MB).

Tests: `payloadkit.offline_installer` gains a `trailer=` parameter; one test installs
from an installer with 100 KiB of trailing bytes; one test fails on a manifest
mismatch while the `mac` matches.

Live check: `python3 -m opl_agent.fetch --from-installer <real exe> --accept-eula --force --no-gui`
installs three depots; a fresh `live.sh` shows the device page without CONFIGURING.

Verdict: will work. The trimmed copy already installed all three depots with matching
checksums (tier H).

### W7. Easy-Switch host OS

Files: `hidpp.py:972-992`, `easy_switch.py:26-42`, `test_easy_switch.py:32`.

- Read 0x1815 `getHostOsVersion` (function 7, not 5: function 5 is `moveHost` and 6 is
  `deleteHost`, both writes; see section 9). The reply is `[hostIndex, osType, major u8,
  minor u16 BE, patch u16 BE]`; map osType 1..9 to the proto `OSInfo.type` names. The proto enum
  names are not in `schema_defaults.py` (only the default `UNKNOWN`); the strings file
  gives the UI keys `OS_WINDOWS`, `OS_MACOS`, `OS_LINUX`, … Start with the bare names
  (`WINDOWS`, `MACOS`, `LINUX`, `ANDROID`, `IOS`, `CHROME`, `WEBOS`, `TIZEN`,
  `WINDOWS_EMBEDDED`) and verify the rendering live.
- When the type is unknown, omit `os` from the host entry instead of sending `UNKNOWN`,
  so the UI shows the bus type line only.
- The evidence says the UI looks the string up by the raw value: "Oops. Something went
  wrong" is exactly `en-US.yaml:219 UNKNOWN`. So `LINUX` would render
  `STRINGS['LINUX']` (probably empty) while `OS_LINUX` would render "Linux". The live
  check has to try both spellings; it is not a formality.
- Expect the Bolt-receiver host to report type 0 on Linux (nothing here ever calls
  `setHostOsVersion`), so the omit-when-unknown branch fires first on this machine; the
  two "xserve" hosts were paired from another OS and may carry a real type.

Tests: fake mouse answers function 7 with type 5 → payload `os.type` is the chosen spelling;
type 0 → no `os` key.

Live check: EASY-SWITCH tab shows "Linux" (or nothing) under each host instead of
"Oops. Something went wrong". Renaming is not offered by this UI build, so no write
check.

Verdict: likely to work; the enum spelling is tier D until rendered, with two
candidates to try. The omit-when-unknown fallback is safe either way.

### W8. Firmware string

Files: `model.py:134`, `test_socket.py:105`, `test_bluetooth_direct.py:96`.

- `"firmwareVersion": format_firmware(self.fw)` (main entity first: `RBM 22.01.B0006`).
- Tests assert the string.

Live check: SETTINGS tab shows "Firmware version RBM 22.01.B0006".

Verdict: will work (proto field is a string, tier B; the formatter exists).

Correction (section 9): the string alone was not enough. The SETTINGS page reads the
version from `activeInterfaces[]` (the entry of type `DEVIO`), not from the top-level
`firmwareVersion`, so the agent also sends one `DEVIO` interface carrying the string.

### W9. DFU routes

Files: `router.py:901-912`, `responses.py:69`, `updates.py`.

- `SET /dfu/<id>/check`: reply `SUCCESS` with `dfu.Info {dfuStatus: "UP_TO_DATE"?}` —
  the enum names are unknown; reply with an empty `dfu.Info` first, broadcast
  `/dfu/update/available` false, and verify the UI text live. Firmware update itself
  stays out of scope (ticket #15 note).
- `SET /dfu/config/update {"autoInstallEnabled"}`: persist in `updates.json`, echo.

Verdict: cosmetic; works as long as the UI accepts an empty Info (tier D, live check).

### W10. Swap feedback

Files: `router.py:457-470`, `swap.py`.

- After a refusal, broadcast `/mouse/global/swap {"state": "LEFT"}` so the toggle snaps
  back immediately, and send a desktop notification (`notify-send`, when present) with
  the reason from `SwapUnsupported`. Keep the reply code.

Verdict: works; the UI ignores reply messages (tier U), so the notification is the only
channel we control.

### W11. Reset writes the hardware wheel bits

Files: `wheel_relay.py:355-360`, `router.py:767-779`.

- `_apply_defaults` writes 0x2121 invert False and hi-res False regardless of the relay
  state, and thumb `invert False, smooth False`; DPI to the sensor default; SmartShift
  left as is (no depot default; `generate_settings: true`).

Test: reset with a non-diverted inverted wheel → fake mouse invert bit cleared.

Verdict: will work.

### W12. Docs and tickets

- `feature-parity.md`: §4.2 routes table, §9 disposition, the Bolt note stays.
- `README.md`: First launch section (three depots, EULA URL), Troubleshooting entry
  for "device stays on CONFIGURING".
- `.claude/skills/verify/SKILL.md`: `readback.py`, `OPL_IPC_LOG_FULL`, the gotcha that
  settings only count when the read-back changes.
- Tickets: comment on #9, #10, #11, #15, #16, #20, #23 with the defect and the commit;
  #4 for the installer fix and pins; #24 for the swap feedback.

## 4. Architecture issues seen during the study (out of scope, recommend tickets)

- `main._watch` (`main.py:58-82`) rescans every 2 s and `get_paired_devices` pings six
  slots with a 1 s timeout each; with empty slots that is up to 6 s of HID++ traffic per
  sweep per receiver. Rescan only on a node change or a HID++ 1.0 connect notification.
- `Device.button_feature` is set only by `Registry.controls()` (`model.py:1090`) or
  `set_divert`; 0x1b04 events before that are dropped (`model.py:608`). Set it in
  `_build`.
- `hidpp._route` (`hidpp.py:421`) tests the HID++ 1.0 sub-id range before the swid
  nibble; a feature index in 0x40..0x7f would be misrouted (theoretical).
- `Device.serial` is never assigned (`model.py:74`); `udid`, `deviceSerialNumber`
  and the Easy-Switch `serialNumber` are always empty.
- `connectionType` is always `BLE_PRO`, even for USB or corded devices (`model.py`).
- `/devices/list` and `/devices/<id>/info` ignore the verb.
- The state-dir resolver is duplicated (`router.py:32-36`, `updates.py:113-115`).
- `updates.json` stores `channel` but never reads it back.
- `hidpp.can_remap` does not check the target's `reprogrammable` flag.
- The default `ThreadPoolExecutor` has no `max_workers`; HID++ writes from overlapping
  UI requests serialize only through `_switch_lock` where it is held.

## 5. Live verification protocol (after W1..W6; repeat at the end)

Prerequisites: real depots in `OPL_DEPOT_DIR` (from the offline installer through the
fixed `--from-installer`, or the pinned URLs), fresh `OPL_STATE_DIR`,
`OPL_IPC_LOG_FULL=1`. Record the baseline with `readback.py` first (expected on this
mouse: DPI 1000; SmartShift ratchet threshold 10; wheel hires False invert False;
thumb mode 0 invert False smooth False; controls 0x50..0xd7 not diverted, no remap;
hosts unchanged).

1. Home → device card clickable (no CONFIGURING).
2. SETTINGS → "Firmware version RBM 22.01.B0006"; CHECK FOR UPDATE answers; swap
   toggle snaps back with a notification.
3. POINT AND SCROLL → Pointer speed 50% on a factory mouse; click 27% → read-back 650.
4. Scroll wheel → Inverted → read-back `invert True`; Standard → False; SmartShift off
   → read-back `freespin`; on → ratchet.
5. Thumb wheel → Inverted → read-back `invert True`; speed change → relay diverts
   (mode 1) and `evtest` shows horizontal wheel events at the new rate.
6. BUTTONS → Forward → picker populated → choose Volume up → read-back `ctl 86 diverted`
   → press → `KEY_VOLUMEUP` injected. Gesture button diverted by default; swipe left →
   desktop switch keys injected.
7. EASY-SWITCH → three hosts without "Oops".
8. RESET TO DEFAULT SETTINGS → YES → read-back equals the baseline; `profiles.json` has
   no entries for the device.
9. Kill and restart the agent → settings reapplied (read-back unchanged) → reset again.
10. Restore the baseline with `readback.py --restore` (new flag) and confirm.

Never switch hosts or pair/unpair devices during the run; never click LOGIN.

## 6. Feasibility verdict

| Item | Verdict | Why |
|---|---|---|
| W1 routing | will work | alias tried live; writes landed |
| W2 scroll enum | will work | value captured; thumb path proves the enum |
| W3 pointer scale | will work | anchors from the depot and the sensor; curve is ours |
| W4 action picker | likely; time-boxed | shape from proto and bundle; picker only provable live |
| W5 gesture default | will work | divert+rawXY possible on 0xc3; card compiles after the modifier fix |
| W6 fetcher + pins | will work | trimmed installer installed all depots; URLs answer 200 |
| W7 host OS | likely | enum spelling verified live; omit-when-unknown is safe |
| W8 firmware string | will work | proto string field |
| W9 DFU | cosmetic | needs one live render |
| W10 swap feedback | will work | notification path is ours |
| W11 reset bits | will work | existing write functions |

Overall: implementable in one integration branch, in the order W0, W1, W2, W3, W4, W5,
W6, W7, W8, W9, W10, W11, W12. W1 gates the live checks of every other item. Estimated
size: W1-W3 one session; W4-W5 one session with hardware; W6 one session; the rest
half a session. Commit per work item; do not push without approval; no Logitech bytes
in the repository (the fixtures copy structure, never Logitech's text or icons).

## 7. Ticket map

| Item | Tickets |
|---|---|
| W1, W2, W3 | #9, #10, #11 |
| W4 | #20, #22 |
| W5 | #21, #23 |
| W6 | #4, #7 |
| W7 | #16 |
| W8, W9 | #15, #6 |
| W10 | #24 |
| W11 | #10, #11 |
| Section 4 | new tickets: rescan cost, button_feature, serial, connectionType |

## 8. Evidence files (scratch, not in the repository)

Session scratchpad `verify/`: `run1/agent.log` (first run, no device depot),
`run2/agent.log` (device page, refusals), `run/agent.log` (alias probe and reset route,
full payloads), `baseline.txt`, screenshots `12-device.png` … `31-after-reset.png`,
`offx/offline.json` (installer manifest, build 876310), `plan/routes-in.txt`,
`plan/routes-reply.txt`, `plan/set-payloads.txt`, `plan/bundle-facts.txt`.

## 9. Corrections found during implementation

- W7: 0x1815 `getHostOsVersion` is function 7. The plan said function 5, which is
  `moveHost` (a write); function 6 is `deleteHost`. The function table, from the
  Logitech agent strings and OpenLogi PR #1824: 0 getFeatureInfos, 1 getHostInfo,
  2 getHostDescriptor, 3 getHostFriendlyName, 4 setHostFriendlyName, 5 moveHost,
  6 deleteHost, 7 getHostOsVersion, 8 setHostOsVersion. A test asserts the agent never
  sends functions 5, 6 or 8, and never writes 0x1814.
- W7: the OS type order is 0 UNKNOWN, 1 WINDOWS, 2 WINDOWS_EMBEDDED, 3 LINUX, 4 CHROME,
  5 ANDROID, 6 MACOS, 7 IOS, 8 WEBOS, 9 TIZEN. The test MX Master 3S reports 0 for all
  three hosts, so the live UI shows the bus type line only.
- W6: the install states are the proto values `COMPLETED` and `INVALID`, not
  `INSTALLED` / `NOT_INSTALLED`.
- W8: the SETTINGS page reads the firmware string from the `DEVIO` entry of
  `activeInterfaces`, not from `Device.Info.firmwareVersion`.
- W9: the UI clears "Checking for updates..." only when `/dfu/update/available`
  broadcasts a `devices.Device.Info` with the device `id`, its `state` and `fwUpdate`.
  A `BoolValue` broadcast leaves the spinner on.
- Profiles: the UI's UPDATE_PROFILES reducer applies `payload.updatedProfiles`
  (`profiles_v2.Update`) and ignores a `profiles` list. Broadcasting `{profiles}` made
  the hotspot labels lag one change behind.
