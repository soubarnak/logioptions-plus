# Logi Options+ 2.8 — reverse-engineering findings

Target: `logioptionsplus_installer.exe`
SHA-256 `ebdc8c4d9647a9d17652d2129563c4251d8c74adeee5d3dedca4551633437f7a`, 49,944,728 bytes.
Byte-identical size to the official online installer at
`https://download01.logi.com/web/ftp/pub/techsupport/optionsplus/logioptionsplus_installer.exe`.

All analysis so far is static. Nothing from Logitech has been executed.

## 1. The installer is only a bootstrapper

| Layer | What it is | Evidence |
|---|---|---|
| Outer stub | Native x64 PE (MSVC 14.38), version 2.8.1479. `RT_RCDATA` 101 holds a 49,057,944-byte payload; type 256 resources 102–121 are localized command-line help strings. | REA `inspect_pe_resources` ev_c74ae979… |
| `logioptionsplus_setup.exe` | .NET Framework 4.7.2 WPF app (ILRepack-merged), assembly 2.8.1479.0. | REA `inspect_managed_artifact` ev_7fc2fd9c… |
| Managed resources | `vc_redist.x64.exe`, `logi_crashpad_handler.exe`, `logi_installer_shared_optionsplus.dll` (native "Install Library", libcurl 8.12.1 + protobuf + xdelta), EULA / privacy HTML, GIFs, licensed BrownPro fonts. | managed resource page ev_bc0099db… |

The actual application is **not** in the online installer. The native install library
downloads it from Logitech's update "pipeline":

- Hosts (from the DLL): `https://updates.optionsplus.logitechg.com`, `https://pipeline.logitech.io`
  (`/pipeline/v2/access/`), plus staging, China and account endpoints.
- Update API paths: `/updates/channel`, `/updates/next/depots`, `/updates/depot/info`,
  `/updates/depot/content`, `/updates/depot/reinstall`, ...
- Protobuf schemas referenced: `logi/common_protocol/updates.proto`, `manifest.proto`,
  `depot_extensions.proto`. Keymaster / access-group logic gates depot access.
- appId `optionsplus5`, bundle id `com.logitech.logioptionsplus`.

## 2. Getting the real application

Logitech publishes an offline installer that embeds every depot:

- `https://download01.logi.com/web/ftp/pub/techsupport/optionsplus/logioptionsplus_installer_offline.exe`
- 724,292,760 bytes, SHA-256 `bcff70d8a90f2da24116821186b52dafda0e1ac61cb6f558d450b41e212d517a`
  (Last-Modified 2026-09-30; this URL always serves the latest build, so the checksum will drift).

The PE overlay is a plain ZIP (`7z x` works) containing 222 `*.depot` files and `offline.json`.
`offline.json` lists each depot with its server path and a `mac` field, which is the SHA-256
of the depot file.

**Stable, pinned URL for the main depot** (verified with HEAD, HTTP 200):

```
https://updates.optionsplus.logitechg.com/depots/c08bc392-923d-42ea-9511-5a5667b4d0fe/logioptionsplus.depot
size 552,459,832  sha256 baf9ecf05f322b42b0c85a2304e33038f929ee49579ed77be193f2983a715869
```

This is usable as a fixed `distfiles` + `checksum` in a Void template. Version 2.8.981479,
buildId 876310, branch `staging_2.8`.

### Depot container format (`tools/undepot.py`)

```
u8[4]  magic            10 01 17 20 for the main app depot
u32 LE index_len
u8[index_len]          JSON {"files":[{"name":..., "mode":...}, ...]}
repeat for each file in index order:
    u32 LE size
    u8[size]  file bytes
```

Device-asset depots (UUID names) use a different header (`06 05 21 20`, JSON with
`header-sha`) and are not decoded yet.

## 3. What the application is made of

`logioptionsplus.depot` → 705 files:

- `logioptionsplus.exe` — Electron **39.1.2** frontend; UI in `resources/app.asar`
  (React/Redux, webpack; `app.min.js` is javascript-obfuscator string-array protected,
  `main.js` and `preload.bundle.js` are only minified). `tools/unasar.py` extracts it.
