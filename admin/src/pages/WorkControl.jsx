import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import Modal from '../components/Modal.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import './WorkControl.css';

const TABS = [
  ['queue', 'Live tasks'],
  ['batches', 'Pack Note batches'],
  ['receiving', 'Receiving review'],
  ['errors', 'Mistake review'],
  ['efficiency', 'Efficiency'],
];

function today(offset = 0) {
  const value = new Date();
  value.setDate(value.getDate() + offset);
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function duration(seconds) {
  const total = Number(seconds || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${rest}s`;
}

function count(value) {
  return Number(value || 0).toLocaleString();
}

function signed(value) {
  if (value === null || value === undefined) return 'No earlier snapshot';
  if (Number(value) === 0) return 'No change';
  return `${Number(value) > 0 ? '+' : '−'}${count(Math.abs(Number(value)))}`;
}

function captureTime(value) {
  if (!value) return 'Not captured yet';
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    day: '2-digit',
    month: 'short',
  }).format(new Date(value));
}

function forecastDuration(value) {
  const total = Math.max(0, Math.ceil(Number(value || 0)));
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  if (!hours) return `${minutes}m`;
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

async function responseBody(response, fallback) {
  if (!response) throw new Error(fallback);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || fallback);
  return body;
}

function TabBar({ value, onChange, counts }) {
  return (
    <div style={styles.tabs} role="tablist" aria-label="Work control views">
      {TABS.map(([key, label]) => (
        <button
          key={key}
          type="button"
          className={`btn${value === key ? ' btn-primary' : ''}`}
          onClick={() => onChange(key)}
          role="tab"
          aria-selected={value === key}
        >
          {label}{counts[key] ? ` · ${counts[key]}` : ''}
        </button>
      ))}
    </div>
  );
}

function Metric({ label, value, note }) {
  return (
    <div style={styles.metric}>
      <div style={styles.metricLabel}>{label}</div>
      <div style={styles.metricValue}>{value}</div>
      {note && <div style={styles.metricNote}>{note}</div>}
    </div>
  );
}

export default function WorkControl() {
  const { warehouseId } = useWarehouse();
  const [tab, setTab] = useState('queue');
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [tasks, setTasks] = useState([]);
  const [batches, setBatches] = useState([]);
  const [receiving, setReceiving] = useState([]);
  const [mistakes, setMistakes] = useState([]);
  const [efficiency, setEfficiency] = useState({ activity: [], confirmed_errors: [] });
  const [workload, setWorkload] = useState({
    latest: null,
    snapshots: [],
    task_progress: [],
    sync: { status: 'missing', age_minutes: null },
    change: {},
  });
  const [range, setRange] = useState({ start: today(-6), end: today() });
  const [batchModal, setBatchModal] = useState(false);
  const [reviewError, setReviewError] = useState(null);
  const [reviewReceiving, setReviewReceiving] = useState(null);

  const loadAll = useCallback(async () => {
    if (!warehouseId) return;
    setLoading(true);
    setError('');
    try {
      const [taskData, batchData, receivingData, errorData, efficiencyData, workloadData] = await Promise.all([
        api.get(`/work-control/tasks/queue?warehouse_id=${warehouseId}`).then((r) => responseBody(r, 'Could not load tasks')),
        api.get(`/work-control/batches?warehouse_id=${warehouseId}`).then((r) => responseBody(r, 'Could not load batches')),
        api.get(`/work-control/receiving-drafts?warehouse_id=${warehouseId}`).then((r) => responseBody(r, 'Could not load receiving drafts')),
        api.get(`/work-control/errors?warehouse_id=${warehouseId}`).then((r) => responseBody(r, 'Could not load mistakes')),
        api.get(`/work-control/reports/efficiency?warehouse_id=${warehouseId}&start=${range.start}&end=${range.end}`).then((r) => responseBody(r, 'Could not load efficiency')),
        api.get(`/work-control/sitegiant/workload?warehouse_id=${warehouseId}&hours=24`)
          .then((r) => responseBody(r, 'Could not load SiteGiant workload'))
          .catch((workloadError) => ({
            latest: null,
            snapshots: [],
            task_progress: [],
            sync: { status: 'unavailable', age_minutes: null },
            change: {},
            error: workloadError.message || 'Could not load SiteGiant workload',
          })),
      ]);
      setTasks(taskData.tasks || []);
      setBatches(batchData.batches || []);
      setReceiving(receivingData.receiving_drafts || []);
      setMistakes(errorData.errors || []);
      setEfficiency(efficiencyData);
      setWorkload(workloadData);
    } catch (loadError) {
      setError(loadError.message || 'Could not load Work Control');
    } finally {
      setLoading(false);
    }
  }, [warehouseId, range.end, range.start]);

  useEffect(() => {
    const timer = window.setTimeout(() => { loadAll(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadAll]);

  const counts = useMemo(() => ({
    queue: tasks.filter((t) => !['COMPLETED', 'CANCELLED'].includes(t.status)).length,
    batches: batches.filter((b) => ['OPEN', 'IN_PROGRESS'].includes(b.status)).length,
    receiving: receiving.filter((r) => r.status === 'SUBMITTED').length,
    errors: mistakes.filter((item) => item.status === 'PENDING').length,
  }), [tasks, batches, receiving, mistakes]);

  async function runAction(action, success) {
    setError('');
    setMessage('');
    setLoading(true);
    try {
      await action();
      setMessage(success);
      await loadAll();
    } catch (actionError) {
      setError(actionError.message || 'Action failed');
      setLoading(false);
    }
  }

  return (
    <div>
      <PageHeader title="Work Control">
        <button className="btn" onClick={loadAll} disabled={loading || !warehouseId}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
        <button className="btn btn-primary" onClick={() => setBatchModal(true)} disabled={!warehouseId}>
          New Pack Note batch
        </button>
      </PageHeader>

      <div style={styles.explainer}>
        This layer assigns work and records time, pauses, photos and reviewed mistakes. It does not post stock or change SiteGiant orders.
      </div>
      {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
      {message && <div style={styles.success}>{message}</div>}
      <TabBar value={tab} onChange={setTab} counts={counts} />

      {tab === 'queue' && <QueueView tasks={tasks} workload={workload} />}
      {tab === 'batches' && <BatchView batches={batches} />}
      {tab === 'receiving' && (
        <ReceivingView drafts={receiving} onReview={setReviewReceiving} />
      )}
      {tab === 'errors' && <ErrorView mistakes={mistakes} onReview={setReviewError} />}
      {tab === 'efficiency' && (
        <EfficiencyView
          report={efficiency}
          range={range}
          setRange={setRange}
          onApply={loadAll}
        />
      )}

      {batchModal && (
        <BatchModal
          warehouseId={warehouseId}
          onClose={() => setBatchModal(false)}
          onCreate={(payload) => runAction(async () => {
            const response = await api.post('/work-control/batches', payload);
            await responseBody(response, 'Could not create batch');
            setBatchModal(false);
            setTab('batches');
          }, 'Pack Note batch created and queued for picking and packing.')}
        />
      )}
      {reviewError && (
        <ErrorReviewModal
          item={reviewError}
          onClose={() => setReviewError(null)}
          onSave={(payload) => runAction(async () => {
            const response = await api.post(`/work-control/errors/${reviewError.error_id}/review`, payload);
            await responseBody(response, 'Could not review mistake');
            setReviewError(null);
          }, 'Mistake review saved. Attribution now appears in the factual report.')}
        />
      )}
      {reviewReceiving && (
        <ReceivingReviewModal
          draft={reviewReceiving}
          onClose={() => setReviewReceiving(null)}
          onSave={(payload) => runAction(async () => {
            const response = await api.post(`/work-control/receiving-drafts/${reviewReceiving.receiving_id}/review`, payload);
            await responseBody(response, 'Could not review receiving draft');
            setReviewReceiving(null);
          }, `Receiving draft ${reviewReceiving.receiving_id} updated.`)}
        />
      )}
    </div>
  );
}

function QueueView({ tasks, workload }) {
  const active = tasks.filter((t) => !['COMPLETED', 'CANCELLED'].includes(t.status));
  const columns = [
    { key: 'task_id', label: 'Task', render: (row) => <span className="mono">#{row.task_id}</span> },
    { key: 'task_type', label: 'Work' },
    { key: 'pack_note_ref', label: 'Pack Note', render: (row) => <span className="mono">{row.pack_note_ref || row.source_ref || '—'}</span> },
    { key: 'status', label: 'Status', render: (row) => <StatusTag status={row.status} /> },
    { key: 'worker', label: 'Employee', render: (row) => row.claimed_by || row.assigned_to || <span style={styles.muted}>Auto queue</span> },
    { key: 'load', label: 'Workload', render: (row) => `${row.order_count || 0} orders · ${row.unit_count || 0} units` },
    { key: 'active_seconds', label: 'Recorded active', render: (row) => duration(row.active_seconds) },
    { key: 'paused_seconds', label: 'Excluded pause', render: (row) => duration(row.paused_seconds) },
  ];
  return (
    <>
      <SiteGiantWorkload workload={workload} />
      <div style={styles.metrics}>
        <Metric label="Waiting" value={active.filter((t) => ['QUEUED', 'ASSIGNED'].includes(t.status)).length} />
        <Metric label="Being worked" value={active.filter((t) => ['CLAIMED', 'IN_PROGRESS'].includes(t.status)).length} />
        <Metric label="Paused" value={active.filter((t) => t.status === 'PAUSED').length} note="Excluded from active time" />
      </div>
      <DataTable rowKey="task_id" columns={columns} data={active} emptyMessage="No open work tasks." />
    </>
  );
}

export function SiteGiantWorkload({ workload }) {
  const latest = workload?.latest;
  const forecast = workload?.forecast;
  const sync = workload?.sync || { status: 'missing' };
  const snapshots = (workload?.snapshots || []).slice(-8).reverse();
  const progress = workload?.task_progress || [];
  const maxRemaining = Math.max(1, ...snapshots.map((item) => Number(item.remaining_packages || 0)));
  const stageItems = latest ? [
    ['Pending', latest.pending_packages, 'pending'],
    ['To process', latest.to_process_packages, 'process'],
    ['Printed', latest.printed_packages, 'printed'],
    ['Pending pickup', latest.pending_pickup_packages, 'pickup'],
  ] : [];
  const taskTypes = ['PICKING', 'PACKING', 'RECEIVING'];
  const taskSummary = taskTypes.map((taskType) => {
    const rows = progress.filter((row) => row.task_type === taskType);
    return {
      taskType,
      open: rows.filter((row) => !['COMPLETED', 'CANCELLED'].includes(row.status))
        .reduce((total, row) => total + Number(row.task_count || 0), 0),
      completedOrders: rows.filter((row) => row.status === 'COMPLETED')
        .reduce((total, row) => total + Number(row.order_count || 0), 0),
    };
  });
  const statusLabel = sync.status === 'current'
    ? 'Hourly feed current'
    : sync.status === 'stale'
      ? 'Feed needs attention'
      : sync.status === 'unavailable'
        ? 'Feed unavailable'
        : 'Waiting for first capture';

  return (
    <section className={`wc-workload wc-workload--${sync.status || 'missing'}`} aria-labelledby="sitegiant-workload-title">
      <div className="wc-workload__head">
        <div>
          <div className="wc-workload__eyebrow">SiteGiant package workload</div>
          <h2 id="sitegiant-workload-title">Hourly order pressure</h2>
          <p>Package totals from SiteGiant, compared with work already queued in this system.</p>
        </div>
        <div className="wc-workload__sync" role="status">
          <span className="wc-workload__sync-dot" aria-hidden="true" />
          <div>
            <strong>{statusLabel}</strong>
            <span>{latest ? `${captureTime(latest.captured_at)} · ${sync.age_minutes ?? 0} min ago` : (workload?.error || 'Install and connect the SiteGiant hourly bridge.')}</span>
          </div>
        </div>
      </div>

      {!latest ? (
        <div className="wc-workload__empty">
          <strong>{sync.status === 'unavailable' ? 'SiteGiant monitoring is temporarily unavailable.' : 'No SiteGiant snapshot has arrived.'}</strong>
          <span>The task queue continues to work normally. Once the bridge sends a capture, this panel will show the hourly backlog.</span>
        </div>
      ) : (
        <>
          {sync.status === 'stale' && (
            <div className="wc-workload__warning">
              The last SiteGiant reading is over 90 minutes old. Treat the figures as historical until the signed-in bridge captures again.
            </div>
          )}

          <div className="wc-workload__summary">
            <div className="wc-workload__primary">
              <span>Not yet processed</span>
              <strong>{count(latest.remaining_packages)}</strong>
              <small>packages · {latest.unprocessed_percent}% of visible pipeline</small>
              <div className={`wc-workload__delta ${(workload.change?.remaining_packages || 0) <= 0 ? 'is-good' : 'is-bad'}`}>
                {signed(workload.change?.remaining_packages)} since previous capture
              </div>
            </div>
            <div className="wc-workload__stages" aria-label="SiteGiant package stages">
              {stageItems.map(([label, value, tone]) => (
                <div className="wc-workload__stage" key={label}>
                  <span className={`wc-workload__stage-mark is-${tone}`} aria-hidden="true" />
                  <div><span>{label}</span><strong>{count(value)}</strong></div>
                </div>
              ))}
              <div className="wc-workload__period">
                <span>SiteGiant dashboard period</span>
                <strong>{latest.period_label || 'Period not supplied'}</strong>
              </div>
            </div>
          </div>

          {forecast && (
            <div className="wc-workload__forecast">
              <div className="wc-workload__forecast-head">
                <div>
                  <strong>Unprinted work forecast</strong>
                  <span>Planning estimate only — official tasks are created after SiteGiant produces the Pack Note.</span>
                </div>
                <span className="wc-workload__forecast-basis">
                  Up to {count(forecast.pack_note_capacity)} orders / Pack Note
                </span>
              </div>
              <div className="wc-workload__forecast-grid">
                <div className="wc-workload__forecast-card is-primary">
                  <span>Estimated Pack Notes</span>
                  <strong>{count(forecast.estimated_pack_notes)}</strong>
                  <small>from {count(forecast.unprinted_packages)} unprinted packages</small>
                </div>
                <div className="wc-workload__forecast-card">
                  <span>Picking labour</span>
                  <strong>{forecastDuration(forecast.estimated_picking_minutes)}</strong>
                  <small>{forecast.rates?.PICKING?.minutes_per_50 || 0}m per 50</small>
                </div>
                <div className="wc-workload__forecast-card">
                  <span>Packing labour</span>
                  <strong>{forecastDuration(forecast.estimated_packing_minutes)}</strong>
                  <small>{forecast.rates?.PACKING?.minutes_per_50 || 0}m per 50</small>
                </div>
                <div className="wc-workload__forecast-card is-elapsed">
                  <span>1 picker + 1 packer</span>
                  <strong>{forecastDuration(forecast.estimated_one_picker_one_packer_minutes)}</strong>
                  <small>estimated elapsed time · {forecastDuration(forecast.estimated_total_labor_minutes)} total labour</small>
                </div>
              </div>
              <p className="wc-workload__forecast-note">
                {Object.values(forecast.rates || {}).every((rate) => rate.source === 'recent_history')
                  ? 'Calibrated from the median of recent real completed batches.'
                  : `Using supervisor baselines until each stage has at least ${forecast.history_threshold?.completed_tasks || 5} real batches and ${count(forecast.history_threshold?.completed_orders || 100)} orders. Simulation records under one minute are ignored.`}
              </p>
            </div>
          )}

          <div className="wc-workload__detail-grid">
            <div className="wc-workload__panel">
              <div className="wc-workload__panel-title">
                <div><strong>Last 8 readings</strong><span>Backlog packages by capture time</span></div>
                <span className="wc-workload__printed-change">Printed {signed(workload.change?.printed_packages)}</span>
              </div>
              <div className="wc-workload__history">
                {snapshots.map((snapshot) => (
                  <div className="wc-workload__history-row" key={snapshot.snapshot_id}>
                    <time dateTime={snapshot.captured_at}>{captureTime(snapshot.captured_at)}</time>
                    <div className="wc-workload__history-track" aria-hidden="true">
                      <span style={{ width: `${Math.max(2, Number(snapshot.remaining_packages || 0) * 100 / maxRemaining)}%` }} />
                    </div>
                    <strong>{count(snapshot.remaining_packages)}</strong>
                  </div>
                ))}
              </div>
            </div>

            <div className="wc-workload__panel">
              <div className="wc-workload__panel-title">
                <div><strong>Warehouse execution</strong><span>Open tasks and today’s completed orders</span></div>
              </div>
              <div className="wc-workload__task-list">
                {taskSummary.map((item) => (
                  <div className="wc-workload__task" key={item.taskType}>
                    <strong>{item.taskType.replace('_', ' ')}</strong>
                    <span><b>{count(item.open)}</b> open tasks</span>
                    <span><b>{count(item.completedOrders)}</b> orders done</span>
                  </div>
                ))}
              </div>
              <p className="wc-workload__footnote">SiteGiant reports packages. Receiving and WMS task units remain separate, so different workload measures are not mixed.</p>
            </div>
          </div>
        </>
      )}
    </section>
  );
}

function BatchView({ batches }) {
  const columns = [
    { key: 'pack_note_ref', label: 'Pack Note', render: (row) => <span className="mono">{row.pack_note_ref}</span> },
    { key: 'platform', label: 'Platform', render: (row) => row.platform || 'Mixed' },
    { key: 'order_count', label: 'Orders' },
    { key: 'progress', label: 'Task progress', render: (row) => `${row.completed_task_count}/${row.task_count}` },
    { key: 'status', label: 'Status', render: (row) => <StatusTag status={row.status} /> },
    { key: 'created_at', label: 'Created', render: (row) => new Date(row.created_at).toLocaleString() },
  ];
  return <DataTable rowKey="batch_id" columns={columns} data={batches} emptyMessage="No Pack Note batches yet." />;
}

function ReceivingView({ drafts, onReview }) {
  const columns = [
    { key: 'receiving_id', label: 'Draft', render: (row) => <span className="mono">GRN-DRAFT-{row.receiving_id}</span> },
    { key: 'po_number', label: 'PO / reference', render: (row) => row.po_number || row.supplier_ref || '—' },
    { key: 'counted_by', label: 'Counted by' },
    { key: 'lines', label: 'SKU lines', render: (row) => row.lines?.length || 0 },
    { key: 'status', label: 'Status', render: (row) => <StatusTag status={row.status} /> },
    { key: 'submitted_at', label: 'Submitted', render: (row) => row.submitted_at ? new Date(row.submitted_at).toLocaleString() : 'Not submitted' },
    { key: 'action', label: '', render: (row) => ['SUBMITTED', 'APPROVED'].includes(row.status) ? <button className="btn btn-sm" onClick={() => onReview(row)}>Review</button> : null },
  ];
  return <DataTable rowKey="receiving_id" columns={columns} data={drafts} emptyMessage="No receiving drafts." />;
}

function ErrorView({ mistakes, onReview }) {
  const columns = [
    { key: 'error_id', label: 'Case', render: (row) => <span className="mono">#{row.error_id}</span> },
    { key: 'error_type', label: 'Type' },
    { key: 'pack_note_ref', label: 'Pack Note', render: (row) => <span className="mono">{row.pack_note_ref || '—'}</span> },
    { key: 'reported_by', label: 'Reported by' },
    { key: 'possible', label: 'Cross-check trail', render: (row) => `Pick: ${row.picker_user_id || '—'} · Pack: ${row.packer_user_id || '—'}` },
    { key: 'responsibility', label: 'Responsibility' },
    { key: 'status', label: 'Status', render: (row) => <StatusTag status={row.status} /> },
    { key: 'action', label: '', render: (row) => row.status === 'PENDING' ? <button className="btn btn-sm" onClick={() => onReview(row)}>Review</button> : null },
  ];
  return <DataTable rowKey="error_id" columns={columns} data={mistakes} emptyMessage="No reported mistakes." />;
}

function EfficiencyView({ report, range, setRange, onApply }) {
  const errorLookup = new Map((report.confirmed_errors || []).map((item) => [`${item.employee}:${item.stage}`, item.confirmed_errors]));
  const rows = (report.activity || []).map((row) => ({
    ...row,
    confirmed_errors: Number(errorLookup.get(`${row.employee}:${row.task_type}`) || 0),
  }));
  const columns = [
    { key: 'employee', label: 'Employee' },
    { key: 'task_type', label: 'Work type' },
    { key: 'completed_tasks', label: 'Tasks' },
    { key: 'orders_handled', label: 'Orders' },
    { key: 'units_handled', label: 'Units' },
    { key: 'active_seconds', label: 'Active time', render: (row) => duration(row.active_seconds) },
    { key: 'paused_seconds', label: 'Excluded pause', render: (row) => duration(row.paused_seconds) },
    { key: 'average_active_seconds', label: 'Avg / task', render: (row) => duration(Math.round(Number(row.average_active_seconds || 0))) },
    { key: 'confirmed_errors', label: 'Confirmed mistakes' },
  ];
  return (
    <>
      <div style={styles.filterRow}>
        <label>Start<input className="form-input" type="date" value={range.start} onChange={(e) => setRange({ ...range, start: e.target.value })} /></label>
        <label>End<input className="form-input" type="date" value={range.end} onChange={(e) => setRange({ ...range, end: e.target.value })} /></label>
        <button className="btn btn-primary" onClick={onApply}>Apply</button>
        <span style={styles.noScore}>No KPI score or commission formula is applied.</span>
      </div>
      <DataTable rowKey={(row) => `${row.employee}:${row.task_type}`} columns={columns} data={rows} emptyMessage="No completed work for this range." />
    </>
  );
}

function BatchModal({ warehouseId, onClose, onCreate }) {
  const [form, setForm] = useState({ pack_note_ref: '', platform: 'TikTok', priority: 50, declared_order_count: '', rows: '' });
  const [error, setError] = useState('');
  function submit() {
    const orders = form.rows.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
      const [order_number, courier_barcode, skuCount, unitCount] = line.split(/[\t,|]/).map((cell) => cell.trim());
      return {
        order_number,
        courier_barcode: courier_barcode || undefined,
        platform: form.platform || undefined,
        sku_count: Number(skuCount || 0),
        unit_count: Number(unitCount || 0),
      };
    });
    if (!form.pack_note_ref.trim()) return setError('Pack Note reference is required.');
    if (!orders.length || orders.some((row) => !row.order_number)) return setError('Add at least one valid order row.');
    if (orders.length > 50) return setError('One Pack Note batch can contain at most 50 orders.');
    const declaredOrderCount = Number(form.declared_order_count || orders.length);
    if (!Number.isInteger(declaredOrderCount) || declaredOrderCount < orders.length || declaredOrderCount > 50) {
      return setError('Actual order count must be between the listed row count and 50.');
    }
    onCreate({
      warehouse_id: warehouseId,
      source_system: 'sitegiant',
      pack_note_ref: form.pack_note_ref.trim(),
      platform: form.platform || null,
      priority: Number(form.priority),
      declared_order_count: declaredOrderCount,
      orders,
      task_types: ['PICKING', 'PACKING'],
    });
  }
  return (
    <Modal
      title="New Pack Note batch"
      onClose={onClose}
      size="large"
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" onClick={submit}>Create & queue</button></>}
    >
      {error && <div className="form-error">{error}</div>}
      <div className="form-row">
        <div className="form-group"><label>Pack Note reference</label><input className="form-input" value={form.pack_note_ref} onChange={(e) => setForm({ ...form, pack_note_ref: e.target.value })} placeholder="e.g. Sheet row 2950" /></div>
        <div className="form-group"><label>Platform</label><select className="form-input" value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value })}><option>TikTok</option><option>Shopee</option><option value="">Mixed</option></select></div>
        <div className="form-group"><label>Priority</label><input className="form-input" type="number" min="0" max="100" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} /></div>
        <div className="form-group"><label>Actual order count</label><input className="form-input" type="number" min="1" max="50" value={form.declared_order_count} onChange={(e) => setForm({ ...form, declared_order_count: e.target.value })} placeholder="e.g. 50" /></div>
      </div>
      <div className="form-group">
        <label>Orders — one per line, up to 50</label>
        <textarea className="form-input" rows="12" value={form.rows} onChange={(e) => setForm({ ...form, rows: e.target.value })} placeholder={'order_number,courier_barcode,sku_count,unit_count\nTTS-10001,MY123456,3,5'} />
        <div style={styles.help}>When the sheet only shows the first and last order numbers, list those two and enter the actual order count above. Scan the Pack Note number, a listed order number or a listed courier barcode to confirm the whole batch.</div>
      </div>
    </Modal>
  );
}

function ErrorReviewModal({ item, onClose, onSave }) {
  const [form, setForm] = useState({ status: 'CONFIRMED', responsibility: 'UNKNOWN', resolution_notes: '' });
  return (
    <Modal title={`Review mistake #${item.error_id}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" onClick={() => onSave(form)}>Save review</button></>}>
      <div style={styles.caseSummary}>{item.description || item.error_type}<br />Picker: {item.picker_user_id || '—'} · Packer: {item.packer_user_id || '—'}</div>
      <EvidenceGallery evidence={item.evidence} />
      <div className="form-group"><label>Decision</label><select className="form-input" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}><option value="CONFIRMED">Confirmed</option><option value="DISMISSED">Dismissed</option></select></div>
      <div className="form-group"><label>Responsibility</label><select className="form-input" value={form.responsibility} onChange={(e) => setForm({ ...form, responsibility: e.target.value })}>{['PICKER', 'PACKER', 'BOTH', 'SUPPLIER', 'SOURCE_DATA', 'SYSTEM', 'UNKNOWN'].map((value) => <option key={value}>{value}</option>)}</select></div>
      <div className="form-group"><label>Review notes</label><textarea className="form-input" rows="4" value={form.resolution_notes} onChange={(e) => setForm({ ...form, resolution_notes: e.target.value })} /></div>
    </Modal>
  );
}

