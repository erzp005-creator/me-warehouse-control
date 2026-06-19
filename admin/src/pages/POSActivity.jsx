import { useEffect, useState } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import './POSActivity.css';

// POS Activity -- retail revenue dashboard. Net revenue + today-vs-yesterday
// pace, this-week-vs-last-week trend, channel (counter/phone) + tender
// (card/cash) splits, full KPIs, and the order list. Backed by
// /api/admin/pos/{summary,sales-orders}.

const STATUS_OPTIONS = ['', 'OPEN', 'ALLOCATED', 'PICKED', 'PACKED', 'SHIPPED', 'CANCELLED'];
const ORDER_TYPE_OPTIONS = ['sale', 'refund', 'all'];
const CHANNEL_OPTIONS = [['', 'All Channels'], ['counter', 'Counter (POS)'], ['phone', 'Phone Order']];
const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const fmtUsd = (c) => (c == null ? '-' : usd.format(c / 100));
function fmtShort(c) {
  const d = (c || 0) / 100;
  if (Math.abs(d) >= 1000) return `$${(d / 1000).toFixed(Math.abs(d) >= 10000 ? 0 : 1)}k`;
  return `$${Math.round(d)}`;
}
function fmtHour(h) { const ap = h < 12 ? 'a' : 'p'; let x = h % 12; if (x === 0) x = 12; return `${x}${ap}`; }
function fmtTs(iso) {
  if (!iso) return '-';
  try { return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); }
  catch { return iso; }
}
function pct(part, whole) { return whole ? Math.round((part / whole) * 100) : 0; }
const todayStr = () => new Date().toLocaleDateString('en-CA', { timeZone: 'America/Denver' }); // YYYY-MM-DD
function addDays(ymd, n) { const d = new Date(`${ymd}T12:00:00`); d.setDate(d.getDate() + n); return d.toLocaleDateString('en-CA'); }
function dateLabel(ymd, isToday) {
  if (isToday) return 'Today';
  if (ymd === addDays(todayStr(), -1)) return 'Yesterday';
  try { return new Date(`${ymd}T12:00:00`).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }); }
  catch { return ymd; }
}

