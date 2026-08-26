(function runSiteGiantBridge() {
  const params = new URLSearchParams(window.location.search);

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

  function captureDashboard() {
    if (window.__ME_WAREHOUSE_SITEGIANT_CAPTURE_RUNNING__) return;
    window.__ME_WAREHOUSE_SITEGIANT_CAPTURE_RUNNING__ = true;
    chrome.runtime.sendMessage({ type: 'sitegiant-page-ready' }).catch(() => {});

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

    const startedAt = Date.now();
    let previousSignature = null;
    let stableReadings = 0;
    function attempt(remainingAttempts) {
      const cards = readPackageCards();
      const dashboardStillLoading = Boolean(document.querySelector('img[alt="loading"]'));
      const required = ['pending_packages', 'to_process_packages', 'printed_packages', 'pending_pickup_packages'];
      const complete = required.every((key) => Number.isInteger(cards[key]));
      const signature = complete ? required.map((key) => cards[key]).join(':') : null;
      const visibleTotal = complete ? required.reduce((total, key) => total + cards[key], 0) : 0;
      stableReadings = signature && signature === previousSignature ? stableReadings + 1 : 0;
      previousSignature = signature;
      const settled = Date.now() - startedAt >= 8000 && stableReadings >= 2 && visibleTotal > 0;
      if (!dashboardStillLoading && complete && settled) {
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
  }

  function captureSkuPage() {
    if (window.__ME_WAREHOUSE_SITEGIANT_SKU_SYNC_RUNNING__) return;
    window.__ME_WAREHOUSE_SITEGIANT_SKU_SYNC_RUNNING__ = true;
    const page = Math.max(Number(params.get('page') || 1), 1);
    const syncRunId = params.get('me_warehouse_sync_run');
    if (!syncRunId) return;

    function readRows() {
      return Array.from(document.querySelectorAll('table tbody tr'))
        .filter((row) => row.querySelectorAll('td').length >= 3)
        .map((row) => {
          const cells = row.querySelectorAll('td');
          const itemLink = cells[1]?.querySelector("a[href*='/items/'][href$='/edit']");
          const image = cells[1]?.querySelector('img');
          const href = itemLink?.getAttribute('href') || null;
          let imageUrl = null;
          try {
            const candidate = image?.src ? new URL(image.src) : null;
            if (candidate && (candidate.hostname === 'sgliteasset.com' || candidate.hostname.endsWith('.sgliteasset.com'))) imageUrl = candidate.href;
          } catch (_) {
            imageUrl = null;
          }
          return {
            sku: cells[2]?.textContent?.trim() || '',
            item_name: itemLink?.textContent?.replace(/\s+/g, ' ').trim() || '',
            source_item_id: href?.match(/\/items\/(\d+)\/edit/)?.[1] || null,
            source_item_url: href ? new URL(href, window.location.origin).href : null,
            image_url: imageUrl,
          };
        })
        .filter((item) => item.sku && item.item_name);
    }

    function readTotalItems() {
      const match = document.body.innerText.match(/\d+\s*-\s*\d+\s+of\s+([\d,]+)\s+items/i);
      return match ? Number(match[1].replace(/,/g, '')) : null;
    }

    function attempt(remainingAttempts) {
      const totalItems = readTotalItems();
      const items = readRows();
      const loadedRows = Array.from(document.querySelectorAll('table tbody tr'))
        .filter((row) => row.querySelectorAll('td').length >= 3).length;
      const expectedRows = totalItems ? Math.min(100, Math.max(totalItems - ((page - 1) * 100), 0)) : 0;
      if (totalItems && items.length > 0 && (!expectedRows || loadedRows >= expectedRows)) {
        chrome.runtime.sendMessage({
          type: 'sitegiant-sku-page',
          page: {
            sync_run_id: syncRunId,
            captured_at: new Date().toISOString(),
            page,
            total_pages: Math.ceil(totalItems / 100),
            total_items: totalItems,
            items,
          },
        });
        return;
      }
      if (remainingAttempts > 0) window.setTimeout(() => attempt(remainingAttempts - 1), 1000);
    }
    attempt(45);
  }

  if (window.location.pathname === '/dashboard' && params.get('me_warehouse_capture') === '1') captureDashboard();
  if (window.location.pathname === '/items' && params.get('me_warehouse_sku_sync') === '1') captureSkuPage();
})();