function ReceivingReviewModal({ draft, onClose, onSave }) {
  const nextStatus = draft.status === 'APPROVED' ? 'POSTED' : 'APPROVED';
  const [form, setForm] = useState({ status: nextStatus, review_notes: '' });
  return (
    <Modal title={`Review GRN-DRAFT-${draft.receiving_id}`} onClose={onClose} size="large" footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" onClick={() => onSave(form)}>Save</button></>}>
      <div style={styles.lineGridHeader}><span>SKU</span><span>Expected</span><span>Received</span><span>Good</span><span>Damaged</span><span>Short</span><span>Over</span></div>
      {(draft.lines || []).map((line) => <div key={line.receiving_line_id} style={styles.lineGrid}><span className="mono">{line.sku}</span><span>{line.expected_quantity ?? '—'}</span><span>{line.received_quantity}</span><span>{line.good_quantity}</span><span>{line.damaged_quantity}</span><span>{line.short_quantity}</span><span>{line.over_quantity}</span></div>)}
      <EvidenceGallery evidence={draft.evidence} />
      <div className="form-row" style={{ marginTop: 18 }}><div className="form-group"><label>Decision</label><select className="form-input" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>{draft.status === 'SUBMITTED' && <><option value="APPROVED">Approve for stock clerk</option><option value="REJECTED">Reject / recount</option></>} {draft.status === 'APPROVED' && <option value="POSTED">Mark posted in WMS</option>}</select></div><div className="form-group"><label>Review notes</label><input className="form-input" value={form.review_notes} onChange={(e) => setForm({ ...form, review_notes: e.target.value })} /></div></div>
    </Modal>
  );
}

