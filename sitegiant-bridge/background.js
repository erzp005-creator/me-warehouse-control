const HOURLY_ALARM = 'sitegiant-hourly-capture';
const TIMEOUT_ALARM = 'sitegiant-capture-timeout';
const DEFAULT_API_BASE = 'https://admin-production-2498.up.railway.app';

function hourKey(iso) {
  return iso.slice(0, 13).replace(/[-T:]/g, '');
}

async function readConfig() {
  const stored = await chrome.storage.local.get(['apiBase', 'warehouseId', 'wmsToken']);
  return {
    apiBase: (stored.apiBase || DEFAULT_API_BASE).replace(/\/$/, ''),
    warehouseId: Number(stored.warehouseId || 1),
    wmsToken: stored.wmsToken || '',
  };
}

async function setStatus(status, message, extra = {}) {
  await chrome.storage.local.set({
    bridgeStatus: status,
    bridgeMessage: message,
    statusUpdatedAt: new Date().toISOString(),
    ...extra,
  });
}

async function closePendingTab() {
  const { pendingTabId } = await chrome.storage.local.get('pendingTabId');
  if (!pendingTabId) return;
  try {
    await chrome.tabs.remove(pendingTabId);
  } catch (_) {
    // The tab may already have been closed by the user.
  }
  await chrome.storage.local.remove('pendingTabId');
}

async function schedule() {
  await chrome.alarms.create(HOURLY_ALARM, { delayInMinutes: 1, periodInMinutes: 60 });
}

async function injectCaptureScript(tabId, tabUrl) {
  if (!tabId || !String(tabUrl || '').startsWith('https://sitegiant.co/dashboard')) return;
  const { pendingTabId } = await chrome.storage.local.get('pendingTabId');
  if (pendingTabId !== tabId) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['content.js'],
    });
  } catch (error) {
    await chrome.alarms.clear(TIMEOUT_ALARM);
    await closePendingTab();
    await setStatus('error', error.message || 'Could not read the SiteGiant dashboard.');
  }
}

async function captureNow() {
  const config = await readConfig();
  if (!config.wmsToken) {
    await setStatus('setup_required', 'Add a SiteGiant capture token in extension settings.');
    return { ok: false, error: 'setup_required' };
  }

  await closePendingTab();
  const url = `https://sitegiant.co/dashboard?me_warehouse_capture=1&capture=${Date.now()}`;
  const tab = await chrome.tabs.create({ url, active: false });
  await chrome.storage.local.set({ pendingTabId: tab.id, captureStartedAt: new Date().toISOString() });
  await chrome.alarms.create(TIMEOUT_ALARM, { delayInMinutes: 3 });
  await setStatus('capturing', 'Opening SiteGiant to read the latest package totals.');
  return { ok: true };
}

async function uploadSnapshot(snapshot, senderTabId) {
  const config = await readConfig();
  if (!config.wmsToken) throw new Error('Capture token is not configured.');

  const payload = {
    ...snapshot,
    warehouse_id: config.warehouseId,
    source_url: 'https://sitegiant.co/dashboard',
    idempotency_key: `sitegiant-${config.warehouseId}-${hourKey(snapshot.captured_at)}`,
  };
  const response = await fetch(`${config.apiBase}/api/work-control/sitegiant/workload-snapshots`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-WMS-Token': config.wmsToken,
    },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error('Capture token was rejected. Create a new sitegiant.capture token.');
    }
    throw new Error(body.error || `Warehouse Control returned HTTP ${response.status}.`);
  }

  await chrome.alarms.clear(TIMEOUT_ALARM);
  await chrome.storage.local.set({
    lastSnapshot: body.snapshot,
    lastCaptureAt: body.snapshot?.captured_at || snapshot.captured_at,
  });
  await setStatus('current', body.duplicate ? 'This hour was already recorded.' : 'Hourly package totals recorded.');

  const { pendingTabId } = await chrome.storage.local.get('pendingTabId');
  if (pendingTabId && pendingTabId === senderTabId) await closePendingTab();
  return body;
}

chrome.runtime.onInstalled.addListener(async () => {
  await schedule();
  const existing = await chrome.storage.local.get(['apiBase', 'warehouseId']);
  await chrome.storage.local.set({
    apiBase: existing.apiBase || DEFAULT_API_BASE,
    warehouseId: existing.warehouseId || 1,
  });
});

chrome.runtime.onStartup.addListener(schedule);

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete') return;
  injectCaptureScript(tabId, tab.url).catch(async (error) => {
    await setStatus('error', error.message || 'Could not start the SiteGiant page reader.');
  });
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === HOURLY_ALARM) {
    try {
      await captureNow();
    } catch (error) {
      await setStatus('error', error.message || 'Could not open SiteGiant.');
    }
  }
  if (alarm.name === TIMEOUT_ALARM) {
    await closePendingTab();
    await setStatus('error', 'SiteGiant did not return totals. Check that Austin Chrome is still signed in.');
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'capture-now') {
    captureNow().then(sendResponse).catch(async (error) => {
      await setStatus('error', error.message || 'Capture could not start.');
      sendResponse({ ok: false, error: error.message });
    });
    return true;
  }
  if (message?.type === 'sitegiant-workload') {
    uploadSnapshot(message.snapshot, sender.tab?.id).then((body) => {
      sendResponse({ ok: true, duplicate: Boolean(body.duplicate) });
    }).catch(async (error) => {
      await chrome.alarms.clear(TIMEOUT_ALARM);
      await closePendingTab();
      await setStatus('error', error.message || 'Could not upload SiteGiant totals.');
      sendResponse({ ok: false, error: error.message });
    });
    return true;
  }
  if (message?.type === 'sitegiant-page-ready') {
    setStatus('capturing', 'SiteGiant opened; waiting for live package totals.').then(() => {
      sendResponse({ ok: true });
    });
    return true;
  }
  return false;
});

