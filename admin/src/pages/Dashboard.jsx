import { useState, useEffect, useMemo } from 'react';
import { api } from '../api.js';
import { formatDateOnly } from '../utils/date.js';
import { useWarehouse } from '../warehouse.jsx';
import { useAuth } from '../auth.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

// v1.8.0 (#299) productivity dashboard. Reads /api/v1/dashboard/
// productivity for the warehouse-scoped per-user metrics, and
// /api/v1/dashboard/preferences for the per-user chart_order +
// default_range + default_view. Single-file pattern matches
// SalesOrders.jsx + TransferOrders.jsx; the single-file layout was
// kept for codebase uniformity.

const COLOR_TOP = '#8e2715';   // Sentry red (top performer per card)
const COLOR_OTHER = '#c4722a'; // Copper (every other user)

const EVENT_LABELS = {
  // picking is measured in distinct orders, not units.
  picking:       { title: 'Picking',      unit: 'orders' },
  packing:       { title: 'Packing',      unit: 'units' },
  shipped:       { title: 'Shipped',      unit: 'orders' },
  received_skus: { title: 'Received',     unit: 'unique SKUs' },
  putaway_skus:  { title: 'Put Away',     unit: 'unique SKUs' },
};

const RANGE_PRESETS = [
  { key: 'today',     label: 'Today' },
  { key: 'yesterday', label: 'Yesterday' },
  { key: 'last_7d',   label: 'Last 7d' },
  { key: 'last_30d',  label: 'Last 30d' },
  { key: 'custom',    label: 'Custom' },
];

