// usage: node mouse.mjs <port> click x y | key <Key> [n] | drag x1 y1 x2 y2 | type text
const [port, op, ...a] = process.argv.slice(2);
const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const page = targets.find(t => t.type === 'page' && t.url.includes('index.html'));
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const call = (m, p = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: p })); setTimeout(() => { if (pending.has(i)) { pending.delete(i); r({ timeout: m }); } }, 5000); });
ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result || m.error); pending.delete(m.id); } };
await new Promise(r => ws.onopen = r);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const M = (type, x, y, extra = {}) => call('Input.dispatchMouseEvent', { type, x: +x, y: +y, button: 'left', clickCount: 1, ...extra });
if (op === 'click') { await M('mouseMoved', a[0], a[1]); await M('mousePressed', a[0], a[1]); await sleep(60); await M('mouseReleased', a[0], a[1]); }
if (op === 'drag') { await M('mousePressed', a[0], a[1]); for (let k = 1; k <= 10; k++) { await M('mouseMoved', +a[0] + (a[2] - a[0]) * k / 10, +a[1] + (a[3] - a[1]) * k / 10, { buttons: 1 }); await sleep(30); } await M('mouseReleased', a[2], a[3]); }
if (op === 'key') { for (let n = 0; n < (+a[1] || 1); n++) { await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: a[0], code: a[0], windowsVirtualKeyCode: { ArrowRight: 39, ArrowLeft: 37, Escape: 27, Enter: 13, Tab: 9 }[a[0]] || 0 }); await call('Input.dispatchKeyEvent', { type: 'keyUp', key: a[0], code: a[0], windowsVirtualKeyCode: { ArrowRight: 39, ArrowLeft: 37, Escape: 27, Enter: 13, Tab: 9 }[a[0]] || 0 }); await sleep(80); } }
if (op === 'type') await call('Input.insertText', { text: a.join(' ') });
console.log(op, 'done'); ws.close(); process.exit(0);
