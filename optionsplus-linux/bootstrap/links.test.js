'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { rewrite, reportIssueUrl, NOT_AVAILABLE_MESSAGE } = require('./links');

const GH = 'https://github.com/soubarnak/logioptions-plus';
const open = (url) => ({ action: 'open', url });
const block = { action: 'block', message: NOT_AVAILABLE_MESSAGE };

test('support pages open the README support section', () => {
  for (const u of [
    'https://support.logi.com/hc/en-us/articles/360023195014',
    'https://support.logi.com/hc/',
    'https://www.logitech.com/support/options-plus',
    'https://www.logitech.com/en-us/support/mx-master-3s',
  ]) assert.deepEqual(rewrite(u), open(`${GH}#support`), u);
});

test('device support opens the device notes for the model', () => {
  assert.deepEqual(rewrite('https://support.logi.com/hc/?mID=mx-master-3s'),
    open(`${GH}/blob/main/docs/devices.md#mx-master-3s`));
  assert.deepEqual(rewrite('https://support.logi.com/hc/en-us?mID=MX%20Master%203S&x=1'),
    open(`${GH}/blob/main/docs/devices.md#mx-master-3s`));
  // no model: falls back to the support section
  assert.deepEqual(rewrite('https://support.logi.com/hc/?mID='), open(`${GH}#support`));
});

test('release notes open GitHub releases', () => {
  assert.deepEqual(rewrite('https://marketplace.logi.com/releasenotes/optionsplus/'),
    open(`${GH}/releases`));
  assert.deepEqual(rewrite('https://marketplace.logi.com/releasenotes/optionsplus'),
    open(`${GH}/releases`));
});

test('feedback opens the new-issue chooser', () => {
  assert.deepEqual(rewrite('https://feedback.logitech.com/s3/abc123'),
    open(`${GH}/issues/new/choose`));
});

test('EULA and privacy URLs pass through unchanged', () => {
  for (const u of [
    'https://www.logitech.com/en-us/legal/eula.html',
    'https://www.logitech.com/en-us/legal/web-privacy-policy.html',
    'https://www.logitech.com/en-us/legal/privacy-policy',
    'https://www.logitech.com/support/privacy',
  ]) assert.deepEqual(rewrite(u), open(u), u);
});

test('cloud hosts are blocked with the not-available message', () => {
  for (const u of [
    'https://logiid.logi.com/login',
    'https://id.logitech.com/authorize?x=1',
    'https://marketplace.logi.com/',
    'https://marketplace.logi.com/plugins/foo',
    'https://recommendations.logi.com/offer',
    'https://sub.recommendation.logitech.com/x',
  ]) assert.deepEqual(rewrite(u), block, u);
  assert.match(NOT_AVAILABLE_MESSAGE, /not available in Options\+ for Linux/);
});

test('other URLs pass through, malformed ones are blocked', () => {
  assert.deepEqual(rewrite('https://example.org/x'), open('https://example.org/x'));
  assert.deepEqual(rewrite('not a url'), block);
});

test('report issue URL uses the bug template with prefilled fields', () => {
  const u = new URL(reportIssueUrl({
    version: '0.1.0', os: 'Void Linux',
    logArchive: '/home/u/.local/state/optionsplus-linux/logs.tar.gz',
  }));
  assert.equal(u.origin + u.pathname, `${GH}/issues/new`);
  assert.equal(u.searchParams.get('template'), 'bug_report.yml');
  assert.equal(u.searchParams.get('version'), '0.1.0');
  assert.equal(u.searchParams.get('os'), 'Void Linux');
  assert.match(u.searchParams.get('logs'), /optionsplus-linux\/logs\.tar\.gz/);
});
