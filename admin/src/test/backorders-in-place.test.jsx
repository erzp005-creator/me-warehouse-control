/**
 * Backorders queue click-through.
 *
 * Three complaints from the warehouse channel, all the same path:
 *   "When I am in the backorder tab of WMS, I would like to stay there
 *    instead of having to go back and reselect it for every order. It
 *    shoots me back to sales orders."
 *
 * Tests pin:
 * - A row click opens the order in place. No navigation, and the queue
 *   stays on screen behind the modal.
 * - The tab lives in the URL, so it survives navigation and is linkable,
 *   and an unknown ?tab= falls back to Waiting instead of querying a tab
 *   the API rejects.
 * - Switching tabs rewrites the URL and refetches against the new tab.
 * - The Cancel button still works and does not open the order (it stops
 *   propagation on a clickable row).
 */

import React, { useEffect } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

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
  useWarehouse: () => ({ warehouseId: null }),
}));

import Backorders from '../pages/Backorders.jsx';

const json = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(body),
});

const BO_ROW = {
  so_id: 501, so_number: 'SO-900-BO', parent_so_number: 'SO-900',
  warehouse_id: 1, status: 'WAITING_STOCK', customer_name: 'Ada Lovelace',
  items: [{ sku: 'SKU-1', qty: 2 }], days_waiting: 3,
  backorder_opened_at: '2026-08-01T00:00:00Z', fulfillable_since: null,
};

// Records the live location so a test can assert nothing navigated away.
// A mutable holder rather than a reassigned module binding, which the
// react-hooks lint rule flags inside a component.
const here = { path: null };
function LocationProbe() {
  const loc = useLocation();
  // Recorded in an effect rather than during render: writing to an outer
  // binding while rendering is a side effect, and the lint rule is right
  // to flag it. Every assertion below is awaited, so the effect has run.
  useEffect(() => { here.path = loc.pathname + loc.search; }, [loc]);
  return null;
}

function wire({ rows = [BO_ROW] } = {}) {
  apiGetMock.mockImplementation((path = '') => {
    if (path.includes('/admin/backorders')) return json({ tab: 'waiting', backorders: rows });
    if (/\/admin\/sales-orders\/\d+\/related/.test(path)) {
      return json({ so_id: 501, related_count: 0, records: [] });
    }
    if (/\/admin\/sales-orders\/\d+$/.test(path)) {
      return json({
        sales_order: {
          so_id: 501, so_number: 'SO-900-BO', customer_name: 'Ada Lovelace',
          status: 'WAITING_STOCK', order_type: 'backorder', parent_so_id: 900,
          order_date: '2026-08-01T00:00:00Z', ship_method: 'GROUND',
          customer_phone: '', customer_email: '', ship_address: '',
          priority: 0, memo: '',
        },
        lines: [], pick_tasks: [],
      });
    }
    return json({});
  });
}

function renderAt(entry = '/backorders') {
  here.path = null;
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <LocationProbe />
      <Routes>
        <Route path="/backorders" element={<Backorders />} />
        <Route path="/sales-orders" element={<div>SALES ORDERS PAGE</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const backordersCalls = () =>
  apiGetMock.mock.calls.map((c) => c[0]).filter((p) => p.includes('/admin/backorders'));

describe('Backorders queue click-through ()', () => {
  beforeEach(() => { apiGetMock.mockReset(); here.path = null; });

  it('opens the order in place instead of navigating to Sales Orders', async () => {
    wire();
    renderAt();
    const cell = await screen.findByText('SO-900-BO');

    await act(async () => { fireEvent.click(cell); });

    // The order modal is up...
    await waitFor(() => expect(screen.getByText('Order Summary')).toBeTruthy());
    // ...the Sales Orders page was never reached...
    expect(screen.queryByText('SALES ORDERS PAGE')).toBeNull();
    expect(here.path.startsWith('/backorders')).toBe(true);
    // ...and the queue is still behind it.
    expect(screen.getByText('Backorders')).toBeTruthy();
  });

  it('defaults to the Waiting tab and puts it in the URL when switched', async () => {
    wire();
    renderAt();
    await screen.findByText('SO-900-BO');
    expect(backordersCalls()[0]).toContain('tab=waiting');

    await act(async () => { fireEvent.click(screen.getByText('Ready to Ship')); });

    await waitFor(() => expect(here.path).toContain('tab=ready-to-ship'));
    expect(backordersCalls().some((p) => p.includes('tab=ready-to-ship'))).toBe(true);
  });

  it('honours ?tab= from the URL on first load', async () => {
    wire();
    renderAt('/backorders?tab=ready-to-ship');
    await waitFor(() => expect(backordersCalls().length).toBeGreaterThan(0));
    // The queue the operator linked to is the one that gets fetched.
    expect(backordersCalls()[0]).toContain('tab=ready-to-ship');
    expect(screen.getByRole('tab', { name: 'Ready to Ship' }).getAttribute('aria-selected'))
      .toBe('true');
  });

  it('falls back to Waiting on an unknown ?tab= rather than querying it', async () => {
    wire();
    renderAt('/backorders?tab=bogus');
    await waitFor(() => expect(backordersCalls().length).toBeGreaterThan(0));
    expect(backordersCalls()[0]).toContain('tab=waiting');
    expect(backordersCalls().some((p) => p.includes('bogus'))).toBe(false);
  });

  it('keeps other query params when the tab changes', async () => {
    wire();
    renderAt('/backorders?warehouse=3');
    await screen.findByText('SO-900-BO');
    await act(async () => { fireEvent.click(screen.getByText('Ready to Ship')); });
    await waitFor(() => expect(here.path).toContain('tab=ready-to-ship'));
    expect(here.path).toContain('warehouse=3');
  });

  it('Cancel opens the cancel confirm, not the order', async () => {
    wire();
    renderAt();
    await screen.findByText('SO-900-BO');

    await act(async () => { fireEvent.click(screen.getByText('Cancel')); });

    expect(screen.getByText('Cancel backorder SO-900-BO?')).toBeTruthy();
    // stopPropagation held: the order modal did not also open.
    expect(screen.queryByText('Order Summary')).toBeNull();
  });

  it('closing the modal leaves the operator on their tab', async () => {
    wire();
    renderAt('/backorders?tab=ready-to-ship');
    const cell = await screen.findByText('SO-900-BO');
    await act(async () => { fireEvent.click(cell); });
    await waitFor(() => expect(screen.getByText('Order Summary')).toBeTruthy());

    await act(async () => { fireEvent.click(screen.getByText('Close')); });

    await waitFor(() => expect(screen.queryByText('Order Summary')).toBeNull());
    expect(here.path).toContain('tab=ready-to-ship');
  });
});
