// Dev helper: reload the frontend and dump renderer console output for N seconds.
// usage: node cdp-console.mjs <port> <seconds> [filter-regex]
const [port, secs, filt] = process.argv.slice(2);
const re = filt ? new RegExp(filt) : null;
const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const page = targets.find(t => t.type === 'page' && t.url.includes('index.html'));
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0;
const send = (method, params = {}) => ws.send(JSON.stringify({ id: ++id, method, params }));
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.method === 'Runtime.consoleAPICalled') {
    const txt = m.params.args.map(a => a.value !== undefined ? (typeof a.value === 'string' ? a.value : JSON.stringify(a.value)) : (a.description || a.type)).join(' ');
    if (!re || re.test(txt)) console.log(`[${m.params.type}] ${txt.slice(0, 600)}`);
  } else if (m.method === 'Runtime.exceptionThrown') {
    console.log('[exception]', JSON.stringify(m.params.exceptionDetails).slice(0, 800));
  }
};
await new Promise(r => ws.onopen = r);
send('Runtime.enable');
send('Page.enable');
send('Page.reload', { ignoreCache: true });
setTimeout(() => { ws.close(); process.exit(0); }, Number(secs) * 1000);
