// globalThis.__T = regex text or '#id'; clicks leaf element whose text matches
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const t = globalThis.__T; let el;
  if (t.startsWith('#') || t.startsWith('[') || t.startsWith('.')) el = document.querySelector(t);
  else { const re = new RegExp('^\\s*(' + t + ')\\s*$', 'i'); el = [...document.querySelectorAll('button,a,[role=button],[role=tab],span,div,p,li,label')].reverse().find(e => re.test(e.innerText || '') && e.offsetParent !== null); }
  if (!el) return 'NOT FOUND: ' + t;
  el.scrollIntoView({block:'center'}); el.click(); await sleep(globalThis.__W || 2500);
  return 'clicked <' + el.tagName + ' id=' + el.id + '> ; now: ' + (document.body.innerText || '').replace(/\n+/g, ' / ').slice(0, 900);
})()
