// Bootstrap for running the unmodified Logi Options+ frontend (app.asar) on a
// system Electron. The stock `electron` binary makes app.isPackaged false, which
// switches the frontend into developer mode (loads http://localhost:8080).
'use strict';
const path = require('path');
const { app } = require('electron');

const asar = process.env.OPL_ASAR || '/usr/lib/optionsplus-linux/app.asar';

Object.defineProperty(app, 'isPackaged', { get: () => true, configurable: true });
app.setName('logioptionsplus');
app.setAppUserModelId && app.setAppUserModelId('com.logitech.logioptionsplus');

// Keep Chromium flags sane on Wayland/X11 alike.
app.commandLine.appendSwitch('ozone-platform-hint', 'auto');

require(path.join(asar, 'main.js'));
