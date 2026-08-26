const HOURLY_ALARM = 'sitegiant-hourly-capture';
const TIMEOUT_ALARM = 'sitegiant-capture-timeout';
const SKU_DAILY_ALARM = 'sitegiant-daily-sku-sync';
const SKU_TIMEOUT_ALARM = 'sitegiant-sku-sync-timeout';
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

async function setSkuStatus(status, message, extra = {}) {
  await chrome.storage.local.set({
    skuSyncStatus: status,
    skuSyncMessage: message,
    skuStatusUpdatedAt: new Date().toISOString(),
    ...extra,
  });
}

async function closeStoredTab(storageKey) {
  const stored = await chrome.storage.local.get(storageKey);
  const tabId = stored[storageKey];
  if (!tabId) return;
  try {
    await chrome.tabs.remove(tabId);
  } catch (_) {
    // The tab may already have been closed by the user.
  }
  await chrome.storage.local.remove(storageKey);
}

async function closePendingTab() {
  await closeStoredTab('pendingTabId');
}

async function closePendingSkuTab() {
  await closeStoredTab('pendingSkuTabId');
}

async function schedule() {
  await chrome.alarms.create(HOURLY_ALARM, { delayInMinutes: 1, periodInMinutes: 60 });
  await chrome.alarms.create(SKU_DAILY_ALARM, { delayInMinutes: 10, periodInMinutes: 24 * 60 });
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

async function syncSkusNow() {
  const config = await readConfig();
  if (!config.wmsToken) {
    await setSkuStatus('setup_required', 'Add a SiteGiant capture token before syncing SKUs.');
    return { ok: false, error: 'setup_required' };
  }
  await closePendingSkuTab();
  const syncRunId = crypto.randomUUID();
  const url = `https://sitegiant.co/items?page=1&limit=100&me_warehouse_sku_sync=1&me_warehouse_sync_run=${encodeURIComponent(syncRunId)}`;
  const tab = await chrome.tabs.create({ url, active: false });
  await chrome.storage.local.set({
    pendingSkuTabId: tab.id,
    skuSyncRunId: syncRunId,
    skuSyncStartedAt: new Date().toISOString(),
  });
  await chrome.alarms.create(SKU_TIMEOUT_ALARM, { delayInMinutes: 10 });
  await setSkuStatus('capturing', 'Reading SiteGiant SKU catalog · page 1…', {
    skuSyncPage: 1,
    skuSyncTotalPages: null,
  });
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
    headers: { 'Content-Type': 'application/json', 'X-WMS-Token': config.wmsToken },
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
  await setStatus('current', body.updated ? 'This hour was refreshed with the latest totals.' : 'Hourly package totals recorded.');
  const { pendingTabId } = await chrome.storage.local.get('pendingTabId');
  if (pendingTabId && pendingTabId === senderTabId) await closePendingTab();
  return body;
}

async function uploadSkuPage(pagePayload, senderTabId) {
  const { skuSyncRunId } = await chrome.storage.local.get('skuSyncRunId');
  if (!skuSyncRunId || skuSyncRunId !== pagePayload.sync_run_id) {
    return { ignored: true, completed: false };
  }
  const config = await readConfig();
  if (!config.wmsToken) throw new Error('Capture token is not configured.');
  const payload = { ...pagePayload, warehouse_id: config.warehouseId };
  const response = await fetch(`${config.apiBase}/api/work-control/sitegiant/skus/sync`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-WMS-Token': config.wmsToken },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error('Capture token was rejected for the SKU catalog.');
    }
    throw new Error(body.error || `Warehouse Control returned HTTP ${response.status}.`);
  }

  if (body.completed) {
    await chrome.alarms.clear(SKU_TIMEOUT_ALARM);
    await chrome.storage.local.set({
      lastSkuSyncAt: pagePayload.captured_at,
      lastSkuSyncCount: pagePayload.total_items,
      skuSyncPage: pagePayload.total_pages,
      skuSyncTotalPages: pagePayload.total_pages,
    });
    await setSkuStatus('current', `${pagePayload.total_items.toLocaleString()} SiteGiant SKUs are ready for receiving.`);
    const { pendingSkuTabId } = await chrome.storage.local.get('pendingSkuTabId');
    if (pendingSkuTabId && pendingSkuTabId === senderTabId) await closePendingSkuTab();
    return body;
  }

  const nextPage = pagePayload.page + 1;
  await setSkuStatus('capturing', `Reading SiteGiant SKU catalog · page ${nextPage} of ${pagePayload.total_pages}…`, {
    skuSyncPage: nextPage,
    skuSyncTotalPages: pagePayload.total_pages,
  });
  const nextUrl = `https://sitegiant.co/items?page=${nextPage}&limit=100&me_warehouse_sku_sync=1&me_warehouse_sync_run=${encodeURIComponent(pagePayload.sync_run_id)}`;
  await chrome.tabs.update(senderTabId, { url: nextUrl, active: false });
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

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === HOURLY_ALARM) {
    try { await captureNow(); } catch (error) { await setStatus('error', error.message || 'Could not open SiteGiant.'); }
  }
  if (alarm.name === SKU_DAILY_ALARM) {
    try { await syncSkusNow(); } catch (error) { await setSkuStatus('error', error.message || 'Could not open the SiteGiant SKU catalog.'); }
  }
  if (alarm.name === TIMEOUT_ALARM) {
    await closePendingTab();
    await setStatus('error', 'SiteGiant did not return totals. Check that Austin Chrome is still signed in.');
  }
  if (alarm.name === SKU_TIMEOUT_ALARM) {
    await closePendingSkuTab();
    await setSkuStatus('error', 'SKU sync timed out. Keep Austin Chrome signed in, then retry.');
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
  if (message?.type === 'sync-skus-now') {
    syncSkusNow().then(sendResponse).catch(async (error) => {
      await setSkuStatus('error', error.message || 'SKU sync could not start.');
      sendResponse({ ok: false, error: error.message });
    });
    return true;
  }
  if (message?.type === 'sitegiant-workload') {
    uploadSnapshot(message.snapshot, sender.tab?.id).then((body) => {
      sendResponse({ ok: true, duplicate: Boolean(body.duplicate), updated: Boolean(body.updated) });
    }).catch(async (error) => {
      await chrome.alarms.clear(TIMEOUT_ALARM);
      await closePendingTab();
      await setStatus('error', error.message || 'Could not upload SiteGiant totals.');
      sendResponse({ ok: false, error: error.message });
    });
    return true;
  }
  if (message?.type === 'sitegiant-sku-page') {
    uploadSkuPage(message.page, sender.tab?.id).then((body) => {
      sendResponse({ ok: true, completed: Boolean(body.completed) });
    }).catch(async (error) => {
      await chrome.alarms.clear(SKU_TIMEOUT_ALARM);
      await closePendingSkuTab();
      await setSkuStatus('error', error.message || 'Could not upload the SiteGiant SKU catalog.');
      sendResponse({ ok: false, error: error.message });
    });
    return true;
  }
  if (message?.type === 'sitegiant-page-ready') {
    setStatus('capturing', 'SiteGiant opened; waiting for live package totals.').then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});