function isoDate(d) {
  // Local YYYY-MM-DD (avoids the toISOString() UTC shift that pushes
  // late-evening dates back a day in non-UTC zones).
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${dd}`;
}

function rangeForPreset(key, customStart, customEnd) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (key === 'today') return { start: isoDate(today), end: isoDate(today) };
  if (key === 'yesterday') {
    const y = new Date(today); y.setDate(y.getDate() - 1);
    return { start: isoDate(y), end: isoDate(y) };
  }
  if (key === 'last_7d') {
    const s = new Date(today); s.setDate(s.getDate() - 6);
    return { start: isoDate(s), end: isoDate(today) };
  }
  if (key === 'last_30d') {
    const s = new Date(today); s.setDate(s.getDate() - 29);
    return { start: isoDate(s), end: isoDate(today) };
  }
  return { start: customStart, end: customEnd };
}

function downloadProductivityCsv(payload) {
  const events = payload.events_visible || [];
  const headerCells = ['User', ...events.map((slug) => EVENT_LABELS[slug]?.title || slug), 'Total'];
  const rows = [headerCells.join(',')];
  for (const u of payload.users || []) {
    const cells = [
      u.display_name || u.username,
      ...events.map((slug) => String(u.metrics[slug] ?? 0)),
      String(u.total ?? 0),
    ];
    rows.push(cells.map((v) => (String(v).includes(',') ? `"${v}"` : v)).join(','));
  }
  const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `productivity-${payload.range?.start || 'today'}_${payload.range?.end || 'today'}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function EventCard({ slug, payload, onExpand, isExpanded }) {
  const total = payload.totals_per_event?.[slug] ?? 0;
  const users = (payload.users || []).filter((u) => (u.metrics?.[slug] ?? 0) > 0);
  users.sort((a, b) => (b.metrics[slug] || 0) - (a.metrics[slug] || 0));
  const top = users[0]?.metrics[slug] || 0;
  const meta = EVENT_LABELS[slug] || { title: slug, unit: '' };

  return (
    <div
      style={styles.card(isExpanded)}
      onClick={onExpand}
      role="button"
      tabIndex={0}
    >
      <div style={styles.cardHeader}>
        <span style={styles.cardTitle}>{meta.title}</span>
        <span style={styles.cardTotal}>{total}</span>
      </div>
      <div style={styles.cardSubheader}>{meta.unit}</div>
      {users.length === 0 ? (
        <div style={styles.cardEmpty}>No data for this range.</div>
      ) : (
        <div style={styles.barChart}>
          {users.map((u, idx) => {
            const value = u.metrics[slug] || 0;
            const ratio = top > 0 ? value / top : 0;
            const color = idx === 0 ? COLOR_TOP : COLOR_OTHER;
            return (
              <div key={u.user_id} style={styles.barRow}>
                <span style={styles.barLabel}>{u.display_name || u.username}</span>
                <div style={styles.barTrack}>
                  <div style={{ ...styles.barFill, width: `${Math.max(2, ratio * 100)}%`, background: color }} />
                </div>
                <span style={styles.barValue}>{value}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProductivityTable({ payload }) {
  const events = payload.events_visible || [];
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
      <thead>
        <tr style={{ borderBottom: '1px solid var(--border)' }}>
          <th style={{ ...styles.th, textAlign: 'left' }}>User</th>
          {events.map((slug) => (
            <th key={slug} style={{ ...styles.th, textAlign: 'right' }}>
              {EVENT_LABELS[slug]?.title || slug}
            </th>
          ))}
          <th style={{ ...styles.th, textAlign: 'right' }}>Total</th>
        </tr>
      </thead>
      <tbody>
        {(payload.users || []).map((u) => (
          <tr key={u.user_id} style={{ borderBottom: '1px solid var(--border)' }}>
            <td style={styles.td}>{u.display_name || u.username}</td>
            {events.map((slug) => (
              <td key={slug} style={{ ...styles.td, textAlign: 'right' }} className="mono">
                {u.metrics[slug] || 0}
              </td>
            ))}
            <td style={{ ...styles.td, textAlign: 'right', fontWeight: 600 }} className="mono">
              {u.total || 0}
            </td>
          </tr>
        ))}
        {(payload.users || []).length === 0 && (
          <tr>
            <td colSpan={events.length + 2} style={{ ...styles.td, color: 'var(--text-secondary)' }}>
              No data for this range.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

// Dashboard is a thin tab shell. The original productivity view became
// one of three tabs; the other two (Received Today, Shipping Health)
// live in this file as siblings so
// they share the warehouse context + page header. Tab selection is
// local-only state (no URL persistence yet -- can be added once
// stakeholders show they want shareable deep links).
export default function Dashboard() {
  const { warehouseId } = useWarehouse();
  const [tab, setTab] = useState('productivity');
  return (
    <div>
      <PageHeader title="Dashboard" />
      <div className="data-tabs" style={{ marginBottom: 16 }}>
        <button
          type="button"
          className={`data-tab${tab === 'productivity' ? ' active' : ''}`}
          onClick={() => setTab('productivity')}
        >
          Productivity
        </button>
        <button
          type="button"
          className={`data-tab${tab === 'received' ? ' active' : ''}`}
          onClick={() => setTab('received')}
        >
          Received
        </button>
        <button
          type="button"
          className={`data-tab${tab === 'shipping' ? ' active' : ''}`}
          onClick={() => setTab('shipping')}
        >
          Marketplace Health
        </button>
        <button
          type="button"
          className={`data-tab${tab === 'local-pickup' ? ' active' : ''}`}
          onClick={() => setTab('local-pickup')}
        >
          Local Pickup
        </button>
      </div>
      {tab === 'productivity' && <ProductivityView warehouseId={warehouseId} />}
      {tab === 'received' && <ReceivedTodayView warehouseId={warehouseId} />}
      {tab === 'shipping' && <ShippingHealthView warehouseId={warehouseId} />}
      {tab === 'local-pickup' && <LocalPickupView warehouseId={warehouseId} />}
    </div>
  );
}

function ProductivityView({ warehouseId }) {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState('');
  const [preferences, setPreferences] = useState({
    chart_order: ['picking', 'packing', 'shipped', 'received_skus', 'putaway_skus'],
    default_range: 'today',
    default_view: 'charts',
  });
  const [rangePreset, setRangePreset] = useState('today');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  const [view, setView] = useState('charts');
  const [expandedSlug, setExpandedSlug] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const [savingPrefs, setSavingPrefs] = useState(false);

  useEffect(() => { loadPreferences(); }, []);

  async function loadPreferences() {
    const res = await api.get('/v1/dashboard/preferences');
    if (res?.ok) {
      const data = await res.json();
      setPreferences(data);
      setRangePreset(data.default_range || 'today');
      setView(data.default_view || 'charts');
    }
  }

  useEffect(() => {
    if (!warehouseId) return;
    if (rangePreset === 'custom' && (!customStart || !customEnd)) return;
    loadProductivity();
  }, [warehouseId, rangePreset, customStart, customEnd]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function loadProductivity() {
    setError('');
    const range = rangeForPreset(rangePreset, customStart, customEnd);
    if (!range.start || !range.end) return;
    const qp = new URLSearchParams({
      start: range.start, end: range.end, warehouse_id: String(warehouseId),
    });
    // productivity is ADMIN-only; a USER hitting the home page should
    // not see "Forbidden" inline. Silent the popup
    // and on 403 just clear the payload so the widget shows nothing.
    const res = await api.get(
      `/v1/dashboard/productivity?${qp}`,
      { silentPermissionDenied: true },
    );
    if (res?.ok) {
      setPayload(await res.json());
    } else if (res?.status === 403) {
      setPayload(null);
    } else {
      const data = await res?.json();
      setPayload(null);
      setError(data?.error || 'Failed to load productivity');
    }
  }

  async function savePreferences(patch) {
    setSavingPrefs(true);
    const res = await api.put('/v1/dashboard/preferences', patch);
    if (res?.ok) {
      const data = await res.json();
      setPreferences(data);
    }
    setSavingPrefs(false);
  }

  function reorderChart(slug, direction) {
    const order = [...preferences.chart_order];
    const idx = order.indexOf(slug);
    if (idx < 0) return;
    const swap = idx + direction;
    if (swap < 0 || swap >= order.length) return;
    [order[idx], order[swap]] = [order[swap], order[idx]];
    savePreferences({ chart_order: order });
  }

  // Visible chart order: filter chart_order by events_visible (so
  // packing disappears when require_packing_before_shipping=false),
  // then append any new events not in the user's saved order.
  const visibleSlugs = useMemo(() => {
    const visible = payload?.events_visible || [];
    const ordered = (preferences.chart_order || []).filter((s) => visible.includes(s));
    for (const s of visible) {
      if (!ordered.includes(s)) ordered.push(s);
    }
    return ordered;
  }, [payload, preferences.chart_order]);

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: 4 }}>
          {RANGE_PRESETS.map((p) => (
            <button
              key={p.key}
              className={`btn btn-sm${rangePreset === p.key ? ' btn-primary' : ''}`}
              onClick={() => setRangePreset(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>
        {rangePreset === 'custom' && (
          <>
            <input
              type="date"
              className="form-input"
              style={{ width: 150 }}
              value={customStart}
              onChange={(e) => setCustomStart(e.target.value)}
            />
            <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>to</span>
            <input
              type="date"
              className="form-input"
              style={{ width: 150 }}
              value={customEnd}
              onChange={(e) => setCustomEnd(e.target.value)}
            />
          </>
        )}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          <button
            className={`btn btn-sm${view === 'charts' ? ' btn-primary' : ''}`}
            onClick={() => setView('charts')}
          >
            Charts
          </button>
          <button
            className={`btn btn-sm${view === 'table' ? ' btn-primary' : ''}`}
            onClick={() => setView('table')}
          >
            Table
          </button>
          {view === 'table' && payload && (
            <button
              className="btn btn-sm"
              onClick={() => downloadProductivityCsv(payload)}
              title="Download CSV"
            >
              Export CSV
            </button>
          )}
          <button
            className="btn btn-sm"
            onClick={() => setShowSettings(true)}
            title="Dashboard settings"
            aria-label="Dashboard settings"
          >
            &#9881;
          </button>
        </div>
      </div>

      {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}

      {!payload ? (
        <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-secondary)' }}>
          Loading...
        </div>
      ) : view === 'table' ? (
        <ProductivityTable payload={payload} />
      ) : expandedSlug ? (
        <div>
          <button className="btn btn-sm" onClick={() => setExpandedSlug(null)} style={{ marginBottom: 12 }}>
            &larr; Back to grid
          </button>
          <div style={styles.expandedShell}>
            <EventCard
              slug={expandedSlug}
              payload={payload}
              onExpand={() => {}}
              isExpanded
            />
          </div>
        </div>
      ) : (
        <div style={styles.grid}>
          {visibleSlugs.map((slug) => (
            <EventCard
              key={slug}
              slug={slug}
              payload={payload}
              onExpand={() => setExpandedSlug(slug)}
            />
          ))}
        </div>
      )}

      {showSettings && (
        <Modal
          title="Dashboard settings"
          onClose={() => setShowSettings(false)}
          footer={<button className="btn" onClick={() => setShowSettings(false)}>Close</button>}
        >
          <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
            Per-user preferences. Saves on every change. {savingPrefs && '(saving...)'}
          </p>
          <div className="form-group">
            <label>Default range</label>
            <select
              className="form-select"
              value={preferences.default_range}
              onChange={(e) => savePreferences({ default_range: e.target.value })}
            >
              {RANGE_PRESETS.map((p) => (
                <option key={p.key} value={p.key}>{p.label}</option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label>Default view</label>
            <select
              className="form-select"
              value={preferences.default_view}
              onChange={(e) => savePreferences({ default_view: e.target.value })}
            >
              <option value="charts">Charts</option>
              <option value="table">Table</option>
            </select>
          </div>
          <div className="form-group">
            <label>Chart order</label>
            <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
              {(preferences.chart_order || []).map((slug, idx) => (
                <li
                  key={slug}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 8,
                    padding: '6px 8px',
                    borderBottom: '1px solid var(--border)',
                  }}
                >
                  <span style={{ flex: 1 }}>
                    {EVENT_LABELS[slug]?.title || slug}
                  </span>
                  <button
                    className="btn btn-sm"
                    onClick={() => reorderChart(slug, -1)}
                    disabled={idx === 0}
                    aria-label="Move up"
                  >&#8593;</button>
                  <button
                    className="btn btn-sm"
                    onClick={() => reorderChart(slug, 1)}
                    disabled={idx === preferences.chart_order.length - 1}
                    aria-label="Move down"
                  >&#8595;</button>
                </li>
              ))}
            </ul>
          </div>
        </Modal>
      )}
    </div>
  );
}

const styles = {
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
    gap: 16,
  },
  expandedShell: {
    maxWidth: 800,
  },
  card: (expanded) => ({
    background: 'var(--card-bg, #fff)',
    border: '1px solid var(--border)',
    borderRadius: 8,
    padding: 16,
    cursor: 'pointer',
    minHeight: expanded ? 480 : 220,
  }),
  cardHeader: {
    display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
    marginBottom: 4,
  },
  cardTitle: { fontSize: 14, fontWeight: 600 },
  cardTotal: { fontSize: 22, fontWeight: 700, color: COLOR_TOP, fontFamily: 'monospace' },
  cardSubheader: { fontSize: 11, color: 'var(--text-secondary)', marginBottom: 12 },
  cardEmpty: { fontSize: 12, color: 'var(--text-secondary)', textAlign: 'center', padding: 20 },
  barChart: { display: 'flex', flexDirection: 'column', gap: 6 },
  barRow: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 },
  barLabel: { width: 90, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  barTrack: { flex: 1, height: 14, background: 'var(--border)', borderRadius: 4 },
  barFill: { height: '100%', borderRadius: 4 },
  barValue: { width: 40, textAlign: 'right', fontFamily: 'monospace' },
  th: { padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 },
  td: { padding: '6px 8px' },
};


// Shared date-range picker for the Received + Shipping Health tabs.
// Mirrors the Productivity tab's RANGE_PRESETS controls so the three
// views read as a coherent set; the parent owns rangePreset +
// customStart / customEnd state so the dashboard can persist a
// single range choice across tab switches if we want that later.
function RangeControls({
  rangePreset, setRangePreset,
  customStart, setCustomStart, customEnd, setCustomEnd,
  loading, onRefresh, trailingChildren,
}) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12,
      marginBottom: 16, flexWrap: 'wrap',
    }}>
      <div style={{ display: 'flex', gap: 4 }}>
        {RANGE_PRESETS.map((p) => (
          <button
            key={p.key}
            className={`btn btn-sm${rangePreset === p.key ? ' btn-primary' : ''}`}
            onClick={() => setRangePreset(p.key)}
          >
            {p.label}
          </button>
        ))}
      </div>
      {rangePreset === 'custom' && (
        <>
          <input
            type="date"
            className="form-input"
            style={{ width: 150 }}
            value={customStart}
            onChange={(e) => setCustomStart(e.target.value)}
          />
          <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>to</span>
          <input
            type="date"
            className="form-input"
            style={{ width: 150 }}
            value={customEnd}
            onChange={(e) => setCustomEnd(e.target.value)}
          />
        </>
      )}
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 4, alignItems: 'center' }}>
        {trailingChildren}
        <button className="btn btn-sm" onClick={onRefresh} disabled={loading}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>
    </div>
  );
}


// ── Received ───────────────────────────────────────────────────────────────

function ReceivedTodayView({ warehouseId }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [rangePreset, setRangePreset] = useState('today');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');

  useEffect(() => {
    if (!warehouseId) return;
    if (rangePreset === 'custom' && (!customStart || !customEnd)) return;
    load();
  }, [warehouseId, rangePreset, customStart, customEnd]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function load() {
    const range = rangeForPreset(rangePreset, customStart, customEnd);
    if (!range.start || !range.end) return;
    setLoading(true);
    const qp = new URLSearchParams({
      warehouse_id: String(warehouseId),
      start: range.start, end: range.end,
    });
    const res = await api.get(
      `/v1/dashboard/received?${qp}`,
      { silentPermissionDenied: true },
    );
    setLoading(false);
    if (!res?.ok) return;
    const data = await res.json();
    setRows(data.pos || []);
  }

  const totalUnits = rows.reduce((acc, r) => acc + (r.units_received || 0), 0);
  const totalLines = rows.reduce((acc, r) => acc + (r.lines_received || 0), 0);
  const distinctReceivers = new Set(rows.flatMap((r) => r.receivers || []));

  return (
    <div>
      <RangeControls
        rangePreset={rangePreset} setRangePreset={setRangePreset}
        customStart={customStart} setCustomStart={setCustomStart}
        customEnd={customEnd} setCustomEnd={setCustomEnd}
        loading={loading} onRefresh={load}
      />
      <div style={{
        display: 'flex', gap: 16, marginBottom: 16, fontSize: 13,
        color: 'var(--text-secondary)', flexWrap: 'wrap', alignItems: 'center',
      }}>
        <span><strong style={{ color: 'var(--text)' }}>{rows.length}</strong> POs received</span>
        <span><strong style={{ color: 'var(--text)' }}>{totalLines}</strong> lines</span>
        <span><strong style={{ color: 'var(--text)' }}>{totalUnits}</strong> units</span>
        <span><strong style={{ color: 'var(--text)' }}>{distinctReceivers.size}</strong> receivers active</span>
      </div>

      {!loading && rows.length === 0 && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          No POs received units in this range.
        </p>
      )}

      {rows.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>PO #</th>
              <th>Vendor</th>
              <th>Status</th>
              <th style={{ textAlign: 'right' }}>Lines</th>
              <th style={{ textAlign: 'right' }}>Units</th>
              <th>Receiver(s)</th>
              <th>Last received</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.po_id}>
                <td className="mono">{r.po_number}</td>
                <td>{r.vendor_name || '-'}</td>
                <td>{r.status}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.lines_received}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.units_received}</td>
                <td>{(r.receivers || []).join(', ') || '-'}</td>
                <td className="mono" style={{ fontSize: 12 }}>
                  {r.last_received_at ? new Date(r.last_received_at).toLocaleTimeString() : '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}


// -- Marketplace Health ---------------------------------------------------
//
// The signal the desk reads first: how many orders need to ship today
// per marketplace (Amazon / eBay / BigCommerce / Phone Orders). Above
// the bubbles, a one-line summary of total orders received per
// marketplace today gives context for the bubble counts. Channel
// identity is sales_orders.order_origin (free-text label written by
// the inbound mapping); the backend pins the set to four DB values
// and the UI translates them to display labels here.

function ShippingHealthView({ warehouseId }) {
  const [data, setData] = useState({ by_source: [] });
  const [loading, setLoading] = useState(false);
  const [rangePreset, setRangePreset] = useState('today');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  const [focused, setFocused] = useState(null);

  useEffect(() => {
    if (!warehouseId) return;
    if (rangePreset === 'custom' && (!customStart || !customEnd)) return;
    load();
  }, [warehouseId, rangePreset, customStart, customEnd]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function load() {
    const range = rangeForPreset(rangePreset, customStart, customEnd);
    if (!range.start || !range.end) return;
    setLoading(true);
    const qp = new URLSearchParams({
      warehouse_id: String(warehouseId),
      start: range.start, end: range.end,
    });
    const res = await api.get(
      `/v1/dashboard/shipping-health?${qp}`,
      { silentPermissionDenied: true },
    );
    setLoading(false);
    if (!res?.ok) return;
    setData(await res.json());
  }

  const rows = data.by_source || [];

  return (
    <div>
      <RangeControls
        rangePreset={rangePreset} setRangePreset={setRangePreset}
        customStart={customStart} setCustomStart={setCustomStart}
        customEnd={customEnd} setCustomEnd={setCustomEnd}
        loading={loading} onRefresh={load}
      />

      <h3 style={{ fontSize: 14, fontWeight: 600, margin: '0 0 12px 0' }}>
        Marketplace Breakdown
      </h3>

      {rows.length === 0 && !loading && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          No marketplace channels are configured. Add one per channel under
          Settings &gt; Marketplace Health to see its health bubble here.
        </p>
      )}

      {/* Top section: per-marketplace Received + Shipped totals
          for the selected range. Same auto-fit wrap pattern as the
          bubbles below so the layout stays consistent on narrow
          windows. */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: 12,
        marginBottom: 16,
      }}>
        {rows.map((r) => (
          <MarketplaceTotalsCard key={r.order_origin} row={r} />
        ))}
      </div>

      <div style={{
        marginBottom: 16,
        borderTop: '3px solid var(--border-dark)',
      }} />

      <h3 style={{ fontSize: 14, fontWeight: 600, margin: '0 0 12px 0' }}>
        Orders that need to ship today
      </h3>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: 12,
        marginBottom: 24,
      }}>
        {rows.map((r) => (
          <ShipTodayBubble
            key={r.order_origin}
            row={r}
            onClick={() => setFocused(r)}
          />
        ))}
      </div>

      {focused && (
        <Modal
          title={`${focused.label} - orders that need to ship today`}
          onClose={() => setFocused(null)}
          size="wide"
          footer={
            <button className="btn btn-primary" onClick={() => setFocused(null)}>
              Close
            </button>
          }
        >
          {(focused.orders || []).length === 0 ? (
            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
              No orders to show.
            </p>
          ) : (
            <table className="lines-table">
              <thead>
                <tr>
                  <th>SO #</th>
                  <th>Customer</th>
                  <th>Status</th>
                  <th>Ship by</th>
                </tr>
              </thead>
              <tbody>
                {focused.orders.map((o) => (
                  <tr key={o.so_id}>
                    <td className="mono">{o.so_number}</td>
                    <td>{o.customer_name || '-'}</td>
                    <td>{o.status}</td>
                    <td className="mono" style={{ fontSize: 12, color: 'var(--accent)' }}>
                      {o.ship_by_date ? formatDateOnly(o.ship_by_date) : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Modal>
      )}
    </div>
  );
}

// Top-section card: per-marketplace Orders Received + Orders Shipped
// totals for the selected range. Two big stats side-by-side. The desk
// reads this for channel-mix context; the action signal lives in the
// bubble row below.
function MarketplaceTotalsCard({ row }) {
  return (
    <div className="card" style={{ padding: 16 }}>
      <div style={{
        marginBottom: 12, paddingBottom: 8,
        borderBottom: '1px solid var(--border)',
        textAlign: 'center',
      }}>
        <span style={{ fontSize: 16, fontWeight: 600 }}>
          {row.label}
        </span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{
            fontSize: 28, fontWeight: 700,
            fontFamily: 'var(--mono, monospace)',
            color: 'var(--text)',
            lineHeight: 1,
          }}>
            {row.orders_received || 0}
          </div>
          <div style={{
            fontSize: 11, marginTop: 4,
            color: 'var(--text-secondary)',
            textTransform: 'uppercase', letterSpacing: 0.4,
          }}>
            Orders Received
          </div>
        </div>
        <div style={{ textAlign: 'center' }}>
          <div style={{
            fontSize: 28, fontWeight: 700,
            fontFamily: 'var(--mono, monospace)',
            color: 'var(--text)',
            lineHeight: 1,
          }}>
            {row.orders_shipped || 0}
          </div>
          <div style={{
            fontSize: 11, marginTop: 4,
            color: 'var(--text-secondary)',
            textTransform: 'uppercase', letterSpacing: 0.4,
          }}>
            Orders Shipped
          </div>
        </div>
      </div>
    </div>
  );
}

// One channel bubble. Shows the number of orders past ship_by_date
// today (or due today) that have not shipped or been cancelled -- the
// number the desk acts on. Click a non-zero bubble to open the SO
// list; zero renders as a green check, non-interactive.
function ShipTodayBubble({ row, onClick }) {
  const needToShip = row.need_to_ship_today || 0;
  const sharedStyle = {
    padding: 20,
    display: 'flex', flexDirection: 'column', alignItems: 'center',
    gap: 8,
    border: needToShip > 0
      ? '2px solid var(--accent)'
      : '1px solid var(--border-dark)',
    background: 'var(--white)',
    fontFamily: 'inherit',
    textAlign: 'center',
  };
  const label = (
    <span style={{ fontSize: 14, fontWeight: 600 }}>
      {row.label}
    </span>
  );
  if (needToShip > 0) {
    return (
      <button
        type="button"
        className="card"
        onClick={onClick}
        style={{ ...sharedStyle, cursor: 'pointer' }}
        title="Show the SO list behind this count"
      >
        {label}
        <span style={{
          fontSize: 44, fontWeight: 700,
          fontFamily: 'var(--mono, monospace)',
          color: 'var(--accent)',
          lineHeight: 1,
        }}>
          {needToShip}
        </span>
      </button>
    );
  }
  return (
    <div className="card" style={sharedStyle}>
      {label}
      <span
        style={{
          fontSize: 44, lineHeight: 1, fontWeight: 700,
          color: '#1f9d55',
        }}
        aria-label="caught up"
        title="No orders need to ship for this channel"
      >
        &#10003;
      </span>
    </div>
  );
}


// -- Local Pickup -----------------------------------------------------------
//
// Daily worklist for the retail shop: every sales order whose ship_method
// names a local pickup / will-call (server filter local_pickup=true, which
// matches "local" OR "pickup"). The counter worker searches by customer or
// order number, narrows by status, edits an order in place, and hits the
// red "Picked Up?" button when a customer collects -- that reuses the SO
// edit endpoint's PICKED/PACKED -> SHIPPED transition, which waives the
// tracking number for pickups. Marking shipped and editing a non-OPEN order
// both need ADMIN or the so-full-edit override, so the controls disable
// (with a reason) for a plain USER and the worklist explains the 403.

const LOCAL_PICKUP_STATUS_OPTIONS = ['All', 'OPEN', 'PICKED', 'PACKED', 'SHIPPED', 'CANCELLED'];
const SHIPPABLE_STATUSES = new Set(['PICKED', 'PACKED']);

function LocalPickupView({ warehouseId }) {
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';
  const hasSOFullEdit = isAdmin || (user?.allowed_overrides || []).includes('so-full-edit');

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [forbidden, setForbidden] = useState(false);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('All');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [editing, setEditing] = useState(null);
  const [shippingId, setShippingId] = useState(null);
  const [confirming, setConfirming] = useState(null);  // SO awaiting pickup confirm
  const [alertMsg, setAlertMsg] = useState(null);       // {title, message} loud modal

  // Debounce the search box so a request does not fire on every keystroke.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  useEffect(() => {
    if (!warehouseId) return;
    load();
  }, [warehouseId, debouncedSearch, statusFilter]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function load() {
    setLoading(true);
    setError('');
    const qp = new URLSearchParams({
      local_pickup: 'true',
      warehouse_id: String(warehouseId),
      per_page: '1000',
    });
    if (debouncedSearch) qp.set('q', debouncedSearch);
    if (statusFilter !== 'All') qp.set('status', statusFilter);
    const res = await api.get(`/admin/sales-orders?${qp}`, { silentPermissionDenied: true });
    setLoading(false);
    if (!res) return;
    if (res.status === 403) { setForbidden(true); setRows([]); setTotal(0); return; }
    setForbidden(false);
    if (!res.ok) { setError('Could not load local pickup orders.'); return; }
    const data = await res.json();
    setRows(data.sales_orders || []);
    setTotal(data.total ?? (data.sales_orders || []).length);
  }

  // Clicking the red button never silently no-ops: if the order can't be
  // picked up (wrong status, or no permission) we say so loudly in a modal;
  // otherwise we open the JSX confirm modal. The real ship runs in doPickup.
  function requestPickup(so) {
    if (!(isAdmin || hasSOFullEdit)) {
      setAlertMsg({
        title: 'Permission needed',
        message: `You can't mark ${so.so_number} picked up. This needs the `
          + 'ADMIN role or the so-full-edit override on your account.',
      });
      return;
    }
    if (!SHIPPABLE_STATUSES.has(so.status)) {
      setAlertMsg({
        title: 'Not ready for pickup',
        message: `${so.so_number} is ${so.status}, not picked yet. An order `
          + 'must be PICKED or PACKED before it can be marked picked up.',
      });
      return;
    }
    setConfirming(so);
  }

  async function doPickup(so) {
    setShippingId(so.so_id);
    // Reuse the SO edit endpoint's ship transition. ship_method already
    // names a local pickup, so the backend waives the tracking number.
    const res = await api.put(
      `/admin/sales-orders/${so.so_id}`,
      { status: 'SHIPPED' },
      { silentPermissionDenied: true },
    );
    setShippingId(null);
    setConfirming(null);
    if (!res) return;
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setAlertMsg({
        title: 'Could not mark picked up',
        message: res.status === 403
          ? `Not allowed: marking ${so.so_number} picked up needs ADMIN or the so-full-edit override.`
          : (data.error || `Could not mark ${so.so_number} picked up (status ${res.status}).`),
      });
      return;
    }
    load();
  }

  if (!warehouseId) {
    return <div style={{ padding: 24, color: 'var(--text-secondary)' }}>Select a warehouse.</div>;
  }
  if (forbidden) {
    return (
      <div style={{ padding: 24, color: 'var(--text-secondary)', maxWidth: 520 }}>
        You do not have access to sales orders. Ask an admin to grant the
        Sales Orders page permission, plus the so-full-edit override to mark
        pickups complete.
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap', alignItems: 'center' }}>
        <input
          className="form-input"
          style={{ width: 280 }}
          placeholder="Search by customer or order #"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="form-select"
          style={{ width: 170 }}
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          {LOCAL_PICKUP_STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>{s === 'All' ? 'All statuses' : s}</option>
          ))}
        </select>
        <span style={{ marginLeft: 'auto', fontSize: 13, color: 'var(--text-secondary)' }}>
          {loading ? 'Loading…' : `${rows.length} order${rows.length === 1 ? '' : 's'}`}
        </span>
        <button className="btn btn-sm" onClick={load} disabled={loading}>Refresh</button>
      </div>

      {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
      {total > rows.length && (
        <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
          Showing {rows.length} of {total}. Narrow with the status filter or search.
        </div>
      )}

      {!loading && rows.length === 0 ? (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No local pickup orders match.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Order #</th>
              <th>Customer</th>
              <th>Status</th>
              <th>Order Date</th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((so) => {
              const status = so.status;
              const canEdit = isAdmin || hasSOFullEdit || status === 'OPEN';
              return (
                <tr key={so.so_id}>
                  <td className="mono">{so.so_number}</td>
                  <td>{so.customer_name || '-'}</td>
                  <td><StatusTag status={status} /></td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {so.order_date ? formatDateOnly(so.order_date)
                      : (so.created_at ? formatDateOnly(so.created_at) : '-')}
                  </td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <button
                      className="btn btn-sm"
                      style={{ marginRight: 8 }}
                      onClick={() => setEditing(so)}
                      disabled={!canEdit}
                      title={canEdit ? 'Edit order' : 'Editing a picked order needs ADMIN or so-full-edit'}
                    >
                      Edit
                    </button>
                    {status === 'SHIPPED' ? (
                      <span style={{ fontSize: 12, color: '#1f9d55', fontWeight: 600 }}>Picked up &#10003;</span>
                    ) : (
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => requestPickup(so)}
                        disabled={shippingId === so.so_id}
                        title="Mark this order picked up (ships it)"
                      >
                        {shippingId === so.so_id ? 'Working…' : 'Picked Up?'}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {editing && (
        <LocalPickupEditModal
          so={editing}
          canEditAll={isAdmin || hasSOFullEdit}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); }}
        />
      )}

      {confirming && (
        <Modal
          title="Mark picked up?"
          onClose={() => (shippingId == null ? setConfirming(null) : null)}
          footer={(
            <>
              <button className="btn" onClick={() => setConfirming(null)} disabled={shippingId != null}>
                Cancel
              </button>
              <button
                className="btn btn-danger"
                onClick={() => doPickup(confirming)}
                disabled={shippingId != null}
              >
                {shippingId != null ? 'Working…' : 'Yes, mark picked up'}
              </button>
            </>
          )}
        >
          <p style={{ marginTop: 0 }}>
            Mark order <strong>{confirming.so_number}</strong>
            {confirming.customer_name ? ` (${confirming.customer_name})` : ''} as picked up?
          </p>
          <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginBottom: 0 }}>
            This marks it <strong>SHIPPED</strong> -- it records the handoff and releases the
            order. It cannot be undone from here.
          </p>
        </Modal>
      )}

      {alertMsg && (
        <Modal
          title={alertMsg.title}
          onClose={() => setAlertMsg(null)}
          footer={<button className="btn btn-primary" onClick={() => setAlertMsg(null)}>OK</button>}
        >
          <p style={{ margin: 0 }}>{alertMsg.message}</p>
        </Modal>
      )}
    </div>
  );
}

// In-place edit of the fields a counter worker touches on a pickup order.
// Full line-item edits stay on the Sales Orders page; this is the narrow
// header surface (PUT /admin/sales-orders/<id>). Prefills from the list row
// -- the list already returns every field shown here -- so no extra fetch.
function LocalPickupEditModal({ so, canEditAll, onClose, onSaved }) {
  const editable = canEditAll || so.status === 'OPEN';
  const [form, setForm] = useState({
    customer_name: so.customer_name || '',
    customer_phone: so.customer_phone || '',
    ship_method: so.ship_method || '',
    ship_address: so.ship_address || '',
    priority: so.priority ?? 0,
    memo: so.memo || '',
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function save() {
    setSaving(true);
    setError('');
    const body = {
      customer_name: form.customer_name || null,
      customer_phone: form.customer_phone || null,
      ship_method: form.ship_method || null,
      ship_address: form.ship_address || null,
      priority: Number(form.priority) || 0,
      memo: form.memo || null,
    };
    const res = await api.put(`/admin/sales-orders/${so.so_id}`, body, { silentPermissionDenied: true });
    setSaving(false);
    if (!res) return;
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setError(
        res.status === 403
          ? 'Not allowed: editing a picked order needs ADMIN or the so-full-edit override.'
          : (data.error || 'Could not save changes.'),
      );
      return;
    }
    onSaved();
  }

  return (
    <Modal
      title={`Edit ${so.so_number}`}
      onClose={onClose}
      footer={(
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={save} disabled={saving || !editable}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </>
      )}
    >
      {!editable && (
        <div className="form-error" style={{ marginBottom: 12 }}>
          This order is {so.status}; editing it needs ADMIN or the so-full-edit override.
        </div>
      )}
      {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
      <div className="form-group">
        <label>Customer name</label>
        <input
          className="form-input" disabled={!editable} value={form.customer_name}
          onChange={(e) => setForm({ ...form, customer_name: e.target.value })}
        />
      </div>
      <div className="form-group">
        <label>Customer phone</label>
        <input
          className="form-input" disabled={!editable} value={form.customer_phone}
          onChange={(e) => setForm({ ...form, customer_phone: e.target.value })}
        />
      </div>
      <div className="form-group">
        <label>Ship method</label>
        <input
          className="form-input" disabled={!editable} value={form.ship_method}
          onChange={(e) => setForm({ ...form, ship_method: e.target.value })}
        />
      </div>
      <div className="form-group">
        <label>Pickup / ship address</label>
        <input
          className="form-input" disabled={!editable} value={form.ship_address}
          onChange={(e) => setForm({ ...form, ship_address: e.target.value })}
        />
      </div>
      <div className="form-group">
        <label>Priority</label>
        <input
          type="number" min="0" max="10" className="form-input" disabled={!editable}
          value={form.priority}
          onChange={(e) => setForm({ ...form, priority: e.target.value })}
        />
      </div>
      <div className="form-group">
        <label>Memo</label>
        <textarea
          className="form-input" rows={3} disabled={!editable} value={form.memo}
          onChange={(e) => setForm({ ...form, memo: e.target.value })}
        />
      </div>
    </Modal>
  );
}
