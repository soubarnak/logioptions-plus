# Device notes

Notes on how individual Logitech devices behave with Options+ for Linux. The device support link
in the app opens the section for your model. The feature-by-feature status is in the
[Supported devices table](../README.md#supported-devices); this page holds what does not fit a
table row.

To add or correct notes for your model, open a
[device support request](https://github.com/soubarnak/logioptions-plus/issues/new/choose) with the
model, the connection (Bolt, Unifying, Bluetooth, cable) and what you saw. A section's heading is
the model's name as the app shows it. The app turns that name into the link anchor by lowercasing
it and replacing each run of other characters with one hyphen, which matches GitHub's anchor only
for names made of letters, digits, hyphens and single spaces.

## MX Master 3S

The reference device, tried on a Logi Bolt receiver. Verified on hardware: discovery, the home
and device pages, the BUTTONS layout, pointer speed, scroll direction, thumb wheel direction,
battery level, the firmware version and the Easy-Switch host list. Button remapping, gesture swipes,
SmartShift writes, host switching and per-application profiles are implemented and tested against
a simulated mouse, but not yet tried on the mouse itself. Firmware updates are not supported. See
the [table](../README.md#supported-devices) for the status of each feature.

## Logi Bolt USB-A receiver

The receiver the reference MX Master 3S was tried on. Discovery and the device list through it are
verified; pairing a new device through it is implemented but not yet tried on hardware.
