import { useState } from 'react';
import { api } from '../api.js';
import Modal from './Modal.jsx';

// Create the <orig>-RMA goods-in SO from an original sale's shipped lines. The
// operator picks which lines + quantities are coming back; the backend mints
// the return SO (order_type=return) linked to the parent. No goods move here --
// the warehouse receives against the RMA later (the RMA page).
export default function CreateRmaModal({ so, lines, onClose, onCreated }) {
  const returnableLines = (lines || []).filter((l) => (l.quantity_shipped || 0) > 0);

  // selection keyed by so_line_id -> quantity to return. Seeded to the full
  // shipped quantity of each line.
  const [selection, setSelection] = useState(() => {
    const sel = {};
    for (const l of returnableLines) sel[l.so_line_id] = l.quantity_shipped;
    return sel;
  });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [memo, setMemo] = useState('');

  function setQty(soLineId, qty) {
    setSelection((s) => {
      const next = { ...s };
      if (qty <= 0) delete next[soLineId];
      else next[soLineId] = qty;
      return next;
    });
  }

  function toggle(line) {
    if (selection[line.so_line_id]) setQty(line.so_line_id, 0);
    else setQty(line.so_line_id, line.quantity_shipped || 0);
  }

  async function submit() {
    const body = { lines: [] };
    for (const l of returnableLines) {
      const qty = selection[l.so_line_id];
      if (qty > 0) {
        body.lines.push({
          item_id: l.item_id,
          quantity: qty,
          original_so_line_id: l.so_line_id,
        });
      }
    }
    if (body.lines.length === 0) {
      setError('Select at least one line to return');
      return;
    }
    if (memo.trim()) body.memo = memo.trim();
    setSaving(true);
    setError('');
    const res = await api.post(`/admin/sales-orders/${so.so_id}/create-rma`, body);
    if (res?.ok) {
      onCreated?.(await res.json());
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to create RMA');
      setSaving(false);
    }
  }

  return (
    <Modal
      title={`Create RMA for ${so.so_number}`}
      onClose={onClose}
      size="wide"
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={saving}>Cancel</button>
          <button
            className="btn btn-primary"
            onClick={submit}
            disabled={saving || returnableLines.length === 0}
          >
            {saving ? 'Creating...' : 'Create RMA'}
          </button>
        </>
      }
    >
      {error && <div className="form-error">{error}</div>}
      {returnableLines.length === 0 ? (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          No shipped lines to return.
        </p>
      ) : (
        <table className="lines-table">
          <thead>
            <tr>
              <th style={{ width: 36 }}></th>
              <th>SKU</th>
              <th>Item</th>
              <th style={{ textAlign: 'right' }}>Shipped</th>
              <th style={{ width: 100, textAlign: 'right' }}>Return Qty</th>
            </tr>
          </thead>
          <tbody>
            {returnableLines.map((l) => {
              const shipped = l.quantity_shipped || 0;
              const qty = selection[l.so_line_id] ?? 0;
              const checked = qty > 0;
              return (
                <tr key={l.so_line_id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={checked}
                      data-testid={`create-rma-line-${l.sku}`}
                      onChange={() => toggle(l)}
                    />
                  </td>
                  <td className="mono">{l.sku}</td>
                  <td style={{ color: 'var(--text-secondary)' }}>{l.item_name}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>{shipped}</td>
                  <td style={{ textAlign: 'right' }}>
                    <input
                      type="number" min={1} max={shipped}
                      className="form-input mono"
                      style={{ width: 80, textAlign: 'right', padding: '4px 8px' }}
                      value={checked ? qty : ''}
                      disabled={!checked}
                      data-testid={`create-rma-qty-${l.sku}`}
                      onChange={(e) =>
                        setQty(
                          l.so_line_id,
                          Math.min(shipped, Math.max(0, parseInt(e.target.value, 10) || 0)),
                        )
                      }
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {returnableLines.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <label style={{ display: 'block', fontSize: 13, color: 'var(--text-secondary)', marginBottom: 4 }}>
            Note (optional)
          </label>
          <textarea
            className="form-input"
            rows={2}
            placeholder="Operator note on this RMA"
            value={memo}
            data-testid="create-rma-memo"
            onChange={(e) => setMemo(e.target.value)}
          />
        </div>
      )}
    </Modal>
  );
}
