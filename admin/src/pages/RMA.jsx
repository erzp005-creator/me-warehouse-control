import { useState, useEffect, useRef } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import Modal from '../components/Modal.jsx';
import { useAuth } from '../auth.jsx';

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
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';
  const [rmas, setRmas] = useState([]);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('All');
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [lines, setLines] = useState([]);
  const [warehouses, setWarehouses] = useState([]);
  const [dispWarehouseId, setDispWarehouseId] = useState('');
  // The disposition bin is chosen via a searchable typeahead (mirrors the
  // Adjustments bin lookup): the operator types a bin code, picks a result,
  // and dispBinId carries the chosen bin_id while binSearch is the visible
  // text. Server-side search avoids the old preload-and-truncate behaviour.
  const [dispBinId, setDispBinId] = useState('');
  const [binSearch, setBinSearch] = useState('');
  const [binResults, setBinResults] = useState([]);
  const [binOpen, setBinOpen] = useState(false);
  const [binSearching, setBinSearching] = useState(false);
  const binRef = useRef(null);
  // Per-line draft state keyed by item_id: { qty, saving, error }.
  const [lineDrafts, setLineDrafts] = useState({});
  // Free-form operator note on the RMA (sales_orders.memo), editable anytime.
  const [memoDraft, setMemoDraft] = useState('');
  const [memoSaving, setMemoSaving] = useState(false);
  const [memoMsg, setMemoMsg] = useState('');
  // ADMIN-only soft-delete (void) of a mistakenly created RMA.
  const [confirmVoid, setConfirmVoid] = useState(false);
  const [voiding, setVoiding] = useState(false);
  const [voidError, setVoidError] = useState('');

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

  // Close the bin results dropdown when the operator clicks away.
  useEffect(() => {
    function handleClick(e) {
      if (binRef.current && !binRef.current.contains(e.target)) setBinOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  // Debounced server-side bin search, scoped to the chosen disposition
  // warehouse. Skips empty queries and stops once a bin is selected. 200 ms
  // matches the Adjustments / PO-line typeahead.
  useEffect(() => {
    const q = binSearch.trim();
    if (!dispWarehouseId || q.length < 1 || dispBinId) {
      setBinResults([]);
      return;
    }
    setBinSearching(true);
    const handle = setTimeout(async () => {
      const res = await api.get(
        `/admin/bins?warehouse_id=${dispWarehouseId}&q=${encodeURIComponent(q)}&per_page=25`,
      );
      setBinSearching(false);
      if (!res?.ok) return;
      const data = await res.json();
      setBinResults(data.bins || []);
    }, 200);
    return () => clearTimeout(handle);
  }, [binSearch, dispWarehouseId, dispBinId]);

  function clearBin() {
    setDispBinId('');
    setBinSearch('');
    setBinResults([]);
    setBinOpen(false);
  }

  function selectBin(bin) {
    setDispBinId(String(bin.bin_id));
    setBinSearch(bin.bin_code);
    setBinOpen(false);
  }

  async function openRma(rma) {
    setSelected(rma);
    const res = await api.get(`/admin/sales-orders/${rma.so_id}`);
    if (!res?.ok) return;
    const data = await res.json();
    setDetail(data.sales_order);
    setLines(data.lines || []);
    setLineDrafts({});
    setMemoDraft(data.sales_order?.memo || '');
    setMemoMsg('');

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
    // Bin starts unselected: the operator searches and picks it, rather than
    // silently inheriting whichever bin happened to sort first.
    clearBin();
  }

  function onWarehouseChange(e) {
    // Switching warehouses invalidates any chosen bin -- a bin belongs to one
    // warehouse, and receive-return rejects a cross-warehouse bin server-side.
    setDispWarehouseId(e.target.value);
    clearBin();
  }

  function closeDetail() {
    setSelected(null);
    setDetail(null);
    setLines([]);
    setWarehouses([]);
    setDispWarehouseId('');
    clearBin();
    setLineDrafts({});
    setMemoDraft('');
    setMemoMsg('');
    setConfirmVoid(false);
    setVoiding(false);
    setVoidError('');
  }

  async function voidReturn() {
    if (!detail) return;
    setVoiding(true);
    setVoidError('');
    const res = await api.post(`/admin/sales-orders/${detail.so_id}/void-return`, {});
    setVoiding(false);
    if (res?.ok) {
      setConfirmVoid(false);
      closeDetail();
      loadRmas();
    } else {
      const data = await res?.json().catch(() => ({}));
      setVoidError(data?.error || 'Failed to void this RMA');
    }
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

  async function saveMemo() {
    if (!detail) return;
    setMemoSaving(true);
    setMemoMsg('');
    const res = await api.patch(
      `/admin/sales-orders/${detail.so_id}/memo`,
      { memo: memoDraft.trim() },  // empty string clears to NULL server-side
    );
    setMemoSaving(false);
    if (res?.ok) {
      setMemoMsg('Saved');
      setDetail((d) => (d ? { ...d, memo: memoDraft.trim() || null } : d));
    } else {
      const data = await res?.json().catch(() => ({}));
      setMemoMsg(data?.error || 'Save failed');
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
          footer={
            <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%' }}>
              {isAdmin ? (
                <button
                  className="btn btn-danger"
                  onClick={() => { setVoidError(''); setConfirmVoid(true); }}
                  data-testid="rma-void"
                >
                  Delete RMA
                </button>
              ) : <span />}
              <button className="btn" onClick={closeDetail}>Close</button>
            </div>
          }
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

          <section className="section">
            <div className="section-title">Note</div>
            <textarea
              className="form-input"
              rows={2}
              placeholder="Operator note on this RMA"
              value={memoDraft}
              data-testid="rma-memo"
              onChange={(e) => { setMemoDraft(e.target.value); setMemoMsg(''); }}
            />
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 6 }}>
              <button
                className="btn btn-sm btn-primary"
                onClick={saveMemo}
                disabled={memoSaving || memoDraft.trim() === (detail.memo || '').trim()}
                data-testid="rma-memo-save"
              >
                {memoSaving ? 'Saving...' : 'Save note'}
              </button>
              {memoMsg && (
                <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{memoMsg}</span>
              )}
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
                <div className="form-group" ref={binRef} style={{ position: 'relative' }}>
                  <label>
                    Bin{' '}
                    {binSearching && (
                      <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                        (searching...)
                      </span>
                    )}
                  </label>
                  <input
                    className="form-input mono"
                    placeholder="Type bin code to search"
                    value={binSearch}
                    data-testid="rma-disposition-bin"
                    onChange={(e) => { setBinSearch(e.target.value); setDispBinId(''); setBinOpen(true); }}
                    onFocus={() => setBinOpen(true)}
                    autoComplete="off"
                  />
                  {binOpen && binResults.length > 0 && (
                    <div style={dropdownStyle}>
                      {binResults.map((b) => (
                        <div
                          key={b.bin_id}
                          style={dropdownItemStyle}
                          data-testid={`rma-bin-opt-${b.bin_id}`}
                          onMouseDown={() => selectBin(b)}
                        >
                          <span className="mono">{b.bin_code}</span>{b.bin_type ? ` (${b.bin_type})` : ''}
                        </div>
                      ))}
                    </div>
                  )}
                  {binOpen && !binSearching && binSearch.trim().length >= 1 && !dispBinId && binResults.length === 0 && (
                    <div style={{ ...dropdownStyle, padding: 12, fontSize: 12, color: 'var(--text-secondary)' }}>
                      No bins match "{binSearch.trim()}" in this warehouse.
                    </div>
                  )}
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

      {confirmVoid && detail && (
        <Modal
          title="Delete RMA?"
          onClose={() => { if (!voiding) setConfirmVoid(false); }}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmVoid(false)} disabled={voiding}>
                Cancel
              </button>
              <button
                className="btn btn-danger"
                onClick={voidReturn}
                disabled={voiding}
                data-testid="rma-void-confirm"
              >
                {voiding ? 'Deleting...' : 'Delete RMA'}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            Remove RMA <span className="mono">{detail.so_number}</span> from the RMA
            list? It is hidden rather than erased (an admin can restore it), and
            only an un-received return can be deleted -- received goods or a linked
            refund will block it.
          </p>
          {voidError && (
            <div className="form-error" data-testid="rma-void-error" style={{ marginTop: 8 }}>
              {voidError}
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}

const dropdownStyle = {
  position: 'absolute',
  top: '100%',
  left: 0,
  right: 0,
  maxHeight: 200,
  overflowY: 'auto',
  background: '#fff',
  border: '1px solid #ddd',
  borderRadius: 8,
  boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
  zIndex: 100,
};

const dropdownItemStyle = {
  padding: '8px 12px',
  cursor: 'pointer',
  borderBottom: '1px solid #f0f0f0',
  fontSize: 13,
};
