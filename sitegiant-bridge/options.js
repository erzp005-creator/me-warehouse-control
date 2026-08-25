const apiBase = document.querySelector('#apiBase');
const warehouseId = document.querySelector('#warehouseId');
const wmsToken = document.querySelector('#wmsToken');
const message = document.querySelector('#message');

async function load() {
  const stored = await chrome.storage.local.get([
    'apiBase', 'warehouseId', 'wmsToken', 'bridgeStatus', 'bridgeMessage', 'lastCaptureAt',
  ]);
  apiBase.value = stored.apiBase || 'https://admin-production-2498.up.railway.app';
  warehouseId.value = stored.warehouseId || 1;
  wmsToken.placeholder = stored.wmsToken ? 'Token is saved — leave blank to keep it' : 'Paste the one-time token here';
  if (stored.bridgeMessage) {
    message.textContent = `${stored.bridgeMessage}${stored.lastCaptureAt ? ` Last capture: ${new Date(stored.lastCaptureAt).toLocaleString()}.` : ''}`;
    message.dataset.status = stored.bridgeStatus || '';
  }
}

document.querySelector('#save').addEventListener('click', async () => {
  const values = {
    apiBase: apiBase.value.trim().replace(/\/$/, ''),
    warehouseId: Number(warehouseId.value),
  };
  if (!/^https:\/\//.test(values.apiBase) || values.warehouseId < 1) {
    message.textContent = 'Enter a valid HTTPS URL and warehouse ID.';
    message.dataset.status = 'error';
    return;
  }
  if (wmsToken.value.trim()) values.wmsToken = wmsToken.value.trim();
  await chrome.storage.local.set(values);
  wmsToken.value = '';
  wmsToken.placeholder = 'Token is saved — leave blank to keep it';
  message.textContent = 'Settings saved locally in this Chrome profile.';
  message.dataset.status = 'current';
});

document.querySelector('#capture').addEventListener('click', async () => {
  message.textContent = 'Starting capture…';
  message.dataset.status = 'capturing';
  const response = await chrome.runtime.sendMessage({ type: 'capture-now' });
  if (!response?.ok) {
    message.textContent = response?.error === 'setup_required'
      ? 'Save a capture token first.'
      : (response?.error || 'Capture could not start.');
    message.dataset.status = 'error';
    return;
  }
  message.textContent = 'SiteGiant opened in the background. This page will update after the totals are recorded.';
  window.setTimeout(load, 6000);
});

load();
