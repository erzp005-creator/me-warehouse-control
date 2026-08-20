/**
 * Dashboard "Local Pickup" tab: lists the local-pickup worklist via the
 * server-side local_pickup filter, and the red "Picked Up?" button marks an
 * order shipped through the SO edit endpoint's PICKED -> SHIPPED transition.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const apiGetMock = vi.fn();
const apiPutMock = vi.fn();
vi.mock('../api.js', () => ({
  api: {
    get: (...a) => apiGetMock(...a),
    put: (...a) => apiPutMock(...a),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));
vi.mock('../warehouse.jsx', () => ({ useWarehouse: () => ({ warehouseId: 1 }) }));
vi.mock('../auth.jsx', () => ({
  useAuth: () => ({ user: { role: 'ADMIN', allowed_overrides: [] } }),
}));

import Dashboard from '../pages/Dashboard.jsx';

const json = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(body),
});

const PICKUP_ORDERS = {
  sales_orders: [
    {
      so_id: 11, so_number: 'SO-PICK-1', customer_name: 'Neal Hoffberg',
      status: 'PICKED', order_date: '2026-06-14T00:00:00Z',
      ship_method: 'Local Pickup (Free)',
      customer_phone: '555-1212', ship_address: '', priority: 0, memo: '',
    },
    {
      so_id: 12, so_number: 'SO-OPEN-1', customer_name: 'Jane Angler',
      status: 'OPEN', order_date: '2026-06-15T00:00:00Z',
      ship_method: 'Will Call - Local',
      customer_phone: '', ship_address: '', priority: 0, memo: '',
    },
  ],
  total: 2, page: 1, pages: 1, per_page: 1000,
};

function wire() {
  apiGetMock.mockImplementation((path = '') => {
    // Order-detail and related-records lookups feed the shared
    // SalesOrderModal the order number now opens. Matched ahead of the
    // list branch, which would otherwise swallow them.
    if (/\/admin\/sales-orders\/\d+\/related/.test(path)) {
      return json({ so_id: 11, related_count: 0, records: [] });
    }
    if (/\/admin\/sales-orders\/\d+$/.test(path)) {
      return json({
        sales_order: PICKUP_ORDERS.sales_orders[0], lines: [], pick_tasks: [],
      });
    }
    if (path.startsWith('/admin/sales-orders')) return json(PICKUP_ORDERS);
    return json({}); // dashboard preferences / productivity / etc.
  });
  apiPutMock.mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) });
}

async function openLocalPickupTab() {
  const view = render(<MemoryRouter><Dashboard /></MemoryRouter>);
  fireEvent.click(await view.findByText('Local Pickup'));
  return view;
}

describe('Dashboard Local Pickup tab', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiPutMock.mockReset();
    wire();
  });

  it('lists local pickup orders via the server-side local_pickup filter', async () => {
    const { findByText } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    await findByText('Neal Hoffberg');
    await findByText('SO-OPEN-1');
    expect(apiGetMock).toHaveBeenCalledWith(
      expect.stringContaining('local_pickup=true'),
      expect.anything(),
    );
  });

  it('passes the status filter through to the request', async () => {
    const { findByText, getByDisplayValue } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    fireEvent.change(getByDisplayValue('All statuses'), { target: { value: 'PICKED' } });
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith(
        expect.stringContaining('status=PICKED'),
        expect.anything(),
      );
    });
  });

  it('sends OPEN,PICKED when the operator picks the Open + Picked view', async () => {
    const { findByText, getByDisplayValue } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    fireEvent.change(getByDisplayValue('All statuses'), { target: { value: 'OPEN,PICKED' } });
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith(
        // comma is percent-encoded by URLSearchParams
        expect.stringContaining('status=OPEN%2CPICKED'),
        expect.anything(),
      );
    });
  });

  it('confirms in a JSX modal (not window.confirm) then ships a PICKED order', async () => {
    const { findByText, getAllByText } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    fireEvent.click(getAllByText('Picked Up?')[0]);  // first row = PICKED
    // A JSX confirm modal appears; nothing ships until it is confirmed.
    const confirmBtn = await findByText('Yes, mark picked up');
    expect(apiPutMock).not.toHaveBeenCalled();
    fireEvent.click(confirmBtn);
    await waitFor(() => {
      expect(apiPutMock).toHaveBeenCalledWith(
        '/admin/sales-orders/11',
        { status: 'SHIPPED' },
        expect.anything(),
      );
    });
  });

  it('shows a loud modal (no silent no-op) when an OPEN order cannot be picked up', async () => {
    const { findByText, getAllByText } = await openLocalPickupTab();
    await findByText('SO-OPEN-1');
    fireEvent.click(getAllByText('Picked Up?')[1]);  // second row = OPEN
    await findByText('Not ready for pickup');
    expect(apiPutMock).not.toHaveBeenCalled();
  });

  // The shared order modal, wired in so a counter operator can see what is
  // on an order without leaving the pickup queue.
  it('opens the shared order modal from the order number', async () => {
    const { findByText, queryByText } = await openLocalPickupTab();
    fireEvent.click(await findByText('SO-PICK-1'));
    await waitFor(() => expect(queryByText('Order Summary')).toBeTruthy());
    // Read-only: the edit form's Save is not reachable from here.
    expect(queryByText('Save')).toBeNull();
  });

  it('does not open the order when a non-link cell is clicked', async () => {
    // The order number is the only click target, so selecting text in
    // another cell to copy it cannot open anything (item 4).
    const { findByText, queryByText } = await openLocalPickupTab();
    fireEvent.click(await findByText('Neal Hoffberg'));
    // Give the modal every chance to open before asserting it did not.
    await act(async () => { await Promise.resolve(); });
    expect(queryByText('Order Summary')).toBeNull();
    expect(await findByText('SO-PICK-1')).toBeTruthy();
  });

  it('closing the order modal leaves the pickup queue in place', async () => {
    const { findByText, queryByText } = await openLocalPickupTab();
    fireEvent.click(await findByText('SO-PICK-1'));
    await waitFor(() => expect(queryByText('Order Summary')).toBeTruthy());

    fireEvent.click(await findByText('Close'));

    await waitFor(() => expect(queryByText('Order Summary')).toBeNull());
    expect(queryByText('SO-PICK-1')).toBeTruthy();
    expect(queryByText('SO-OPEN-1')).toBeTruthy();
  });

  it('the narrow pickup Edit form is unchanged and still reachable', async () => {
    // Deliberately NOT swapped for the shared edit surface: partial-fulfill
    // and admin-pick do not belong on a pickup counter screen.
    const { findByText, getAllByText, queryByText } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    fireEvent.click(getAllByText('Edit')[0]);
    await waitFor(() => expect(queryByText('Customer name')).toBeTruthy());
    expect(queryByText('Partially Fulfill')).toBeNull();
    expect(queryByText('Admin Pick')).toBeNull();
  });
});
