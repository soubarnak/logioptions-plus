// External link policy for the unmodified Options+ UI (feature-parity.md 8.3).
// Pure URL-in / decision-out so it can be tested without Electron.
'use strict';

const GH = 'https://github.com/soubarnak/logioptions-plus';
const NOT_AVAILABLE_MESSAGE = 'This feature is not available in Options+ for Linux.';

const open = (url) => ({ action: 'open', url });
const block = () => ({ action: 'block', message: NOT_AVAILABLE_MESSAGE });

const hostIs = (host, domain) => host === domain || host.endsWith('.' + domain);

function deviceSlug(model) {
  return model.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

// -> { action: 'open', url } | { action: 'block', message }
function rewrite(raw) {
  let u;
  try { u = new URL(raw); } catch { return block(); }
  const host = u.hostname.toLowerCase();
  const path = u.pathname.toLowerCase();

  // Logitech's terms apply to Logitech's payload: leave them alone.
  if (hostIs(host, 'logitech.com') && /eula|privacy/.test(path)) return open(raw);

  // Cloud services we do not provide.
  if (host === 'logiid.logi.com' || host === 'id.logitech.com' ||
      host === 'marketplace.logi.com' && !path.startsWith('/releasenotes') ||
      /(^|[.-])recommendations?\./.test(host) && (hostIs(host, 'logi.com') || hostIs(host, 'logitech.com'))) {
    return block();
  }

  if (host === 'marketplace.logi.com') return open(`${GH}/releases`);
  if (host === 'feedback.logitech.com') return open(`${GH}/issues/new/choose`);

  if (host === 'support.logi.com') {
    const slug = deviceSlug(u.searchParams.get('mID') || '');
    return open(slug ? `${GH}/blob/main/docs/devices.md#${slug}` : `${GH}#support`);
  }
  if (host === 'www.logitech.com' && /^\/([a-z]{2}-[a-z]{2}\/)?support(\/|$)/.test(path)) {
    return open(`${GH}#support`);
  }

  return open(raw);
}

// "Report issue": new-issue form from the bug template, log archive path prefilled.
function reportIssueUrl({ version = '', os = '', logArchive = '' } = {}) {
  const q = new URLSearchParams({ template: 'bug_report.yml', version, os, logs: logArchive });
  return `${GH}/issues/new?${q}`;
}

module.exports = { rewrite, reportIssueUrl, NOT_AVAILABLE_MESSAGE };
