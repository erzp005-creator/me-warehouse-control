import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import Modal from '../components/Modal.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';

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
  const [range, setRange] = useState({ start: today(-6), end: today() });
  const [batchModal, setBatchModal] = useState(false);
  const [reviewError, setReviewError] = useState(null);
  const [reviewReceiving, setReviewReceiving] = useState(null);

  const loadAll = useCallback(async () => {
    if (!warehouseId) return;
    setLoading(true);
    setError('');
    try {
      const [taskData, batchData, receivingData, errorData, efficiencyData] = await Promise.all([
        api.get(`/work-control/tasks/queue?warehouse_id=${warehouseId}`).then((r) => responseBody(r, 'Could not load tasks')),
        api.get(`/work-control/batches?warehouse_id=${warehouseId}`).then((r) => responseBody(r, 'Could not load batches')),
        api.get(`/work-control/receiving-drafts?warehouse_id=${warehouseId}`).then((r) => responseBody(r, 'Could not load receiving drafts')),
        api.get(`/work-control/errors?warehouse_id=${warehouseId}`).then((r) => responseBody(r, 'Could not load mistakes')),
        api.get(`/work-control/reports/efficiency?warehouse_id=${warehouseId}&start=${range.start}&end=${range.end}`).then((r) => responseBody(r, 'Could not load efficiency')),
      ]);
      setTasks(taskData.tasks || []);
      setBatches(batchData.batches || []);
      setReceiving(receivingData.receiving_drafts || []);
      setMistakes(errorData.errors || []);
      setEfficiency(efficiencyData);
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

      {tab === 'queue' && <QueueView tasks={tasks} />}
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

function QueueView({ tasks }) {
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
      <div style={styles.metrics}>
        <Metric label="Waiting" value={active.filter((t) => ['QUEUED', 'ASSIGNED'].includes(t.status)).length} />
        <Metric label="Being worked" value={active.filter((t) => ['CLAIMED', 'IN_PROGRESS'].includes(t.status)).length} />
        <Metric label="Paused" value={active.filter((t) => t.status === 'PAUSED').length} note="Excluded from active time" />
      </div>
      <DataTable rowKey="task_id" columns={columns} data={active} emptyMessage="No open work tasks." />
    </>
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
  const [form, setForm] = useState({ pack_note_ref: '', platform: 'TikTok', priority: 50, rows: '' });
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
    onCreate({
      warehouse_id: warehouseId,
      source_system: 'sitegiant',
      pack_note_ref: form.pack_note_ref.trim(),
      platform: form.platform || null,
      priority: Number(form.priority),
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
      </div>
      <div className="form-group">
        <label>Orders — one per line, up to 50</label>
        <textarea className="form-input" rows="12" value={form.rows} onChange={(e) => setForm({ ...form, rows: e.target.value })} placeholder={'order_number,courier_barcode,sku_count,unit_count\nTTS-10001,MY123456,3,5'} />
        <div style={styles.help}>Paste CSV, tab-separated or pipe-separated rows. Scanning any listed courier barcode later resolves this whole Pack Note.</div>
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
