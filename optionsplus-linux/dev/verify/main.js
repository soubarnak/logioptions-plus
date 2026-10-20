'use strict';
const { app, webContents, ipcMain } = require('electron');
const t0 = Date.now();
const L = (...a) => console.log('[TRACE +' + (Date.now() - t0) + 'ms]', ...a);
app.on('web-contents-created', (_e, wc) => {
  const orig = wc.send.bind(wc);
  wc.send = (ch, ...args) => {
    if (!/ON_MESSAGE/.test(ch)) L('main->renderer', ch, JSON.stringify(args).slice(0, 200));
    else L('main->renderer', ch, JSON.stringify(args).slice(0, 120));
    return orig(ch, ...args);
  };
  wc.on('did-start-loading', () => L('did-start-loading', wc.getURL()));
  wc.on('console-message', (e, level, message) => { const msg = (e && e.message) || message; L('console', String(msg).replace(/%c/g,'').slice(0, 500)); });
  if (process.env.OPL_DBG) { try { wc.debugger.attach('1.3'); wc.debugger.sendCommand('Runtime.enable');
    const fmt = o => o ? (o.value !== undefined ? JSON.stringify(o.value) : (o.preview ? '{' + o.preview.properties.map(p => p.name + ':' + (p.value || (p.valuePreview && JSON.stringify(p.valuePreview.properties.map(q=>q.name+'='+q.value))))).join(', ') + '}' : (o.description || o.type))) : '';
    wc.debugger.on('message', (_e, m, p) => {
      if (m === 'Runtime.consoleAPICalled' && (p.type === 'error' || p.type === 'warning' || /error|exception/i.test(JSON.stringify(p.args).slice(0,300)))) L('CDP', p.type, p.args.map(fmt).join(' | ').replace(/%c|font-weight[^;]*;|color: gray;/g,'').slice(0, 1500));
      if (m === 'Runtime.exceptionThrown') L('EXC', JSON.stringify(p.exceptionDetails).slice(0, 1500));
    }); } catch (e) { L('dbg fail', e.message); } }
  wc.on('did-finish-load', () => L('did-finish-load', wc.getURL()));
  if (process.env.OPL_SHOT && !wc.__shot) {
    wc.__shot = true;
    // poll a trigger file: the CDP probe cannot write files, the harness can
    const fs = require('fs');
    const t = setInterval(async () => {
      const go = process.env.OPL_SHOT + '.go';
      if (wc.isDestroyed()) { clearInterval(t); return; }
      if (!fs.existsSync(go) || wc.getURL().indexOf('index.html') < 0) return;
      let out = fs.readFileSync(go, 'utf8').trim() || process.env.OPL_SHOT; fs.unlinkSync(go);
      try { const img = await wc.capturePage(); fs.writeFileSync(out, img.toPNG()); L('shot', out); }
      catch (e) { L('shot failed', String(e)); }
    }, 300);
  }
  wc.on('ipc-message', (_e, ch, ...a) => L('renderer->main', ch, JSON.stringify(a).slice(0, 160)));
});
require(require('path').join(__dirname, '..', '..', 'bootstrap', 'main.js'));
