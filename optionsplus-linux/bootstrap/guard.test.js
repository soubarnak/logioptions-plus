'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { guardWebRequest, matchPattern, matchesFilter, isOwnPage, OURS_URLS } = require('./guard');
const { isBlockedLoad } = require('./links');

// Stands in for session.webRequest: records each (filter, listener) the wrapper installs.
function fakeWebRequest() {
  const wr = { installed: [] };
  wr.onBeforeRequest = (...args) => {
    const listener = args.find((a) => typeof a === 'function') || null;
    const filter = args.find((a) => a && typeof a === 'object') || null;
    wr.installed.push({ filter, listener });
  };
  wr.current = () => wr.installed[wr.installed.length - 1];
  // run a request through whatever listener is installed; resolves to the callback response
  wr.request = (details) => {
    const cur = wr.current();
    if (!cur.listener) return { passed: true };
    if (!matchesFilter(cur.filter, details)) return { passed: true }; // Electron skips it
    let res;
    cur.listener(details, (r) => { res = r; });
    return res;
  };
  return wr;
}
const setup = () => {
  const wr = fakeWebRequest();
  const blocked = [];
  guardWebRequest(wr, { isBlockedLoad, onBlocked: (d) => blocked.push(d) });
  return { wr, blocked, set: wr.onBeforeRequest };
};
const req = (url, resourceType = 'mainFrame') => ({ url, resourceType });

test('match patterns follow Electron', () => {
  assert.equal(matchPattern('*://*.example.com/*', 'https://a.b.example.com/x'), true);
  assert.equal(matchPattern('*://*.example.com/*', 'https://example.com/'), true);
  assert.equal(matchPattern('*://*.example.com/*', 'https://notexample.com/'), false);
  assert.equal(matchPattern('https://example.com/a/*', 'http://example.com/a/b'), false);
  assert.equal(matchPattern('https://example.com/a/*', 'https://example.com/a/b?q=1'), true);
  assert.equal(matchPattern('https://example.com/a', 'https://example.com/b'), false);
  assert.equal(matchPattern('<all_urls>', 'https://x.org/'), true);
  assert.equal(matchPattern('*://*/*', 'file:///etc/hosts'), false);
  assert.equal(matchPattern('http://localhost:8080/*', 'http://localhost:8080/x'), true);
  assert.equal(matchPattern('http://localhost:8080/*', 'http://localhost:9/x'), false);
  assert.equal(matchPattern('*://*.example.com/*', 'not a url'), false);
});

test('filter combines urls, excludeUrls and types', () => {
  const d = req('https://a.example.com/x', 'xhr');
  assert.equal(matchesFilter(null, d), true);
  assert.equal(matchesFilter({}, d), true);
  assert.equal(matchesFilter({ urls: ['*://*.example.com/*'] }, d), true);
  assert.equal(matchesFilter({ urls: ['*://*.example.com/*'], excludeUrls: ['*://a.example.com/*'] }, d), false);
  assert.equal(matchesFilter({ types: ['image'] }, d), false);
  assert.equal(matchesFilter({ types: ['xhr', 'image'] }, d), true);
});

test('our own filter covers every host isCloudHost blocks', () => {
  for (const u of [
    'https://logiid.logi.com/login', 'https://id.logitech.com/x', 'https://marketplace.logi.com/',
    'https://recommendations.logi.com/', 'https://sub.recommendation.logitech.com/x',
    'https://optionsplus-recommendation.logitech.com/x', 'https://recommendations.logitech.com/',
  ]) {
    assert.equal(isBlockedLoad(u), true, u);
    assert.ok(OURS_URLS.some((p) => matchPattern(p, u)), u);
  }
});

test('without a listener of theirs, ours is installed filtered to Logitech domains', () => {
  const { wr } = setup();
  assert.deepEqual(wr.current().filter, { urls: OURS_URLS });
  assert.deepEqual(wr.request(req('https://example.org/')), { passed: true });
  assert.deepEqual(wr.request(req('https://marketplace.logi.com/')), { cancel: true });
});

test('their listener only sees requests matching their filter', () => {
  const { wr, set } = setup();
  const seen = [];
  set({ urls: ['*://*.example.com/*'], types: ['xhr'] }, (d, cb) => { seen.push(d.url); cb({ redirectURL: 'x' }); });
  assert.deepEqual(wr.request(req('https://a.example.com/1', 'xhr')), { redirectURL: 'x' });
  assert.deepEqual(wr.request(req('https://a.example.com/2', 'image')), {});
  assert.deepEqual(wr.request(req('https://other.org/3', 'xhr')), { passed: true });
  assert.deepEqual(seen, ['https://a.example.com/1']);
});

test('the registered filter is the union of theirs and ours; unfiltered theirs means unfiltered', () => {
  const { wr, set } = setup();
  set({ urls: ['*://*.example.com/*'] }, (d, cb) => cb({}));
  assert.deepEqual(wr.current().filter.urls, ['*://*.example.com/*', ...OURS_URLS]);
  set((d, cb) => cb({}));
  assert.equal(wr.current().filter, null);
  set({ types: ['xhr'] }, (d, cb) => cb({}));
  assert.equal(wr.current().filter, null);
});

test('their excludeUrls do not hide ours', () => {
  const { wr, set } = setup();
  set({ urls: ['<all_urls>'], excludeUrls: ['*://*.logi.com/*'] }, (d, cb) => cb({}));
  assert.deepEqual(wr.request(req('https://marketplace.logi.com/')), { cancel: true });
});

test('null clears theirs and a second call replaces it', () => {
  const { wr, set } = setup();
  const calls = [];
  set((d, cb) => { calls.push('a'); cb({}); });
  set((d, cb) => { calls.push('b'); cb({}); });
  wr.request(req('https://example.org/'));
  assert.deepEqual(calls, ['b']);
  set(null);
  wr.request(req('https://example.org/'));
  assert.deepEqual(calls, ['b']);
  assert.deepEqual(wr.current().filter, { urls: OURS_URLS });
});

test('ours runs before theirs, and blocked requests never reach theirs', () => {
  const { wr, blocked, set } = setup();
  const order = [];
  set((d, cb) => { order.push('theirs:' + d.url); cb({}); });
  assert.deepEqual(wr.request(req('https://marketplace.logi.com/plugins')), { cancel: true });
  assert.deepEqual(order, []);
  assert.equal(blocked.length, 1);
  assert.equal(blocked[0].resourceType, 'mainFrame');
  wr.request(req('https://marketplace.logi.com/plugins', 'subFrame'));
  assert.equal(blocked.length, 2);
  assert.deepEqual(order, []);
  // allowed and non-frame requests do reach theirs, after ours let them through
  wr.request(req('https://example.org/'));
  wr.request(req('https://marketplace.logi.com/img.png', 'image'));
  assert.deepEqual(order, ['theirs:https://example.org/', 'theirs:https://marketplace.logi.com/img.png']);
  assert.equal(blocked.length, 2);
});

test('the UI\'s own page is the file:// page', () => {
  assert.equal(isOwnPage('file:///usr/lib/optionsplus-linux/app.asar/index.html'), true);
  assert.equal(isOwnPage('https://marketplace.logi.com/'), false);
  assert.equal(isOwnPage(''), false);
  assert.equal(isOwnPage(undefined), false);
});