- `logioptionsplus_agent.exe` — Qt5 C++ backend that talks to devices. Uses `HID.DLL` +
  `SETUPAPI` (`HidD_GetFeature/SetFeature/GetInputReport`, `SetupDi*`), `WINUSB`,
  Windows `BluetoothGATT*` APIs, `SendInput` for synthetic input, Media Foundation (webcams),
  UIAutomation, d2d1/dwrite (overlay). Log strings show a full HID++ 2.0 stack
  (`feature_by_id_async ... feature_id=0x%04x`).
- `logioptionsplus_appbroker.exe`, `logioptionsplus_updater.exe`, crashpad.
- `data/` — device DB (`devices.json`, `devices/`, `DeviceCompatibility.json`), action
  presets, 6.3 MB of strings, firmware public keys.
- `platforms/qwindows.dll` — Qt platform plugin, Windows only.

## 4. UI ↔ agent IPC (the key seam)

`main.js` builds the socket path as

```js
name = "logitech_kiros_agent";            // also "logitech_kiros_logivoice"
hash = MD5(os.userInfo().username)
win  ? `\\.\pipe\${name}-${hash}` : `/tmp/${name}-${hash}`
```

So on any non-Windows platform the UI connects to a **Unix domain socket**
`/tmp/logitech_kiros_agent-<md5(username)>`. The macOS build already uses this path,
so the frontend is portable as-is.

### Wire framing (agent connection)

Outbound from UI (`send`):

```
u32 LE total = len("json") + len(payload) + 8      <-- written LITTLE-endian
u32 BE proto_len = 4
"json"
u32 BE payload_len
payload (UTF-8 JSON)
```

Inbound to UI (`network.init` state machine): reads the first u32 as **big-endian and
discards it**, then `proto_len` BE, protocol string (must be `json`), `payload_len` BE,
payload. An agent implementation must therefore parse the first word of incoming frames
as LE and may write anything there (write the correct length, BE is safest for the UI).

Messages are JSON with a `msgId` (parsed with `parseInt(...,10)`), a verb
(GET / SET / REMOVE; SUBSCRIBE is handled via `register_subscription`) and a `path`.

A second connection to the plugin service (`LogiPluginService`) uses a different framing:
16-byte header with signature `"LogiConn"` (hex `4C6F6769436F6E6E`), version 1,
u32 LE payload size, payload, then u64 LE hash of the payload.

### Agent route inventory

`re/agent_routes.txt` — 875 distinct route paths extracted from the agent binary
(`re/agent_route_namespaces.txt` for counts). Largest namespaces: `lps` 123, `devices` 82,
`logivoice` 40, `updates` 38, `uvc` 31, `api` 29, `macros` 25, `applications` 17,
`accounts` 17, `lighting` 15, `haptics` 14, `dfu` 14, `backlight` 13, `mouse` 11,
`battery` 9, `onboard_profiles` 9, `flow` 9.

Which subset the UI needs to boot and show a device cannot be recovered statically
(obfuscated `app.min.js`). It has to be observed from a running agent.

## 5. Host facts (this machine)

- Logitech Bolt receiver `046d:c548` on `/dev/hidraw1`–`hidraw4`.
- `/dev/hidraw*` is `crw------- root` — any Linux path needs a udev rule
  (`TAG+="uaccess"` or `GROUP="input"`; user is in `input`).
- Void repos have `electron35` (35.7.2; app was built for 39.1.2), `wine-11.20`,
  `cross-x86_64-w64-mingw32`, `Solaar-1.1.20`, `logiops-0.3.5`. None installed except socat.

## 6. Port options for Void Linux

1. **Wine hybrid** — agent (`logioptionsplus_agent.exe`) under Wine 11 with winebus hidraw
   backend; Electron UI native on Linux using the asar; a small mingw bridge relays the
   Wine named pipe `\\.\pipe\logitech_kiros_agent-<md5>` to TCP, `socat` relays TCP to
   `/tmp/logitech_kiros_agent-<md5>`. Unknowns: whether winebus exposes the Bolt vendor HID++
   collection, whether the agent survives without WinRT/MF/UIAutomation/BluetoothGATT,
   Electron 35 vs 39 compatibility.
2. **Native Linux agent** — clean-room daemon implementing the captured JSON route subset
   over HID++ 2.0 on hidraw, with the real UI on top. Needs a traffic capture from option 1
   first to learn the required routes and payload shapes.
