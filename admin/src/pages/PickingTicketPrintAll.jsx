import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import { TicketDocument } from './PickingTicketPrint.jsx';
import { PRINT_BATCH_LIMIT } from './pickingConstants.js';
import './pickingTicket.css';

const PICKABLE_STATUSES = ['OPEN', 'PICKED'];

// POST /admin/sales-orders/mark-printed with one retry. Replaces the
// previous fire-and-forget POST whose `.catch(() => {})` swallowed
// every failure, so a transient network blip left printed_at NULL in
// the DB while the operator walked away with the paper. Returns true
// only when the server confirmed the write.
async function markPrintedWithRetry(soIds) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await api.post('/admin/sales-orders/mark-printed', { so_ids: soIds });
      if (res?.ok) return true;
    } catch (_) {
      // fall through to retry
    }
    if (attempt === 0) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }
  return false;
}

// Standalone print-queue view. Opened in a new tab from the picking
// tickets page; renders every ticket in the current status filter
// stacked one per page so the user can hit Ctrl/Cmd+P natively. No
// toolbar, no auto-print, no admin Layout chrome.
export default function PickingTicketPrintAll() {
  const [params] = useSearchParams();
  const status = params.get('status') || 'OPEN';
  const warehouseId = params.get('warehouse_id') || '';
  // Comma-separated SO ids from the list page: the authoritative set
  // AND order to print, already Hide-Printed-filtered and sorted exactly
  // as the operator sees them. When present we print precisely these and
  // skip re-deriving the queue, so the print stack can never drift from
  // the screen. Absent (a hand-typed or bookmarked deep link) we fall
  // back to fetching the status queue in warehouse-walk order.
  const requestedOrderParam = params.get('so_ids') || '';
  const [tickets, setTickets] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [markPrintedFailed, setMarkPrintedFailed] = useState(false);
  const [retryingMark, setRetryingMark] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError('');
      setMarkPrintedFailed(false);

      // Resolve the ordered set of SO ids to print. When the list page
      // handed over so_ids, that IS the queue - already Hide-Printed
      // filtered and sorted as the operator sees it - so we use it
      // verbatim and never re-fetch the list. This is what keeps the
      // print stack identical to the screen (no already-printed tickets
      // leaking in, no sort drift).
      let soIds = requestedOrderParam
        ? requestedOrderParam.split(',').map((s) => parseInt(s, 10)).filter(Number.isFinite)
        : [];

      if (soIds.length === 0) {
        // Deep-link fallback only (hand-typed or bookmarked URL with no
        // so_ids). Rediscover the status queue ourselves and sort by
        // warehouse-walk order (primary_bin_pick_sequence) with a
        // ship_by_date tiebreak. No Hide-Printed filter here by design:
        // a bare deep link is an explicit "print this whole status".
        const statuses = status === 'ALL' ? PICKABLE_STATUSES : [status];
        const listResponses = await Promise.all(
          statuses.map((s) => {
            const qs = new URLSearchParams({
              status: s,
              per_page: String(PRINT_BATCH_LIMIT),
              // primary_bin_pick_sequence drives the warehouse-walk sort.
              include_primary_bin: 'true',
            });
            if (warehouseId) qs.set('warehouse_id', warehouseId);
            return api.get(`/admin/sales-orders?${qs.toString()}`);
          }),
        );
        const orders = [];
        for (const res of listResponses) {
          if (res?.ok) {
            const data = await res.json();
            orders.push(...(data.sales_orders || []));
          }
        }
        orders.sort((a, b) => {
          const aps = a.primary_bin_pick_sequence;
          const bps = b.primary_bin_pick_sequence;
          if (aps != null && bps != null) return aps - bps;
          if (aps != null) return -1;
          if (bps != null) return 1;
          const ad = a.ship_by_date;
          const bd = b.ship_by_date;
          if (!ad && !bd) return 0;
          if (!ad) return 1;
          if (!bd) return -1;
          return new Date(ad) - new Date(bd);
        });
        soIds = orders.map((o) => o.so_id);
      }

      if (cancelled) return;

      // One tab renders at most PRINT_BATCH_LIMIT tickets. The list page
      // already slices its hand-off to this; we re-clamp so a stale or
      // hand-edited URL can't stack an unbounded run into one tab.
      soIds = soIds.slice(0, PRINT_BATCH_LIMIT);
      if (soIds.length === 0) {
        setTickets([]);
        setLoading(false);
        return;
      }

      // Fetch ticket detail per SO in the resolved order. Promise.all
      // preserves index order, so the rendered stack matches soIds.
      const detailResponses = await Promise.all(
        soIds.map((id) => api.get(`/admin/sales-orders/${id}/picking-ticket`)),
      );
      const out = [];
      for (const res of detailResponses) {
        if (!res?.ok) continue;
        const data = await res.json();
        if (data.sales_order) {
          out.push({ so: data.sales_order, lines: data.lines || [], branding: data.branding || {} });
        }
      }
      if (cancelled) return;
      if (out.length === 0) {
        setError('No tickets could be loaded.');
        setLoading(false);
        return;
      }
      setTickets(out);
      setLoading(false);
      // mig 064: only mark SOs whose ticket data actually made it into
      // the render array. Orders that failed earlier (404, network
      // error, malformed payload) stay unprinted so they reappear in
      // the queue and the operator can retry rather than silently
      // losing them. Persistent mark-printed failures surface via the
      // toast below so the operator knows to verify the list rather
      // than assume the DB matches the screen.
      const ok = await markPrintedWithRetry(out.map((t) => t.so.so_id));
      if (!cancelled && !ok) setMarkPrintedFailed(true);
    }
    load();
    return () => { cancelled = true; };
  }, [status, warehouseId, requestedOrderParam]);

  async function retryMarkPrinted() {
    if (tickets.length === 0) return;
    setRetryingMark(true);
    const ok = await markPrintedWithRetry(tickets.map((t) => t.so.so_id));
    setRetryingMark(false);
    if (ok) setMarkPrintedFailed(false);
  }

  // Update the tab title once we know the count, so the user can
  // tell the queue tabs apart.
  useEffect(() => {
    if (loading) {
      document.title = 'Loading picking tickets…';
    } else if (error) {
      document.title = 'Picking tickets - error';
    } else {
      document.title = `Picking tickets (${tickets.length}) - ${status}`;
    }
  }, [loading, error, tickets.length, status]);

  if (loading) {
    return <div className="pt-root"><div className="pt-page">Loading tickets…</div></div>;
  }

  if (error) {
    return (
      <div className="pt-root">
        <div className="pt-page">
          <h2>Could not render tickets</h2>
          <p>{error}</p>
        </div>
      </div>
    );
  }

  if (tickets.length === 0) {
    return (
      <div className="pt-root">
        <div className="pt-page">
          <h2>No tickets to print</h2>
          <p>No sales orders matched status {status}.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="pt-root">
      {markPrintedFailed && (
        <div className="pt-toast pt-no-print" role="alert">
          <span>
            Some tickets may not have been marked as printed. Refresh
            the list to verify, or retry.
          </span>
          <button type="button" onClick={retryMarkPrinted} disabled={retryingMark}>
            {retryingMark ? 'Retrying…' : 'Retry'}
          </button>
          <button type="button" onClick={() => setMarkPrintedFailed(false)}>
            Dismiss
          </button>
        </div>
      )}
      {tickets.map(({ so, lines, branding }) => (
        <TicketDocument key={so.so_id} so={so} lines={lines} branding={branding} />
      ))}
    </div>
  );
}
