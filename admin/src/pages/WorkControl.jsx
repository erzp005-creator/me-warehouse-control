import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { useAuth } from '../auth.jsx';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import BarcodeCamera from '../components/BarcodeCamera.jsx';
import Modal from '../components/Modal.jsx';
import PageHeader from '../components/PageHeader.jsx';
import PwaStatus from '../components/PwaStatus.jsx';
import StatusTag from '../components/StatusTag.jsx';
import { prepareReceivingEntry } from './workControlReceiving.js';
import { prepareWorkIssue } from './workControlIssues.js';
import './WorkControl.css';

const TABS = [
  ['queue', 'Live tasks'],
  ['batches', 'Pack Note batches'],
  ['receiving', 'Receiving review'],
  ['errors', 'Mistake review'],
  ['efficiency', 'Efficiency'],
];

const WORK_FUNCTION_TASK_TYPES = {
  pick: 'PICKING',
  pack: 'PACKING',
  receive: 'RECEIVING',
  putaway: 'PUTAWAY',
  count: 'STOCK_CHECK',
};

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

function TabBar({ value, onChange, counts, tabs = TABS }) {
  return (
    <div style={styles.tabs} role="tablist" aria-label="Work control views">
      {tabs.map(([key, label]) => (
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
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';
  const [tab, setTab] = useState('queue');
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [tasks, setTasks] = useState([]);
  const [batches, setBatches] = useState([]);
  const [receiving, setReceiving] = useState([]);
  const [users, setUsers] = useState([]);
  const [mistakes, setMistakes] = useState([]);
  const [efficiency, setEfficiency] = useState({ activity: [], confirmed_errors: [] });
  const [personalReport, setPersonalReport] = useState(null);
  const [personalReportError, setPersonalReportError] = useState('');
  const [personalPeriod, setPersonalPeriod] = useState('today');
  const [workload, setWorkload] = useState({
    latest: null,
    snapshots: [],
    task_progress: [],
    sync: { status: 'missing', age_minutes: null },
    change: {},
  });
  const [range, setRange] = useState({ start: today(-6), end: today() });
  const [batchModal, setBatchModal] = useState(false);
  const [receivingTaskModal, setReceivingTaskModal] = useState(false);
  const [countingTask, setCountingTask] = useState(null);
  const [scanTask, setScanTask] = useState(null);
  const [pauseTask, setPauseTask] = useState(null);
  const [issueTask, setIssueTask] = useState(null);
  const [reviewError, setReviewError] = useState(null);
  const [reviewReceiving, setReviewReceiving] = useState(null);

  const loadAll = useCallback(async () => {
    if (!warehouseId) return;
    setLoading(true);
    setError('');
    try {
      const taskData = await api.get(`/work-control/tasks/queue?warehouse_id=${warehouseId}`)
        .then((r) => responseBody(r, 'Could not load tasks'));
      setTasks(taskData.tasks || []);
      if (!isAdmin) {
        setPersonalReportError('');
        try {
          const reportResponse = await api.get(`/work-control/reports/me?warehouse_id=${warehouseId}`);
          const reportData = await responseBody(reportResponse, 'Could not load your work record.');
          setPersonalReport(reportData);
        } catch (reportError) {
          setPersonalReportError(reportError.message || 'Could not load your work record. Refresh to try again.');
        }
        return;
      }

      const [batchData, receivingData, errorData, efficiencyData, workloadData, userData] = await Promise.all([
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
        api.get('/admin/users', { silentPermissionDenied: true })
          .then((r) => (r?.ok ? r.json() : { users: [] }))
          .catch(() => ({ users: [] })),
      ]);
      setBatches(batchData.batches || []);
      setReceiving(receivingData.receiving_drafts || []);
      setUsers(userData.users || []);
      setMistakes(errorData.errors || []);
      setEfficiency(efficiencyData);
      setWorkload(workloadData);
    } catch (loadError) {
      setError(loadError.message || 'Could not load Work Control');
    } finally {
      setLoading(false);
    }
  }, [isAdmin, warehouseId, range.end, range.start]);

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
      const result = await action();
      setMessage(typeof success === 'function' ? success(result) : success);
      await loadAll();
    } catch (actionError) {
      setError(actionError.message || 'Action failed');
      setLoading(false);
    }
  }

  const workerTaskTypes = (user?.allowed_functions || [])
    .map((key) => WORK_FUNCTION_TASK_TYPES[key])
    .filter(Boolean);

  function transitionWorkerTask(task, action, extra = {}) {
    return runAction(async () => {
      const response = await api.post(`/work-control/tasks/${task.task_id}/transition`, {
        action,
        device_id: 'employee-web',
        ...extra,
      });
      return responseBody(response, `Could not ${action.toLowerCase()} task`);
    }, (body) => {
      if (action === 'COMPLETE') {
        return body.next_task
          ? `Task #${task.task_id} completed. Next task #${body.next_task.task_id} is ready.`
          : `Task #${task.task_id} completed. No suitable task is waiting.`;
      }
      return `Task #${task.task_id} ${action.toLowerCase()}d.`;
    });
  }

  return (
    <div>
      <PageHeader title="Work Control">
        <button className="btn" onClick={loadAll} disabled={loading || !warehouseId}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
        {isAdmin && (
          <button className="btn btn-primary" onClick={() => setBatchModal(true)} disabled={!warehouseId}>
            New Pack Note batch
          </button>
        )}
        {isAdmin && (
          <button className="btn" onClick={() => setReceivingTaskModal(true)} disabled={!warehouseId}>
            New receiving task
          </button>
        )}
      </PageHeader>

      {!isAdmin && <PwaStatus />}

      <div style={styles.explainer}>
        This layer assigns work and records time, pauses, photos and reviewed mistakes. It does not post stock or change SiteGiant orders.
      </div>
      {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
      {message && <div style={styles.success}>{message}</div>}
      <TabBar
        value={tab}
        onChange={setTab}
        counts={counts}
        tabs={isAdmin ? TABS : TABS.filter(([key]) => key === 'queue')}
      />

      {tab === 'queue' && (
        <>
          {!isAdmin && (
            <PersonalWorkSummary
              report={personalReport}
              reportError={personalReportError}
              period={personalPeriod}
              onPeriodChange={setPersonalPeriod}
              onRetry={loadAll}
              loading={loading}
            />
          )}
          <QueueView
            tasks={tasks}
            workload={workload}
            showWorkload={isAdmin}
            currentUser={user}
            onClaim={(task) => runAction(async () => {
              const response = await api.post('/work-control/tasks/claim-next', {
                warehouse_id: warehouseId,
                task_types: ['RECEIVING'],
                device_id: 'admin-web',
              });
              const body = await responseBody(response, 'Could not claim receiving task');
              if (!body.task || Number(body.task.task_id) !== Number(task.task_id)) {
                throw new Error('Another current task must be finished before this receiving task can be claimed.');
              }
            }, `Receiving task #${task.task_id} claimed.`)}
            onStart={(task) => runAction(async () => {
              const response = await api.post(`/work-control/tasks/${task.task_id}/transition`, {
                action: 'START',
                device_id: 'admin-web',
              });
              await responseBody(response, 'Could not start receiving task');
            }, `Receiving task #${task.task_id} started.`)}
            onCount={setCountingTask}
            onWorkerScan={setScanTask}
            onWorkerComplete={(task) => transitionWorkerTask(task, 'COMPLETE', {
              claim_next: true,
              next_task_types: workerTaskTypes,
            })}
            onWorkerPause={setPauseTask}
            onWorkerResume={(task) => transitionWorkerTask(task, 'RESUME')}
            onWorkerReport={setIssueTask}
          />
        </>
      )}
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
      {receivingTaskModal && (
        <ReceivingTaskModal
          warehouseId={warehouseId}
          users={users}
          currentUsername={user?.username}
          onClose={() => setReceivingTaskModal(false)}
          onCreate={(payload) => runAction(async () => {
            const response = await api.post('/work-control/tasks', payload);
            const body = await responseBody(response, 'Could not create receiving task');
            setReceivingTaskModal(false);
            setTab('queue');
            return body;
          }, 'Receiving task created and assigned.')}
        />
      )}
      {countingTask && (
        <DesktopReceivingModal
          warehouseId={warehouseId}
          task={countingTask}
          onClose={() => setCountingTask(null)}
          claimNext={!isAdmin}
          nextTaskTypes={workerTaskTypes}
          onComplete={async ({ receiving: draft, next_task: nextTask }) => {
            setCountingTask(null);
            setTab(isAdmin ? 'receiving' : 'queue');
            setMessage(nextTask
              ? `Receiving draft ${draft.receiving_id} submitted. Next task #${nextTask.task_id} is ready.`
              : `Receiving draft ${draft.receiving_id} submitted for stock-clerk review.`);
            await loadAll();
          }}
        />
      )}
      {scanTask && (
        <WorkerScanModal
          task={scanTask}
          onClose={() => setScanTask(null)}
          onSubmit={(barcode) => runAction(async () => {
            const verified = await api.post(`/work-control/tasks/${scanTask.task_id}/verify-scan`, {
              barcode,
              device_id: 'employee-web',
            });
            await responseBody(verified, 'Barcode is not part of this Pack Note');
            const started = await api.post(`/work-control/tasks/${scanTask.task_id}/transition`, {
              action: 'START',
              reason_code: 'BATCH_BARCODE_SCANNED',
              notes: `Scanned ${barcode}`,
              device_id: 'employee-web',
            });
            const body = await responseBody(started, 'Could not start task');
            setScanTask(null);
            return body;
          }, `Task #${scanTask.task_id} verified and started.`)}
        />
      )}
      {pauseTask && (
        <WorkerPauseModal
          task={pauseTask}
          onClose={() => setPauseTask(null)}
          onSubmit={(reasonCode, notes) => runAction(async () => {
            const response = await api.post(`/work-control/tasks/${pauseTask.task_id}/transition`, {
              action: 'PAUSE',
              reason_code: reasonCode,
              notes,
              device_id: 'employee-web',
            });
            const body = await responseBody(response, 'Could not pause task');
            setPauseTask(null);
            return body;
          }, `Task #${pauseTask.task_id} paused. Paused time is excluded.`)}
        />
      )}
      {issueTask && (
        <WorkerIssueModal
          warehouseId={warehouseId}
          task={issueTask}
          onClose={() => setIssueTask(null)}
          onComplete={async ({ errorId, photoAttached }) => {
            setIssueTask(null);
            setMessage(`Issue #${errorId} reported${photoAttached ? ' with photo' : ''}. It remains unconfirmed until an admin reviews it.`);
            await loadAll();
          }}
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

function stageLabel(value) {
  return {
    PICKING: 'Picking',
    PACKING: 'Packing',
    RECEIVING: 'Receiving',
    PUTAWAY: 'Putaway',
    STOCK_CHECK: 'Stock check',
    OTHER: 'Other',
  }[value] || String(value || 'Other').replaceAll('_', ' ');
}

function personalRangeLabel(range, period) {
  if (!range?.start) return period === 'today' ? 'Today' : 'This week';
  const formatter = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });
  const start = formatter.format(new Date(`${range.start}T12:00:00`));
  if (period === 'today' || range.start === range.end) return start;
  const end = formatter.format(new Date(`${range.end}T12:00:00`));
  return `${start} – ${end}`;
}

export function PersonalWorkSummary({ report, reportError, period, onPeriodChange, onRetry, loading }) {
  if (!report && loading) {
    return <section className="wc-personal wc-personal--state" aria-busy="true">Loading your work record…</section>;
  }
  if (!report && reportError) {
    return (
      <section className="wc-personal wc-personal--state" role="alert">
        <span>{reportError}</span>
        <button className="btn btn-sm" type="button" onClick={onRetry}>Try again</button>
      </section>
    );
  }
  if (!report) return null;

  const selected = report.periods?.[period] || { summary: {}, activity: [], recent: [] };
  const summary = selected.summary || {};
  const activity = selected.activity || [];
  const recent = selected.recent || [];
  return (
    <section className="wc-personal" aria-labelledby="personal-work-title">
      <header className="wc-personal__head">
        <div>
          <h2 id="personal-work-title">My work record</h2>
          <p>{report.full_name || report.employee} · {personalRangeLabel(selected.range, period)}</p>
        </div>
        <div className="wc-personal__period" role="group" aria-label="Work record period">
          <button type="button" aria-pressed={period === 'today'} onClick={() => onPeriodChange('today')}>Today</button>
          <button type="button" aria-pressed={period === 'week'} onClick={() => onPeriodChange('week')}>This week</button>
        </div>
      </header>

      <div className="wc-personal__totals" aria-label="Personal work totals">
        <div><span>Completed</span><strong>{count(summary.completed_tasks)}</strong><small>tasks</small></div>
        <div><span>Orders</span><strong>{count(summary.orders_handled)}</strong><small>handled</small></div>
        <div><span>Active time</span><strong>{duration(summary.active_seconds)}</strong><small>{duration(summary.paused_seconds)} pause excluded</small></div>
        <div><span>Confirmed mistakes</span><strong>{count(summary.confirmed_mistakes)}</strong><small>after admin review</small></div>
      </div>

      <div className="wc-personal__issue-note">
        <span>Issues you reported: <strong>{count(summary.reported_issues)}</strong></span>
        <span>Still pending review: <strong>{count(summary.pending_reported_issues)}</strong></span>
      </div>

      {activity.length ? (
        <div className="wc-personal__breakdown" aria-label="Work type breakdown">
          {activity.map((row) => (
            <div className="wc-personal__stage" key={row.task_type}>
              <div><span>Work</span><strong>{stageLabel(row.task_type)}</strong></div>
              <div><span>Tasks</span><strong>{count(row.completed_tasks)}</strong></div>
              <div><span>Load</span><strong>{row.orders_handled ? `${count(row.orders_handled)} orders` : `${count(row.skus_handled)} SKU · ${count(row.units_handled)} units`}</strong></div>
              <div><span>Active</span><strong>{duration(row.active_seconds)}</strong></div>
              <div><span>Average / task</span><strong>{duration(row.average_active_seconds)}</strong></div>
            </div>
          ))}
        </div>
      ) : (
        <div className="wc-personal__empty">No completed tasks in this period yet. Your current task remains in the live queue below.</div>
      )}

      {!!recent.length && (
        <details className="wc-personal__recent">
          <summary>Recent completed tasks</summary>
          <div>
            {recent.map((task) => (
              <div className="wc-personal__recent-row" key={task.task_id}>
                <span><strong>{stageLabel(task.task_type)}</strong><small className="mono">{task.reference || `Task #${task.task_id}`}</small></span>
                <span>{task.order_count ? `${count(task.order_count)} orders` : `${count(task.unit_count)} units`}</span>
                <time dateTime={task.completed_at}>{new Date(task.completed_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>
              </div>
            ))}
          </div>
        </details>
      )}

      <footer>No KPI score, commission formula or staff ranking is applied.</footer>
    </section>
  );
}

export function QueueView({
  tasks,
  workload,
  showWorkload,
  currentUser,
  onClaim,
  onStart,
  onCount,
  onWorkerScan,
  onWorkerComplete,
  onWorkerPause,
  onWorkerResume,
  onWorkerReport,
}) {
  const active = tasks.filter((t) => !['COMPLETED', 'CANCELLED'].includes(t.status));
  function taskAction(row) {
    if (!currentUser?.username) return null;
    const mine = row.claimed_by === currentUser.username || row.assigned_to === currentUser.username;
    if (row.task_type === 'RECEIVING') {
      if (['QUEUED', 'ASSIGNED'].includes(row.status) && (!row.assigned_to || row.assigned_to === currentUser.username)) {
        return <button className="btn btn-sm" onClick={() => onClaim(row)}>Claim</button>;
      }
      if (row.status === 'CLAIMED' && mine) {
        return <button className="btn btn-sm" onClick={() => onStart(row)}>Start</button>;
      }
      if (row.status === 'IN_PROGRESS' && mine) {
        return (
          <div style={styles.taskActions}>
            <button className="btn btn-sm btn-primary" onClick={() => onCount(row)}>Count arrival</button>
            {currentUser.role !== 'ADMIN' && <button className="btn btn-sm" onClick={() => onWorkerReport(row)}>Report issue</button>}
          </div>
        );
      }
      return null;
    }
    if (currentUser.role === 'ADMIN' || !mine) return null;
    if (row.status === 'CLAIMED' && ['PICKING', 'PACKING'].includes(row.task_type)) {
      return <button className="btn btn-sm btn-primary" onClick={() => onWorkerScan(row)}>Scan to start</button>;
    }
    if (row.status === 'IN_PROGRESS') {
      return (
        <div style={styles.taskActions}>
          <button className="btn btn-sm btn-primary" onClick={() => onWorkerComplete(row)}>100% complete</button>
          <button className="btn btn-sm" onClick={() => onWorkerPause(row)}>Pause</button>
          <button className="btn btn-sm" onClick={() => onWorkerReport(row)}>Report issue</button>
        </div>
      );
    }
    if (row.status === 'PAUSED') {
      return (
        <div style={styles.taskActions}>
          <button className="btn btn-sm btn-primary" onClick={() => onWorkerResume(row)}>Resume</button>
          <button className="btn btn-sm" onClick={() => onWorkerReport(row)}>Report issue</button>
        </div>
      );
    }
    return null;
  }
  const columns = [
    { key: 'task_id', label: 'Task', render: (row) => <span className="mono">#{row.task_id}</span> },
    { key: 'task_type', label: 'Work' },
    { key: 'pack_note_ref', label: 'Pack Note', render: (row) => <span className="mono">{row.pack_note_ref || row.source_ref || '—'}</span> },
    { key: 'status', label: 'Status', render: (row) => <StatusTag status={row.status} /> },
    { key: 'worker', label: 'Employee', render: (row) => row.claimed_by || row.assigned_to || <span style={styles.muted}>Auto queue</span> },
    { key: 'load', label: 'Workload', render: (row) => `${row.order_count || 0} orders · ${row.unit_count || 0} units` },
    { key: 'active_seconds', label: 'Recorded active', render: (row) => duration(row.active_seconds) },
    { key: 'paused_seconds', label: 'Excluded pause', render: (row) => duration(row.paused_seconds) },
    { key: 'action', label: '', render: taskAction },
  ];
  return (
    <>
      {showWorkload && <SiteGiantWorkload workload={workload} />}
      <div style={styles.metrics}>
        <Metric label="Waiting" value={active.filter((t) => ['QUEUED', 'ASSIGNED'].includes(t.status)).length} />
        <Metric label="Being worked" value={active.filter((t) => ['CLAIMED', 'IN_PROGRESS'].includes(t.status)).length} />
        <Metric label="Paused" value={active.filter((t) => t.status === 'PAUSED').length} note="Excluded from active time" />
      </div>
      <DataTable rowKey="task_id" columns={columns} data={active} emptyMessage="No open work tasks." />
    </>
  );
}

const EMPTY_ISSUE_FORM = {
  error_type: 'WRONG_QUANTITY',
  order_reference: '',
  sku: '',
  quantity: '',
  description: '',
};

function WorkerIssueModal({ warehouseId, task, onClose, onComplete }) {
  const [form, setForm] = useState(EMPTY_ISSUE_FORM);
  const [photo, setPhoto] = useState(null);
  const [createdCase, setCreatedCase] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  function requestClose() {
    if (submitting) return;
    if (createdCase && photo) {
      setError(`Issue #${createdCase.error_id} is saved. Retry the photo upload before closing.`);
      return;
    }
    onClose();
  }

  async function submit() {
    const prepared = prepareWorkIssue(task, warehouseId, form);
    if (prepared.error) return setError(prepared.error);
    setSubmitting(true);
    setError('');
    try {
      let issue = createdCase;
      if (!issue) {
        const response = await api.post('/work-control/errors', prepared.payload);
        issue = await responseBody(response, 'Could not save the issue. Try again.');
        setCreatedCase(issue);
      }

      if (photo) {
        const evidence = new FormData();
        evidence.append('error_id', String(issue.error_id));
        evidence.append('note', `${task.task_type || 'Task'} issue evidence`);
        evidence.append('photo', photo, photo.name || `issue-${issue.error_id}.jpg`);
        const evidenceResponse = await api.post('/work-control/evidence', evidence);
        await responseBody(evidenceResponse, `Issue #${issue.error_id} is saved, but the photo did not upload. Retry to attach it.`);
      }

      await onComplete({ errorId: issue.error_id, photoAttached: Boolean(photo) });
    } catch (submitError) {
      setError(submitError.message || 'Could not report the issue. Try again.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title={`Report issue · task #${task.task_id}`}
      onClose={requestClose}
      footer={(
        <>
          <button className="btn" type="button" onClick={requestClose} disabled={submitting}>Close</button>
          <button className="btn btn-primary" type="button" onClick={submit} disabled={submitting}>
            {submitting ? (createdCase ? 'Uploading photo…' : 'Saving…') : (createdCase ? 'Retry photo upload' : 'Send for review')}
          </button>
        </>
      )}
    >
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="wc-issue-context">
        <span>{task.task_type}</span>
        <strong className="mono">Pack Note {task.pack_note_ref || task.source_ref || '—'}</strong>
        <small>This creates a pending case only. An admin must verify responsibility.</small>
      </div>
      <div className="form-group">
        <label htmlFor="work-issue-type">Issue type</label>
        <select id="work-issue-type" className="form-input" value={form.error_type} onChange={(event) => setForm({ ...form, error_type: event.target.value })} disabled={Boolean(createdCase)}>
          <option value="WRONG_QUANTITY">Wrong quantity</option>
          <option value="WRONG_ITEM">Wrong item / SKU</option>
          <option value="WRONG_ORDER">Wrong order</option>
          <option value="DAMAGED_ITEM">Damaged item</option>
          <option value="LABEL_ERROR">Wrong tracking label</option>
          <option value="SKU_NOT_FOUND">SKU not found</option>
          <option value="OTHER">Other</option>
        </select>
      </div>
      <div className="form-row">
        <div className="form-group">
          <label htmlFor="work-issue-order">Courier barcode / order</label>
          <input id="work-issue-order" className="form-input" value={form.order_reference} onChange={(event) => setForm({ ...form, order_reference: event.target.value })} placeholder="Scan or enter if one order is affected" maxLength="128" disabled={Boolean(createdCase)} />
        </div>
        <div className="form-group">
          <label htmlFor="work-issue-sku">SKU</label>
          <input id="work-issue-sku" className="form-input" value={form.sku} onChange={(event) => setForm({ ...form, sku: event.target.value })} placeholder="Optional" maxLength="128" disabled={Boolean(createdCase)} />
        </div>
        <div className="form-group wc-issue-quantity">
          <label htmlFor="work-issue-quantity">Qty</label>
          <input id="work-issue-quantity" className="form-input" type="number" min="1" inputMode="numeric" value={form.quantity} onChange={(event) => setForm({ ...form, quantity: event.target.value })} disabled={Boolean(createdCase)} />
        </div>
      </div>
      <div className="form-group">
        <label htmlFor="work-issue-description">What happened?</label>
        <textarea id="work-issue-description" className="form-input" rows="4" value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} placeholder="State what you found and what you did next" maxLength="2000" disabled={Boolean(createdCase)} />
      </div>
      <div className="form-group">
        <label htmlFor="work-issue-photo">Photo (optional)</label>
        <input id="work-issue-photo" className="form-input" type="file" accept="image/jpeg,image/png,image/webp,image/heic" capture="environment" onChange={(event) => setPhoto(event.target.files?.[0] || null)} disabled={Boolean(createdCase)} />
        <div style={styles.help}>{photo ? `Selected: ${photo.name || 'camera photo'}` : 'Use a photo when it helps the admin verify the issue.'}</div>
      </div>
      {createdCase && <div className="wc-receiving-lock" role="status">Issue #{createdCase.error_id} is already saved. Only the remaining photo upload will be retried.</div>}
    </Modal>
  );
}

export function WorkerScanModal({ task, onClose, onSubmit }) {
  const [barcode, setBarcode] = useState('');
  const [busy, setBusy] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [scanMessage, setScanMessage] = useState('');

  function acceptCameraScan(value) {
    setBarcode(value);
    setCameraOpen(false);
    setScanMessage(`Scanned ${value}. Confirm below to start the timer.`);
  }

  async function submit(event) {
    event.preventDefault();
    const value = barcode.trim();
    if (!value || busy) return;
    setBusy(true);
    try {
      await onSubmit(value);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Start task #${task.task_id}`} onClose={onClose} footer={(
      <>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="submit" form="worker-scan-form" className="btn btn-primary" disabled={busy || !barcode.trim()}>
          {busy ? 'Verifying…' : 'Verify & start'}
        </button>
      </>
    )}>
      <form id="worker-scan-form" onSubmit={submit}>
        <div style={styles.workerTaskSummary}>
          <strong>{task.task_type}</strong>
          <span>Pack Note {task.pack_note_ref || task.source_ref || '—'} · {count(task.order_count)} orders</span>
        </div>
        <div className="form-group">
          <label htmlFor="worker-task-barcode">Scan Pack Note or one courier/order barcode</label>
          <input
            id="worker-task-barcode"
            className="form-input"
            value={barcode}
            onChange={(event) => setBarcode(event.target.value)}
            placeholder="Scan or type barcode"
            autoFocus
            autoComplete="off"
          />
          <div className="wc-scan-actions">
            <button type="button" className="btn" onClick={() => setCameraOpen((open) => !open)} disabled={busy}>
              {cameraOpen ? 'Close camera' : 'Open camera scanner'}
            </button>
            <span>Bluetooth and USB scanners can scan directly into the field.</span>
          </div>
          {cameraOpen && <BarcodeCamera label="Pack Note or courier barcode" onDetected={acceptCameraScan} onClose={() => setCameraOpen(false)} />}
          {scanMessage && <div className="wc-scan-confirmed" role="status">{scanMessage}</div>}
          <div style={styles.help}>One matching barcode confirms the entire Pack Note batch. The timer starts only after verification.</div>
        </div>
      </form>
    </Modal>
  );
}

export function WorkerPauseModal({ task, onClose, onSubmit }) {
  const [reasonCode, setReasonCode] = useState('WAITING_STOCK');
  const [notes, setNotes] = useState('Waiting for stock');
  const [busy, setBusy] = useState(false);
  const reasons = [
    ['WAITING_STOCK', 'Waiting for stock'],
    ['SYSTEM_DELAY', 'System / printer delay'],
    ['SUPERVISOR_REQUEST', 'Supervisor request'],
    ['BREAK', 'Break'],
    ['OTHER', 'Other'],
  ];

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      await onSubmit(reasonCode, notes.trim() || reasons.find(([code]) => code === reasonCode)?.[1] || reasonCode);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Pause task #${task.task_id}`} onClose={onClose} footer={(
      <>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="submit" form="worker-pause-form" className="btn btn-primary" disabled={busy}>
          {busy ? 'Pausing…' : 'Pause task'}
        </button>
      </>
    )}>
      <form id="worker-pause-form" onSubmit={submit}>
        <div style={styles.workerTaskSummary}>
          <strong>{task.task_type}</strong>
          <span>Paused time is excluded from active work time.</span>
        </div>
        <div className="form-group">
          <label htmlFor="worker-pause-reason">Reason</label>
          <select
            id="worker-pause-reason"
            className="form-input"
            value={reasonCode}
            onChange={(event) => {
              setReasonCode(event.target.value);
              setNotes(reasons.find(([code]) => code === event.target.value)?.[1] || '');
            }}
          >
            {reasons.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
          </select>
        </div>
        <div className="form-group">
          <label htmlFor="worker-pause-notes">Note</label>
          <input id="worker-pause-notes" className="form-input" value={notes} onChange={(event) => setNotes(event.target.value)} />
        </div>
      </form>
    </Modal>
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

function ReceivingTaskModal({ warehouseId, users, currentUsername, onClose, onCreate }) {
  const activeUsers = users.filter((item) => item.is_active !== false);
  const [form, setForm] = useState({
    assigned_to: currentUsername || '',
    source_ref: '',
    priority: 70,
    sku_count: 1,
    unit_count: 1,
    complexity_note: '',
  });
  const [error, setError] = useState('');

  function submit() {
    const sourceRef = form.source_ref.trim();
    const skuCount = Number(form.sku_count);
    const unitCount = Number(form.unit_count);
    if (!form.assigned_to) return setError('Choose the employee who will count this arrival.');
    if (!sourceRef) return setError('Arrival reference is required.');
    if (!Number.isInteger(skuCount) || skuCount < 1) return setError('SKU count must be at least 1.');
    if (!Number.isInteger(unitCount) || unitCount < 1) return setError('Unit count must be at least 1.');
    onCreate({
      warehouse_id: warehouseId,
      task_type: 'RECEIVING',
      priority: Number(form.priority),
      assigned_to: form.assigned_to,
      source_ref: sourceRef,
      order_count: 0,
      sku_count: skuCount,
      unit_count: unitCount,
      complexity_note: form.complexity_note.trim() || null,
    });
  }

  return (
    <Modal
      title="New receiving task"
      onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" onClick={submit}>Create & assign</button></>}
    >
      {error && <div className="form-error">{error}</div>}
      <div className="form-group">
        <label>Assigned employee</label>
        <select className="form-input" value={form.assigned_to} onChange={(event) => setForm({ ...form, assigned_to: event.target.value })}>
          <option value="">Choose employee</option>
          {activeUsers.map((item) => <option key={item.user_id} value={item.username}>{item.full_name} · {item.username}</option>)}
        </select>
      </div>
      <div className="form-group">
        <label>Arrival reference</label>
        <input className="form-input" value={form.source_ref} onChange={(event) => setForm({ ...form, source_ref: event.target.value })} placeholder="e.g. SIMULATION-RECEIVING-01 or supplier DO" />
      </div>
      <div className="form-row">
        <div className="form-group"><label>Priority</label><input className="form-input" type="number" min="0" max="100" value={form.priority} onChange={(event) => setForm({ ...form, priority: event.target.value })} /></div>
        <div className="form-group"><label>Expected SKUs</label><input className="form-input" type="number" min="1" value={form.sku_count} onChange={(event) => setForm({ ...form, sku_count: event.target.value })} /></div>
        <div className="form-group"><label>Expected units</label><input className="form-input" type="number" min="1" value={form.unit_count} onChange={(event) => setForm({ ...form, unit_count: event.target.value })} /></div>
      </div>
      <div className="form-group">
        <label>Complexity note</label>
        <textarea className="form-input" rows="3" value={form.complexity_note} onChange={(event) => setForm({ ...form, complexity_note: event.target.value })} placeholder="Optional: mixed cartons, small parts, difficult count…" />
      </div>
      <div style={styles.help}>The assignee claims and starts this task before recording quantities and photos. Simulation references remain clearly identifiable in the audit trail.</div>
    </Modal>
  );
}

const EMPTY_RECEIVING_FORM = { expected: '', received: '', damaged: '0', note: '' };

export function DesktopReceivingModal({
  warehouseId,
  task,
  onClose,
  onComplete,
  claimNext = false,
  nextTaskTypes = [],
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [selected, setSelected] = useState(null);
  const [form, setForm] = useState(EMPTY_RECEIVING_FORM);
  const [photo, setPhoto] = useState(null);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [entries, setEntries] = useState([]);
  const [noResults, setNoResults] = useState(false);
  const [manualSku, setManualSku] = useState('');
  const [manualName, setManualName] = useState('');
  const [searching, setSearching] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [creatingSku, setCreatingSku] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [draftSession, setDraftSession] = useState(null);
  const [uploadProgress, setUploadProgress] = useState({ done: 0, total: 0 });
  const [error, setError] = useState('');
  const receivedUnits = entries.reduce((total, entry) => total + entry.received_quantity, 0);
  const listLocked = Boolean(draftSession);

  function chooseSku(item) {
    if (entries.some((entry) => entry.sku.toUpperCase() === item.sku.toUpperCase())) {
      setError(`${item.sku} is already in this receipt. Edit the existing line instead.`);
      return;
    }
    setSelected(item);
    setQuery(item.sku);
    setResults([]);
    setNoResults(false);
    setError('');
  }

  async function searchSku() {
    const value = query.trim();
    if (!value) return setError('Enter an iSKU or item name.');
    setSearching(true);
    setError('');
    setNoResults(false);
    try {
      const response = await api.get(`/work-control/skus?warehouse_id=${warehouseId}&q=${encodeURIComponent(value)}&limit=12`);
      const body = await responseBody(response, 'Could not search SKU catalog. Check the connection and try again.');
      const matches = body.skus || [];
      setResults(matches);
      setNoResults(matches.length === 0);
      if (!matches.length) setManualSku(value.toUpperCase());
    } catch (searchError) {
      setError(searchError.message || 'Could not search SKU catalog. Check the connection and try again.');
    } finally {
      setSearching(false);
    }
  }

  async function createManualSku() {
    const sku = manualSku.trim().toUpperCase();
    const itemName = manualName.trim();
    if (!sku) return setError('Enter the new SKU.');
    if (!itemName) return setError('Enter the item name.');
    setCreatingSku(true);
    setError('');
    try {
      const response = await api.post('/work-control/skus', {
        warehouse_id: warehouseId,
        sku,
        item_name: itemName,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok && !body.sku) throw new Error(body.error || 'Could not add the SKU.');
      chooseSku(body.sku);
      setManualName('');
    } catch (createError) {
      setError(createError.message || 'Could not add the SKU. Check the connection and try again.');
    } finally {
      setCreatingSku(false);
    }
  }

  function resetCurrentEntry() {
    setQuery('');
    setResults([]);
    setSelected(null);
    setForm(EMPTY_RECEIVING_FORM);
    setPhoto(null);
    setNoResults(false);
    setManualSku('');
    setManualName('');
    setFileInputKey((value) => value + 1);
  }

  function addEntry() {
    const prepared = prepareReceivingEntry(selected, form, photo);
    if (prepared.error) return setError(prepared.error);
    setEntries((current) => [...current, prepared.entry]);
    resetCurrentEntry();
    setError('');
  }

  function editEntry(index) {
    const entry = entries[index];
    setEntries((current) => current.filter((_, entryIndex) => entryIndex !== index));
    setSelected(entry.catalog);
    setQuery(entry.sku);
    setForm({
      expected: entry.expected_quantity ?? '',
      received: String(entry.received_quantity),
      damaged: String(entry.damaged_quantity),
      note: entry.notes || '',
    });
    setPhoto(entry.photo);
    setFileInputKey((value) => value + 1);
    setError('');
  }

  function requestClose() {
    if (submitting) return;
    if (draftSession) {
      setError(`Draft GRN ${draftSession.draft.receiving_id} has been created. Retry the remaining photo uploads before closing.`);
      return;
    }
    onClose();
  }

  async function submit() {
    if (selected || form.received !== '' || photo) return setError('Add the current SKU to the counted list before submitting.');
    if (!entries.length) return setError('Add at least one counted SKU before submitting.');

    setSubmitting(true);
    setError('');
    try {
      let session = draftSession;
      if (!session) {
        const createdResponse = await api.post('/work-control/receiving-drafts', {
          warehouse_id: warehouseId,
          task_id: task.task_id,
          source_system: 'manual',
          supplier_ref: task.source_ref || null,
          notes: `Employee web receiving count for task #${task.task_id}`,
          lines: entries.map((entry) => ({
            sku: entry.sku,
            item_name: entry.item_name,
            expected_quantity: entry.expected_quantity,
            received_quantity: entry.received_quantity,
            good_quantity: entry.good_quantity,
            damaged_quantity: entry.damaged_quantity,
            notes: entry.notes,
          })),
        });
        const created = await responseBody(createdResponse, 'Could not create the receiving draft. Your counted list is still on screen; try again.');
        session = { draft: created.receiving, uploadedLineIds: [] };
        setDraftSession(session);
      }

      const uploadedLineIds = [...session.uploadedLineIds];
      setUploadProgress({ done: uploadedLineIds.length, total: entries.length });
      for (const entry of entries) {
        const line = session.draft.lines?.find((item) => item.sku.toUpperCase() === entry.sku.toUpperCase());
        if (!line?.receiving_line_id) throw new Error(`Draft line for ${entry.sku} was not created.`);
        if (uploadedLineIds.includes(line.receiving_line_id)) continue;
        const evidence = new FormData();
        evidence.append('receiving_line_id', String(line.receiving_line_id));
        evidence.append('note', `Arrival photo · ${entry.sku}`);
        evidence.append('photo', entry.photo, entry.photo.name || `receiving-${session.draft.receiving_id}-${entry.sku}.jpg`);
        const photoResponse = await api.post('/work-control/evidence', evidence);
        await responseBody(photoResponse, `Could not upload the photo for ${entry.sku}. Retry to continue from this SKU.`);
        uploadedLineIds.push(line.receiving_line_id);
        session = { ...session, uploadedLineIds: [...uploadedLineIds] };
        setDraftSession(session);
        setUploadProgress({ done: uploadedLineIds.length, total: entries.length });
      }

      const submitResponse = await api.post(`/work-control/receiving-drafts/${session.draft.receiving_id}/submit`, {
        claim_next: claimNext,
        next_task_types: claimNext ? nextTaskTypes : null,
        device_id: claimNext ? 'employee-web' : 'admin-web',
      });
      const submitted = await responseBody(submitResponse, 'Could not send the Draft GRN for review. Retry; uploaded photos will not be repeated.');
      await onComplete(submitted);
    } catch (submitError) {
      setError(submitError.message || 'Could not submit the receiving count. Your counted list is still on screen; try again.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title={`Count arrival · task #${task.task_id}`}
      onClose={requestClose}
      size="large"
      footer={(
        <>
          <button className="btn" onClick={requestClose} disabled={submitting}>Close</button>
          <button className="btn btn-primary" onClick={submit} disabled={submitting || !entries.length}>
            {submitting
              ? `Uploading ${uploadProgress.done}/${uploadProgress.total || entries.length}…`
              : `Submit ${entries.length} SKU for review`}
          </button>
        </>
      )}
    >
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="wc-receiving-summary">
        <div><span>Reference</span><strong className="mono">{task.source_ref || '—'}</strong></div>
        <div><span>Counted SKU</span><strong>{entries.length} / {task.sku_count || '—'}</strong></div>
        <div><span>Received units</span><strong>{count(receivedUnits)} / {task.unit_count ? count(task.unit_count) : '—'}</strong></div>
      </div>

      {draftSession && (
        <div className="wc-receiving-lock" role="status">
          Draft GRN {draftSession.draft.receiving_id} is saved. The counted list is locked while remaining photos upload; press Submit again to retry safely.
        </div>
      )}

      <div className="wc-receiving-layout">
        <section className="wc-receiving-entry" aria-labelledby="receiving-entry-title">
          <h3 id="receiving-entry-title">Add a counted SKU</h3>
          <div className="form-group">
            <label htmlFor="receiving-sku-search">SiteGiant iSKU or item name</label>
            <div className="wc-receiving-search">
              <input
                id="receiving-sku-search"
                className="form-input"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); searchSku(); } }}
                placeholder="Scan SKU or search item name"
                disabled={listLocked}
                autoComplete="off"
              />
              <button className="btn" type="button" onClick={searchSku} disabled={searching || listLocked}>{searching ? 'Searching…' : 'Search'}</button>
              <button className="btn" type="button" onClick={() => setCameraOpen((open) => !open)} disabled={listLocked}>
                {cameraOpen ? 'Close camera' : 'Scan with camera'}
              </button>
            </div>
            {cameraOpen && (
              <BarcodeCamera
                label="SKU"
                onDetected={(value) => {
                  setQuery(value);
                  setCameraOpen(false);
                  setResults([]);
                  setNoResults(false);
                  setError('');
                }}
                onClose={() => setCameraOpen(false)}
              />
            )}
            {query && !selected && <div style={styles.help}>After scanning, press Search to confirm the SKU and show its previous photo.</div>}
          </div>
          {!!results.length && (
            <div style={styles.skuResults} aria-label="SKU search results">
              {results.map((item) => (
                <button key={item.sku_catalog_id} type="button" style={{ ...styles.skuResult, ...(selected?.sku_catalog_id === item.sku_catalog_id ? styles.skuResultSelected : {}) }} onClick={() => chooseSku(item)}>
                  <span className="mono">{item.sku}</span>
                  <span>{item.item_name}</span>
                </button>
              ))}
            </div>
          )}
          {noResults && !listLocked && (
            <div className="wc-receiving-new-sku">
              <strong>No matching SKU</strong>
              <span>Add it locally for this receipt. Stock Clerk can review it later.</span>
              <div className="form-row">
                <div className="form-group"><label htmlFor="manual-sku">New SKU</label><input id="manual-sku" className="form-input" value={manualSku} onChange={(event) => setManualSku(event.target.value)} maxLength="128" /></div>
                <div className="form-group"><label htmlFor="manual-item-name">Item name</label><input id="manual-item-name" className="form-input" value={manualName} onChange={(event) => setManualName(event.target.value)} maxLength="500" /></div>
              </div>
              <button className="btn" type="button" onClick={createManualSku} disabled={creatingSku}>{creatingSku ? 'Adding…' : 'Add new SKU'}</button>
            </div>
          )}
          {selected && (
            <div style={styles.selectedSku}>
              <div className="wc-receiving-selected-title"><strong className="mono">{selected.sku}</strong><span>{selected.item_name}</span></div>
              <div style={styles.skuReferenceLinks}>
                {selected.image_url && <a href={selected.image_url} target="_blank" rel="noreferrer">Open SiteGiant image</a>}
                {selected.source_item_url && <a href={selected.source_item_url} target="_blank" rel="noreferrer">Open SiteGiant item</a>}
              </div>
              {selected.last_evidence_id ? (
                <div style={styles.previousEvidence}>
                  <span>Previous receiving photo · {selected.last_received_at ? new Date(selected.last_received_at).toLocaleString() : ''}</span>
                  <img src={`/api/work-control/evidence/${selected.last_evidence_id}`} alt={`Previous receiving evidence for ${selected.sku}`} style={styles.previousEvidenceImage} />
                </div>
              ) : <div style={styles.help}>No previous receiving photo for this SKU yet.</div>}
            </div>
          )}
          <div className="wc-receiving-qty">
            <div className="form-group"><label htmlFor="receiving-expected">Expected</label><input id="receiving-expected" className="form-input" type="number" min="0" inputMode="numeric" value={form.expected} onChange={(event) => setForm({ ...form, expected: event.target.value })} disabled={listLocked} /></div>
            <div className="form-group"><label htmlFor="receiving-received">Received</label><input id="receiving-received" className="form-input" type="number" min="0" inputMode="numeric" value={form.received} onChange={(event) => setForm({ ...form, received: event.target.value })} disabled={listLocked} /></div>
            <div className="form-group"><label htmlFor="receiving-damaged">Damaged</label><input id="receiving-damaged" className="form-input" type="number" min="0" inputMode="numeric" value={form.damaged} onChange={(event) => setForm({ ...form, damaged: event.target.value })} disabled={listLocked} /></div>
          </div>
          <div className="form-group"><label htmlFor="receiving-note">Count note</label><input id="receiving-note" className="form-input" value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} placeholder="Optional discrepancy or carton note" maxLength="1000" disabled={listLocked} /></div>
          <div className="form-group">
            <label htmlFor="receiving-photo">Arrival photo for this SKU</label>
            <input key={fileInputKey} id="receiving-photo" className="form-input" type="file" accept="image/jpeg,image/png,image/webp,image/heic" capture="environment" onChange={(event) => setPhoto(event.target.files?.[0] || null)} disabled={listLocked} />
            <div style={styles.help}>{photo ? `Selected: ${photo.name || 'camera photo'}` : 'On a phone, this opens the rear camera. One photo is required per SKU.'}</div>
          </div>
          <button className="btn btn-primary wc-receiving-add" type="button" onClick={addEntry} disabled={listLocked}>Add SKU to counted list</button>
        </section>

        <section className="wc-receiving-list" aria-labelledby="receiving-list-title">
          <div className="wc-receiving-list-head"><h3 id="receiving-list-title">Counted list</h3><span>{entries.length} SKU · {count(receivedUnits)} units</span></div>
          {!entries.length ? (
            <div className="wc-receiving-empty">Search a SKU, enter its quantity, attach a photo and add it here.</div>
          ) : entries.map((entry, index) => (
            <article className="wc-receiving-line" key={entry.sku}>
              <div className="wc-receiving-line-title"><strong className="mono">{entry.sku}</strong><span>{entry.item_name}</span></div>
              <dl>
                <div><dt>Expected</dt><dd>{entry.expected_quantity ?? '—'}</dd></div>
                <div><dt>Received</dt><dd>{entry.received_quantity}</dd></div>
                <div><dt>Damaged</dt><dd>{entry.damaged_quantity}</dd></div>
              </dl>
              <div className="wc-receiving-photo-name">Photo: {entry.photo.name || 'camera photo'}</div>
              {!listLocked && <div className="wc-receiving-line-actions"><button className="btn btn-sm" type="button" onClick={() => editEntry(index)}>Edit</button><button className="btn btn-sm" type="button" onClick={() => setEntries((current) => current.filter((_, entryIndex) => entryIndex !== index))}>Remove</button></div>}
            </article>
          ))}
        </section>
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
      <div style={styles.lineGridHeader}><span>SKU / item</span><span>Expected</span><span>Received</span><span>Good</span><span>Damaged</span><span>Short</span><span>Over</span></div>
      {(draft.lines || []).map((line) => <div key={line.receiving_line_id} style={styles.lineGrid}><span><span className="mono">{line.sku}</span>{line.item_name && <small style={styles.lineItemName}>{line.item_name}</small>}</span><span>{line.expected_quantity ?? '—'}</span><span>{line.received_quantity}</span><span>{line.good_quantity}</span><span>{line.damaged_quantity}</span><span>{line.short_quantity}</span><span>{line.over_quantity}</span></div>)}
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
  taskActions: { display: 'flex', flexWrap: 'wrap', gap: 6, justifyContent: 'flex-end' },
  workerTaskSummary: { display: 'flex', flexDirection: 'column', gap: 4, padding: 12, marginBottom: 14, border: '1px solid var(--border)', borderRadius: 6, background: 'var(--surface-muted)', color: 'var(--text-secondary)' },
  skuResults: { display: 'grid', gap: 7, maxHeight: 250, marginBottom: 14, overflowY: 'auto' },
  skuResult: { display: 'grid', gridTemplateColumns: 'minmax(150px, .65fr) 1.35fr', gap: 12, width: '100%', padding: '10px 12px', border: '1px solid var(--border)', borderRadius: 5, background: 'var(--surface)', color: 'var(--text)', textAlign: 'left', cursor: 'pointer' },
  skuResultSelected: { borderColor: 'var(--info)', background: 'var(--info-bg)' },
  selectedSku: { padding: 14, border: '1px solid var(--border)', borderRadius: 6, background: 'var(--surface-muted)' },
  skuReferenceLinks: { display: 'flex', flexWrap: 'wrap', gap: 12, marginTop: 8, fontSize: 12 },
  previousEvidence: { display: 'grid', gap: 7, marginTop: 12, color: 'var(--text-secondary)', fontSize: 12 },
  previousEvidenceImage: { width: 180, height: 130, objectFit: 'cover', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--surface)' },
  caseSummary: { padding: 12, marginBottom: 16, background: 'var(--surface-muted)', lineHeight: 1.6 },
  lineGridHeader: { display: 'grid', gridTemplateColumns: '2fr repeat(6, 1fr)', gap: 8, padding: '8px 10px', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: 11, fontWeight: 700 },
  lineGrid: { display: 'grid', gridTemplateColumns: '2fr repeat(6, 1fr)', gap: 8, padding: '9px 10px', borderBottom: '1px solid var(--border)', fontSize: 13 },
  lineItemName: { display: 'block', marginTop: 3, color: 'var(--text-secondary)', fontFamily: 'var(--font-sans)', lineHeight: 1.35 },
  evidenceSection: { marginTop: 18 },
  evidenceTitle: { fontSize: 12, fontWeight: 700, marginBottom: 8 },
  evidenceGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 10 },
  evidenceLink: { display: 'flex', flexDirection: 'column', color: 'inherit', textDecoration: 'none' },
  evidenceImage: { width: '100%', height: 120, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--surface-muted)' },
  evidenceCaption: { fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 },
  noEvidence: { marginTop: 14, padding: 10, background: 'var(--surface-muted)', color: 'var(--text-secondary)', fontSize: 12 },
};
