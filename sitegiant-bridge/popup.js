async function refresh() {
  const stored = await chrome.storage.local.get([
    'bridgeStatus', 'bridgeMessage', 'lastCaptureAt', 'lastSnapshot',
  ]);
  document.querySelector('#status').textContent = stored.bridgeStatus === 'current'
    ? 'Feed current'
    : stored.bridgeStatus === 'capturing'
      ? 'Capturing'
      : stored.bridgeStatus === 'setup_required'
        ? 'Setup required'
        : stored.bridgeStatus === 'error'
          ? 'Needs attention'
          : 'Waiting for first capture';
  document.querySelector('#dot').dataset.status = stored.bridgeStatus || 'missing';
  document.querySelector('#detail').textContent = stored.bridgeMessage || 'No hourly reading has been recorded yet.';
  if (stored.lastSnapshot) {
    document.querySelector('#totals').hidden = false;
    document.querySelector('#remaining').textContent = Number(stored.lastSnapshot.remaining_packages || 0).toLocaleString();
    if (stored.lastCaptureAt) document.querySelector('#detail').textContent += ` ${new Date(stored.lastCaptureAt).toLocaleString()}.`;
  }
}

document.querySelector('#capture').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'capture-now' });
  window.close();
});
document.querySelector('#settings').addEventListener('click', () => chrome.runtime.openOptionsPage());
refresh();