export default function POSActivity() {
  const [summary, setSummary] = useState(null);
  const [salesOrders, setSalesOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [orderType, setOrderType] = useState('sale');
  const [channel, setChannel] = useState('');
  const [status, setStatus] = useState('');
  const [terminalFilter, setTerminalFilter] = useState('');
  const [date, setDate] = useState(todayStr());
  const [loading, setLoading] = useState(false);

  function loadSummary() {
    api.get(`/admin/pos/summary?date=${date}`).then(async (r) => { if (r?.ok) setSummary(await r.json()); });
  }
  function loadOrders() {
    const p = new URLSearchParams({ page, per_page: 50, order_type: orderType, date });
    if (channel) p.set('channel', channel);
    if (status) p.set('status', status);
    if (terminalFilter) p.set('terminal_id', terminalFilter);
    setLoading(true);
    api.get(`/admin/pos/sales-orders?${p}`).then(async (r) => {
      setLoading(false);
      if (!r?.ok) return;
      const d = await r.json();
      setSalesOrders(d.sales_orders || []);
      setPagination({ page: d.page, pages: d.pages, total: d.total, per_page: d.per_page });
    });
  }
  useEffect(() => { loadSummary(); }, [date]);
  useEffect(() => { loadOrders(); }, [page, orderType, channel, status, terminalFilter, date]);

  const t = summary?.today || {};
  const wk = summary?.week || {};
  const terminals = summary?.active_terminals || [];
  const net = t.net_cents || 0;
  const vsY = summary?.vs_yesterday_cents || 0;
  const wkDelta = pct((wk.this_total_cents || 0) - (wk.last_total_cents || 0), wk.last_total_cents || 0);
  const asOf = new Date().toLocaleString('en-US', { timeZone: 'America/Denver', hour: 'numeric', minute: '2-digit' });
  const isToday = summary ? summary.is_today : date === todayStr();
  const selLabel = dateLabel(summary?.date || date, isToday);
  const priorLabel = isToday ? 'yesterday' : 'prior day';

  const columns = [
    { key: 'so_number', label: 'SO #', mono: true },
    { key: 'created_at', label: 'Date', render: (r) => fmtTs(r.created_at) },
    { key: 'channel', label: 'Channel', render: (r) => r.channel || 'POS' },
    { key: 'status', label: 'Status' },
    { key: 'terminal_id', label: 'Terminal', mono: true, render: (r) => r.terminal_id || '-' },
    { key: 'customer_name', label: 'Customer', render: (r) => r.customer_name || '(walk-in)' },
    { key: 'total_cents', label: 'Total', render: (r) => fmtUsd(r.total_cents) },
    { key: 'payment_method', label: 'Tender', render: (r) => r.payment_method || '-' },
    { key: 'external_txn_ref', label: 'Windcave Ref', mono: true, render: (r) => r.external_txn_ref || '-' },
  ];

  return (
    <div>
      <PageHeader title="POS Activity">
        <div className="pos-datebar">
          <button className={`pos-dbtn${date === todayStr() ? ' on' : ''}`} onClick={() => { setDate(todayStr()); setPage(1); }}>Today</button>
          <button className={`pos-dbtn${date === addDays(todayStr(), -1) ? ' on' : ''}`} onClick={() => { setDate(addDays(todayStr(), -1)); setPage(1); }}>Yesterday</button>
          <input type="date" className="form-input pos-dateinput" value={date} max={todayStr()}
            onChange={(e) => { if (e.target.value) { setDate(e.target.value); setPage(1); } }} />
          <button className="btn" onClick={() => { loadSummary(); loadOrders(); }}>Refresh</button>
        </div>
      </PageHeader>

      <div className="pos-grid">
        {/* Hero: net revenue + pace */}
        <div className="pos-card">
          <div className="pos-hero-top">
            <div>
              <div className="pos-eyebrow">Net Revenue · {selLabel}</div>
              <div className="pos-net">{fmtUsd(net)}</div>
              <DeltaPill cents={vsY} suffix={`vs. ${priorLabel}`} />
              <div className="pos-net-sub">
                <b>{t.sales_count || 0}</b> sales · <b>{t.refund_count || 0}</b> refunds · avg <b>{fmtUsd(t.avg_sale_cents || 0)}</b>
              </div>
            </div>
            <div className="pos-asof">
              {isToday ? `as of ${asOf} MT` : selLabel}
              <div className="pos-term-chips">
                {terminals.length === 0
                  ? <span className="pos-chip">no active terminals</span>
                  : terminals.map((id) => <span className="pos-chip" key={id}><span className="dot" />{id}</span>)}
              </div>
            </div>
          </div>
          <div className="pos-legend">
            <span><i className="sw today" /> {selLabel}</span>
            <span><i className="sw yest" /> {isToday ? 'Yesterday' : 'Prior day'}</span>
          </div>
          <PaceCurve pace={summary?.pace} currentHour={summary?.current_hour ?? 23} />
        </div>

        {/* Full KPIs */}
        <div className="pos-kpis">
          <Kpi label="This Week" value={fmtUsd(wk.this_total_cents || 0)}
            sub={<DeltaText pct={wkDelta} suffix="vs last week" />} rail="var(--accent)" />
          <Kpi label="Counter (POS)" value={fmtUsd(t.counter_cents || 0)}
            sub={`${t.counter_count || 0} sales · ${pct(t.counter_cents, t.sales_total_cents)}%`} rail="var(--copper)" />
          <Kpi label="Phone Orders" value={fmtUsd(t.phone_cents || 0)}
            sub={`${t.phone_count || 0} orders · ${pct(t.phone_cents, t.sales_total_cents)}%`} rail="#1e5f8a" />
          <Kpi label="Avg Sale" value={fmtUsd(t.avg_sale_cents || 0)} sub="per transaction" rail="var(--purple)" />
          <Kpi label="Refunds" value={t.refund_count || 0} sub={fmtUsd(t.refund_total_cents || 0)} rail="var(--danger)" />
          <Kpi label="Active Terminals" value={terminals.length || '-'} sub={`last 24h${terminals.length ? ' · ' + terminals.join(', ') : ''}`} rail="var(--success)" />
        </div>

        {/* Trend + composition */}
        <div className="pos-row2">
          <div className="pos-card">
            <div className="pos-panel-title">Daily revenue · this week vs last</div>
            <div className="pos-panel-note">
              {fmtUsd(wk.this_total_cents || 0)} this week ·{' '}
              <DeltaText pct={wkDelta} suffix="vs last week" inline />
            </div>
            <WeekBars week={summary?.week} />
          </div>
          <div className="pos-card">
            <div className="pos-panel-title">Today's revenue mix</div>
            <div className="pos-panel-note">how the money came in</div>
            <SplitBar title="Channel" segs={[
              { label: 'Counter', cents: t.counter_cents || 0, color: '#c4722a' },
              { label: 'Phone', cents: t.phone_cents || 0, color: '#1e5f8a' },
            ]} />
            <SplitBar title="Tender" segs={tenderSegs(summary?.tenders)} />
          </div>
        </div>

        {/* Filters + table */}
        <div className="filter-bar">
          <select className="form-select" value={orderType} onChange={(e) => { setOrderType(e.target.value); setPage(1); }} style={{ width: 'auto' }}>
            {ORDER_TYPE_OPTIONS.map((v) => <option key={v} value={v}>{v === 'all' ? 'All Types' : v[0].toUpperCase() + v.slice(1)}</option>)}
          </select>
          <select className="form-select" value={channel} onChange={(e) => { setChannel(e.target.value); setPage(1); }} style={{ width: 'auto' }}>
            {CHANNEL_OPTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <select className="form-select" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} style={{ width: 'auto' }}>
            {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s || 'All Statuses'}</option>)}
          </select>
          <input className="form-input" placeholder="Terminal ID" value={terminalFilter} onChange={(e) => { setTerminalFilter(e.target.value); setPage(1); }} style={{ maxWidth: 200 }} />
        </div>

        <DataTable columns={columns} data={salesOrders} pagination={pagination} onPageChange={setPage}
          emptyMessage={loading ? 'Loading…' : 'No POS orders match the current filters.'} />
      </div>
    </div>
  );
}