function EvidenceGallery({ evidence = [] }) {
  if (!evidence.length) return <div style={styles.noEvidence}>No evidence photo attached.</div>;
  return (
    <div style={styles.evidenceSection}>
      <div style={styles.evidenceTitle}>Evidence photos</div>
      <div style={styles.evidenceGrid}>
        {evidence.map((item) => (
          <a
            key={item.evidence_id}
            href={`/api/work-control/evidence/${item.evidence_id}`}
            target="_blank"
            rel="noreferrer"
            style={styles.evidenceLink}
          >
            <img
              src={`/api/work-control/evidence/${item.evidence_id}`}
              alt={item.note || `Evidence ${item.evidence_id}`}
              style={styles.evidenceImage}
            />
            <span style={styles.evidenceCaption}>{item.note || `Photo #${item.evidence_id}`}</span>
          </a>
        ))}
      </div>
    </div>
  );
}

const styles = {
  tabs: { display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 18 },
  explainer: { padding: '10px 12px', marginBottom: 12, background: 'var(--surface-muted)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text-secondary)', fontSize: 13 },
  success: { padding: '9px 12px', marginBottom: 12, border: '1px solid #7aa784', background: '#edf7ef', color: '#245f31', borderRadius: 4 },
  metrics: { display: 'grid', gridTemplateColumns: 'repeat(3, minmax(140px, 1fr))', gap: 12, marginBottom: 16 },
  metric: { border: '1px solid var(--border)', borderRadius: 6, padding: 14, background: 'var(--surface)' },
  metricLabel: { color: 'var(--text-secondary)', fontSize: 12, textTransform: 'uppercase', letterSpacing: '.04em' },
  metricValue: { fontFamily: 'var(--font-mono)', fontSize: 28, fontWeight: 700, marginTop: 4 },
  metricNote: { color: 'var(--text-secondary)', fontSize: 11, marginTop: 2 },
  filterRow: { display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', padding: 12, marginBottom: 16, background: 'var(--surface-muted)', borderRadius: 4 },
  noScore: { marginLeft: 'auto', alignSelf: 'center', color: 'var(--text-secondary)', fontSize: 12 },
  muted: { color: 'var(--text-secondary)' },
  help: { color: 'var(--text-secondary)', fontSize: 12, marginTop: 5 },
  caseSummary: { padding: 12, marginBottom: 16, background: 'var(--surface-muted)', lineHeight: 1.6 },
  lineGridHeader: { display: 'grid', gridTemplateColumns: '2fr repeat(6, 1fr)', gap: 8, padding: '8px 10px', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: 11, fontWeight: 700 },
  lineGrid: { display: 'grid', gridTemplateColumns: '2fr repeat(6, 1fr)', gap: 8, padding: '9px 10px', borderBottom: '1px solid var(--border)', fontSize: 13 },
  evidenceSection: { marginTop: 18 },
  evidenceTitle: { fontSize: 12, fontWeight: 700, marginBottom: 8 },
  evidenceGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 10 },
  evidenceLink: { display: 'flex', flexDirection: 'column', color: 'inherit', textDecoration: 'none' },
  evidenceImage: { width: '100%', height: 120, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--surface-muted)' },
  evidenceCaption: { fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 },
  noEvidence: { marginTop: 14, padding: 10, background: 'var(--surface-muted)', color: 'var(--text-secondary)', fontSize: 12 },
};
