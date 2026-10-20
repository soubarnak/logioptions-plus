(() => {
  let fiber=null; for (const el of document.querySelectorAll('*')) { const k=Object.keys(el).find(k=>k.startsWith('__reactContainer$')||k.startsWith('__reactFiber$')); if(k){fiber=el[k];break;} }
  const stack=[fiber]; let store=null;
  while(stack.length){const n=stack.pop(); const p=n.memoizedProps; if(p&&p.store&&p.store.getState){store=p.store;break;} if(n.child)stack.push(n.child); if(n.sibling)stack.push(n.sibling);}
  window.__st = store;
  const st=store.getState(); const js=v=>v&&v.toJS?v.toJS():v;
  const sel = (globalThis.__PATH||'').split('.').filter(Boolean);
  let v = st; for (const k of sel) { v = js(v); v = v && v[k]; }
  v = js(v);
  if (!sel.length) return Object.keys(st).join(' ');
  return JSON.stringify(v, (k,x)=> typeof x==='string' && x.length>150 ? x.slice(0,80)+'…' : x, 1).slice(0, 6000);
})()