function tenderSegs(tenders) {
  const colors = { card: '#8e2715', cash: '#2d7a3a', split: '#c4722a', unknown: '#a89a88' };
  return (tenders || []).map((x) => ({
    label: x.method ? x.method[0].toUpperCase() + x.method.slice(1) : 'Other',
    cents: x.cents, color: colors[x.method] || '#8a6d1b',
  }));
}

function DeltaPill({ cents, suffix }) {
  const dir = cents > 0 ? 'up' : cents < 0 ? 'down' : 'flat';
  const arrow = cents > 0 ? '▲' : cents < 0 ? '▼' : '·';
  return <div className={`pos-delta ${dir}`}>{arrow} {fmtUsd(Math.abs(cents))} {suffix}</div>;
}
function DeltaText({ pct, suffix, inline }) {
  const dir = pct > 0 ? 'up' : pct < 0 ? 'down' : '';
  const arrow = pct > 0 ? '▲' : pct < 0 ? '▼' : '·';
  const body = <><span className={dir}>{arrow} {Math.abs(pct)}%</span> {suffix}</>;
  return inline ? <span>{body}</span> : body;
}
function Kpi({ label, value, sub, rail }) {
  return (
    <div className="pos-kpi" style={{ '--accent-rail': rail }}>
      <div className="pos-kpi-label">{label}</div>
      <div className="pos-kpi-value">{value}</div>
      <div className="pos-kpi-sub">{sub}</div>
    </div>
  );
}

function SplitBar({ title, segs }) {
  const total = segs.reduce((s, x) => s + Math.max(0, x.cents), 0);
  return (
    <div className="pos-split">
      <div className="pos-split-head"><span>{title}</span><span>{fmtUsd(total)}</span></div>
      <div className="pos-splitbar">
        {total === 0
          ? <div className="pos-splitseg" style={{ flex: 1, color: 'var(--text-secondary)', justifyContent: 'center' }}>no data</div>
          : segs.filter((s) => s.cents > 0).map((s) => (
            <div key={s.label} className="pos-splitseg" style={{ flex: s.cents, background: s.color }} title={`${s.label} ${fmtUsd(s.cents)}`}>
              {pct(s.cents, total) >= 8 ? `${pct(s.cents, total)}%` : ''}
            </div>
          ))}
      </div>
      <div className="pos-split-legend">
        {segs.map((s) => (
          <span key={s.label}><i className="dot" style={{ background: s.color }} />{s.label} <b>{fmtUsd(s.cents)}</b></span>
        ))}
      </div>
    </div>
  );
}

