// Dev helper: screenshot / inspect the running frontend over the Chrome DevTools Protocol.
// usage: node cdp.mjs <port> <out.png> [js-expression]
import fs from 'node:fs';
const [port, out, expr] = process.argv.slice(2);
const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const page = targets.find(t => t.type === 'page' && t.url.includes('index.html')) || targets.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const call = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result || m.error); pending.delete(m.id); } };
await new Promise(r => ws.onopen = r);
const shot = await call('Page.captureScreenshot', { format: 'png' });
if (shot.data) fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
const r = await call('Runtime.evaluate', { expression: expr || 'document.body.innerText.slice(0,2000)', returnByValue: true });
console.log(JSON.stringify(r.result ? r.result.value : r, null, 1));
ws.close();
