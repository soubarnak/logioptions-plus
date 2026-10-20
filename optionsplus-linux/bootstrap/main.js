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

// Every external link goes through shell.openExternal in the main process; apply the
// link policy (links.js) before the frontend loads, so app.asar stays unmodified.
// OPL_LINK_LOG=<file> appends "<requested>\t<opened|blocked>\t<url>" (live verification).
const { shell, dialog } = require('electron');
const { rewrite } = require('./links');
const realOpenExternal = shell.openExternal.bind(shell);
shell.openExternal = async (url, ...rest) => {
  const d = rewrite(String(url));
  if (process.env.OPL_LINK_LOG) {
    try {
      require('fs').appendFileSync(process.env.OPL_LINK_LOG,
        `${url}\t${d.action === 'open' ? 'opened' : 'blocked'}\t${d.url || ''}\n`);
    } catch (_) { /* logging is best effort */ }
  }
  if (d.action === 'block') {
    dialog.showMessageBox({ type: 'info', message: d.message, buttons: ['OK'] }).catch(() => {});
    return;
  }
  return realOpenExternal(d.url, ...rest);
};

require(path.join(asar, 'main.js'));
