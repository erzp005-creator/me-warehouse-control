import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
// expected_date is a date-only string. new Date('2026-08-25') parses as UTC
// midnight and renders a day early west of it, which is the bug this helper
// exists to avoid.
import { formatDateOnly } from '../utils/date.js';

// Partial-fulfill / backorders dashboard. Two tabs:
//   Waiting       - status=WAITING_STOCK, oldest backorder_opened_at
//                   surfaces first so a long-tail backorder is not
//                   buried under a fresh one.
//   Ready to ship - status IN (OPEN, PICKED, PACKED) with
//                   backorder_opened_at IS NOT NULL. These have been
//                   flipped to OPEN by the receipt-hook matcher and
//                   are eligible for the next pick batch.
// Click row opens the existing SO edit modal via
// /sales-orders?focus=<so_number>; the SO page reads the focus query
// param and pops the modal in-place.

const TABS = [
  { key: 'waiting', label: 'Waiting' },
  { key: 'ready-to-ship', label: 'Ready to Ship' },
];

const CANCEL_REASONS = [
  { value: 'found', label: 'Found in warehouse' },
  { value: 'customer_asked', label: 'Customer asked to cancel' },
  { value: 'refunded', label: 'Refunded' },
  { value: 'other', label: 'Other' },
];


// The operator asked for a way to tell whether the item has already
// been ordered. open_po is derived (no backorder-to-PO link exists), so it
// names the PO it is claiming rather than showing a bare checkbox. Null is
// rendered explicitly: "no open PO" has to read differently from a field that
// did not load.
function OrderedLine({ openPo }) {
  if (!openPo) return <div>not on an open PO</div>;
  return (
    <div>
      on <span className="mono">{openPo.po_number}</span>
      {openPo.expected_date
        ? `, expected ${formatDateOnly(openPo.expected_date)}`
        : ', no expected date'}
    </div>
  );
}

// a SKU alone is not enough to know what you are looking at when
// working the queue, so the name sits under the mono SKU line.
function ItemsCell({ items }) {
  if (!items || items.length === 0) {
    return <span style={{ color: 'var(--text-secondary)' }}>(none)</span>;
  }
  return (
    <div style={{ fontSize: 12, lineHeight: 1.4 }}>
      {items.map((it, i) => (
        <div key={i} style={i > 0 ? { marginTop: 6 } : undefined}>
          <div>
            <span className="mono">{it.sku}</span>
            {' × '}
            {it.qty}
          </div>
          {it.item_name && <div>{it.item_name}</div>}
          <OrderedLine openPo={it.open_po} />
        </div>
      ))}
    </div>
  );
}