// ── Pace curve: cumulative net revenue, today (solid area) vs yesterday (dotted) ──
const PW = 900, PH = 230, PP = { l: 14, r: 44, t: 18, b: 24 };
function PaceCurve({ pace, currentHour }) {
  const [hover, setHover] = useState(null);
  if (!pace) return <div className="pos-chart-wrap"><div className="pos-chart-empty">Loading…</div></div>;
  const td = pace.today.map((p) => p.cents);
  const yd = pace.yesterday.map((p) => p.cents);

  const active = [];
  for (let h = 0; h < 24; h++) {
    const a = td[h] - (h ? td[h - 1] : 0);
    const b = yd[h] - (h ? yd[h - 1] : 0);
    if (a > 0 || b > 0) active.push(h);
  }
  const yMax = Math.max(...td, ...yd, 0);
  if (yMax === 0) return <div className="pos-chart-wrap"><div className="pos-chart-empty">No POS revenue today or yesterday.</div></div>;

  let lo = active.length ? Math.max(0, Math.min(...active) - 1) : 8;
  let hi = active.length ? Math.min(23, Math.max(Math.max(...active), currentHour) + 1) : 18;
  if (hi - lo < 6) hi = Math.min(23, lo + 6);

  const plotW = PW - PP.l - PP.r, plotH = PH - PP.t - PP.b;
  const sx = (h) => PP.l + ((h - lo) / (hi - lo)) * plotW;
  const sy = (c) => PP.t + plotH - (c / yMax) * plotH;

  const pts = (arr, end) => {
    const out = [];
    for (let h = lo; h <= Math.min(hi, end); h++) out.push([sx(h), sy(arr[h])]);
    return out;
  };
  const line = (p) => p.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');

  const todayPts = pts(td, Math.min(hi, currentHour));
  const yestPts = pts(yd, hi);
  const area = todayPts.length
    ? `${line(todayPts)} L${todayPts[todayPts.length - 1][0].toFixed(1)} ${(PP.t + plotH).toFixed(1)} L${todayPts[0][0].toFixed(1)} ${(PP.t + plotH).toFixed(1)} Z`
    : '';
  const nowH = Math.min(hi, currentHour);
  const grid = [0, 0.5, 1];
  const labelStep = (hi - lo) > 11 ? 2 : 1;

  return (
    <div className="pos-chart-wrap">
      <svg className="pos-chart" viewBox={`0 0 ${PW} ${PH}`} role="img" aria-label="Cumulative revenue, today vs yesterday">
        <defs>
          <linearGradient id="paceFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#c4722a" stopOpacity="0.34" />
            <stop offset="100%" stopColor="#c4722a" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {grid.map((g) => {
          const y = PP.t + plotH - g * plotH;
          return (
            <g key={g}>
              <line x1={PP.l} y1={y} x2={PW - PP.r} y2={y} stroke="var(--border)" />
              <text x={PW - PP.r + 4} y={y + 4} className="pos-axis" textAnchor="start">{fmtShort(g * yMax)}</text>
            </g>
          );
        })}
        {[...Array(hi - lo + 1)].map((_, i) => {
          const h = lo + i;
          return i % labelStep === 0
            ? <text key={h} x={sx(h)} y={PH - 7} className="pos-axis" textAnchor="middle">{fmtHour(h)}</text>
            : null;
        })}

        {area && <path d={area} fill="url(#paceFill)" />}
        <path d={line(yestPts)} fill="none" stroke="#a89a88" strokeWidth="2" strokeDasharray="3 4" />
        <path d={line(todayPts)} fill="none" stroke="#8e2715" strokeWidth="2.5" />
        {todayPts.length > 0 && <circle cx={sx(nowH)} cy={sy(td[nowH])} r="4.5" fill="#8e2715" stroke="#fff" strokeWidth="2" />}

        {/* hover hit-areas per hour */}
        {[...Array(hi - lo + 1)].map((_, i) => {
          const h = lo + i;
          return (
            <rect key={h} x={sx(h) - plotW / (hi - lo) / 2} y={PP.t} width={plotW / (hi - lo)} height={plotH}
              fill="transparent"
              onMouseEnter={() => setHover({ h, x: sx(h), today: td[h], yest: yd[h], future: h > currentHour })}
              onMouseLeave={() => setHover(null)} />
          );
        })}
        {hover && <line x1={hover.x} y1={PP.t} x2={hover.x} y2={PP.t + plotH} stroke="var(--border-dark)" />}
      </svg>
      {hover && (
        <div className="pos-tip" style={{ left: `${(hover.x / PW) * 100}%`, top: `${(sy(Math.max(hover.today, hover.yest)) / PH) * 100}%` }}>
          <div>by {fmtHour((hover.h + 1) % 24)}</div>
          {!hover.future && <div className="tip-amt">{fmtUsd(hover.today)} <span style={{ fontWeight: 400 }}>today</span></div>}
          <div className="tip-muted">{fmtUsd(hover.yest)} yesterday</div>
        </div>
      )}
    </div>
  );
}

