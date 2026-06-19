import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import Modal from '../components/Modal.jsx';

// The money-back counterpart to the RMA page: lists the <orig>-REFUND
// credit-memo SOs (order_type=refund) the POS mints when a refund is issued.
// Read-only -- the refund already moved through the payment processor and a
// downstream ledger; this is the record. A partial refund leaves the original
// partly refundable, so the
// same original can spawn several -REFUND / -REFUND-2 memos over time.
const REFUND_STATUS_OPTIONS = ['All', 'SHIPPED', 'CANCELLED'];

export default function Refunds() {
  const [refunds, setRefunds] = useState([]);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('All');
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [lines, setLines] = useState([]);

  useEffect(() => {
    loadRefunds();
  }, [statusFilter, search]);

  async function loadRefunds() {
    const qp = new URLSearchParams({ order_type: 'refund', per_page: '50' });
    if (statusFilter !== 'All') qp.set('status', statusFilter);
    if (search) qp.set('q', search);
    const res = await api.get(`/admin/sales-orders?${qp}`);
    if (res?.ok) {
      const data = await res.json();
      setRefunds(data.sales_orders || []);
    }
  }

  async function openRefund(refund) {
    setSelected(refund);
    const res = await api.get(`/admin/sales-orders/${refund.so_id}`);
    if (!res?.ok) return;
    const data = await res.json();
    setDetail(data.sales_order);
    setLines(data.lines || []);
  }

  function closeDetail() {
    setSelected(null);
    setDetail(null);
    setLines([]);
  }

  const columns = [
    { key: 'so_number', label: 'Refund', mono: true },
    { key: 'customer_name', label: 'Customer', render: (r) => r.customer_name || '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    {
      key: 'created_at', label: 'Refunded', mono: true,
      render: (r) => (r.created_at ? r.created_at.slice(0, 10) : '-'),
    },
  ];

  return (
    <div>
      <PageHeader title="Refunds" />
      <div className="filter-bar">
        <select
          className="form-select"
          style={{ width: 180 }}
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          {REFUND_STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>{s === 'All' ? 'All statuses' : s}</option>
          ))}
        </select>
        <input
          className="form-input"
          style={{ width: 260 }}
          placeholder="Search by refund number"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <DataTable
        columns={columns}
        data={refunds}
        onRowClick={openRefund}
        emptyMessage="No refunds found"
      />

      {selected && detail && (
        <Modal
          title={`Refund ${detail.so_number}`}
          onClose={closeDetail}
          footer={<button className="btn" onClick={closeDetail}>Close</button>}
          size="wide"
        >
          <section className="section">
            <div className="section-title">Summary</div>
            <div className="detail-grid detail-grid-2col" style={{ marginBottom: 0 }}>
              <span className="detail-label">Status</span>
              <span><StatusTag status={detail.status} /></span>
              <span className="detail-label">Customer</span>
              <span>{detail.customer_name || '-'}</span>
              {detail.parent_so_number && (
                <>
                  <span className="detail-label">Original order</span>
                  <span className="mono">{detail.parent_so_number}</span>
                </>
              )}
            </div>
          </section>

          <section className="section" style={{ marginBottom: 0 }}>
            <div className="section-title">Refunded Lines</div>
            {lines.length > 0 ? (
              <table className="lines-table">
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Item</th>
                    <th style={{ textAlign: 'right' }}>Qty</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l) => (
                    <tr key={l.so_line_id}>
                      <td className="mono">{l.sku}</td>
                      <td style={{ color: 'var(--text-secondary)' }}>{l.item_name}</td>
                      <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_ordered}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No refund lines</p>
            )}
          </section>
        </Modal>
      )}
    </div>
  );
}