export default function Backorders() {
  const navigate = useNavigate();
  const { warehouseId } = useWarehouse();
  const [tab, setTab] = useState('waiting');
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [actionError, setActionError] = useState('');
  const [successBanner, setSuccessBanner] = useState('');

  // Cancel-BO confirm modal state. cancelTarget holds the BO row
  // being cancelled; cancelReason is a dropdown enum mirroring the
  // backend's CANCEL_REASONS.
  const [cancelTarget, setCancelTarget] = useState(null);
  const [cancelReason, setCancelReason] = useState('other');
  const [cancelSubmitting, setCancelSubmitting] = useState(false);
  const [cancelError, setCancelError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setActionError('');
    const qs = new URLSearchParams({ tab });
    if (warehouseId) qs.set('warehouse_id', String(warehouseId));
    const res = await api.get(`/admin/backorders?${qs.toString()}`);
    if (res?.ok) {
      const data = await res.json();
      setRows(data.backorders || []);
    } else {
      setRows([]);
      setActionError('Failed to load backorders.');
    }
    setLoading(false);
  }, [tab, warehouseId]);

  useEffect(() => { load(); }, [load]);

  function openCancel(row) {
    setCancelTarget(row);
    setCancelReason('other');
    setCancelError('');
  }

  function closeCancel() {
    setCancelTarget(null);
    setCancelReason('other');
    setCancelError('');
    setCancelSubmitting(false);
  }

  async function submitCancel() {
    if (!cancelTarget) return;
    setCancelSubmitting(true);
    setCancelError('');
    const res = await api.post(
      `/admin/sales-orders/${cancelTarget.so_id}/cancel-backorder`,
      { cancellation_reason: cancelReason },
    );
    setCancelSubmitting(false);
    if (!res?.ok) {
      let data = null;
      try { data = await res?.json(); } catch (_) { /* non-JSON */ }
      setCancelError(data?.error || 'Failed to cancel backorder.');
      return;
    }
    const soNumber = cancelTarget.so_number;
    closeCancel();
    setSuccessBanner(`Backorder ${soNumber} cancelled (${cancelReason}).`);
    setTimeout(() => setSuccessBanner(''), 6000);
    load();
  }

  // Both tabs share a base column set; ready-to-ship adds
  // "Fulfillable since" so the operator can prioritise the longest-
  // waiting ready batch first.
  const baseColumns = [
    { key: 'so_number', label: 'Backorder #', mono: true },
    { key: 'parent_so_number', label: 'Parent SO', mono: true,
      render: (r) => r.parent_so_number || <span style={{ color: 'var(--text-secondary)' }}>-</span> },
    { key: 'customer_name', label: 'Customer',
      render: (r) => r.customer_name || <span style={{ color: 'var(--text-secondary)' }}>-</span> },
    { key: 'items', label: 'Items', render: (r) => <ItemsCell items={r.items} /> },
    { key: 'days_waiting', label: 'Days waiting',
      render: (r) => (
        <span className="mono">
          {r.days_waiting}
        </span>
      ),
    },
  ];

  const readyColumn = {
    key: 'fulfillable_since',
    label: 'Fulfillable since',
    render: (r) => r.fulfillable_since
      ? new Date(r.fulfillable_since).toLocaleDateString()
      : <span style={{ color: 'var(--text-secondary)' }}>-</span>,
  };

  const actionsColumn = {
    key: 'actions',
    label: '',
    render: (r) => (
      <button
        className="btn btn-sm btn-danger"
        onClick={(e) => { e.stopPropagation(); openCancel(r); }}
        title="Cancel this backorder"
      >
        Cancel
      </button>
    ),
  };

  const columns = tab === 'ready-to-ship'
    ? [...baseColumns, readyColumn, actionsColumn]
    : [...baseColumns, actionsColumn];

  function openRowInSalesOrders(row) {
    // The SO list page reads ?focus=<so_number> to auto-open the
    // edit modal for that row. Mirrors the deep-link pattern the
    // Teams adaptive card uses so /backorders -> click -> SO modal
    // is the same path as Teams ping -> Open in Sentry -> SO modal.
    navigate(`/sales-orders?focus=${encodeURIComponent(row.so_number)}`);
  }

  return (
    <div>
      <PageHeader title="Backorders" />
      {successBanner && (
        <div
          role="status"
          style={{
            margin: '0 0 12px 0',
            padding: '8px 12px',
            background: 'var(--success-bg, #e8f5e9)',
            color: 'var(--success, #2e7d32)',
            border: '1px solid var(--success, #2e7d32)',
            borderRadius: 4,
            fontSize: 13,
          }}
        >
          {successBanner}
        </div>
      )}
      {actionError && (
        <div className="form-error" style={{ marginBottom: 12 }}>{actionError}</div>
      )}
      <div className="section">
        <div role="tablist" style={{ display: 'flex', gap: 4, marginBottom: 12 }}>
          {TABS.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              className={`btn ${tab === t.key ? 'btn-primary' : ''}`}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <DataTable
          rowKey="so_id"
          columns={columns}
          data={rows}
          loading={loading}
          emptyMessage={tab === 'waiting'
            ? 'No backorders waiting for stock.'
            : 'No backorders are ready to ship right now.'}
          onRowClick={openRowInSalesOrders}
        />
      </div>

      {cancelTarget && (
        <Modal
          title={`Cancel backorder ${cancelTarget.so_number}?`}
          onClose={closeCancel}
          footer={
            <>
              <button className="btn" onClick={closeCancel} disabled={cancelSubmitting}>
                Keep Backorder
              </button>
              <button
                className="btn btn-danger"
                onClick={submitCancel}
                disabled={cancelSubmitting}
              >
                {cancelSubmitting ? 'Cancelling...' : 'Cancel Backorder'}
              </button>
            </>
          }
        >
          {cancelError && (
            <div className="form-error" style={{ marginBottom: 12 }}>{cancelError}</div>
          )}
          <p style={{ fontSize: 13, marginBottom: 12 }}>
            Cancelling will mark <strong>{cancelTarget.so_number}</strong> as
            CANCELLED. The parent SO (<span className="mono">{cancelTarget.parent_so_number}</span>)
            is unaffected. A backorder.cancelled event fires on the
            integration outbox + Teams channel.
          </p>
          <div className="form-group">
            <label>Reason</label>
            <select
              className="form-select"
              value={cancelReason}
              onChange={(e) => setCancelReason(e.target.value)}
            >
              {CANCEL_REASONS.map((r) => (
                <option key={r.value} value={r.value}>{r.label}</option>
              ))}
            </select>
          </div>
        </Modal>
      )}
    </div>
  );
}