// ── Weekly bars: this week (filled) vs last week (light), per weekday ──
const WW = 900, WH = 200, WP = { l: 14, r: 40, t: 14, b: 22 };
function WeekBars({ week }) {
  const [hover, setHover] = useState(null);
  if (!week) return <div className="pos-chart-wrap"><div className="pos-chart-empty">Loading…</div></div>;
  const thisW = week.this || [], lastW = week.last || [];
  const yMax = Math.max(...thisW.map((d) => d.net_cents), ...lastW.map((d) => d.net_cents), 0);
  if (yMax === 0) return <div className="pos-chart-wrap"><div className="pos-chart-empty">No revenue this week or last.</div></div>;

  const plotW = WW - WP.l - WP.r, plotH = WH - WP.t - WP.b;
  const slot = plotW / 7;
  const bw = slot * 0.3;
  const yb = (c) => WP.t + plotH - (c / yMax) * plotH;
  const grid = [0, 0.5, 1];

  return (
    <div className="pos-chart-wrap">
      <svg className="pos-chart hoverable" viewBox={`0 0 ${WW} ${WH}`} role="img" aria-label="Daily revenue this week vs last">
        {grid.map((g) => {
          const y = WP.t + plotH - g * plotH;
          return (
            <g key={g}>
              <line x1={WP.l} y1={y} x2={WW - WP.r} y2={y} stroke="var(--border)" />
              <text x={WW - WP.r + 4} y={y + 4} className="pos-axis" textAnchor="start">{fmtShort(g * yMax)}</text>
            </g>
          );
        })}
        {DOW.map((d, i) => {
          const cx = WP.l + i * slot + slot / 2;
          const last = lastW[i] || { net_cents: 0 };
          const cur = thisW[i] || { net_cents: 0, is_today: false, is_future: false };
          return (
            <g key={d}>
              <rect x={cx - bw - 2} y={yb(last.net_cents)} width={bw} height={Math.max(0, WP.t + plotH - yb(last.net_cents))}
                rx="2" className="pos-bar" fill="#ddd2c2"
                onMouseEnter={() => setHover({ d, which: 'last week', cents: last.net_cents, x: cx, y: yb(last.net_cents) })}
                onMouseLeave={() => setHover(null)} />
              {!cur.is_future && (
                <rect x={cx + 2} y={yb(cur.net_cents)} width={bw} height={Math.max(cur.net_cents > 0 ? 2 : 0, WP.t + plotH - yb(cur.net_cents))}
                  rx="2" className={`pos-bar${cur.is_today ? ' is-active' : ''}`} fill={cur.is_today ? '#8e2715' : '#c4722a'}
                  onMouseEnter={() => setHover({ d, which: cur.is_today ? 'today' : 'this week', cents: cur.net_cents, x: cx, y: yb(cur.net_cents) })}
                  onMouseLeave={() => setHover(null)} />
              )}
              <text x={cx} y={WH - 6} className="pos-axis" textAnchor="middle" fontWeight={cur.is_today ? 700 : 400}>{d}</text>
            </g>
          );
        })}
      </svg>
      {hover && (
        <div className="pos-tip" style={{ left: `${(hover.x / WW) * 100}%`, top: `${(hover.y / WH) * 100}%` }}>
          <div>{hover.d} · {hover.which}</div>
          <div className="tip-amt">{fmtUsd(hover.cents)}</div>
        </div>
      )}
    </div>
  );
}
