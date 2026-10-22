# Changelog

All notable changes to Options+ for Linux. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.2]

### Added

- A second application-menu entry, "Options+ (X11)", that starts the UI with
  `OPL_OZONE_PLATFORM=x11`. It is the quickest fix where the default window never appears, with no
  configuration to edit.

## [0.1.1]

### Added

- `OPL_OZONE_PLATFORM` (`auto`, `wayland` or `x11`) and the optional
  `~/.config/optionsplus-linux/launcher.conf` select Electron's Ozone platform. The default is
  unchanged.

### Documentation

- Troubleshooting entry for the window that never appears on hybrid NVIDIA/AMD laptops under
  Wayland.

## [0.1.0]

First release: a native Linux agent that speaks HID++ to Logitech mice and serves the unmodified
Options+ UI (run on Electron 35), packaged as a signed XBPS repository for Void Linux. Covers device
discovery, button remapping and system actions, pointer and wheel settings, battery, Easy-Switch,
per-application profiles, pairing, and a first-launch download of Logitech's own UI data.
