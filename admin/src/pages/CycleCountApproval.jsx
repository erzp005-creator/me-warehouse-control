import { useState, useEffect } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';

export default function CycleCountApproval() {
  const [adjustments, setAdjustments] = useState([]);
  const [decisions, setDecisions] = useState({});
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [sortBy, setSortBy] = useState('count'); // 'count' | 'bin'

  useEffect(() => { loadPending(); }, []);

  async function loadPending() {
    const res = await api.get('/admin/adjustments/pending');
    if (res?.ok) {
      const data = await res.json();
      const items = data.adjustments || [];
      setAdjustments(items);
      const defaults = {};
      items.forEach((a) => { defaults[a.adjustment_id] = 'approve'; });
      setDecisions(defaults);
    }
  }

  function setDecision(adjustmentId, action) {
    setDecisions((prev) => ({ ...prev, [adjustmentId]: action }));
  }

  async function submitGroup(countId) {
    setSubmitting(true);
    setMessage('');
    const groupItems = groups[countId] || [];
    const payload = groupItems.map((a) => ({
      adjustment_id: a.adjustment_id,
      action: decisions[a.adjustment_id] || 'approve',
    }));
    const res = await api.post('/admin/adjustments/review', { decisions: payload });
    if (res?.ok) {
      setMessage(`Bin decisions submitted successfully`);
      loadPending();
    } else {
      const data = await res?.json();
      setMessage(data?.error || 'Failed to submit decisions');
    }
    setSubmitting(false);
  }

  function approveAll(countId) {
    const groupItems = groups[countId] || [];
    const updated = { ...decisions };
    groupItems.forEach((a) => { updated[a.adjustment_id] = 'approve'; });
    setDecisions(updated);
  }

  function rejectAll(countId) {
    const groupItems = groups[countId] || [];
    const updated = { ...decisions };
    groupItems.forEach((a) => { updated[a.adjustment_id] = 'reject'; });
    setDecisions(updated);
  }

  // Group adjustments by cycle_count_id
  const groups = {};
  adjustments.forEach((a) => {
    const key = a.cycle_count_id || 'unknown';
    if (!groups[key]) groups[key] = [];
    groups[key].push(a);
  });

  // Order the count cards by the selected key. Each count is scoped to a
  // single bin, so bin sort and count-number sort both operate at the
  // group level.
  const sortedGroups = Object.entries(groups).sort(([idA, itemsA], [idB, itemsB]) => {
    if (sortBy === 'bin') {
      return (itemsA[0]?.bin_code || '').localeCompare(itemsB[0]?.bin_code || '', undefined, { numeric: true });
    }
    return (Number(idA) || 0) - (Number(idB) || 0);
  });

  const thStyle = { textAlign: 'left', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 };
  const tdStyle = { padding: '6px 8px' };

  return (
    <div>
      <PageHeader title="Count Approvals" />

      {message && (
        <div style={{ marginBottom: 12, fontSize: 13, color: 'var(--success)' }}>{message}</div>
      )}

      {adjustments.length === 0 ? (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No pending adjustments</p>
      ) : (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, fontSize: 12, color: 'var(--text-secondary)' }}>
            <span>Sort by</span>
            <button
              className={`btn btn-sm${sortBy === 'count' ? ' btn-primary' : ''}`}
              onClick={() => setSortBy('count')}
            >
              Count #
            </button>
            <button
              className={`btn btn-sm${sortBy === 'bin' ? ' btn-primary' : ''}`}
              onClick={() => setSortBy('bin')}
            >
              Bin
            </button>
          </div>
          {sortedGroups.map(([countId, items]) => (
            <div key={countId} className="card" style={{ marginBottom: 16 }}>
              <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 12 }}>
                Count #{countId} - {items[0].bin_code}
              </div>
              <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border)' }}>
                    <th style={thStyle}>SKU</th>
                    <th style={thStyle}>Item Name</th>
                    <th style={thStyle}>Bin</th>
                    <th style={{ ...thStyle, textAlign: 'right' }}>Expected</th>
                    <th style={{ ...thStyle, textAlign: 'right' }}>Scanned</th>
                    <th style={{ ...thStyle, textAlign: 'right' }}>Variance</th>
                    <th style={thStyle}>Counted By</th>
                    <th style={thStyle}>Decision</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((a) => {
                    const change = a.quantity_change;
                    const changeColor = change < 0 ? 'var(--copper)' : 'var(--success)';
                    const changeText = change > 0 ? `+${change}` : `${change}`;
                    return (
                      <tr key={a.adjustment_id} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td className="mono" style={tdStyle}>{a.sku}</td>
                        <td style={{ ...tdStyle, color: 'var(--text-secondary)' }}>{a.item_name}</td>
                        <td className="mono" style={tdStyle}>{a.bin_code}</td>
                        <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{a.expected_quantity ?? '-'}</td>
                        <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{a.counted_quantity ?? '-'}</td>
                        <td className="mono" style={{ ...tdStyle, textAlign: 'right', color: changeColor, fontWeight: 600 }}>{changeText}</td>
                        <td style={tdStyle}>{a.adjusted_by || '-'}</td>
                        <td style={tdStyle}>
                          <label style={{ marginRight: 12, fontSize: 12, cursor: 'pointer' }}>
                            <input
                              type="radio"
                              name={`decision-${a.adjustment_id}`}
                              checked={decisions[a.adjustment_id] === 'approve'}
                              onChange={() => setDecision(a.adjustment_id, 'approve')}
                              style={{ marginRight: 4 }}
                            />
                            Approve
                          </label>
                          <label style={{ fontSize: 12, cursor: 'pointer' }}>
                            <input
                              type="radio"
                              name={`decision-${a.adjustment_id}`}
                              checked={decisions[a.adjustment_id] === 'reject'}
                              onChange={() => setDecision(a.adjustment_id, 'reject')}
                              style={{ marginRight: 4 }}
                            />
                            Reject
                          </label>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                <button className="btn btn-primary btn-sm" onClick={() => submitGroup(countId)} disabled={submitting}>
                  {submitting ? 'Submitting...' : 'Submit'}
                </button>
                <button className="btn btn-sm" onClick={() => approveAll(countId)}>Approve All</button>
                <button className="btn btn-sm" onClick={() => rejectAll(countId)}>Reject All</button>
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
