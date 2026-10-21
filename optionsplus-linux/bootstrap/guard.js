// Request guard for the UI's own sessions. No electron import here so the logic can be
// tested with a fake webRequest object.
'use strict';

// Every host isCloudHost() (links.js) can block lives under one of these domains.
const OURS_URLS = [
  '*://*.logi.com/*', '*://logi.com/*',
  '*://*.logitech.com/*', '*://logitech.com/*',
];

const escapeRe = (s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&');

// Electron match pattern: <all_urls> or scheme://host/path. "*" in the scheme means
// http/https/ws/wss, "*." in the host means any subdomain (or the bare domain), and
// "*" in the path matches anything.
function matchPattern(pattern, raw) {
  if (pattern === '<all_urls>') return /^(https?|wss?|ftp|file):/i.test(raw);
  const m = /^([^:]+):\/\/([^/]*)(\/.*)?$/.exec(pattern);
  if (!m) return false;
  let u;
  try { u = new URL(raw); } catch { return false; }
  const scheme = u.protocol.slice(0, -1);
  if (m[1] === '*' ? !/^(https?|wss?)$/.test(scheme) : m[1].toLowerCase() !== scheme) return false;
  const host = u.hostname.toLowerCase();
  const patHost = m[2].toLowerCase();
  const [h, port] = patHost.split(/:(?=[^:]*$)/);
  if (port !== undefined && port !== '*' && port !== (u.port || '')) return false;
  if (h === '*') { /* any host */ }
  else if (h.startsWith('*.')) { if (host !== h.slice(2) && !host.endsWith(h.slice(1))) return false; }
  else if (h !== host) return false;
  const pathRe = new RegExp('^' + (m[3] || '/*').split('*').map(escapeRe).join('.*') + '$');
  return pathRe.test(u.pathname + u.search);
}

// webRequest filter ({ urls, types, excludeUrls }) against request details. A missing
// filter, or one without urls, matches everything.
function matchesFilter(filter, details) {
  if (!filter) return true;
  const { urls, types, excludeUrls } = filter;
  if (urls && urls.length && !urls.some((p) => matchPattern(p, details.url))) return false;
  if (excludeUrls && excludeUrls.some((p) => matchPattern(p, details.url))) return false;
  if (types && types.length && !types.includes(details.resourceType)) return false;
  return true;
}

// Is this the app's own UI page (what the main window shows)?
const isOwnPage = (url) => typeof url === 'string' && url.startsWith('file://');

// Wrap webRequest.onBeforeRequest so ours runs first and the UI's own (filtered) listener
// still works. Each session accepts a single listener, so the setter is replaced: a
// listener installed later is kept aside and called after ours instead of replacing it.
// onBlocked(details) runs for every blocked load; ours then cancels the request.
function guardWebRequest(wr, { isBlockedLoad, onBlocked }) {
  const setNext = wr.onBeforeRequest.bind(wr);
  let theirs = null; // { filter, listener }
  const ours = (details, callback) => {
    if ((details.resourceType === 'mainFrame' || details.resourceType === 'subFrame') && isBlockedLoad(details.url)) {
      onBlocked(details);
      return callback({ cancel: true });
    }
    if (theirs && matchesFilter(theirs.filter, details)) return theirs.listener(details, callback);
    return callback({});
  };
  const install = () => {
    // Ours must see every cloud-host request; add their URLs, or go unfiltered with them.
    // Their types/excludeUrls are applied in `ours`, not here, since they would hide ours.
    if (!theirs) return setNext({ urls: OURS_URLS }, ours);
    const urls = theirs.filter && theirs.filter.urls;
    if (!urls || !urls.length) return setNext(ours);
    return setNext({ urls: [...new Set([...urls, ...OURS_URLS])] }, ours);
  };
  install();
  wr.onBeforeRequest = (...args) => {
    // (filter?, listener) or (null) to clear
    const listener = args.find((a) => typeof a === 'function');
    const filter = args.find((a) => a && typeof a === 'object');
    theirs = listener ? { filter: filter || null, listener } : null;
    install();
  };
}

// The UI builds the scroll wheel speed row with the class `hidden-on-windows` unless its main
// process answers IS_MAC with true, and its CSS hides that class (height 0, invisible). The agent
// does support scroll wheel speed (through the uinput relay), so show the row again. The rule is
// as narrow as the UI allows: only that row. The row keeps the bottom padding the hidden rule
// leaves it, which is the spacing between rows (not confirmed against a macOS render).
const SCROLL_SPEED_CSS = '.scrollwheel-speed.hidden-on-windows{height:auto!important;visibility:visible!important}';

// The wheel relay is on unless OPL_WHEEL_RELAY=0, the variable the agent reads. The UI process
// cannot see the relay being switched on or off while the agent runs, so this follows the
// environment the launcher gave both of them.
function wheelRelayEnabled(env) {
  return (env || {}).OPL_WHEEL_RELAY !== '0';
}

// Insert the CSS into the UI's own page each time it loads (a navigation drops inserted CSS).
function showScrollSpeed(contents, env) {
  if (!wheelRelayEnabled(env)) return;
  contents.on('dom-ready', () => {
    try {
      if (isOwnPage(contents.getURL())) contents.insertCSS(SCROLL_SPEED_CSS).catch(() => {});
    } catch (_) { /* the page is already gone */ }
  });
}

module.exports = {
  guardWebRequest, matchPattern, matchesFilter, isOwnPage, OURS_URLS,
  SCROLL_SPEED_CSS, wheelRelayEnabled, showScrollSpeed,
};
