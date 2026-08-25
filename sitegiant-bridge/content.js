(function captureSiteGiantDashboard() {
  const params = new URLSearchParams(window.location.search);
  if (window.location.pathname !== '/dashboard' || params.get('me_warehouse_capture') !== '1') return;
  if (window.__ME_WAREHOUSE_SITEGIANT_CAPTURE_RUNNING__) return;
  window.__ME_WAREHOUSE_SITEGIANT_CAPTURE_RUNNING__ = true;
  chrome.runtime.sendMessage({ type: 'sitegiant-page-ready' }).catch(() => {});

  function numberFrom(value) {
    const digits = String(value || '').replace(/[^0-9]/g, '');
    return digits ? Number(digits) : null;
  }

  function toIsoDate(label) {
    const match = String(label || '').trim().match(/^(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})$/);
    if (!match) return null;
    const months = {
      Jan: '01', Feb: '02', Mar: '03', Apr: '04', May: '05', Jun: '06',
      Jul: '07', Aug: '08', Sep: '09', Oct: '10', Nov: '11', Dec: '12',
    };
    const month = months[match[2]];
    if (!month) return null;
    return `${match[3]}-${month}-${match[1].padStart(2, '0')}`;
  }

  function readPackageCards() {
    const values = {};
    document.querySelectorAll('.overview-stats').forEach((card) => {
      const label = card.querySelector('.task-subtitle')?.textContent?.trim().toLowerCase();
      const value = numberFrom(card.querySelector('.count')?.textContent);
      if (!label || value === null) return;
      if (label === 'pending package') values.pending_packages = value;
      if (label === 'to process package') values.to_process_packages = value;
      if (label === 'printed package') values.printed_packages = value;
      if (label === 'pending pickup package') values.pending_pickup_packages = value;
    });
    return values;
  }

  function readPeriod() {
    const caption = Array.from(document.querySelectorAll('.caption'))
      .map((node) => node.textContent?.replace(/\s+/g, ' ').trim())
      .find((text) => /^From\s+/i.test(text || '') && /\s+to\s+/i.test(text || ''));
    const match = caption?.match(/^From\s+(.+?)\s+to\s+(.+)$/i);
    return {
      period_label: caption || null,
      period_start: match ? toIsoDate(match[1]) : null,
      period_end: match ? toIsoDate(match[2]) : null,
    };
  }

  function readTodayOrders() {
    const candidates = Array.from(document.querySelectorAll('div, span, p'))
      .filter((node) => /^today order$/i.test(node.textContent?.trim() || ''));
    for (const label of candidates) {
      const container = label.parentElement;
      if (!container) continue;
      const numbers = Array.from(container.querySelectorAll('strong, b, .count, h1, h2, h3'))
        .map((node) => numberFrom(node.textContent))
        .filter((value) => value !== null);
      if (numbers.length) return numbers[0];
    }
    return null;
  }

  function attempt(remainingAttempts) {
    const cards = readPackageCards();
    // SiteGiant initially renders all four cards as zero while its dashboard
    // request is still in flight. Do not mistake those placeholders for a
    // real zero-workload snapshot.
    const dashboardStillLoading = Boolean(document.querySelector('img[alt="loading"]'));
    const required = [
      'pending_packages',
      'to_process_packages',
      'printed_packages',
      'pending_pickup_packages',
    ];
    if (!dashboardStillLoading && required.every((key) => Number.isInteger(cards[key]))) {
      chrome.runtime.sendMessage({
        type: 'sitegiant-workload',
        snapshot: {
          captured_at: new Date().toISOString(),
          ...readPeriod(),
          ...cards,
          dashboard_order_count: readTodayOrders(),
        },
      });
      return;
    }
    if (remainingAttempts > 0) window.setTimeout(() => attempt(remainingAttempts - 1), 1000);
  }

  attempt(30);
})();

