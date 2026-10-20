// usage: node ev.mjs <port> <js-file-or-expr> [shot-out]
import fs from 'node:fs';
const [port, src, shot] = process.argv.slice(2);
const expr = fs.existsSync(src) ? fs.readFileSync(src, 'utf8') : src;
const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const page = targets.find(t => t.type === 'page' && t.url.includes('index.html')) || targets.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const call = (m, p = {}, to = 60000) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: p })); setTimeout(() => { if (pending.has(i)) { pending.delete(i); r({ timeout: m }); } }, to); });
ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result || m.error); pending.delete(m.id); } };
await new Promise(r => ws.onopen = r);
const r = await call('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
const v = r.result ? r.result.value : r;
console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 1));
ws.close();
if (shot) {
  const S = (process.env.OPL_SHOT || '/tmp/opl-shot.png') + '.go';
  fs.writeFileSync(S, shot);
  for (let n = 0; n < 40 && fs.existsSync(S); n++) await new Promise(r => setTimeout(r, 250));
  await new Promise(r => setTimeout(r, 500));
  console.log(fs.existsSync(shot) ? 'shot ' + shot : 'shot missing');
}
process.exit(0);
