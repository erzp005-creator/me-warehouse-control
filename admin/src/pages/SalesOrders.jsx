import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import { formatDateOnly } from '../utils/date.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import SalesOrderModal from '../components/SalesOrderModal.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'PICKED', 'PACKED', 'SHIPPED', 'CANCELLED', 'REFUNDED'];

export default function SalesOrders() {
  const [searchParams] = useSearchParams();
  const [search, setSearch] = useState(searchParams.get('q') || '');
  const [orders, setOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('All');
  const [successBanner, setSuccessBanner] = useState('');
  // Which order the modal is showing, and which surface. Both cleared on
  // close. The modal owns everything else about itself, including moving
  // between related records.
  const [openSoId, setOpenSoId] = useState(null);
  const [openMode, setOpenMode] = useState('view');

  useEffect(() => { loadOrders(); }, [page, statusFilter, search]);  // eslint-disable-line react-hooks/exhaustive-deps

  // ?focus=<so_number> deep-link, used by the Teams adaptive card
  // so the operator goes straight to the order instead of hunting for the
  // row in the list. Effect runs once per focus param change; looks up the
  // SO by number via the list endpoint and opens the modal on the first
  // match.
  //
  // : this opened the EDIT form while clicking a row on this same page
  // opened the read-only view. Same apparent action, two destinations, and
  // the deep-link reached the more dangerous one. It also meant operators
  // arriving from a Teams ping landed in a textarea, which is why the memo
  // was reported as "only visible if you edit the order" -- the read-only
  // view has always rendered it. Both now resolve to the view.
  useEffect(() => {
    const target = searchParams.get('focus');
    if (!target) return;
    let cancelled = false;
    (async () => {
      const qs = new URLSearchParams({ q: target, per_page: '1' });
      const res = await api.get(`/admin/sales-orders?${qs.toString()}`);
      if (!res?.ok || cancelled) return;
      const data = await res.json();
      const row = (data.sales_orders || []).find(
        (r) => String(r.so_number).toLowerCase() === String(target).toLowerCase(),
      );
      if (row && !cancelled) { setOpenMode('view'); setOpenSoId(row.so_id); }
    })();
    return () => { cancelled = true; };
  }, [searchParams]);

  async function loadOrders() {
    const qp = new URLSearchParams({ page: String(page), per_page: '50' });
    if (statusFilter !== 'All') qp.set('status', statusFilter);
    if (search) qp.set('q', search);
    // Returns (RMAs) + refunds (credit memos) live on the Returns page, not
    // the sales ledger.
    qp.set('exclude_post_fulfillment', 'true');
    const res = await api.get(`/admin/sales-orders?${qp}`);
    if (res?.ok) {
      const data = await res.json();
      setOrders(data.sales_orders || []);
      setPagination({ page: data.page, pages: data.pages, total: data.total });
    }
  }

  function openView(so) { setOpenMode('view'); setOpenSoId(so.so_id); }
  function openEdit(so) { setOpenMode('edit'); setOpenSoId(so.so_id); }

  // onChanged carries an optional banner message from the modal (partial
  // fulfill, admin pick, admin ship). The banner lives here because it
  // belongs to the list the operator returns to.
  function handleChanged(payload) {
    loadOrders();
    if (payload?.message) {
      setSuccessBanner(payload.message);
      setTimeout(() => setSuccessBanner(''), 6000);
    }
  }

  const columns = [
    { key: 'so_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'ship_by_date', label: 'Ship By', mono: true, render: (r) => r.ship_by_date ? formatDateOnly(r.ship_by_date) : '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'created_at', label: 'Created', render: (r) => r.created_at ? new Date(r.created_at).toLocaleDateString() : '-' },
    { key: 'actions', label: '', render: (r) => (
      <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }} aria-label="Edit" title="Edit">&#9998;</button>
    )},
  ];

  return (
    <div>
      <PageHeader title="Sales Orders" />
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

      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Status:</label>
        <select className="form-select" value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }} style={{ width: 160 }}>
          {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <input
          className="form-input"
          style={{ maxWidth: 320 }}
          placeholder="Search by SO number or customer"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
        />
      </div>

      <DataTable
        rowKey="so_id"
        columns={columns}
        data={orders}
        pagination={pagination}
        onPageChange={setPage}
        onRowClick={openView}
        clickColumn="so_number"
        emptyMessage="No sales orders found"
      />

      <SalesOrderModal
        soId={openSoId}
        mode={openMode}
        onClose={() => setOpenSoId(null)}
        onChanged={handleChanged}
      />
    </div>
  );
}