3. **Existing tools** — Solaar / logiops from Void repos.

Licensing constraint on every path: Logitech binaries, asar, device DB and fonts must not
be redistributed. A Void template can only fetch them from Logitech's URL at build time
(`restricted=yes`, like other nonfree templates).

## Tools

- `tools/undepot.py <file.depot> <outdir>` — unpack main-app depot format.
- `tools/unasar.py <app.asar> <outdir>` — unpack Electron ASAR.

## 7. Native agent built and validated (2026-10-20)

Took port option 2 (native Linux agent). No Wine capture run was needed in the end: the
full protocol schema was recovered statically, so routes and payload shapes came from the
schema plus the one saved boot capture, not from a live Wine run.

### Protocol schema recovered
- `tools/protodump.py <agent.exe> re/proto` recovered **82 `.proto` files** (the agent's
  embedded `FileDescriptorProto` set) under `re/proto/` — `logi/protocol/*.proto` (devices,
  mouse, wireless, backlight, lps, pipl, app_permissions, application, ...) plus
  `google/protobuf/*`. This is the authoritative, complete message schema.
- IPC framing confirmed: on connect the agent sends one **protobuf** (not json) frame =
  `Message{verb=OPTIONS(7), path="/", origin="backend"}` as a "backend ready" greeting
  (`\x10\x07\x1a\x01/\x22\x07backend`); all subsequent request/reply traffic is the `json`
  sub-protocol.

### Agent implementation — `optionsplus-linux/opl_agent/`
- `hidpp.py` — dependency-free HID++ 2.0 over hidraw. Discovery by report-descriptor
  (usage pages 0xFF00/0xFF43), root ping/getFeature, FeatureSet, DeviceNameAndType,
  UnifiedBattery 0x1004 / BatteryStatus 0x1000, AdjustableDPI 0x2201, SmartShift
  0x2110/0x2111, Bolt/Unifying paired-device enumeration. **Verified on real hardware**:
  Bolt `046d:c548` on `/dev/hidraw3`, MX Master 3S at device index 2, HID++ 4.5,
  battery 85% discharging, DPI 1000, SmartShift ratchet. Bolt pairing register 0xB5/0x5n
  layout: `[0]` echo, `[1]` kind, `[2:4]` wpid little-endian, `[4:8]` unit id (MX Master 3S
  reads `51 42 34b0 43ccd316`, so wpid `b034`). The catalogue lists both `046d_b034` and
  `046d_b043` as MX Master 3S, so a wrong byte order still matched a real entry.
- `responses.py` — static boot-route table; payloads are schema-default functional values
  (all permissions granted, en-US, privacy consent DISABLED, LPS endpoint LOADED), **not**
  copied vendor config. `re/boot_routes_ref.txt` is the distilled route/type reference.
- `model.py` — turns live HID++ discovery into `Device.Info` / `DevicePermanentData` /
  `wireless.Battery` payloads; battery cache + poller.
- `router.py` — boot table, device routes (`/devices/list`, `/devices/<id>/info`,
  `/battery/<id>/state`), `initial_state()` on-subscribe pushes. `OPL_REPLAY=<json>` is a
  dev switch to replay exact captured payloads.
- `main.py` — wires IPC server + registry + router + battery poller.

End-to-end over the real IPC socket the agent answers the full boot burst SUCCESS and
reports the real mouse + battery (verified with a Python client and with the real
Options+ renderer on system `electron35`).

### UI splash blocker (solved) and device page

