/**
 * Backorder queue Items column.
 *
 * Both came from the same warehouse-channel message:
 *
 *   "Can you add the item name to the b/o list in Sentry."
 *   "and maybe a check box so I know if it has been ordered."
 *
 * The first is the item name. The second is the open-PO status, and it is
 * deliberately not a
 * checkbox: nothing links a backorder to the PO that will satisfy it, so the
 * answer is derived from open PO lines for the same item in the backorder's
 * warehouse. A derived answer cannot tell a PO placed for this backorder from
 * general replenishment, so the cell names the PO it is claiming and shows
 * when it lands, letting the operator judge it.
 *
 * The not-ordered state renders explicitly. "No open PO" has to read
 * differently from a field that failed to load, otherwise the column answers
 * the operator's question with silence.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

const apiGetMock = vi.fn();
vi.mock('../api.js', () => ({
  api: {
    get: (...a) => apiGetMock(...a),
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));
vi.mock('../auth.jsx', () => ({
  useAuth: () => ({ user: { role: 'ADMIN', allowed_overrides: [] } }),
}));
vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1 }),
}));

import Backorders from '../pages/Backorders.jsx';

const json = (body) => Promise.resolve({
  ok: true, status: 200, json: () => Promise.resolve(body),
});

function row(items) {
  return {
    so_id: 501, so_number: 'SO-900-BO', parent_so_number: 'SO-900',
    warehouse_id: 1, status: 'WAITING_STOCK', customer_name: 'Ada Lovelace',
    items, days_waiting: 3,
    backorder_opened_at: '2026-08-01T00:00:00Z', fulfillable_since: null,
  };
}

async function renderQueue(items) {
  apiGetMock.mockImplementation((path = '') => (
    path.includes('/admin/backorders')
      ? json({ tab: 'waiting', backorders: [row(items)] })
      : json({})
  ));
  render(
    <MemoryRouter initialEntries={['/backorders']}>
      <Routes><Route path="/backorders" element={<Backorders />} /></Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText('SO-900-BO')).toBeInTheDocument());
}

describe('Backorder Items column', () => {
  beforeEach(() => { apiGetMock.mockReset(); });

  it('shows the item name alongside the SKU', async () => {
    await renderQueue([
      { sku: 'HAR-DUB-01', item_name: 'Hareline Dubbin Ice Dub', qty: 2, open_po: null },
    ]);

    expect(screen.getByText('HAR-DUB-01')).toBeInTheDocument();
    expect(screen.getByText('Hareline Dubbin Ice Dub')).toBeInTheDocument();
  });

  it('names the PO and when it lands, not a checkbox', async () => {
    await renderQueue([
      {
        sku: 'HAR-DUB-01', item_name: 'Hareline Dubbin Ice Dub', qty: 2,
        open_po: { po_number: 'PO-2026-001', expected_date: '2026-08-25', quantity_remaining: 100 },
      },
    ]);

    expect(screen.getByText('PO-2026-001')).toBeInTheDocument();
    // A date-only string must not be parsed as UTC midnight, which
    // renders a day early in MDT. 08/25, never 08/24.
    expect(screen.getByText(/expected 08\/25\/2026/)).toBeInTheDocument();
  });

  it('says so explicitly when nothing is on order', async () => {
    await renderQueue([
      { sku: 'HAR-DUB-01', item_name: 'Hareline Dubbin Ice Dub', qty: 2, open_po: null },
    ]);

    expect(screen.getByText(/not on an open PO/i)).toBeInTheDocument();
  });

  it('handles an open PO carrying no expected date', async () => {
    await renderQueue([
      {
        sku: 'HAR-DUB-01', item_name: 'Hareline Dubbin Ice Dub', qty: 2,
        open_po: { po_number: 'PO-2026-003', expected_date: null, quantity_remaining: 50 },
      },
    ]);

    expect(screen.getByText('PO-2026-003')).toBeInTheDocument();
    expect(screen.getByText(/no expected date/i)).toBeInTheDocument();
  });

  it('renders each line of a multi-item backorder independently', async () => {
    await renderQueue([
      {
        sku: 'HAR-DUB-01', item_name: 'Hareline Dubbin Ice Dub', qty: 2,
        open_po: { po_number: 'PO-2026-001', expected_date: '2026-08-25', quantity_remaining: 100 },
      },
      { sku: 'LOO-NIP-04', item_name: 'Loon Outdoors Ergo Nipper', qty: 1, open_po: null },
    ]);

    expect(screen.getByText('Hareline Dubbin Ice Dub')).toBeInTheDocument();
    expect(screen.getByText('Loon Outdoors Ergo Nipper')).toBeInTheDocument();
    expect(screen.getByText('PO-2026-001')).toBeInTheDocument();
    expect(screen.getByText(/not on an open PO/i)).toBeInTheDocument();
  });

  it('survives a payload with no item name', async () => {
    // Older cached payloads carry sku and
    // qty only. The cell should degrade rather than render "undefined".
    await renderQueue([{ sku: 'HAR-DUB-01', qty: 2 }]);

    expect(screen.getByText('HAR-DUB-01')).toBeInTheDocument();
    expect(screen.queryByText(/undefined/i)).not.toBeInTheDocument();
    expect(screen.getByText(/not on an open PO/i)).toBeInTheDocument();
  });
});
