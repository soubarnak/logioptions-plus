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
const { rewrite, isBlockedLoad, NOT_AVAILABLE_MESSAGE } = require('./links');
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

// The UI also opens cloud pages in its own windows (the marketplace button sends IPC
// OPEN_MARKETPLACE and the main process loads the URL in a new BrowserWindow), which
// never touches shell.openExternal. Stop those loads at the network layer instead.
// webRequest is per session and a webview may use its own partition, so hook every
// session as it is created. Each session accepts a single onBeforeRequest listener, so
// wrap the setter: a listener the frontend installs later runs after ours instead of
// replacing it.
const { BrowserWindow } = require('electron');
const { guardWebRequest, isOwnPage } = require('./guard');
let lastNotice = 0;
const notifyBlocked = () => {
  const now = Date.now();
  if (now - lastNotice < 3000) return; // one page makes several requests
  lastNotice = now;
  dialog.showMessageBox({ type: 'info', message: NOT_AVAILABLE_MESSAGE, buttons: ['OK'] }).catch(() => {});
};
const logLoad = (url, verdict) => {
  if (!process.env.OPL_LINK_LOG) return;
  try { require('fs').appendFileSync(process.env.OPL_LINK_LOG, `${url}\t${verdict}\t\n`); } catch (_) { /* best effort */ }
};
// Only a blocked main-frame load closes the window (a blocked subframe is just cancelled),
// and never the UI's own window: that is the one showing the app's file:// page.
function blockLoad(url, contents, mainFrame) {
  logLoad(url, 'blocked-load');
  notifyBlocked();
  if (!mainFrame) return;
  try {
    const win = contents && BrowserWindow.fromWebContents(contents.hostWebContents || contents);
    if (win && !win.isDestroyed() && !isOwnPage(win.webContents.getURL())) {
      setImmediate(() => { if (!win.isDestroyed()) win.close(); });
    }
  } catch (_) { /* the window is already gone */ }
}

const guarded = new WeakSet();
function guardSession(ses) {
  if (!ses || guarded.has(ses)) return;
  guarded.add(ses);
  guardWebRequest(ses.webRequest, {
    isBlockedLoad,
    onBlocked: (details) => blockLoad(details.url, details.webContents, details.resourceType === 'mainFrame'),
  });
}

app.on('session-created', guardSession);
app.on('web-contents-created', (_e, contents) => {
  guardSession(contents.session); // covers sessions created before this handler ran
  // Belt and braces for navigations the network hook would see too late to close the window.
  const guard = (event, url) => {
    if (isBlockedLoad(url)) { event.preventDefault(); blockLoad(url, contents, event.isMainFrame !== false); }
  };
  contents.on('will-navigate', guard);
  contents.on('will-redirect', guard);
});
app.whenReady().then(() => guardSession(require('electron').session.defaultSession));

require(path.join(asar, 'main.js'));