The unmodified Windows `app.asar` on native `electron35` now boots past the splash, shows the
home page, lists the MX Master 3S with its Logitech artwork, and opens the device page with
working button hotspots. The earlier conclusion ("not a protocol-reply issue, replay does not
fix it") was wrong: the replay test ran while the reply-id bug below was present.

Causes, in the order they were found:

1. **Reply id field.** The UI sends `msg_id` (snake_case). The agent must answer with
   `msgId` holding the same value. The renderer does `parseInt(msgId)` and looks the reply
   up in its pending-call table, so an empty `msgId` left every call pending.
2. **BROADCAST frames must carry no `result`.** The renderer routes any frame that has a
   `result` (or is not a BROADCAST) by `msgId` to the pending-call table. Only a
   result-less BROADCAST reaches the handler subscribed on its path.
3. **Uncaught SET rejections cancel the saga tree.** Several boot sagas `call()` a SET
   without a try/catch (`/applications/scan/installed`,
   `/lps/start_library_package_update_check`). One rejected call cancels the whole
   redux-saga root ("The above error occurred in task iterator"). The agent now accepts
   unknown SETs with SUCCESS.
4. **proto3 default fields.** The real agent prints every scalar/repeated field with its
   default value. The UI uses `exists(x.enabled)` checks, so a missing `false` counts as
   absent. `tools/protodefaults.py` generates `opl_agent/schema_defaults.py` from
   `re/proto`, and `opl_agent/defaults.py` fills defaults into every reply by `@type`.
5. **Common strings.** Splash step `resources` waits for `isCommonStringAvailable`, set from
   `GET /resources/localized_packages` when `packages[0]` has `available` and `strings` for
   `logioptionsplus/data/strings`. Served from the `logioptionsplus` depot
   `data/strings/<lang>.yaml` (19 languages; read with PyYAML `BaseLoader`).
6. **Telemetry opt-in.** `/crash_reporting/status` and `/scarif/status` answer
   `enabled false, userPrompted true`, so the opt-in page is skipped and telemetry stays off.

Device card and device page:

- **Catalogue identity.** `Device.Info` is enriched from
  `logioptionsplus/data/devices/devices_initial.json`, matched on interface id
  `046d_<wpid>`: `modelId`/`deviceModel` `2b034`, `depot` `mx_master_3s`, `slotPrefix`
  `mx-master-3s-2b034`, display names, and `thumbnail`. The other `devices_*.json` files
  are not plain JSON.
- **`pipeline://<depot>/<path>`** has no handler in the renderer; the agent resolves it to a
  `file://` URL inside the unpacked depot.
- **`optionsDevice` must be false.** `optionsDevice || unsupportedInKiros` marks a legacy
  Logi Options device, and the card shows "NOT SUPPORTED YET".
- **Device resources.** `saga::fetchDeviceResources` runs only when `resourcesAvailable` is
  true. It sends `GET /devices/resources {id, deviceModel}` and expects
  `devices.Resources{resources:[{key, contentType, url}]}` built from the depot
  `manifest.json` entry for the model. `image_metadata` is read with a synchronous XHR and
  `onboarding_data` with `fetch`, so JSON resources go out as `data:` URIs and images as
  `file://`. The same saga waits on `GET /applications/predefined/installed` and `GET /rac`;
  any rejection gives resourceState RETRY ("Download failed / Check network").
- **Home-list gate.** The home page keeps a device only if
  `profileReadyDeviceList[slotPrefix] > 0`: the number of assignments in the default profile
  (`420fd454-0c36-499d-bde4-146823b16147`) whose `slotId.split('_')[0]` equals the
  slotPrefix. `GET /v2/profiles` now returns that profile with assignments
  `<slotPrefix>_c<controlId>` (and `_thumb_wheel_adapter`) built from the depot's
  `default_configurations.json` (`control_id_mappings`, `mouseThumbWheelAction`). Devices
  outside the catalogue keep a `046d_<pid>` slotPrefix and cannot pass this gate.

Runtime data layout: `OPL_DEPOT_DIR` (default `/usr/lib/optionsplus-linux/logitech/depots`)
holds one directory per unpacked depot; `logioptionsplus/data` is the main data directory
(override `OPL_DATA_DIR`). Depot files in the offline installer are named `<name>.depot`
and match `offline.json` entries by size and SHA-256 (`mac`).

Dev aids: `OPL_UI_LOGGING=1` makes `/logging/optionsplus_backend/status` report
`enabledByCfg`, which keeps the renderer's console logging on (it switches itself off
otherwise). Renderer logging also needs `logi_features.cfg` with `logging_enabled=true` in
`~/.config/logioptionsplus/`.

Open items: button remapping is display-only (assignments are not yet applied to the device
over HID++ 0x1b04); POINT AND SCROLL and SETTINGS tabs are untested; the xbps template and
udev rule are not written yet.
