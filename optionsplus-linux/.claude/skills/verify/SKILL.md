---
name: verify
description: Run the native agent with the real Options+ UI on electron35 and drive it over CDP to verify behavior end to end.
---

# Verify optionsplus-linux at runtime

The surface is the real Options+ Electron UI talking to `opl_agent` over the
`/tmp/logitech_kiros_agent-<md5(user)>` socket, and the mouse itself over hidraw.

## Prerequisites (outside the repo, never commit them)

- Logitech payload unpacked from the offline installer (`tools/undepot.py`):
  - `OPL_ASAR`: the frontend `app.asar`
  - `OPL_DATA_DIR`: the `logioptionsplus` depot's `data/` directory
  - `OPL_DEPOT_DIR`: one directory per device depot (for example `mx_master_3s/`)
- `electron35`, `node` (for the CDP drivers), read/write access to the receiver's hidraw node.

## Launch

```bash
OPL_ASAR=... OPL_DATA_DIR=... OPL_DEPOT_DIR=... OPL_SHOT=/some/dir/shot.png dev/verify/live.sh &
```

- A fresh `mktemp` state dir is used unless `OPL_STATE_DIR` is set. A fresh dir exercises first run.
- Logs: `agent.log` (every IPC frame and HID++ tx/rx), `ui.log`, `pids`, all next to `OPL_SHOT`.
- Stop it with `kill $UI` from `pids`. The agent exits with the UI.

## Drive (from dev/verify, with OPL_SHOT exported)

- `node ev.mjs 9340 "<js expr>" [out.png]` evaluates in the renderer and optionally saves a screenshot.
  CDP `Page.captureScreenshot` times out on this app. `main.js` captures via
  `webContents.capturePage()` when `$OPL_SHOT.go` appears.
- `node mouse.mjs 9340 click X Y | key Escape [n] | type text | drag x1 y1 x2 y2` sends real input.
  Coordinates match the 960x860 screenshot.
- `click.js` clicks the visible element whose text matches `globalThis.__T` (or a CSS selector).
- `st.js` dumps the redux store; set `globalThis.__PATH='device.devices'` first.

## Flows worth driving

- Home card: `#device-list-device-item-anchor-image`. Device page tabs: BUTTONS, POINT AND SCROLL, SETTINGS.
- Hotspots: `#button-body-title-c82|c83|c86|c195|c196|thumb_wheel_adapter|mouse_settings`.
- Pointer speed: click the track in the side panel, then read the mouse's DPI with
  `hidpp.get_dpi(hidpp.open_node('/dev/hidrawN'), idx)` to check that it reached the hardware.
- App settings gear (891,77): theme and language persist to `configuration.json` in the state dir.

## Gotchas

- `OPL_UI_LOGGING=1` is set by `live.sh`, so App Settings shows "Data is being logged". This is a dev aid, not a bug.
- Unknown SETs return SUCCESS from the agent. A UI change that "sticks" proves nothing until the HID++ tx shows up in `agent.log` or a hardware read-back confirms it.
- Do not click LOGIN/CREATE ACCOUNT or Bluetooth pairing. Both leave the local machine.
