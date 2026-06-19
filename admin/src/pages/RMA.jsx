import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import Modal from '../components/Modal.jsx';

// A return SO (the <orig>-RMA) is received one item at a time into a chosen
// disposition: the warehouse + bin decide whether the goods go back as
// sellable stock or are held as defective / open-box. Sentry carries that
// location on the return.received event for a downstream ledger's GL.
// CANCELLED is intentionally absent: there is no RMA-cancel action yet, and the
// generic sales-order cancel unwinds allocation/picking, which is wrong for a
// goods-in return. Re-add this filter once a proper RMA cancel exists, so the
// dropdown never offers a state nothing can reach.
const RMA_STATUS_OPTIONS = ['All', 'OPEN', 'PARTIALLY_RECEIVED', 'RECEIVED'];

export default function RMA() {
  const [rmas, setRmas] = useState([]);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('All');
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [lines, setLines] = useState([]);
  const [warehouses, setWarehouses] = useState([]);
  const [bins, setBins] = useState([]);
  const [dispWarehouseId, setDispWarehouseId] = useState('');
  const [dispBinId, setDispBinId] = useState('');
  // Per-line draft state keyed by item_id: { qty, saving, error }.
  const [lineDrafts, setLineDrafts] = useState({});

  useEffect(() => {
    loadRmas();
  }, [statusFilter, search]);

  async function loadRmas() {
    const qp = new URLSearchParams({ order_type: 'return', per_page: '50' });
    if (statusFilter !== 'All') qp.set('status', statusFilter);
    if (search) qp.set('q', search);
    const res = await api.get(`/admin/sales-orders?${qp}`);
    if (res?.ok) {
      const data = await res.json();
      setRmas(data.sales_orders || []);
    }
  }

  async function loadBins(warehouseId) {
    setDispBinId('');
    if (!warehouseId) {
      setBins([]);
      return;
    }
    const res = await api.get(
      `/admin/bins?warehouse_id=${warehouseId}&per_page=500`,
    );
    if (res?.ok) {
      const b = (await res.json()).bins || [];
      setBins(b);
      setDispBinId(b[0] ? String(b[0].bin_id) : '');
    } else {
      setBins([]);
    }
  }

  async function openRma(rma) {
    setSelected(rma);
    const res = await api.get(`/admin/sales-orders/${rma.so_id}`);
    if (!res?.ok) return;
    const data = await res.json();
    setDetail(data.sales_order);
    setLines(data.lines || []);
    setLineDrafts({});

    const whRes = await api.get('/admin/warehouses?per_page=500');
    let whs = [];
    if (whRes?.ok) {
      whs = (await whRes.json()).warehouses || [];
    }
    setWarehouses(whs);
    // Default the disposition to the RMA's own warehouse (the common
    // sellable-restock case); the operator can switch to a defective bin.
    const defaultWh = data.sales_order?.warehouse_id || whs[0]?.warehouse_id || '';
    setDispWarehouseId(defaultWh ? String(defaultWh) : '');
    await loadBins(defaultWh || null);
  }

  function onWarehouseChange(e) {
    const wid = e.target.value;
    setDispWarehouseId(wid);
    loadBins(wid ? parseInt(wid, 10) : null);
  }

  function closeDetail() {
    setSelected(null);
    setDetail(null);
    setLines([]);
    setWarehouses([]);
    setBins([]);
    setDispWarehouseId('');
    setDispBinId('');
    setLineDrafts({});
  }

  async function refreshDetail() {
    if (!detail) return;
    const res = await api.get(`/admin/sales-orders/${detail.so_id}`);
    if (res?.ok) {
      const data = await res.json();
      setDetail(data.sales_order);
      setLines(data.lines || []);
    }
  }

  function updateDraft(itemId, patch) {
    setLineDrafts((d) => ({ ...d, [itemId]: { ...(d[itemId] || {}), ...patch } }));
  }

  async function receiveLine(line) {
    const draft = lineDrafts[line.item_id] || {};
    const qty = parseInt(draft.qty, 10);
    const warehouseId = parseInt(dispWarehouseId, 10);
    const binId = parseInt(dispBinId, 10);
    if (isNaN(qty) || qty <= 0) {
      updateDraft(line.item_id, { error: 'Enter a positive quantity' });
      return;
    }
    if (!warehouseId || !binId) {
      updateDraft(line.item_id, { error: 'Pick a disposition warehouse + bin' });
      return;
    }
    updateDraft(line.item_id, { saving: true, error: '' });
    const res = await api.post(
      `/admin/sales-orders/${detail.so_id}/receive-return`,
      { item_id: line.item_id, quantity: qty, warehouse_id: warehouseId, bin_id: binId },
    );
    if (res?.ok) {
      setLineDrafts((d) => {
        const next = { ...d };
        delete next[line.item_id];
        return next;
      });
      await refreshDetail();
      loadRmas();
    } else {
      const data = await res?.json();
      updateDraft(line.item_id, {
        saving: false,
        error: data?.error || 'Failed to receive',
      });
    }
  }

  const columns = [
    { key: 'so_number', label: 'RMA', mono: true },
    { key: 'customer_name', label: 'Customer', render: (r) => r.customer_name || '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    {
      key: 'created_at', label: 'Created', mono: true,
      render: (r) => (r.created_at ? r.created_at.slice(0, 10) : '-'),
    },
  ];

  const canReceive =
    detail && detail.status !== 'RECEIVED' && detail.status !== 'CANCELLED';

  return (
    <div>
      <PageHeader title="RMA" />
      <div className="filter-bar">
        <select
          className="form-select"
          style={{ width: 180 }}
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          {RMA_STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>{s === 'All' ? 'All statuses' : s}</option>
          ))}
        </select>
        <input
          className="form-input"
          style={{ width: 260 }}
          placeholder="Search by RMA number"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <DataTable
        columns={columns}
        data={rmas}
        onRowClick={openRma}
        emptyMessage="No RMAs found"
      />

      {selected && detail && (
        <Modal
          title={`RMA ${detail.so_number}`}
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
              <span className="detail-label">Warehouse</span>
              <span className="mono">{detail.warehouse_id ?? '-'}</span>
            </div>
          </section>

          {canReceive && (
            <section className="section">
              <div className="section-title">Disposition</div>
              <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 8 }}>
                Where the returned goods land: pick the sellable or
                defective / open-box warehouse + bin before receiving.
              </p>
              <div className="form-row">
                <div className="form-group">
                  <label>Warehouse</label>
                  <select
                    className="form-select"
                    value={dispWarehouseId}
                    onChange={onWarehouseChange}
                    data-testid="rma-disposition-warehouse"
                  >
                    <option value="">Select warehouse</option>
                    {warehouses.map((w) => (
                      <option key={w.warehouse_id} value={w.warehouse_id}>
                        {w.warehouse_code} - {w.warehouse_name}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="form-group">
                  <label>Bin</label>
                  <select
                    className="form-select"
                    value={dispBinId}
                    onChange={(e) => setDispBinId(e.target.value)}
                    data-testid="rma-disposition-bin"
                  >
                    <option value="">Select bin</option>
                    {bins.map((b) => (
                      <option key={b.bin_id} value={b.bin_id}>
                        {b.bin_code}{b.bin_type ? ` (${b.bin_type})` : ''}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </section>
          )}

          <section className="section" style={{ marginBottom: 0 }}>
            <div className="section-title">Return Lines</div>
            {!canReceive && (
              <p style={{
                fontSize: 12, color: 'var(--text-secondary)',
                marginBottom: 8, fontStyle: 'italic',
              }}>
                RMA status is {detail.status}; receiving is closed.
              </p>
            )}
            {lines.length > 0 ? (
              <table className="lines-table">
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Item</th>
                    <th style={{ textAlign: 'right' }}>To Return</th>
                    <th style={{ textAlign: 'right' }}>Received</th>
                    <th style={{ textAlign: 'right' }}>Remaining</th>
                    {canReceive && (
                      <>
                        <th style={{ width: 90, textAlign: 'right' }}>Qty</th>
                        <th style={{ width: 110 }}></th>
                      </>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l) => {
                    const remaining =
                      (l.quantity_ordered || 0) - (l.quantity_received || 0);
                    const draft = lineDrafts[l.item_id] || {};
                    const lineReceivable = canReceive && remaining > 0;
                    return (
                      <tr key={l.so_line_id}>
                        <td className="mono">{l.sku}</td>
                        <td style={{ color: 'var(--text-secondary)' }}>{l.item_name}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_ordered}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_received}</td>
                        <td className="mono" style={{
                          textAlign: 'right',
                          color: remaining > 0 ? 'var(--copper)' : 'var(--text-secondary)',
                          fontWeight: remaining > 0 ? 600 : 400,
                        }}>{remaining}</td>
                        {canReceive && (
                          <>
                            <td style={{ textAlign: 'right' }}>
                              <input
                                type="number" min={1} max={remaining}
                                className="form-input mono"
                                style={{ width: 76, textAlign: 'right', padding: '4px 8px' }}
                                value={draft.qty ?? ''}
                                disabled={!lineReceivable || draft.saving}
                                placeholder={lineReceivable ? '0' : ''}
                                data-testid={`rma-qty-${l.sku}`}
                                onChange={(e) => updateDraft(l.item_id, { qty: e.target.value })}
                              />
                            </td>
                            <td style={{ textAlign: 'right' }}>
                              <button
                                className="btn btn-sm btn-primary"
                                disabled={!lineReceivable || draft.saving}
                                data-testid={`rma-receive-${l.sku}`}
                                onClick={() => receiveLine(l)}
                              >
                                {draft.saving ? 'Receiving...' : 'Receive'}
                              </button>
                            </td>
                          </>
                        )}
                      </tr>
                    );
                  })}
                  {Object.entries(lineDrafts).filter(([, d]) => d.error).map(([itemId, d]) => (
                    <tr key={`err-${itemId}`}>
                      <td colSpan={canReceive ? 7 : 5}>
                        <div className="form-error" style={{ fontSize: 12, padding: '4px 0' }}>
                          {d.error}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No return lines</p>
            )}
          </section>
        </Modal>
      )}
    </div>
  );
}
